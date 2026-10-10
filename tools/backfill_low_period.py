#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""史低期记忆回填（§3.6 扩展，2026-10-09；数据源从 seen_deal 改为 ITAD history）。

背景：新史低的「上一次史低」没有现成数据源 ——

- ``storelow/v2`` 对**新史低**返回的是「本次自己」（≈ 本次折扣开始），拿不到上一次；
- 全商店口径的 ``historylow/v1`` 也不是 Steam 史低。

改用 ITAD ``GET /games/history/v2``（**逐条**、``shops=61`` Steam 口径）拉价格流水，
从中推出**上一次史低期的开始**（价格 ≤ 此前历史最低的时刻，见 :func:`low_period_starts`）。
日常运行从上线时刻起自然积累；本脚本做冷启动 / 定点回填。

- **只补「能进列表」的条目**（``classify.is_shown``，判据唯一出处）—— 回填是逐条 GET、
  受配额约束，没必要给永远不展示的冷门条目花钱。
- **只填空、不覆盖**：已有 ``low_period.prev`` 的整条跳过；``cur`` 不回退（记忆里的
  cur 可能比流水还新 —— 此时把它并进时间线，prev 自然取流水里最新那段）。幂等可重跑。
- **限流**：复用 :func:`src.itad.build_client`（与正式管线**同一套** ``RateLimiter``
  参数，绝不轮换 IP），并可选 ``--limit`` 限量抽样。
- ``since`` 必须显式给（ITAD 默认只回最近 3 个月，而「上一次史低」实测常在
  1~2 年前：8 个新史低样本间隔 74~769 天）。

用法（本机试跑 / Actions ``backfill.yml`` 同一入口）::

    python tools/backfill_low_period.py --state data/state.json            # 真写
    python tools/backfill_low_period.py --state data/state.json --dry-run  # 只看统计
    python tools/backfill_low_period.py --since-days 800 --limit 30         # 抽样试跑
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.config import load_config  # noqa: E402
from src.httpclient import Blocked, HttpError  # noqa: E402
from src.itad import build_client  # noqa: E402
from src.pipeline import log  # noqa: E402  统一输出出口（stdout + flush）
from src.state import State  # noqa: E402
from tools.backfill_common import (  # noqa: E402
    event_kinds,
    fmt_mmss,
    summarize_events,
    write_step_summary,
)

#: ``since`` 默认回溯天数：实测上一次史低期常在 1~2 年前（74~769 天）。
DEFAULT_SINCE_DAYS = 800
#: 进度心跳间隔（条）：7.7k 条约 50 分钟，每 100 条报一次否则日志全程静默
#: （backfill 日志审查 P1：「在跑 / 被限流 / 卡死」无法区分）。
HEARTBEAT_EVERY = 100


def low_period_starts(rows: list[dict]) -> list[str]:
    """从 ``history/v2`` 价格流水推「史低期开始」的时刻（升序）。

    只做**取数**（把 ``history/v2`` 的嵌套结构拍平成 ``[(ts, price)]``）；
    判定本身在 :func:`src.classify.low_period_starts` —— 与日常增量那条时间线
    共用同一份规则（架构检查卡片 02：从前这里另写了一套，两边互不知情）。
    ``deal`` / ``price`` 缺失的行由那边自动跳过。
    """
    points: list[tuple[str, int]] = []
    for row in rows or []:
        ts = row.get("timestamp")
        price = ((row.get("deal") or {}).get("price") or {}).get("amountInt")
        if ts and price is not None:
            points.append((ts, price))
    return classify.low_period_starts(points)


def derive_low_period(rows: list[dict], existing_cur: str | None, tz) -> tuple[str, str] | None:
    """推 ``(cur, prev)``；没有「上一次史低期」时返回 ``None``。

    时间线合并规则同样复用 :func:`src.classify.low_period_pair`（唯一出处）。
    """
    return classify.low_period_pair(low_period_starts(rows), existing_cur, tz)


