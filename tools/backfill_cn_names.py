#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文名回填（小黑盒 → game_meta.title_zh；2026-10-09，方案见 .scratch/cn-name-backfill/）。

背景：``title_zh`` 现状只靠 Steam ``appdetails?l=schinese`` 逐条取 ——
无官方中文化的游戏 Steam 回退英文名，约 2.4 万条 ``title_zh`` 存的是英文。
本脚本从**小黑盒**（免 hkey 形态的 ``get_game_detail``）补**社区中文名**：

- **范围 = 能进列表**（``classify.is_shown_meta``，quality + notable，2026-10-09
  用户定案：看不见的档位不花请求；B/C 全量扩展出局）；
- **只补缺、不覆盖官方名**：``title_zh`` 含 CJK（哪怕单字《茧》）一律跳过，
  纯英文的才视为可补（覆盖写入，英文名在 deal ``title`` / 可由 appid 重查，无损）；
- **质量闸**：写入要求「含 ≥1 CJK 汉字且不含假名」(:func:`src.heybox.is_cn_name`)；
  繁体原样接受（不翻译不转换）；落空记负缓存（cache ``heybox_miss``，TTL 90 天）；
- **appid 层去重**：seen_deal 幂等键是单条折扣（同一游戏多条 deal）、ITAD
  uuid→appid 多对一（同一游戏多个 gid）—— 一个 appid 只发**一次**请求，
  命中后**同写所有指向它的 gid**；
- **限速自守**：默认 2s/req、单轮 ``--limit 1500``、墙钟 60min、
  风控信号（403/429/连续 ≥3 次请求失败）立即停止本轮并**落盘已填部分**退出 ——
  宁慢勿封，绝不轮换 IP；
- **防接口漂移**：按评价数降序先查热门，若前 10 条命中率仍为 0 → 判定响应
  形态漂移 / 静默风控，中止本轮（参数错必须当错误，不能当「没数据」）。

用法（本机试跑 / Actions ``backfill.yml`` 同一入口，script 参数选本脚本）::

    python tools/backfill_cn_names.py --state data/state.json --dry-run --limit 5   # 探针
    python tools/backfill_cn_names.py --state data/state.json                       # 正式一轮
    python tools/backfill_cn_names.py --limit 30                                    # 抽样
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
from src.heybox import HeyboxBlocked, HeyboxClient, has_cjk, is_cn_name  # noqa: E402
from src.httpclient import Blocked, HttpError  # noqa: E402
from src.pipeline import log  # noqa: E402  统一输出出口（stdout + flush）
from src.ratelimit import RateLimiter  # noqa: E402
from src.state import State  # noqa: E402
from tools.backfill_common import (  # noqa: E402
    event_kinds,
    fmt_mmss,
    summarize_events,
    write_step_summary,
)

#: 每轮默认限量（用户 2026-10-09 定案「限时限量、不图快不图量」）：
#: 2s/req × 1500 ≈ 50min，稳在墙钟预算与 Actions 90min 上限之内。
DEFAULT_LIMIT = 1500
#: 单轮墙钟预算（秒）：到点优雅收尾落盘退出，不管跑了多少（用户定案）。
DEFAULT_TIME_BUDGET = 3600
#: 请求间隔默认值：小黑盒无官方配额文档，宁慢勿封（探针全绿后可 --min-interval 1.5 试水）。
DEFAULT_MIN_INTERVAL = 2.0
#: 落空负缓存 TTL（天）：期内不重查已知落空；到期自动重查（小黑盒日后补了译名可自愈）。
DEFAULT_MISS_TTL_DAYS = 90
#: 连续失败中止阈值（用户定案「遇到问题直接停止」）——单次失败重试耗尽后跳过计数，
#: 连续 ≥3 次 = 不是偶发抖动，中止本轮。
CONSECUTIVE_FAIL_LIMIT = 3
#: 零命中守卫：先查的都是评价数最多的热门游戏，小黑盒几乎必有中文名；
#: 查了这么多还一个没中 → 响应形态漂移 / 静默风控，中止。
ZERO_HIT_GUARD = 10
#: 进度心跳间隔（请求数）：2s/req 正式跑约 50 分钟，此前只有 dry-run 才逐条打印、
#: 正式跑静默（backfill 日志审查 P1）。每 50 次请求报一次进度。
HEARTBEAT_EVERY = 50

