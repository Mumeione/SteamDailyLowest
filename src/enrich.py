# -*- coding: utf-8 -*-
"""给「进列表」的条目补展示数据：中文名 + 跨区比价（对应 §2.5 / §7.2 / §7.4）。

放在独立模块是因为它同时要用到 `steam`（网络）、`fx`（汇率）、`state`（缓存）——
让 `run.py` 只负责编排，不掺业务细节。

两条实测得出的关键结论决定了这里怎么写：

1. **中文名只能逐游戏取**（多 appid 时 `appdetails` 只接受 `filters=price_overview`，
   而那个值不返回 `name`）→ 所以给它**永久缓存**进 `game_meta`，只在第一次遇到时请求。
2. **各区价格可以批量**（每区 1 次请求即可覆盖整个列表）→ **现价每轮真查**
   （S7 换模型）：现价（final）不进缓存；区域**原价**（initial）永久缓存进
   `cache.json`（键 `appid|cc`）—— 真查响应里的原价回写校准（区域重定价
   自愈并记日志），原价缓存不随折扣期失效。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import fx as fx_module
from .httpclient import HttpError
from .steam import SteamClient

#: 比价地区的显示名（§7.2）。区域已收敛为 ua+in（config `compare_countries`）；
#: 未列出的 cc 回落显示地区码 —— 新增比价区时在这里补显示名
COMPARE_LABELS = {"UA": "乌克兰区", "IN": "印度区"}


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

    跨区比价（S7 换模型）：现价（final）每轮对进列表条目**真查**（每区 1 次
    批量/20 个 appid）；真查响应里的区域原价（initial）回写 `appid|cc` 永久
    缓存 —— 与缓存不一致即 Valve 区域重定价，记日志自愈。真查失败的区域
    该轮直接缺行（不回落，下轮自愈）。CNY 换算与差价百分比按当天汇率现算
    —— 汇率不进缓存，永远新鲜。
    """
    facts = {
        "title_fetched": 0,
        "title_cached": 0,
        "compare_batches": 0,
        "compare_repriced": 0,
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

    # ---- 2. 跨区价格：现价每轮真查（每区 1 次批量/20 个 appid）----
    countries = [c for c in (cfg.get("compare_countries") or []) if c]
    raw_by_appid: dict[int, dict[str, dict]] = {}   # appid -> cc -> price_overview
    for cc in countries:
        try:
            prices = client.prices(appids, cc)
        except HttpError as exc:
            log(f"[warn] 跨区价格取失败（{cc}）：{exc}，该区本轮缺行，下轮自愈")
            facts["errors"] += 1
            continue
        for appid, price in prices.items():
            raw_by_appid.setdefault(appid, {})[cc] = price
        facts["compare_batches"] += 1

    # ---- 3. 真查即校准：响应里的区域原价回写 appid|cc 永久缓存 ----
    # 与缓存不一致 = Valve 区域重定价，回写自愈并记日志（验收要求）；
    # 首见写入不算重定价，不记日志。
    for appid, by_cc in raw_by_appid.items():
        for cc, price in by_cc.items():
            old = state.set_compare_original(appid, cc, price.get("initial"),
                                             price.get("currency"), now)
            if old is not None:
                facts["compare_repriced"] += 1
                log(f"      [info] 区域重定价（{cc}）：appid={appid} "
                    f"原价 {old} → {price.get('initial')}（已回写校准）")

    # ---- 4. 拼成卡片要用的比价行（含相对国区的差价百分比）----
    for entry in all_entries:
        appid = entry.get("appid")
        if not appid:
            continue
        by_cc = raw_by_appid.get(appid) or {}
        raw_rows = [
            {"cc": cc,
             "label": COMPARE_LABELS.get(cc, cc),
             "currency": (by_cc.get(cc) or {}).get("currency"),
             "final": (by_cc.get(cc) or {}).get("final")}
            for cc in countries if by_cc.get(cc)
        ]
        entry["compare"] = _assemble_rows(raw_rows, entry, fx)
    facts["compare_fetched"] = sum(1 for by_cc in raw_by_appid.values() if by_cc)
    return facts


def _assemble_rows(raw_rows: list[dict], entry: dict, fx: dict | None) -> list[dict]:
    """把真查到的原币种行换算成卡片要展示的行。

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
