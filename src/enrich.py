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
from .httpclient import Blocked, HttpError
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
        except Blocked:
            raise                 # 滥用封禁：上抛中止本轮，绝不能当「单条失败」继续打
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
        except Blocked:
            raise                 # 滥用封禁：上抛中止本轮（与中文名路径同口径）
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
        entry["compare"] = assemble_rows(raw_rows, entry, fx)
    facts["compare_fetched"] = sum(1 for by_cc in raw_by_appid.values() if by_cc)
    return facts


def assemble_rows(raw_rows: list[dict], entry: dict, fx: dict | None) -> list[dict]:
    """把原币种行换算成卡片要展示的行（cny_minor / diff_pct 按传入汇率现算）。

    **公开函数**：`tools/render_report.py` 用它把 cache.json 里的区域原价
    组装成本地预览能看的比价行（2026-10-07），口径必须与生产完全一致。

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


def fill_compare(state, cfg: dict, entry: dict, fx: dict | None, *,
                 truth_by_appid: dict | None = None,
                 appid: int | None = None) -> bool:
    """给**一条** entry 补 ``entry["compare"]``；返回是否**由估算**补上。

    真查结果（``truth_by_appid``：appid → 展示行）命中就回灌；没命中（或没传真查
    结果）就用「外区原价缓存 × 国区折扣比例」估算（:func:`estimate_compare`）。
    条目已有 ``compare`` 或补不出任何区域时不动、返回 False。

    **这是「估算谁吃」的唯一出处**（2026-10-09 架构检查卡片 05）：``run.render_pass``
    与 ``tools/render_report`` 的本地预览从前各写一遍遍历，差别只在「真查结果从哪来」
    —— 线上是本轮 enrich 的结果，本地预览不发任何网络请求所以**完全没有**（连真查
    覆盖的条目也用缓存估算，好让预览完全离线可看）。差异点做成参数，别在调用点
    再抄一遍判断。

    ``appid``：``seen_deal`` 批次的条目没有 appid（它在 ``game_meta`` 里），由调用方
    用 ``state.meta(...)`` 解析后传入（同 :func:`estimate_compare` 的教训）。
    """
    if entry.get("compare"):
        return False
    resolved = appid or entry.get("appid")
    if truth_by_appid and resolved and truth_by_appid.get(resolved):
        entry["compare"] = truth_by_appid[resolved]
        return False
    countries = [c for c in (cfg.get("compare_countries") or []) if c]
    est = estimate_compare(state, entry, countries, fx, appid=resolved)
    if est:
        entry["compare"] = est
        return True
    return False


def estimate_compare(state, entry: dict, countries: list[str], fx: dict | None,
                     appid: int | None = None) -> list[dict]:
    """用**区域原价永久缓存** × 国区折扣比例，估算该条目的跨区比价行。

    口径（2026-10-08 用户定案，此前只在本地预览里有、生产漏了）：
    **原价永久缓存就是拿来估算现价的**。每轮现查只覆盖「当日新增 + 即将到期」
    两个板块 —— 每日抓取的意义是防打折**中途降价**，这两板块的比价必须真实；
    其余板块（热门/大额折扣等历史条目）的比价行用估算：

        外区现价 = 外区原价缓存 × (国区现价 / 国区原价)

    refs 实测：15/20 与真查完全一致、5/20 差 1~2 个百分点（各区四舍五入反算）。
    差价百分比与 ¥ 换算按当天汇率现算（:func:`assemble_rows`），不进缓存。

    条目缺 appid / 国区现价 / 国区原价，或该游戏没有任何区域的原价缓存时
    返回 ``[]`` —— 前端回落「本轮未取到数据」。真查覆盖的条目**不该走到这里**
    （调用方先查真查结果，命中就别调本函数）。

    ``appid``：`seen_deal` 批次的条目没有 appid（它在 game_meta 里），由调用方
    用 ``state.meta(...)`` 解析后传入（同 graft_compare_from_cache 的教训）。
    """
    appid = appid or entry.get("appid")
    base, regular = entry.get("price_int"), entry.get("regular_int")
    if not appid or not base or not regular:
        return []
    ratio = base / regular
    raw_rows = []
    for cc in countries:
        cached = state.compare_entry(appid, cc) or {}
        initial = cached.get("initial")
        if initial is None:
            continue
        currency = cached.get("currency")
        raw_rows.append({
            "cc": cc,
            "label": COMPARE_LABELS.get(cc, cc),
            "currency": currency,
            "final": int(round(initial * ratio)),
        })
    if not raw_rows:
        return []
    return assemble_rows(raw_rows, entry, fx)
