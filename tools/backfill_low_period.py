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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.config import load_config  # noqa: E402
from src.httpclient import HttpError  # noqa: E402
from src.itad import build_client  # noqa: E402
from src.state import State  # noqa: E402

#: ``since`` 默认回溯天数：实测上一次史低期常在 1~2 年前（74~769 天）。
DEFAULT_SINCE_DAYS = 800


def low_period_starts(rows: list[dict]) -> list[str]:
    """从 ``history/v2`` 价格流水推「史低期开始」的时刻（升序）。

    按时间升序走一遍，价格 **≤ 此前历史最低** 的变更即一段史低期的开始
    （``<`` 是新史低、``==`` 是平史低，两者都算「在史低」）。``deal`` / ``price``
    缺失的行跳过。
    """
    points: list[tuple[str, int]] = []
    for row in rows or []:
        ts = row.get("timestamp")
        price = ((row.get("deal") or {}).get("price") or {}).get("amountInt")
        if ts and price is not None:
            points.append((ts, price))
    points.sort(key=lambda p: p[0])
    starts: list[str] = []
    seen_min: int | None = None
    for ts, price in points:
        if seen_min is None or price <= seen_min:
            starts.append(ts)
            seen_min = price
    return starts


def _parse(ts: str | None, tz):
    return classify.parse_time(ts, tz) if ts else None


def derive_low_period(rows: list[dict], existing_cur: str | None, tz) -> tuple[str, str] | None:
    """推 ``(cur, prev)``；没有「上一次史低期」时返回 ``None``。

    时间线 = 流水里的史低期 ＋ 记忆里已有的 ``cur``（**同日去重，保留记忆的拼写**）。
    ``cur`` = 时间线最后一段、``prev`` = 它前一段。这样记忆里的 cur 比流水还新时
    （日常刚入账的周期 ITAD 还没记），prev 自然取流水里最新那段 —— 正是「上一次」。
    """
    starts = low_period_starts(rows)
    cur_at = _parse(existing_cur, tz)
    if cur_at is not None:
        same_day = [t for t in starts if (_parse(t, tz) or cur_at).date() == cur_at.date()]
        if same_day:
            starts = [existing_cur if t in same_day else t for t in starts]
        else:
            starts.append(existing_cur)
    timeline = sorted(
        ((_parse(t, tz), t) for t in starts),
        key=lambda p: p[0] or datetime.min.replace(tzinfo=timezone.utc),
    )
    timeline = [(d, t) for d, t in timeline if d is not None]
    if len(timeline) < 2:
        return None
    return timeline[-1][1], timeline[-2][1]


def select_targets(state: State, cfg: dict) -> list[str]:
    """回填目标：seen_deal 里**能进列表**的史低条目（按 game_id 去重、稳定排序）。

    判据复用唯一出处 :func:`classify.is_shown`。⚠️ 本脚本按 ``tier_of`` 判档，而
    ``tier_of`` 只给 quality/notable/cold/other（``pending`` 是**抓取状态**，不由
    它给）—— 所以实际选中的是 quality/notable；「详情待补」的条目等详情到位、
    下一轮再补（脚本可重跑，自愈）。
    """
    gids: set[str] = set()
    for entry in state.seen_deal.values():
        gid = entry.get("game_id")
        if not gid:
            continue
        reviews = (state.meta(gid) or {}).get("reviews")
        if classify.is_shown(classify.tier_of(reviews, cfg)):
            gids.add(gid)
    return sorted(gids)


def backfill(state: State, fetch, cfg: dict, tz, limit: int = 0) -> dict[str, int]:
    """逐个拉历史、推史低期、写 ``game_meta[gid].low_period``（不落盘）。

    ``fetch(game_id) -> list[dict]`` 由调用方注入（真跑是
    :meth:`ItadClient.fetch_price_history` 的偏函数，测试注入假函数）。
    返回统计。只填空：已有 ``prev`` 整条跳过。
    """
    targets = select_targets(state, cfg)
    stats = {"targets": len(targets), "fetched": 0,
             "filled": 0, "skipped_complete": 0, "no_prev": 0}
    for gid in targets:
        if limit and stats["fetched"] >= limit:
            break
        meta = state.game_meta.get(gid) or {}
        existing = meta.get("low_period") or {}
        if existing.get("prev"):
            stats["skipped_complete"] += 1        # 已有上一次史低期，整条不覆盖
            continue
        resolved = derive_low_period(fetch(gid), existing.get("cur"), tz)
        stats["fetched"] += 1
        if resolved is None:
            stats["no_prev"] += 1                 # 推不出上一次（该游戏首次史低）
            continue
        cur_ts, prev_ts = resolved
        meta["low_period"] = {"cur": cur_ts, "prev": prev_ts}
        state.game_meta[gid] = meta
        stats["filled"] += 1
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
        print(f"state 文件不存在：{state_path}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    tz = classify.zone(cfg.get("timezone", "Asia/Shanghai"))
    country = args.country or cfg["country"]

    state = State(state_path, tz=tz)
    state.load()
    client = build_client(cfg, log=lambda msg: print(msg, flush=True))
    # since 按 **UTC** 组串（ITAD 认 UTC；早先用本地时区拼 'Z' 会差一个时区）
    since = (datetime.now(timezone.utc)
             - timedelta(days=args.since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    targets = len(select_targets(state, cfg))

    def fetch(game_id: str) -> list[dict]:
        try:
            return client.fetch_price_history(game_id, country, since=since)
        except HttpError as exc:                  # 单条失败不阻断整轮（含 ItadError 子类）
            print(f"[warn] {game_id} 价格流水抓取失败，跳过：{exc}", file=sys.stderr)
            return []

    print(f"回填目标（能进列表）：{targets} 个｜since={since}｜国家={country}"
          f"｜限流={cfg['itad_rate_limit']}@≥{cfg['itad_min_interval']}s"
          + (f"｜limit={args.limit}" if args.limit else ""))
    stats = backfill(state, fetch, cfg, tz, limit=args.limit)
    print(f"抓取 {stats['fetched']} 个游戏的价格流水")
    print(f"回填 low_period：{stats['filled']} 个"
          f"（已有 prev 跳过 {stats['skipped_complete']}；推不出上一次 {stats['no_prev']}）")
    if args.dry_run:
        print("dry-run：不写盘。")
        return 0
    if stats["filled"]:
        state.save(datetime.now(timezone.utc))
        print(f"已写入：{state_path}")
    else:
        print("无需写入（没有新增）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
