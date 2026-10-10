#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封面回填（Steam GetItems ``library_capsule`` → game_meta.cover；2026-10-10）。

背景：封面单源从 ITAD boxart 换成 Steam 小封面（``include_assets=True`` 的
``assets.library_capsule``，300×450 竖版）—— 见 ``src/steam_browse``。
封面口径：**ITAD boxart 优先**（URL 可由 ``game_id`` 现拼，占绝大多数）；
只有 ITAD 无 boxart 的条目才用 Steam 小封面补 —— 存量这类条目要一次性补齐，
否则页面上仍是灰块。

口径（封面是 **ITAD 优先、Steam 补缺**，本脚本只跑「补缺」这一半）：

- **范围 = 能进列表**（:func:`classify.is_shown_meta`) **且 ITAD 无 boxart**
  （实测进列表里约 9.5%）：有 ITAD 封面的一律不碰，看不见的档位不花请求；
- **只补缺**：已有 ``cover`` 的条目跳过（幂等，重跑几乎零请求）；
- **appid 层去重**：ITAD uuid→appid 多对一，一个 appid 只查一次，
  命中后**同写所有指向它的 gid**；
- **批量**：GetItems 250/批（``steam_browse`` 的 URL 双闸自动再切），
  每批落盘一次，中断可续；
- **限速自守**：走 ``steam_browse`` 独立限流通道（默认 150/300s、0.5s 间隔），
  403/429 立即中止本轮并落盘已填部分 —— 宁慢勿封，绝不轮换 IP。

用法（Actions ``backfill.yml`` 同一入口，script 参数选本脚本）::

    python tools/backfill_steam_cover.py --state data/state.json --dry-run --limit 250
    python tools/backfill_steam_cover.py --state data/state.json
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.config import load_config, parse_rate_limit  # noqa: E402
from src.httpclient import Blocked, HttpError  # noqa: E402
from src.pipeline import log  # noqa: E402  统一输出出口（stdout + flush）
from src.ratelimit import RateLimiter  # noqa: E402
from src.state import State  # noqa: E402
from src.steam_browse import SteamBrowseClient  # noqa: E402
from tools.backfill_common import (  # noqa: E402
    event_kinds,
    fmt_mmss,
    summarize_events,
    write_step_summary,
)

#: 单批 appid 上限（与服务端硬上限一致；实际还会被 URL 双闸再切小）
DEFAULT_BATCH_SIZE = 250
#: 单轮墙钟预算（秒）：本任务几分钟量级，1800 足够且留足余量。
DEFAULT_TIME_BUDGET = 1800
#: 连续失败中止阈值：不是偶发抖动就停手（与 backfill_cn_names 同款语义）。
CONSECUTIVE_FAIL_LIMIT = 3

ABORT_BLOCKED = "blocked"
ABORT_CONSECUTIVE = "consecutive_failures"
ABORT_TIME_UP = "time_up"


def select_targets(state: State, cfg: dict) -> list[tuple[int, list[str]]]:
    """回填目标：``[(appid, [gid…]), …]`` —— **ITAD 无 boxart**、能进列表、还没有
    Steam 封面、按 appid 去重。

    判据的唯一出处 :func:`classify.is_shown_meta`（与史低期 / 中文名回填同款）。
    """
    seen_gids: set[str] = set()
    rows: dict[int, list[str]] = {}
    for entry in state.seen_deal.values():
        gid = entry.get("game_id")
        if not gid or gid in seen_gids:
            continue
        seen_gids.add(gid)
        if entry.get("boxart"):
            continue                                # ITAD 已有封面：不需要 Steam 补
        meta = state.game_meta.get(gid) or {}
        if not meta.get("appid"):
            continue                                # 没法请求，直接出局
        if meta.get("cover"):
            continue                                # 只补缺
        if not classify.is_shown_meta(state.meta(gid), cfg):
            continue
        rows.setdefault(int(meta["appid"]), []).append(gid)
    return [(appid, sorted(gids)) for appid, gids in sorted(rows.items())]


