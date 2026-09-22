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
) -> dict:
    """就地补充 `shown` 里每个条目的 `title_zh` 与 `compare`，并返回统计。

    只处理**进列表**的条目 —— 没进列表的游戏一个 Steam 请求都不发。
    """
    facts = {
        "title_fetched": 0,
        "title_cached": 0,
        "compare_batches": 0,
        "price_mismatch": [],
        "errors": 0,
    }
    appids = [e["appid"] for e in shown if e.get("appid")]
    if not appids:
        return facts

    # ---- 1. 中文名：逐游戏，命中缓存就不发请求（§2.5）----
    for entry in shown:
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

    # ---- 2. 跨区价格：每区 1 次批量请求（§2.5）----
    countries = [c for c in (cfg.get("compare_countries") or []) if c]
    prices_by_cc: dict[str, dict] = {}
    for cc in countries:
        try:
            prices_by_cc[cc] = client.prices(appids, cc)
        except HttpError as exc:
            log(f"[warn] 跨区价格取失败（{cc}）：{exc}")
            facts["errors"] += 1
            prices_by_cc[cc] = {}
        facts["compare_batches"] += 1

    # ---- 3. 拼成卡片要用的比价行（含相对国区的差价百分比）----
    for entry in shown:
        base = entry.get("price_int")
        appid = entry.get("appid")
        rows: list[dict] = []
        for cc in countries:
            payload = (prices_by_cc.get(cc) or {}).get(appid)
            if not payload or appid is None:
                continue
            final = payload.get("final")
            currency = payload.get("currency")
            cny = fx_module.to_base_minor(final, currency, fx) if fx else None
            diff = None
            if cny is not None and base:
                diff = int(round((cny - base) / base * 100))
            rows.append({
                "cc": cc,
                "label": COMPARE_LABELS.get(cc, cc),
                "currency": currency,
                "final": final,
                "cny_minor": cny,
                "diff_pct": diff,
            })
        entry["compare"] = rows
    return facts