#: 中止原因 → 统计键（Blocked / 连续失败 / 零命中 / 时间到）。前三个按风控处理。
ABORT_BLOCKED = "blocked"
ABORT_CONSECUTIVE = "consecutive_failures"
ABORT_ZERO_HIT = "zero_hit"
ABORT_TIME_UP = "time_up"


def select_targets(state: State, cfg: dict, now: datetime,
                   miss_ttl_days: int = DEFAULT_MISS_TTL_DAYS
                   ) -> list[tuple[int, list[str]]]:
    """回填目标：``[(appid, [gid…]), …]``，按评价数降序。

    - 判据唯一出处 :func:`classify.is_shown_meta`（与史低期回填同款——不把
      「没抓详情」当放行，冷门条目不花请求）；
    - 排序 = **评价数降序**（动态层 reviews.count）：热门游戏小黑盒译名命中率
      最高、报表收益最大，也为零命中守卫提供统计意义；
    - **appid 层去重**（两层都实测踩过）：seen_deal 幂等键是单条折扣（同一
      游戏多条 deal）、ITAD uuid→appid 多对一（同一游戏多个 gid）—— 同一
      appid 的所有 gid 聚成一组，一次请求命中后同写全组；
    - 过滤：缺 appid / ``title_zh`` 已含 CJK（含单字《茧》）/ 负缓存期内，
      都在这里直接出局。
    """
    seen_gids: set[str] = set()
    rows: dict[int, tuple[int, list[str]]] = {}     # appid -> (-评价数, [gid…])
    for entry in state.seen_deal.values():
        gid = entry.get("game_id")
        if not gid or gid in seen_gids:
            continue
        seen_gids.add(gid)
        if not classify.is_shown_meta(state.meta(gid), cfg):
            continue
        meta = state.game_meta.get(gid) or {}
        appid = meta.get("appid")
        if not appid:
            continue                                # 没法请求，直接出局
        appid = int(appid)
        if has_cjk((meta.get("title_zh") or "").strip()):
            continue                                # 已有中文名 → 只补缺，不覆盖
        missed_at = state.heybox_missed_at(gid)
        if missed_at is not None and now - missed_at < timedelta(days=miss_ttl_days):
            continue                                # 负缓存期内：已知落空，不重查
        reviews = (state.dyn(gid) or {}).get("reviews") or {}
        try:
            count = int(reviews.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if appid in rows:
            rows[appid][1].append(gid)              # 同 appid 的另一 uuid → 并组
        else:
            rows[appid] = (-count, [gid])
    # 评价数降序；平手按 appid 升序保证稳定
    ordered = sorted(rows.items(), key=lambda kv: (kv[1][0], kv[0]))
    return [(appid, sorted(gids)) for appid, (_, gids) in ordered]


def backfill(state: State, fetch_name, targets: list[tuple[int, list[str]]],
             now: datetime, limit: int = 0, time_budget: float = 0.0,
             clock=time.monotonic, dry_run: bool = False, *,
             heartbeat_every: int = HEARTBEAT_EVERY, progress=None) -> dict:
    """逐个查小黑盒、过质量闸、写 ``game_meta[gid].title_zh``（受控写口）。

    ``fetch_name(appid) -> str | None`` 由调用方注入（真跑是
    :meth:`HeyboxClient.fetch_name` 的偏函数，测试注入假函数）。
    ``targets`` 由 :func:`select_targets` 给出（main 只调一次 select，
    传入本函数 —— 修掉「打印一遍、内部再扫一遍」的双重全量扫描）。
    返回统计；**风控级中止**（Blocked 上抛 / 连续失败 / 零命中 / 时间到）通过
    ``stats["aborted"]`` 报告而非异常——调用方拿到部分成果先落盘再退出。

    命中/落空都**同写该 appid 名下的所有 gid**（uuid 多对一：一次请求喂饱全组）。
    ``dry_run``：照常发请求、照常统计，但**不写任何状态**（探针用）。

    **进度心跳**：每 ``heartbeat_every`` 次成功请求调一次
    ``progress(stats, elapsed_seconds)``（不传则静默）—— 正式跑约 50 分钟，
    没有心跳就无法区分「在跑 / 被风控 / 卡死」；统计末尾附带 ``elapsed``。
    """
    stats = {"targets": len(targets), "requested": 0, "filled": 0, "missed": 0,
             "errors": 0,
             "aborted": None}
    start = clock()
    consecutive_failures = 0

    for appid, gids in targets:
        if limit and stats["requested"] >= limit:
            break
        if time_budget and clock() - start > time_budget:
            stats["aborted"] = ABORT_TIME_UP        # 时间到：优雅收尾，不算风控
            break
        try:
            name = fetch_name(appid)
        except Blocked:
            stats["aborted"] = ABORT_BLOCKED        # 滥用封禁：绝不硬扛
            break
        except HttpError as exc:
            stats["errors"] += 1
            consecutive_failures += 1
            print(f"[warn] appid={appid} 小黑盒请求失败：{exc}",
                  file=sys.stderr, flush=True)
            if consecutive_failures >= CONSECUTIVE_FAIL_LIMIT:
                stats["aborted"] = ABORT_CONSECUTIVE
                break
            continue
        consecutive_failures = 0
        stats["requested"] += 1

        if is_cn_name(name):
            if not dry_run:
                for gid in gids:                    # uuid 多对一：一次请求喂饱全组
                    state.set_title_zh(gid, name.strip(), now)
            stats["filled"] += len(gids)
            if dry_run:
                log(f"[dry-run] appid={appid}（{len(gids)} 个 gid）→ {name}")
        else:
            if not dry_run:
                for gid in gids:
                    state.heybox_miss(gid, now)     # 落空负缓存：TTL 内不重查
            stats["missed"] += len(gids)
            if dry_run and name:
                log(f"[dry-run] appid={appid} 非中文名，不入库：{name!r}")

        if stats["requested"] >= ZERO_HIT_GUARD and stats["filled"] == 0:
            stats["aborted"] = ABORT_ZERO_HIT       # 热门游戏全落空 = 形态漂移/静默风控
            break
        if heartbeat_every and progress and stats["requested"] % heartbeat_every == 0:
            progress(stats, clock() - start)
    stats["elapsed"] = clock() - start
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="中文名回填（小黑盒 get_game_detail → game_meta.title_zh）")
    parser.add_argument("--state", default="data/state.json", help="state.json 路径（默认 data/state.json）")
    parser.add_argument("--config", default="config.json", help="配置文件（默认 config.json）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"单轮最多查多少个 appid（默认 {DEFAULT_LIMIT}；0=不限，靠墙钟预算兜底）")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL,
                        help=f"请求最小间隔秒数（默认 {DEFAULT_MIN_INTERVAL}s，宁慢勿封）")
    parser.add_argument("--time-budget", type=int, default=DEFAULT_TIME_BUDGET,
                        help=f"单轮墙钟预算秒数（默认 {DEFAULT_TIME_BUDGET}s，到点优雅收尾）")
    parser.add_argument("--miss-ttl-days", type=int, default=DEFAULT_MISS_TTL_DAYS,
                        help=f"落空负缓存天数（默认 {DEFAULT_MISS_TTL_DAYS}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只查不写（探针：--limit 5 --dry-run 看打印的名字对不对）")
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
    limiter = RateLimiter(name="heybox", max_calls=30, window_seconds=60.0,
                          min_interval=args.min_interval)
    client = HeyboxClient(limiter=limiter, log=lambda msg: print(msg, flush=True))

    # select 只跑一次，结果同时用于打印与回填（--miss-ttl-days 因此真正生效）
    targets = select_targets(state, cfg, now, args.miss_ttl_days)
    gid_total = sum(len(gids) for _, gids in targets)
    # 心跳分母 = 本轮实际会查的上限（limit 大于目标数时不能虚高）
    total_label = min(args.limit, len(targets)) if args.limit else len(targets)
    log(f"回填目标（能进列表、缺中文名、负缓存外）：{len(targets)} 个 appid"
        f"（覆盖 {gid_total} 个 gid）｜间隔 ≥{args.min_interval}s"
        f"｜limit={args.limit or '不限'}｜预算 {args.time_budget}s"
        + ("｜DRY-RUN" if args.dry_run else ""))

    def progress(stats: dict, elapsed: float) -> None:
        rate = stats["requested"] / (elapsed / 60) if elapsed > 0 else 0.0
        log(f"进度 {stats['requested']}/{total_label} · "
            f"已填 {stats['filled']} · 落空 {stats['missed']} · "
            f"失败 {stats['errors']} · 速率 {rate:.0f}/min · 已用 {fmt_mmss(elapsed)}")

    stats = backfill(state, client.fetch_name, targets, now,
                     limit=args.limit, time_budget=float(args.time_budget),
                     dry_run=args.dry_run, progress=progress)

    elapsed = stats["elapsed"]
    rate = stats["requested"] / (elapsed / 60) if elapsed > 0 else 0.0
    aborted_reason = {
        ABORT_BLOCKED: "小黑盒风控封禁（403/429）",
        ABORT_CONSECUTIVE: f"连续 ≥{CONSECUTIVE_FAIL_LIMIT} 次请求失败",
        ABORT_ZERO_HIT: f"前 {ZERO_HIT_GUARD} 条零命中（响应形态漂移或静默风控）",
        ABORT_TIME_UP: "墙钟预算用尽（正常收尾）",
    }.get(stats["aborted"])
    log(f"请求 {stats['requested']}｜命中 {stats['filled']}｜落空 {stats['missed']}"
        f"｜单条失败 {stats['errors']}")
    log(f"小黑盒请求 {client.calls} 次（限流事件 {client.rate_limit_events}）· "
        f"异常事件：{summarize_events(client.events)}")
    log(f"总耗时 {fmt_mmss(elapsed)}"
        + (f"（速率 {rate:.0f}/min）" if stats["requested"] else ""))
    # 统计直接进运行 Summary（backfill.yml 只补脚本/参数/退出码，这里补统计/耗时）
    write_step_summary("backfill_cn_names", [
        ("结果", ("dry-run（不写盘）" if args.dry_run else
                  f"已填 {stats['filled']} / 落空 {stats['missed']}")
                + (f"；中止：{aborted_reason}" if aborted_reason else "")),
        ("请求数", f"{stats['requested']}/{total_label}（单条失败 {stats['errors']}）"),
        ("小黑盒请求", f"{client.calls} 次（限流事件 {client.rate_limit_events}）"),
        ("异常事件", summarize_events(client.events)),
        ("总耗时", fmt_mmss(elapsed) + (f"（{rate:.0f}/min）" if stats["requested"] else "")),
    ])
    if stats["aborted"]:
        print(f"[中止] {aborted_reason}；已填部分照常落盘。",
              file=sys.stderr, flush=True)

    if args.dry_run:
        log("dry-run：不写盘。")
        return 0
    if stats["filled"] or stats["missed"]:
        # 落空负缓存也值得落盘（下次触发少烧一批请求）
        # 回填留痕：data 分支的 run_log 里能查到「哪天回填了什么」
        # （mode=backfill 与 daily/prefetch 记录并存；about 页按缺省键渲染，互不干扰）
        state.add_run_log(
            {
                "run_at": now.isoformat(timespec="seconds"),
                "mode": "backfill",
                "script": "backfill_cn_names",
                "itad_requests": 0,
                "steam_requests": 0,
                "requested": stats["requested"],
                "filled": stats["filled"],
                "missed": stats["missed"],
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

    if stats["aborted"] in (ABORT_BLOCKED, ABORT_CONSECUTIVE, ABORT_ZERO_HIT):
        print("      封禁/异常期间不再请求；请先排查（不要轮换 IP）。",
              file=sys.stderr, flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
