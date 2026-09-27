# -*- coding: utf-8 -*-
"""给「进列表」的条目补展示数据：中文名 + 跨区比价（对应 §2.5 / §7.2 / §7.4）。

放在独立模块是因为它同时要用到 `steam`（网络）、`fx`（汇率）、`state`（缓存）——
让 `run.py` 只负责编排，不掺业务细节。

两条实测得出的关键结论决定了这里怎么写：

1. **中文名只能逐游戏取**（多 appid 时 `appdetails` 只接受 `filters=price_overview`，
   而那个值不返回 `name`）→ 所以给它**永久缓存**进 `game_meta`，只在第一次遇到时请求。
2. **各区价格可以批量**（每区 1 次请求即可覆盖整个列表）→ 每次运行实时拉，不进缓存。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import fx as fx_module
from .httpclient import HttpError
from .steam import SteamClient

#: 比价地区的显示名（§7.2）
COMPARE_LABELS = {"UA": "乌克兰区", "IN": "印度区", "CN": "国区", "US": "美区",
                  "TR": "土耳其区", "BR": "巴西区", "RU": "俄区"}


def load_fx(cfg: dict, today: str, log: Callable[[str], None]) -> dict | None:
    """取当天汇率（有缓存就复用）；失败时返回 None，**不让整轮失败**。"""
    cache_path = cfg.get("fx_cache_path") or "data/fx_cache.json"
    from pathlib import Path

    path = Path(cache_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    try:
        return fx_module.load_or_fetch(path, today=today, log=log,
                                       timeout=float(cfg.get("probe_timeout_seconds", 6)))
    except fx_module.FxError as exc:
        log(f"[warn] 汇率取不到，本轮不换算跨区价（只显示原币种）：{exc}")
        return None


def enrich_steam(
    client: SteamClient,
    state,
    shown: list[dict],
    cfg: dict,
    fx: dict | None,
    now: datetime,
    log: Callable[[str], None],
    upcoming: list[dict] | None = None,
) -> dict:
    """就地补充 `shown`（及 `upcoming`）里每个条目的 `title_zh` 与 `compare`，并返回统计。

    只处理**进列表**的条目 —— 没进列表的游戏一个 Steam 请求都不发。

    跨区比价走 `appid|expiry` 缓存（2026-09-27 方案 B）：同一折扣期内不重拉。
    即将过期条目大多不是当日新增、此前从没拉过，首次开启当天有一波回填，
    之后每天只拉「新进入窗口且无缓存」的条目。两个集合的 appid 合并去重后
    一次批量拉取（20 个/批），缓存只存**原币种**价，CNY 换算与差价百分比
    按当天汇率现算 —— 汇率不进缓存，永远新鲜。
    """
    facts = {
        "title_fetched": 0,
        "title_cached": 0,
        "compare_batches": 0,
        "compare_cache_hits": 0,
        "compare_fetched": 0,
        "price_mismatch": [],
        "errors": 0,
    }
    all_entries = shown + (upcoming or [])
    appids = [e["appid"] for e in all_entries if e.get("appid")]
    if not appids:
        return facts

    # ---- 1. 中文名：逐游戏，命中缓存就不发请求（§2.5）----
    for entry in all_entries:
        appid = entry.get("appid")
        if not appid:
            continue
        cached = state.title_zh(entry.get("game_id"))
        if cached:
            entry["title_zh"] = cached
            facts["title_cached"] += 1
            continue
        try:
            info = client.info(appid, cc=cfg.get("country", "CN"))
        except HttpError as exc:
            log(f"[warn] 中文名取失败：{entry.get('title')}（{exc}）")
            facts["errors"] += 1
            continue
        if not info or not info.get("name"):
            continue
        name = info["name"].strip()      # Steam 偶尔会存带尾随空格的本地化标题
        state.set_title_zh(entry.get("game_id"), name, now)
        entry["title_zh"] = name
        facts["title_fetched"] += 1
        # 顺带用 Steam 国区价与 ITAD 的价对一次（§11 的验收项，不额外发请求）
        steam_final, itad_price = info.get("final"), entry.get("price_int")
        if steam_final is not None and itad_price:
            if abs(int(steam_final) - int(itad_price)) > 1:
                facts["price_mismatch"].append({
                    "title": entry.get("title"),
                    "appid": appid,
                    "itad_price_int": itad_price,
                    "steam_price_int": int(steam_final),
                })

    # ---- 2. 跨区价格：先查缓存，miss 的才发请求（每区 1 次批量/20 个 appid）----
    countries = [c for c in (cfg.get("compare_countries") or []) if c]
    raw_by_appid: dict[int, dict[str, dict]] = {}   # appid -> cc -> price_overview

    need_fetch: dict[int, list[dict]] = {}          # appid -> 需要它的条目（含 expiry）
    for entry in all_entries:
        appid = entry.get("appid")
        if not appid:
            continue
        expiry = entry.get("expiry")
        cached_rows = state.compare_cached(appid, expiry)
        if cached_rows is not None:
            entry["compare"] = _assemble_rows(cached_rows, entry, fx)
            facts["compare_cache_hits"] += 1
            continue
        need_fetch.setdefault(appid, []).append(entry)

    cacheable = True
    if need_fetch:
        fetched_appids = list(need_fetch)
        failed_ccs: set[str] = set()
        for cc in countries:
            try:
                prices = client.prices(fetched_appids, cc)
            except HttpError as exc:
                log(f"[warn] 跨区价格取失败（{cc}）：{exc}")
                facts["errors"] += 1
                failed_ccs.add(cc)
                prices = {}
            for appid, price in prices.items():
                raw_by_appid.setdefault(appid, {})[cc] = price
            facts["compare_batches"] += 1
        # ⚠️ 只要有一个区整批失败就不落缓存：否则失败区在整个折扣期内
        # 都不会重试（缓存键到折扣结束才过期）。本轮照常用成功区的数据渲染，
        # 缓存推迟到下轮全部区都成功时再写。
        cacheable = not failed_ccs
        if failed_ccs:
            log(f"[warn] 跨区比价有整区失败（{', '.join(sorted(failed_ccs))}），"
                f"本轮不落比价缓存，下轮整批重试")

    # ---- 3. 拼成卡片要用的比价行（含相对国区的差价百分比），并写入缓存 ----
    for appid, entries in need_fetch.items():
        rows_written = False
        for entry in entries:
            raw_rows = [
                {"cc": cc,
                 "label": COMPARE_LABELS.get(cc, cc),
                 "currency": (raw_by_appid.get(appid, {}).get(cc) or {}).get("currency"),
                 "final": (raw_by_appid.get(appid, {}).get(cc) or {}).get("final")}
                for cc in countries if raw_by_appid.get(appid, {}).get(cc)
            ]
            entry["compare"] = _assemble_rows(raw_rows, entry, fx)
            if cacheable:
                state.set_compare(appid, entry.get("expiry"), raw_rows, now)
            if raw_rows:
                rows_written = True
        if rows_written:
            facts["compare_fetched"] += 1   # 按去重后的 appid 计数
    return facts


def _assemble_rows(raw_rows: list[dict], entry: dict, fx: dict | None) -> list[dict]:
    """把缓存/拉取到的原币种行换算成卡片要展示的行。

    ``cny_minor`` 与 ``diff_pct`` 按当前传入的汇率现算（不进缓存）；
    汇率缺失时只显示原币种价（与旧行为一致）。
    """
    base = entry.get("price_int")
    rows: list[dict] = []
    for item in raw_rows:
        final, currency = item.get("final"), item.get("currency")
        cny = fx_module.to_base_minor(final, currency, fx) if fx else None
        diff = None
        if cny is not None and base:
            diff = int(round((cny - base) / base * 100))
        rows.append({
            "cc": item.get("cc"),
            "label": item.get("label"),
            "currency": currency,
            "final": final,
            "cny_minor": cny,
            "diff_pct": diff,
        })
    return rows
