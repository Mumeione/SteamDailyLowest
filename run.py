# -*- coding: utf-8 -*-
"""CLI 入口（对应 docs/DEVELOPMENT.md §12）。

    python run.py              # 日常：抓列表 → 筛 → 出首版报表 → 取当日新增详情 → 覆盖报表
    python run.py --baseline   # 只把当前全部折扣写入状态，不取详情、不出报表
    python run.py --audit      # 体检：无 filter 全量抓取，统计完整分布，不取详情、不出报表

两个必须遵守的结构性约定（§3.3）：

1. **报表永远不等详情。** 先在缓存上渲染一版页面（缺的标「详情待补」），
   再去抓详情，抓完覆盖同一份文件。任何时刻页面都在，被打断也留下一版可看的报表。
2. **状态先落盘。** 写完 `seen_deal` 立刻 `save()`，再进详情阶段 ——
   否则详情全部失败时那一轮的攒库会一起丢。

第一版只上线「当日新增」一个视图（§1.1）：其余视图的数据照常写进状态库先攒着。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from src import classify, enrich, report
from src.config import ConfigError, api_key, load_config, parse_rate_limit, resolve_path
from src.httpclient import Blocked, HttpError
from src.itad import (
    SWEEP_FULL,
    SWEEP_LOW_ONLY,
    SWEEP_MODES,
    ItadClient,
    ItadError,
)
from src.ratelimit import RateLimiter
from src.state import State
from src.steam import SteamClient

ROOT = Path(__file__).resolve().parent
DETAIL_SAVE_EVERY = 20


def log(message: str) -> None:
    print(message, flush=True)


def progress_log(pages: int, total: int) -> None:
    """翻页进度：只在第 1 页与每 10 页打一行（全量口径有 162 页）。"""
    if pages == 1 or pages % 10 == 0:
        log(f"      翻页 {pages}：累计 {total} 条")


def build_client(cfg: dict) -> ItadClient:
    calls, window = parse_rate_limit(cfg["itad_rate_limit"])
    limiter = RateLimiter(
        name="itad",
        max_calls=calls,
        window_seconds=window,
        min_interval=float(cfg["itad_min_interval"]),
    )
    return ItadClient(
        api_key=api_key(cfg),
        limiter=limiter,
        timeout=float(cfg["request_timeout_seconds"]),
        pause=float(cfg["request_pause_seconds"]),
        log=log,
    )


def build_steam_client(cfg: dict) -> SteamClient:
    """Steam store 全站按**同一个预算**合并计数（§2.2 / §3.3）。"""
    calls, window = parse_rate_limit(cfg["steam_rate_limit"], default=(150, 300))
    limiter = RateLimiter(
        name="steam",
        max_calls=calls,
        window_seconds=window,
        min_interval=float(cfg["steam_min_interval"]),
    )
    return SteamClient(
        limiter=limiter,
        timeout=float(cfg.get("steam_timeout_seconds", 15)),
        pause=float(cfg["request_pause_seconds"]),
        log=log,
        lang=cfg.get("steam_lang", "schinese"),
        batch_size=int(cfg.get("steam_batch_size", 20)),
    )


def detail_targets(hist_low: list[dict], candidates: list[dict], state: State,
                   cfg: dict, now: datetime) -> tuple[list[dict], dict]:
    """决定这一轮要给哪些游戏抓详情。

    两段（用户要求「只跑符合要求的史低目录、别每轮全跑一遍」）：

    1. **当日新增 → 全部抓**（本轮报表的核心，必须完整）
    2. **目录里的其它史低 → 按每日预算增量补**（`detail_scope=catalog`）
       优先级：折扣力度大的先补（最可能被人看到）；命中缓存的直接跳过。
       `detail_scope=new_today` 或 `detail_daily_budget=0` 时退化成旧行为。

    这里的「目录」= 通过筛选链的史低（本体 + 付费 + `flag != None`），与报表口径一致。
    **不额外用服务端 `steamCount` 再筛一道** —— 那会静默丢掉「ITAD 缺评测数据、
    但 Steam 上评价很多」的游戏，正是 §3.5 要避免的事（详见 §12.1）。
    """
    scope = (cfg.get("detail_scope") or "new_today").lower()
    budget = int(cfg.get("detail_daily_budget", 0) or 0)
    ttl = int(cfg.get("reviews_ttl_days", 7))
    empty_ttl = int(cfg.get("reviews_empty_ttl_days", 3))

    targets = list(candidates)
    backfill: list[dict] = []
    if scope == "catalog" and budget > 0:
        new_ids = {e.get("game_id") for e in candidates}
        rest = [e for e in hist_low if e.get("game_id") not in new_ids]
        rest.sort(key=lambda e: (-(int(e.get("cut") or 0)), e.get("expiry") or ""))
        for entry in rest:
            if len(backfill) >= budget:
                break
            if state.meta_valid(entry.get("game_id"), now, ttl, empty_ttl):
                continue
            backfill.append(entry)
    targets.extend(backfill)
    return targets, {
        "scope": scope,
        "budget": budget,
        "new_today": len(candidates),
        "backfill_chosen": len(backfill),
    }


def count_backlog(hist_low: list[dict], state: State, cfg: dict, now: datetime) -> int:
    """目录里还有多少条没详情（给页面显示增量补齐的进度）。"""
    ttl = int(cfg.get("reviews_ttl_days", 7))
    empty_ttl = int(cfg.get("reviews_empty_ttl_days", 3))
    return sum(
        1 for e in hist_low
        if not state.meta_valid(e.get("game_id"), now, ttl, empty_ttl)
    )


def resolve_sweep(cfg: dict, audit: bool) -> str:
    """体检模式强制全量口径；其余按配置（默认只取史低，27 页）。"""
    if audit:
        return SWEEP_FULL
    sweep = cfg.get("sweep_mode") or SWEEP_LOW_ONLY
    if sweep not in SWEEP_MODES:
        raise ConfigError(f"sweep_mode 只能是 {SWEEP_MODES}，当前 {sweep!r}")
    return sweep


def funnel(entries: list[dict], cfg: dict) -> dict:
    """筛选链（§3.2）：只留本体游戏 → 排除免费 → 判史低。

    ⚠️ 当 `sweep_mode=low_only` 时，`type` 与 `flag` 已经由服务端过滤过了，
    所以 `type_not_game` / `not_historical_low` / `flag_price_mismatch`
    三个计数必然为 0 —— 那不是「今天没有异常」，而是**这两个口径看不到**。
    要看真实分布请跑 `--audit`。
    """
    counts = {
        "type_not_game": 0,
        "mature": 0,
        "free": 0,
        "price_missing": 0,
        "below_min_cut": 0,
        "above_max_price": 0,
        "store_low_missing": 0,
        "not_historical_low": 0,
        "flag_price_mismatch": 0,
    }
    hist_low: list[dict] = []
    only_type = cfg.get("only_type")
    min_cut = cfg.get("min_cut") or 0
    max_price = cfg.get("max_price")

    for entry in entries:
        if only_type and entry.get("type") != only_type:
            counts["type_not_game"] += 1
            continue
        if cfg.get("exclude_mature") and entry.get("mature"):
            counts["mature"] += 1
            continue
        price = entry.get("price_int")
        if price is None:
            counts["price_missing"] += 1
            continue
        if cfg.get("exclude_free") and price <= 0:
            counts["free"] += 1
            continue
        if (entry.get("cut") or 0) < min_cut:
            counts["below_min_cut"] += 1
            continue
        if max_price is not None and price > max_price:
            counts["above_max_price"] += 1
            continue

        kind = classify.low_kind(entry)  # 直接用 deal.flag（§4.1）
        if kind == "unknown":
            counts["store_low_missing"] += 1
            continue
        if kind is None:
            counts["not_historical_low"] += 1
            if classify.flag_price_mismatch(entry):
                counts["flag_price_mismatch"] += 1
            continue
        entry["low_kind"] = kind
        hist_low.append(entry)

    return {"counts": counts, "hist_low": hist_low}


def flag_distribution(entries: list[dict]) -> dict:
    """`flag` 分布（体检用）：能看出 ITAD 的口径有没有变。"""
    dist: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("flag"))
        dist[key] = dist.get(key, 0) + 1
    return dist


def pick_new_today(hist_low: list[dict], tz, today, has_seen) -> tuple[list[dict], dict]:
    """当日新增（§4.2）：主口径 timestamp，仅在缺失时才用「首次见到」兜底。"""
    picked: list[dict] = []
    by_reason = {"timestamp": 0, "first_seen": 0, "not_new": 0}
    for entry in hist_low:
        if classify.timestamp_is_today(entry, today, tz):
            picked.append(entry)
            by_reason["timestamp"] += 1
            continue
        if classify.timestamp_missing(entry, tz) and not has_seen(entry):
            entry["new_reason"] = "first_seen"
            picked.append(entry)
            by_reason["first_seen"] += 1
            continue
        by_reason["not_new"] += 1
    return picked, by_reason


def fetch_details(client: ItadClient, state: State, candidates: list[dict], cfg: dict, now: datetime) -> int:
    """只抓「当日新增」的详情，缓存命中不发请求；按 uuid 逐条落盘（断点续传）。

    是否发请求由 :meth:`State.meta_valid` 决定 —— 「抓过但没拿到 appid / 好评率」
    的条目也按 ``reviews_empty_ttl_days`` 计 TTL，不再每轮重复请求。
    """
    ttl = int(cfg.get("reviews_ttl_days", 7))
    empty_ttl = int(cfg.get("reviews_empty_ttl_days", 3))
    fetched = 0
    for entry in candidates:
        game_id = entry.get("game_id")
        if state.meta_valid(game_id, now, ttl, empty_ttl):
            continue
        try:
            info = client.fetch_info(game_id)
        except ItadError as exc:
            log(f"[warn] 详情抓取失败，标记「详情待补」：{entry.get('title')}（{exc}）")
            continue
        if not info:
            log(f"[warn] 详情为空，标记「详情待补」：{entry.get('title')}")
            continue
        state.set_meta(game_id, info.get("appid"), info.get("reviews"), now)
        fetched += 1
        if fetched % DETAIL_SAVE_EVERY == 0:
            state.save()  # 断点续传：中途失败下次只补缺的
    return fetched


def merge_details(state: State, entries: list[dict], cfg: dict) -> list[dict]:
    """把详情缓存合并进条目并分档（§3.5 / §7.2）。

    「详情待补」只用于**这一轮没抓到详情**的条目（抓取失败、被中断）；
    抓到但 ITAD 没有 Steam 好评率数据 → 归入「冷门 / 无数据」，不展示。

    中文名也在这里合并 —— 它是 `game_meta` 里的一等缓存字段，
    **不能只在 enrich 阶段才读**：首版渲染不跑 enrich（那时大多还没有 appid），
    漏掉这一步就会让已经有中文名的游戏在首版退化成英文名。
    """
    merged: list[dict] = []
    for entry in entries:
        meta = state.meta(entry.get("game_id"))
        item = dict(entry)
        item["title_zh"] = meta.get("title_zh") if meta else None
        if not meta or not meta.get("fetched_at"):
            item["appid"] = None
            item["reviews"] = None
            item["tier"] = classify.TIER_PENDING
        else:
            item["appid"] = meta.get("appid")
            item["reviews"] = meta.get("reviews")
            item["tier"] = classify.tier_of(item["reviews"], cfg)
        merged.append(item)
    return merged


def render_pass(state: State, candidates: list[dict], cfg: dict, now: datetime,
                stats_of: Callable[[dict], dict], *, announce_merges: bool,
                enrich_hook: Callable[[list[dict], dict], dict] | None = None,
                fx: dict | None = None) -> dict:
    """合并缓存 → 多版本去重 → 分档 →（补 Steam 展示数据）→ 出报表。**可以调用多次**。

    第一次调用发生在详情还没抓的时候（缺的标「详情待补」，页面立刻可看），
    第二次在详情抓完之后（覆盖同一份文件）。

    ``stats_of`` 接收本轮的统计事实、返回要写进页面的 ``stats`` ——
    这样「概览」里的分档条数与实际渲染出来的分组一定是同一份数据。

    ``enrich_hook`` 只在**最后一版**传（首版里的条目大多还没有 appid，
    补不出东西，白白发 Steam 请求）。
    """
    merged = merge_details(state, candidates, cfg)
    kept, deduped = classify.dedupe_by_appid(merged)
    if announce_merges:
        for record in deduped:
            losers = "、".join(f"《{d['title']}》{d['price_int']}" for d in record["dropped"])
            log(f"      多版本合并：appid={record['appid']} 保留《{record['kept']}》"
                f"（{record['kept_price_int']}），合并掉 {losers}")

    tier_counts = {tier: 0 for tier in classify.TIER_LABELS}
    shown: list[dict] = []
    for entry in kept:
        tier_counts[entry["tier"]] += 1
        if classify.is_shown(entry["tier"]):
            shown.append(entry)

    info = {
        "tier": tier_counts,
        "shown": len(shown),
        "deduped": deduped,
        "kept": len(kept),
    }
    if enrich_hook:
        info["steam"] = enrich_hook(shown, info)
    stats = stats_of(info)
    labels = report.tier_labels(cfg)
    cards = [report.build_card(entry, now, labels) for entry in shown]
    info["paths"] = report.render(cfg, cards, stats, now, fx=fx, steam=info.get("steam"))
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


def collect(cfg: dict, client: ItadClient, sweep: str):
    """抓折扣列表并归一化。"""
    items = client.fetch_deals(
        country=cfg["country"],
        limit=200,
        max_deals=cfg.get("max_deals"),
        sweep=sweep,
        progress=progress_log,
    )
    if not items:
        raise ItadError("折扣列表为空 —— 中止，不生成空报表")
    return items, [classify.normalize_item(item) for item in items]


def run_daily(cfg: dict, audit: bool = False) -> int:
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    today = now.date()
    sweep = resolve_sweep(cfg, audit)

    state = State(resolve_path(cfg, "state_path"), tz=tz).load()
    client = build_client(cfg)

    mode_label = "体检 --audit" if audit else "日常运行"
    total_steps = 3 if audit else 7
    log("=" * 70)
    log(f"SteamDailyLowest {mode_label} {now.isoformat(timespec='seconds')}"
        f"（时区 {cfg['timezone']}，抓取口径 {sweep}）")
    log("=" * 70)

    dropped = state.cleanup_expired(now, int(cfg["expired_retention_days"]))
    log(f"[1/{total_steps}] 留存清理：删除过期超过 {cfg['expired_retention_days']} 天的条目 {dropped} 条")

    if audit:
        items, normalized = collect(cfg, client, sweep)
        deals_fetched = len(items)
        log(f"[2/{total_steps}] 抓取折扣列表：{deals_fetched} 条（口径 {sweep}）")
        result = funnel(normalized, cfg)
        counts = result["counts"]
        hist_low = result["hist_low"]
        log(f"[3/{total_steps}] 筛选链：本体游戏 {deals_fetched - counts['type_not_game']} · "
            f"排除免费 {counts['free']} · 史低 {len(hist_low)}")
        log("      " + " · ".join(f"{k}={v}" for k, v in counts.items() if v))
        log(f"      flag 分布：{flag_distribution(normalized)}")
        for entry in hist_low:
            state.record_seen(entry, now)
        state.add_run_log(
            {
                "run_at": now.isoformat(timespec="seconds"),
                "mode": "audit",
                "sweep": sweep,
                "deals_fetched": deals_fetched,
                "itad_requests": client.calls,
                "steam_requests": 0,
                "filtered": counts,
                "flag_distribution": flag_distribution(normalized),
                "hist_low_total": len(hist_low),
                "errors": [],
            },
            keep=int(cfg.get("run_log_keep", 30)),
        )
        state.save(now)
        log(f"      体检完成：未取详情、未产出报表。ITAD 请求 {client.calls} 次，"
            f"状态库已写入 {resolve_path(cfg, 'state_path')}")
        return 0

    items, normalized = collect(cfg, client, sweep)
    deals_fetched = len(items)
    log(f"[2/{total_steps}] 抓取折扣列表：{deals_fetched} 条（口径 {sweep}）")

    result = funnel(normalized, cfg)
    counts = result["counts"]
    hist_low = result["hist_low"]
    log(f"[3/{total_steps}] 筛选链：本体游戏 {deals_fetched - counts['type_not_game']} · "
        f"排除免费 {counts['free']} · 史低 {len(hist_low)}")
    shown_counts = " · ".join(f"{k}={v}" for k, v in counts.items() if v)
    if sweep == SWEEP_LOW_ONLY:
        shown_counts += "   （low_only 口径下 type/flag 计数必然为 0，要看分布请跑 --audit）"
    log(f"      {shown_counts}")

    candidates, by_reason = pick_new_today(
        hist_low, tz, today,
        lambda e: state.has_seen(e.get("game_id"), e.get("price_int"), e.get("expiry")),
    )
    log(f"[4/{total_steps}] 当日新增候选：{len(candidates)} 条"
        f"（timestamp 命中 {by_reason['timestamp']}，首次见到兜底 {by_reason['first_seen']}）")

    # 状态库按完整结构写：所有史低都攒库（其余视图以后再开，§1.1）
    for entry in hist_low:
        state.record_seen(entry, now)
    state.save(now)  # ← 关键：先落盘，别让详情阶段的失败把这一轮的攒库一起带走
    log(f"      状态库已落盘（{len(hist_low)} 条已见记录）：随后即便详情全失败也不会丢")

    # ---- 详情抓取目标：当日新增全部 + 目录内按预算增量补 ----
    targets, target_info = detail_targets(hist_low, candidates, state, cfg, now)
    log(f"      详情目标 {len(targets)} 个：当日新增 {target_info['new_today']} 个"
        f" + 目录增量补 {target_info['backfill_chosen']} 个"
        f"（口径 {target_info['scope']}，每日预算 {target_info['budget']}）")

    fx_rates = enrich.load_fx(cfg, today.isoformat(), log)
    steam_client = build_steam_client(cfg)

    def stats_of(detail_fetched: int, backlog: int):
        def build(info: dict) -> dict:
            return build_stats(
                sweep=sweep, deals_fetched=deals_fetched, hist_low_total=len(hist_low),
                candidates=len(candidates), info=info, detail_fetched=detail_fetched,
                detail_targets_n=len(targets), detail_backlog=backlog, now=now,
            )
        return build

    # ---- 首版报表：不等详情（§3.3）----
    first = render_pass(state, candidates, cfg, now, stats_of(0, count_backlog(hist_low, state, cfg, now)),
                        announce_merges=False, fx=fx_rates)
    log(f"[5/{total_steps}] 首版报表已生成（缺详情的标「详情待补」，页面立刻可看）："
        f"{first['paths']['index']}")

    # ---- 慢的部分放最后 ----
    detail_fetched = fetch_details(client, state, targets, cfg, now)
    state.save(now)
    backlog = count_backlog(hist_low, state, cfg, now)
    log(f"[6/{total_steps}] 取详情：新抓 {detail_fetched} 个"
        f"（目标 {len(targets)} 个，其余命中缓存）；目录还差 {backlog} 条")

    def do_enrich(shown: list[dict], info: dict) -> dict:
        facts = enrich.enrich_steam(steam_client, state, shown, cfg, fx_rates, now, log)
        log(f"      中文名：新取 {facts['title_fetched']} 个 · 命中缓存 {facts['title_cached']} 个"
            f"；跨区比价批量 {facts['compare_batches']} 次"
            + (f"；Steam 请求合计 {steam_client.calls} 次" if steam_client.calls else ""))
        if facts["price_mismatch"]:
            for item in facts["price_mismatch"]:
                log(f"      [warn] 国区价对不上：{item['title']} "
                    f"ITAD={item['itad_price_int']} Steam={item['steam_price_int']}")
        return facts

    final = render_pass(state, candidates, cfg, now, stats_of(detail_fetched, backlog),
                        announce_merges=True, enrich_hook=do_enrich, fx=fx_rates)
    log(f"[7/{total_steps}] 完整版报表已覆盖：进列表 {final['shown']} 条（分档 {final['tier']}）")

    errors = [e for e in client.events if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")]
    state.add_run_log(
        {
            "run_at": now.isoformat(timespec="seconds"),
            "mode": "daily",
            "sweep": sweep,
            "deals_fetched": deals_fetched,
            "itad_requests": client.calls,
            "steam_requests": steam_client.calls,
            "filtered": counts,
            "new_by_reason": by_reason,
            "new_today_raw": len(candidates),
            "new_today_shown": final["shown"],
            "detail_scope": target_info["scope"],
            "detail_daily_budget": target_info["budget"],
            "detail_targets": len(targets),
            "detail_fetched": detail_fetched,
            "detail_backlog": backlog,
            "detail_pending": final["tier"].get(classify.TIER_PENDING, 0),
            "title_zh_fetched": (final.get("steam") or {}).get("title_fetched", 0),
            "title_zh_cached": (final.get("steam") or {}).get("title_cached", 0),
            "price_mismatch": (final.get("steam") or {}).get("price_mismatch") or [],
            "deduped_versions": len(final["deduped"]),
            "tier": final["tier"],
            "fx": {"date": (fx_rates or {}).get("date"), "base": (fx_rates or {}).get("base")},
            "limiter": client.limiter.stats(),
            "steam_limiter": steam_client.limiter.stats(),
            "errors": errors + [e for e in steam_client.events
                                if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")],
        },
        keep=int(cfg.get("run_log_keep", 30)),
    )
    state.save(now)
    log(f"      ITAD 请求 {client.calls} 次（限流事件 {client.rate_limit_events}）· "
        f"Steam 请求 {steam_client.calls} 次（限流事件 {steam_client.rate_limit_events}）· "
        f"状态库已写入 {resolve_path(cfg, 'state_path')}")
    return 0


def run_baseline(cfg: dict) -> int:
    """只写状态：不发详情请求、不产出报表（§4.2）。"""
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    sweep = resolve_sweep(cfg, audit=False)
    state = State(resolve_path(cfg, "state_path"), tz=tz).load()
    client = build_client(cfg)

    log("=" * 70)
    log(f"SteamDailyLowest --baseline {now.isoformat(timespec='seconds')}"
        f"（时区 {cfg['timezone']}，抓取口径 {sweep}）")
    log("=" * 70)

    dropped = state.cleanup_expired(now, int(cfg["expired_retention_days"]))
    items, normalized = collect(cfg, client, sweep)
    result = funnel(normalized, cfg)
    hist_low = result["hist_low"]
    new_count = 0
    for entry in hist_low:
        _, first_time = state.record_seen(entry, now)
        if first_time:
            new_count += 1

    log(f"抓取 {len(items)} 条，史低 {len(hist_low)} 条，新写入状态 {new_count} 条，留存清理 {dropped} 条")
    log(f"未取详情、未产出报表。ITAD 请求 {client.calls} 次。")

    state.add_run_log(
        {
            "run_at": now.isoformat(timespec="seconds"),
            "mode": "baseline",
            "sweep": sweep,
            "deals_fetched": len(items),
            "itad_requests": client.calls,
            "steam_requests": 0,
            "filtered": result["counts"],
            "hist_low_total": len(hist_low),
            "seen_written": new_count,
            "errors": [],
        },
        keep=int(cfg.get("run_log_keep", 30)),
    )
    state.save(now)
    log(f"状态库已写入：{resolve_path(cfg, 'state_path')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Steam 史低日报（第一版：只上线「当日新增」）")
    parser.add_argument("--baseline", action="store_true",
                        help="只把当前全部折扣写入状态，不取详情、不出报表")
    parser.add_argument("--audit", action="store_true",
                        help="体检：无 filter 全量抓取（162 页），统计完整分布与 §4.1 交叉校验，"
                             "不取详情、不出报表")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.audit:
            return run_daily(cfg, audit=True)
        return run_baseline(cfg) if args.baseline else run_daily(cfg)
    except ConfigError as exc:
        log(f"[错误] {exc}")
        return 2
    except Blocked as exc:
        # ITAD / Steam 任一被滥用封禁都走这里（两者共用 httpclient 的策略）
        log(f"[中止] {exc}")
        log("      本轮未产出报表；请先排查是否触发滥用封禁（不要轮换 IP）。")
        return 3
    except HttpError as exc:
        log(f"[错误] 请求失败：{exc}")
        return 4
    except Exception:
        log("[错误] 未预期的异常：")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
