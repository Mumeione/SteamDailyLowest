# -*- coding: utf-8 -*-
"""渲染层：把状态库缓存合并成卡片 → 出报表（对应 docs/DEVELOPMENT.md §7）。

**为什么单独一层**（2026-10-09 架构检查卡片 05）：这一层原先长在 ``run.py``
（CLI 入口）里，于是 ``tools/render_report.py``（零网络的本地预览工具）为了拿
``render_pass`` / ``build_stats`` 必须 ``import run`` —— 一个只读的渲染工具去 import
1400 行的**入口** module，读代码的人得先分清「哪些是编排、哪些是渲染」。
现在边界是：

* :mod:`src.pipeline` —— 渲染层：``merge_details`` / ``render_pass`` / ``build_stats``
  / ``count_backlog``（**纯函数式**：只吃 state/cfg/now，不发任何网络请求）；
* ``run.py`` —— 编排与 CLI：抓取、详情管线、退出码、Actions 摘要。它反过来 import
  本层，并**转发导出**这几个名字（老调用点与测试照常 ``from run import render_pass``）。

``log`` 也放这里：渲染层要打进度日志，而它是被两边共用的那一行 print。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import classify, enrich, report
from .state import State


def log(message: str) -> None:
    """统一的一行输出（run.py 与本层共用；测试里注入不进来的地方直接调它）。"""
    print(message, flush=True)


def count_backlog(entries: list[dict], state: State, cfg: dict, now: datetime) -> int:
    """目录里还有多少条没详情（给页面显示增量补齐的进度）。

    S6 口径：只数**会真正派生为目标**的条目（折扣活跃 / 当日新增 / 重判），
    非折扣期与 unlisted 冻结条目不再计入 —— 33,891 条 game_meta 存量里
    大部分是非折扣期条目，按旧口径数会虚高且永远清不掉。
    条目池 ``entries`` 由调用方给：daily 传本轮 hist_low；prefetch 传
    :func:`latest_entries`（S8 起 prefetch 无本轮列表）。
    """
    return sum(
        1 for e in entries
        if e.get("game_id") and classify.entry_needs_detail(
            e, state, cfg, now, is_new=classify.is_new_today(e, now))
    )


def merge_details(state: State, entries: list[dict], cfg: dict) -> list[dict]:
    """把详情缓存合并进条目并分档（§3.5 / §7.2）。

    「详情待补」只用于**这一轮没抓到详情**的条目（抓取失败、被中断）；
    抓到但 ITAD 没有 Steam 好评率数据 → 归入「冷门 / 无数据」，不展示。

    中文名也在这里合并 —— 它是 `game_meta` 里的一等缓存字段，
    **不能只在 enrich 阶段才读**：首版渲染不跑 enrich（那时大多还没有 appid），
    漏掉这一步就会让已经有中文名的游戏在首版退化成英文名。
    「上次史低时间」同样在这里合并，但它走的是折扣期暂存
    ``low_time_cache``（§3.6，键 ``game_id|expiry``），不属于 game_meta。
    """
    merged: list[dict] = []
    for entry in entries:
        meta = state.meta(entry.get("game_id"))
        item = dict(entry)
        # 「进列表」的档位组装在这一句里（classify.merge_tier，卡片 07）：
        # unlisted → COLD（决策 17，不能归 PENDING，否则以展示档复入列表）、
        # 没抓过详情 → PENDING、其余按评价数据判 quality/notable/cold/other。
        # 顺带回填卡片要用的 appid / reviews —— 三个字段本来就是同一判定的产物。
        item["tier"], item["appid"], item["reviews"] = classify.merge_tier(meta, cfg)
        item["title_zh"] = meta.get("title_zh") if meta else None
        item["last_low_at"] = state.last_low_at(entry.get("game_id"), entry.get("expiry"))
        # 史低期记忆（§3.6 扩展，2026-10-09）：新史低的「距上次史低」来自
        # game_meta.low_period.prev（上一段史低期的开始）—— storelow/v2 对
        # 新史低返回空，只有自己攒。平史低不用它（走 last_low_at，更精确）。
        item["prev_low_at"] = state.prev_low_start(entry.get("game_id"))
        # 厂商 / stats（快照 v3，2026-09-30）：同样来自 meta 合并视图的一等缓存字段。
        # 旧条目尚无这些键 → 一律给 None / []，消费方（快照）不得因此判「异常」。
        item["publishers"] = (meta or {}).get("publishers") or []
        item["developers"] = (meta or {}).get("developers") or []
        item["stats"] = (meta or {}).get("stats")
        merged.append(item)
    return merged


def render_pass(state: State, candidates: list[dict], cfg: dict, now: datetime,
                stats_of: Callable[[dict], dict], *, announce_merges: bool,
                enrich_hook: Callable[[list[dict], list[dict], dict], dict] | None = None,
                fx: dict | None = None,
                upcoming: list[dict] | None = None,
                all_entries: list[dict] | None = None) -> dict:
    """合并缓存 → 多版本去重 → 分档 →（补 Steam 展示数据）→ 出报表。**可以调用多次**。

    第一次调用发生在详情还没抓的时候（缺的标「详情待补」，页面立刻可看），
    第二次在详情抓完之后（覆盖同一份文件）。

    ``stats_of`` 接收本轮的统计事实、返回要写进页面的 ``stats`` ——
    这样「概览」里的分档条数与实际渲染出来的分组一定是同一份数据。

    ``enrich_hook`` 只在**最后一版**传（首版里的条目大多还没有 appid，
    补不出东西，白白发 Steam 请求）；接收 ``(当日新增, 即将过期, info)``
    两个集合，跨区比价在两者间合并 appid 去重后现价真查（S7 换模型：
    原价 `appid|cc` 永久缓存 + 真查即校准）。

    ``upcoming``：「即将过期」视图的候选条目（调用方按
    :func:`classify.in_view` 筛好）；``None`` = 本轮不产出该视图。

    ``all_entries``（重构 S5）：今日筛选链通过的**全量**史低（hist_low），
    用于板块完整列表的分片 ``all/<板块>_<片号>.js``（懒加载数据源，2026-10-08 起
    取代单文件 ``all.js``）；
    ``None`` = 不产出分片（``output/all/`` 目录都不会建）。
    """
    def build_view(entries: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        merged = merge_details(state, entries, cfg)
        kept, deduped = classify.dedupe_by_appid(merged)
        shown = [entry for entry in kept if classify.is_shown(entry["tier"])]
        return kept, deduped, shown

    kept, deduped, shown = build_view(candidates)
    if announce_merges:
        for record in deduped:
            losers = "、".join(f"《{d['title']}》{d['price_int']}" for d in record["dropped"])
            log(f"      多版本合并：appid={record['appid']} 保留《{record['kept']}》"
                f"（{record['kept_price_int']}），合并掉 {losers}")

    # upcoming 的 None（不产出该视图）与 []（产出但为空）是两种不同语义，不能混
    has_upcoming = upcoming is not None
    # upcoming_shown_items：已合并详情、已按 appid 去重、且已过口碑分档（is_shown）——
    # 即「即将过期」视图里实际进列表的那批，供 data 分支的 expiring.json 导出。
    # ⚠️ 与 info["upcoming_shown"]（数字）并存：后者被 run_log / render_report 使用，
    # 不要把数字字段改成列表，否则 298 条数组会被塞进 state.json（changelog v3）。
    upcoming_shown_items: list[dict] = []
    upcoming_shown: list[dict] = []
    if has_upcoming:
        _, _, upcoming_shown = build_view(upcoming)
        upcoming_shown_items = upcoming_shown

    tier_counts = {tier: 0 for tier in classify.TIER_LABELS}
    for entry in kept:
        tier_counts[entry["tier"]] += 1

    info = {
        "tier": tier_counts,
        "shown": len(shown),
        "deduped": deduped,
        "kept": len(kept),
        "upcoming_shown": len(upcoming_shown),
        "upcoming_shown_items": upcoming_shown_items,
    }
    if enrich_hook:
        info["steam"] = enrich_hook(shown, upcoming_shown, info)
    stats = stats_of(info)
    labels = report.tier_labels(cfg)

    # ---- 比价结果回灌 + 估算补齐（S9 回归修复 + 2026-10-08 估算定案）----
    # enrich 就地写的是 `shown` / `upcoming_shown` 这批 dict；而首页四板块、大卡、
    # all.js 的列表都走下面 `build_view(all_entries)` **另建**的 all_cards ——
    # merge_details 里 `item = dict(entry)` 是拷贝，两批 dict 不共享，
    # 不回灌的话 compare 永远传不到页面（S9 把首页池子从 items 换成 all_cards 后
    # 才暴露：`graft_compare_from_cache` 只在本地预览里补，掩盖了线上）。
    # 现查与估算的分工（2026-10-08 用户定案）：**真查只保证「当日新增 + 即将到期」
    # 两个板块的真实性**（每日抓取的意义是防打折中途降价）；其余板块的历史条目
    # 用「外区原价永久缓存 × 国区折扣比例」估算（原价缓存就是为此备料，见
    # enrich.estimate_compare）—— 零额外 Steam 请求。
    compare_by_appid: dict[int, list[dict]] = {}
    for entry in shown + upcoming_shown:
        appid = entry.get("appid")
        if appid and entry.get("compare"):
            compare_by_appid[appid] = entry["compare"]

    # ---- 重构 S5：「全部」视图数据 = 今日筛选链全量（含当日新增），逐条打视图标志 ----
    # 懒加载视图（板块完整列表）的数据源：all/<板块>_<片号>.js 用它。
    # ⚠️ 2026-10-08 起不再统计视图按钮 count（那组 payload 已删）。
    # 比价数据：真查覆盖条目回灌（当日新增 + 即将到期）；其余历史条目按
    # 「原价缓存 × 国区折扣比例」估算（2026-10-08 定案，见 enrich.estimate_compare）
    # —— 仍然零额外 Steam 请求，缓存里没有原价的（极少数）才回落前端说明行。
    all_cards: list[dict] | None = None
    if all_entries is not None:
        _, _, all_shown = build_view(all_entries)
        all_cards = []
        for entry in all_shown:
            # 真查命中回灌、否则估算 —— 判断只有一份（enrich.fill_compare，卡片 05）
            enrich.fill_compare(state, cfg, entry, fx, truth_by_appid=compare_by_appid)
            card = report.build_card(entry, now, labels, cfg)
            card["views"] = [
                key for key in classify.VIEW_KEYS
                if classify.in_view(key, entry, now, cfg)
            ]
            # S9：首页四板块归属（前端按它筛板块的完整列表；同一游戏可跨板块）
            card["sections"] = report.section_keys(card, cfg)
            all_cards.append(card)
        info["all_shown"] = len(all_shown)

    cards = [report.build_card(entry, now, labels, cfg) for entry in shown]
    info["paths"] = report.render(
        cfg, cards, stats, now, fx=fx, steam=info.get("steam"),
        all_cards=all_cards,
        run_log=state.run_log,   # S9：「关于网站」页的每日更新日志
    )
    return info


def build_stats(*, sweep: str, deals_fetched: int, hist_low_total: int, candidates: int,
                info: dict, detail_fetched: int, detail_targets_n: int,
                detail_backlog: int, now: datetime) -> dict:
    """页面「概览」与 `latest.json` 的口径（§7.1）。

    ⚠️ `new_today_raw` 才是**真实当日新增量**；`new_today_shown` 是过了好评分档
    之后实际进列表的条数。两者可能差很多（实测 105 vs 20），页面必须显示前者。
    """
    steam = info.get("steam") or {}
    return {
        "sweep": sweep,
        "deals_fetched": deals_fetched,
        "hist_low_total": hist_low_total,
        "new_today_raw": candidates,
        "new_today_shown": info["shown"],
        "detail_fetched": detail_fetched,
        "detail_targets": detail_targets_n,
        "detail_backlog": detail_backlog,
        "detail_pending": info["tier"].get(classify.TIER_PENDING, 0),
        "deduped_versions": len(info["deduped"]),
        "title_fetched": steam.get("title_fetched", 0),
        "title_cached": steam.get("title_cached", 0),
        "compare_batches": steam.get("compare_batches", 0),
        "price_mismatch": steam.get("price_mismatch") or [],
        "last_run_at": now.isoformat(timespec="seconds"),
    }