def select_targets(state: State, cfg: dict) -> list[str]:
    """回填目标：seen_deal 里**能进列表**的史低条目（按 game_id 去重、稳定排序）。

    判据复用唯一出处 :func:`classify.is_shown_meta`（2026-10-09 卡片 07：从
    ``is_shown(tier_of(...))`` 收敛而来）—— 它只回答「按**已抓到的评价数据**
    够不够格」，**不**把「没抓过详情」当 PENDING 放行：``pending`` 是抓取状态而不是
    档位，放行等于给永远不展示的冷门条目白花配额。所以实际选中的是 quality/notable；
    「详情待补」的条目等详情到位、下一轮再补（脚本可重跑，自愈）。
    """
    gids: set[str] = set()
    for entry in state.seen_deal.values():
        gid = entry.get("game_id")
        if gid and classify.is_shown_meta(state.meta(gid), cfg):
            gids.add(gid)
    return sorted(gids)


def backfill(state: State, fetch, cfg: dict, tz, limit: int = 0, *,
             targets: list[str] | None = None,
             heartbeat_every: int = HEARTBEAT_EVERY,
             clock=time.monotonic, progress=None) -> dict[str, int]:
    """逐个拉历史、推史低期、写 ``game_meta[gid].low_period``（不落盘）。

    ``fetch(game_id) -> list[dict]`` 由调用方注入（真跑是
    :meth:`ItadClient.fetch_price_history` 的偏函数，测试注入假函数）。
    ``targets`` 由 :func:`select_targets` 给出（缺省时内部自查 —— main 只调
    一次 select 并传入，修掉「打印一遍、内部再扫一遍」的双重全量扫描）。
    返回统计。只填空：已有 ``prev`` 整条跳过。

    **进度心跳**：每 ``heartbeat_every`` 个目标调一次
    ``progress(i, stats, elapsed_seconds)``（不传则静默）—— 真跑约 50 分钟，
    没有心跳就无法区分「在跑 / 被限流 / 卡死」。
    """
    if targets is None:
        targets = select_targets(state, cfg)
    stats = {"targets": len(targets), "fetched": 0,
             "filled": 0, "skipped_complete": 0, "no_prev": 0}
    start = clock()
    for i, gid in enumerate(targets, 1):
        if limit and stats["fetched"] >= limit:
            break
        meta = state.game_meta.get(gid) or {}
        existing = meta.get("low_period") or {}
        if existing.get("prev"):
            stats["skipped_complete"] += 1        # 已有上一次史低期，整条不覆盖
        else:
            resolved = derive_low_period(fetch(gid), existing.get("cur"), tz)
            stats["fetched"] += 1
            if resolved is None:
                stats["no_prev"] += 1             # 推不出上一次（该游戏首次史低）
            else:
                cur_ts, prev_ts = resolved
                # 受控写口（不直写 game_meta，见 State.set_low_period 的注释）
                state.set_low_period(gid, cur_ts, prev_ts)
                stats["filled"] += 1
        # 心跳按目标序号计（含跳过项），**任何**分支都不会静默太久 —— 不能放在
        # 某个成功分支里，否则长段 no_prev（首次史低）会连续几百条没有输出
        if heartbeat_every and progress and i % heartbeat_every == 0:
            progress(i, stats, clock() - start)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="史低期记忆回填（ITAD history/v2 → game_meta.low_period）")
    parser.add_argument("--state", default="data/state.json", help="state.json 路径（默认 data/state.json）")
    parser.add_argument("--config", default="config.json", help="配置文件（默认 config.json；key 支持 ITAD_API_KEY 环境变量）")
    parser.add_argument("--country", default=None, help="覆盖配置里的国家码（默认取配置 country）")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS,
                        help=f"价格流水回溯天数（默认 {DEFAULT_SINCE_DAYS}）")
    parser.add_argument("--limit", type=int, default=0, help="最多查多少个游戏（0=不限，抽样试跑用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写盘")
    args = parser.parse_args()

    state_path = Path(args.state)
    if not state_path.is_file():
        print(f"state 文件不存在：{state_path}", file=sys.stderr, flush=True)
        return 2

    cfg = load_config(args.config)
    tz = classify.zone(cfg.get("timezone", "Asia/Shanghai"))
    country = args.country or cfg["country"]

    state = State(state_path, tz=tz)
    state.load()
    client = build_client(cfg, log=log)
    # since 按 **UTC** 组串（ITAD 认 UTC；早先用本地时区拼 'Z' 会差一个时区）
    since = (datetime.now(timezone.utc)
             - timedelta(days=args.since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # select 只跑一次，结果同时用于打印与回填（7.7k 条开跑前不多扫一遍 seen_deal）
    targets = select_targets(state, cfg)
    errors = {"n": 0}                             # 单条失败计数（不阻断整轮）

    def fetch(game_id: str) -> list[dict]:
        try:
            return client.fetch_price_history(game_id, country, since=since)
        except Blocked:                           # 滥用封禁：绝不降级硬扛，中止整轮
            raise
        except HttpError as exc:                  # 单条失败不阻断整轮（ItadError 是它的子类）
            errors["n"] += 1
            print(f"[warn] {game_id} 价格流水抓取失败，跳过：{exc}",
                  file=sys.stderr, flush=True)
            return []

    def progress(i: int, stats: dict, elapsed: float) -> None:
        rate = stats["fetched"] / (elapsed / 60) if elapsed > 0 else 0.0
        log(f"进度 {i}/{stats['targets']} · 已查 {stats['fetched']} · "
            f"已填 {stats['filled']} · 跳过 {stats['skipped_complete']} · "
            f"失败 {errors['n']} · 速率 {rate:.0f}/min · 已用 {fmt_mmss(elapsed)}")

    log(f"回填目标（能进列表）：{len(targets)} 个｜since={since}｜国家={country}"
        f"｜限流={cfg['itad_rate_limit']}@≥{cfg['itad_min_interval']}s"
        + (f"｜limit={args.limit}" if args.limit else ""))
    t0 = time.monotonic()
    try:
        stats = backfill(state, fetch, cfg, tz, limit=args.limit,
                         targets=targets, progress=progress)
    except Blocked as exc:
        # 滥用封禁：与 run.py 同口径（退出码 3）。不写盘 —— 回填是幂等的，下次重跑即可。
        print(f"[中止] {exc}", file=sys.stderr, flush=True)
        print("      封禁期间不再请求；请先排查（不要轮换 IP）。",
              file=sys.stderr, flush=True)
        write_step_summary("backfill_low_period", [
            ("结果", "❌ 滥用封禁中止（退出码 3）—— 未写盘，排查后重跑即可"),
            ("异常事件", summarize_events(client.events)),
            ("总耗时", fmt_mmss(time.monotonic() - t0)),
        ])
        return 3
    elapsed = time.monotonic() - t0
    rate = stats["fetched"] / (elapsed / 60) if elapsed > 0 else 0.0
    log(f"抓取 {stats['fetched']} 个游戏的价格流水（失败跳过 {errors['n']} 条）")
    log(f"回填 low_period：{stats['filled']} 个"
        f"（已有 prev 跳过 {stats['skipped_complete']}；推不出上一次 {stats['no_prev']}）")
    log(f"ITAD 请求 {client.calls} 次（限流事件 {client.rate_limit_events}）· "
        f"异常事件：{summarize_events(client.events)}")
    log(f"总耗时 {fmt_mmss(elapsed)}"
        + (f"（速率 {rate:.0f}/min）" if stats["fetched"] else ""))
    # 统计直接进运行 Summary（backfill.yml 只补脚本/参数/退出码，这里补统计/耗时）
    write_step_summary("backfill_low_period", [
        ("结果", "dry-run（不写盘）" if args.dry_run else
                 f"已填 {stats['filled']} / 目标 {stats['targets']}"),
        ("已查流水", f"{stats['fetched']}（失败跳过 {errors['n']}）"),
        ("已有 prev 跳过", stats["skipped_complete"]),
        ("推不出上一次", stats["no_prev"]),
        ("ITAD 请求", f"{client.calls} 次（限流事件 {client.rate_limit_events}）"),
        ("异常事件", summarize_events(client.events)),
        ("总耗时", fmt_mmss(elapsed) + (f"（{rate:.0f}/min）" if stats["fetched"] else "")),
    ])
    if args.dry_run:
        log("dry-run：不写盘。")
        return 0
    if stats["filled"]:
        # 回填留痕：data 分支的 run_log 里能查到「哪天回填了什么」
        # （mode=backfill 与 daily/prefetch 记录并存；about 页按缺省键渲染，互不干扰）
        state.add_run_log(
            {
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mode": "backfill",
                "script": "backfill_low_period",
                "itad_requests": client.calls,
                "steam_requests": 0,
                "targets": stats["targets"],
                "filled": stats["filled"],
                "fetch_errors": errors["n"],
                "event_kinds": event_kinds(client.events),
            },
            keep=int(cfg.get("run_log_keep", 30)),
        )
        state.save(datetime.now(timezone.utc))
        log(f"已写入：{state_path}")
    else:
        log("无需写入（没有新增）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