def backfill(state: State, client: SteamBrowseClient,
             targets: list[tuple[int, list[str]]], now: datetime, *,
             batch_size: int = DEFAULT_BATCH_SIZE, limit: int = 0,
             time_budget: float = 0.0, clock=time.monotonic,
             dry_run: bool = False, progress=None) -> dict:
    """按批查 GetItems、写 ``game_meta[gid].cover``（受控写口）。

    ``targets`` 由 :func:`select_targets` 给出（main 只调一次）。风控级中止
    （Blocked / 连续失败 / 时间到）通过 ``stats["aborted"]`` 报告而非异常 ——
    调用方拿到部分成果先落盘再退出。
    """
    stats = {"targets": len(targets), "batches": 0, "requested": 0,
             "filled": 0, "no_cover": 0, "errors": 0, "aborted": None}
    start = clock()
    consecutive_failures = 0
    done = 0

    for offset in range(0, len(targets), batch_size):
        if limit and done >= limit:
            break
        if time_budget and clock() - start > time_budget:
            stats["aborted"] = ABORT_TIME_UP
            break
        # --limit 精确到**条**（不是批）：剩余额度小于一批时只取那么多
        room = batch_size if not limit else min(batch_size, limit - done)
        chunk = targets[offset:offset + room]
        appids = [appid for appid, _ in chunk]
        try:
            metas = client.fetch(appids)
        except Blocked:
            stats["aborted"] = ABORT_BLOCKED        # 滥用封禁：绝不硬扛
            break
        except HttpError as exc:
            stats["errors"] += 1
            consecutive_failures += 1
            print(f"[warn] GetItems 批失败（{len(chunk)} 个 appid）：{exc}",
                  file=sys.stderr, flush=True)
            if consecutive_failures >= CONSECUTIVE_FAIL_LIMIT:
                stats["aborted"] = ABORT_CONSECUTIVE
                break
            continue
        consecutive_failures = 0
        stats["batches"] += 1
        stats["requested"] += len(appids)
        done += len(chunk)

        for appid, gids in chunk:
            meta = metas.get(appid)
            cover = meta.cover if meta else None
            if not cover:
                stats["no_cover"] += len(gids)      # 服务端没给该资产 → 保持灰块
                if dry_run:
                    log(f"[dry-run] appid={appid} 无 library_capsule → 跳过")
                continue
            if not dry_run:
                for gid in gids:                    # uuid 多对一：一次请求喂饱全组
                    state.set_meta_extras(gid, cover=cover)
            stats["filled"] += len(gids)
            if dry_run:
                log(f"[dry-run] appid={appid}（{len(gids)} 个 gid）→ {cover}")
        if not dry_run:
            state.save(now)                         # 断点续传：每批落盘
        if progress:
            progress(stats, clock() - start)

    stats["elapsed"] = clock() - start
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="封面回填（Steam GetItems library_capsule → game_meta.cover）")
    parser.add_argument("--state", default="data/state.json", help="state.json 路径")
    parser.add_argument("--config", default="config.json", help="配置文件")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"每批 appid 数（默认 {DEFAULT_BATCH_SIZE}）")
    parser.add_argument("--limit", type=int, default=0,
                        help="单轮最多查多少个 appid（默认 0 = 不限；精确到条，非批）")
    parser.add_argument("--time-budget", type=int, default=DEFAULT_TIME_BUDGET,
                        help=f"单轮墙钟预算秒数（默认 {DEFAULT_TIME_BUDGET}s，到点优雅收尾）")
    parser.add_argument("--dry-run", action="store_true", help="只查不写（探针用）")
    args = parser.parse_args()

    state_path = Path(args.state)
    if not state_path.is_file():
        print(f"state 文件不存在：{state_path}", file=sys.stderr, flush=True)
        return 2

    cfg = load_config(args.config)
    tz = classify.zone(cfg.get("timezone", "Asia/Shanghai"))
    now = datetime.now(timezone.utc)

    state = State(state_path, tz=tz)
    state.load()

    calls, window = parse_rate_limit(cfg.get("steam_browse_rate_limit") or "150 / 300s",
                                     default=(150, 300))
    limiter = RateLimiter(name="steam_browse", max_calls=calls, window_seconds=window,
                          min_interval=float(cfg.get("steam_browse_min_interval", 0.5)))
    client = SteamBrowseClient(
        limiter=limiter,
        timeout=float(cfg.get("request_timeout_seconds", 25)),
        pause=float(cfg.get("request_pause_seconds", 0.3)),
        log=lambda msg: print(msg, flush=True),
    )

    targets = select_targets(state, cfg)
    gid_total = sum(len(gids) for _, gids in targets)
    batches_est = (len(targets) + args.batch_size - 1) // args.batch_size
    log(f"回填目标（能进列表、ITAD 无 boxart、缺 Steam 封面）：{len(targets)} 个 appid"
        f"（覆盖 {gid_total} 个 gid）｜约 {batches_est} 批"
        f"｜预算 {args.time_budget}s" + ("｜DRY-RUN" if args.dry_run else ""))

    def progress(stats: dict, elapsed: float) -> None:
        rate = stats["requested"] / (elapsed / 60) if elapsed > 0 else 0.0
        log(f"进度 {stats['requested']}/{len(targets)} · "
            f"已填 {stats['filled']} · 无资产 {stats['no_cover']} · "
            f"失败 {stats['errors']} · 速率 {rate:.0f}/min · 已用 {fmt_mmss(elapsed)}")

    stats = backfill(state, client, targets, now, batch_size=args.batch_size,
                     limit=args.limit, time_budget=float(args.time_budget),
                     dry_run=args.dry_run, progress=progress)

    elapsed = stats["elapsed"]
    aborted_reason = {
        ABORT_BLOCKED: "Steam 风控封禁（403/429）",
        ABORT_CONSECUTIVE: f"连续 ≥{CONSECUTIVE_FAIL_LIMIT} 批失败",
        ABORT_TIME_UP: "墙钟预算用尽（正常收尾）",
    }.get(stats["aborted"])
    log(f"批次 {stats['batches']}｜请求 appid {stats['requested']}"
        f"｜已填 {stats['filled']}｜无资产 {stats['no_cover']}｜批失败 {stats['errors']}")
    log(f"GetItems 请求 {client.calls} 次（限流事件 {client.rate_limit_events}）· "
        f"异常事件：{summarize_events(client.events)}")
    log(f"总耗时 {fmt_mmss(elapsed)}")
    write_step_summary("backfill_steam_cover", [
        ("结果", ("dry-run（不写盘）" if args.dry_run else f"已填 {stats['filled']}")
                + (f"；中止：{aborted_reason}" if aborted_reason else "")),
        ("批次", f"{stats['batches']}（请求 appid {stats['requested']}）"),
        ("无封面资产", str(stats["no_cover"])),
        ("GetItems 请求", f"{client.calls} 次（限流事件 {client.rate_limit_events}）"),
        ("异常事件", summarize_events(client.events)),
        ("总耗时", fmt_mmss(elapsed)),
    ])
    if stats["aborted"]:
        print(f"[中止] {aborted_reason}；已填部分照常落盘。",
              file=sys.stderr, flush=True)

    if args.dry_run:
        log("dry-run：不写盘。")
        return 0
    if stats["filled"]:
        # 回填留痕：data 分支 run_log 里能查到「哪天回填了什么」
        state.add_run_log(
            {
                "run_at": now.isoformat(timespec="seconds"),
                "mode": "backfill",
                "script": "backfill_steam_cover",
                "itad_requests": 0,
                "steam_requests": client.calls,
                "requested": stats["requested"],
                "filled": stats["filled"],
                "no_cover": stats["no_cover"],
                "aborted": stats["aborted"],
                "fetch_errors": stats["errors"],
                "event_kinds": event_kinds(client.events),
            },
            keep=int(cfg.get("run_log_keep", 30)),
        )
        state.save(datetime.now(timezone.utc))
        log(f"已写入：{state_path}")
    else:
        log("无需写入（没有新增）。")

    if stats["aborted"] in (ABORT_BLOCKED, ABORT_CONSECUTIVE):
        print("      封禁/异常期间不再请求；请先排查（不要轮换 IP）。",
              file=sys.stderr, flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())