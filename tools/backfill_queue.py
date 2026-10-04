#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3 一次性回填：把秋促欠账 uuid 清单批量补进 ``game_meta``（重构 spec §5 S3）。

用法（GitHub Actions ``backfill.yml`` 手动触发；本机也可跑）：
    python tools/backfill_queue.py \
        --state data/state.json \
        --uuids .scratch/backfill-autumn-2026/uuids.jsonl \
        [--limit 500]

选择规则（与 ``run.detail_targets`` 派生式欠账**同口径**）：
- game_id 已不在 ``seen_deal``（被留存清理吃掉了）→ 跳过：孤儿 meta 没有消费方，不制造
- 已有有效详情（``meta_valid``）→ 跳过：**幂等**，重跑零请求
- ``detail_recently_failed``（3 天冷却内失败）→ 跳过：坏 uuid 不反复重试
其余进 :func:`run.fetch_details` 批量管线（lookup → GetItems → info/v2 降级，
请求量 ≈ 清单数/250 + 1 次 lookup）。

跑完后 ``uuids.jsonl`` 的历史使命即完成，可从仓库删除。
报告追加到 ``$GITHUB_STEP_SUMMARY``（存在时），stdout 同步打印。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import build_client, build_steam_browse_client, fetch_details, log  # noqa: E402
from src import classify  # noqa: E402
from src.config import api_key, load_config  # noqa: E402
from src.itad import ItadClient  # noqa: E402
from src.state import State  # noqa: E402
from src.steam_browse import SteamBrowseClient  # noqa: E402


def select_targets(entries: list[dict], state: State, now: datetime,
                   ttl: int, empty_ttl: int) -> tuple[list[dict], dict]:
    """从清单里挑出本轮要回填的条目（规则见模块 docstring），保持清单顺序。"""
    gids = {e.get("game_id") for e in state.seen_deal.values()}
    targets: list[dict] = []
    stats = {"orphan_skipped": 0, "valid_skipped": 0, "cooldown_skipped": 0}
    for entry in entries:
        gid = entry.get("game_id")
        if not gid:
            continue
        if gid not in gids:
            stats["orphan_skipped"] += 1
        elif state.meta_valid(gid, now, ttl, empty_ttl):
            stats["valid_skipped"] += 1
        elif state.detail_recently_failed(gid, now, empty_ttl):
            stats["cooldown_skipped"] += 1
        else:
            targets.append(entry)
    return targets, stats


def coverage(state: State) -> tuple[int, int, int]:
    """(seen_deal 去重 game_id 数, 其中已有 appid 的, 其中已有 reviews 的)——验收指标。"""
    gids = {e.get("game_id") for e in state.seen_deal.values() if e.get("game_id")}
    with_appid = sum(1 for g in gids if state.has_appid(g))
    with_reviews = sum(1 for g in gids
                       if (state.meta(g) or {}).get("reviews"))
    return len(gids), with_appid, with_reviews


def write_summary(text: str) -> None:
    """追加到 $GITHUB_STEP_SUMMARY（存在时；stdout 已打印过同样内容）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S3 一次性回填（批量详情管线）")
    ap.add_argument("--state", default="data/state.json")
    ap.add_argument("--uuids", default=".scratch/backfill-autumn-2026/uuids.jsonl")
    ap.add_argument("--limit", type=int, default=0,
                    help="本次最多处理的条数（0 = 全部；首跑可取 250 试跑）")
    ap.add_argument("--fallback-budget", type=int, default=0,
                    help="本轮 info/v2 降级上限（0 = 用配置默认 1000；收尾轮可调大一次跑完）")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.fallback_budget > 0:
        cfg["detail_fallback_budget"] = args.fallback_budget
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    ttl = int(cfg.get("reviews_ttl_days", 7))
    empty_ttl = int(cfg.get("reviews_empty_ttl_days", 3))

    state = State(Path(args.state), tz=tz).load()
    entries = [json.loads(line)
               for line in Path(args.uuids).read_text(encoding="utf-8").splitlines()
               if line.strip()]
    gids_before, appid_before, reviews_before = coverage(state)

    targets, stats = select_targets(entries, state, now, ttl, empty_ttl)
    remaining = 0
    if args.limit > 0 and len(targets) > args.limit:
        remaining = len(targets) - args.limit
        targets = targets[:args.limit]
    log(f"回填目标 {len(targets)} 个（清单 {len(entries)} 条：孤儿跳过 "
        f"{stats['orphan_skipped']} · 已有有效详情 {stats['valid_skipped']} · "
        f"冷却中 {stats['cooldown_skipped']}"
        + (f" · 本轮截断 {remaining}" if remaining else "") + "）")

    fetched = {"fetched": 0, "fallback_fetched": 0, "fallback_skipped": 0}
    itad_calls = browse_calls = 0
    if targets:
        client: ItadClient = build_client(cfg)
        browse: SteamBrowseClient = build_steam_browse_client(cfg)
        fetched = fetch_details(client, state, targets, cfg, now, browse=browse)
        itad_calls, browse_calls = client.calls, browse.calls
        log(f"ITAD 请求 {itad_calls} 次 · GetItems 请求 {browse_calls} 次")
    else:
        log("没有需要回填的条目（清单已消化完），零请求")
    state.save(now)

    gids_after, appid_after, reviews_after = coverage(state)
    still_missing = sum(1 for e in targets
                        if not state.meta_valid(e.get("game_id"), now, ttl, empty_ttl))
    report = "\n".join([
        "## S3 欠账回填报告",
        f"- 清单 {len(entries)} 条 → 本轮处理 **{len(targets)}** 条"
        + (f"（截断 {remaining}）" if remaining else ""),
        f"- 新抓详情 **{fetched['fetched']}**（GetItems 两跳 "
        f"{fetched['fetched'] - fetched['fallback_fetched']} + info/v2 降级 "
        f"{fetched['fallback_fetched']}，降级预算截断 {fetched['fallback_skipped']}）",
        f"- 跳过：孤儿 {stats['orphan_skipped']} · 已有有效详情 "
        f"{stats['valid_skipped']} · 冷却中 {stats['cooldown_skipped']}",
        f"- game_meta 覆盖率（seen_deal 去重 {gids_after} 个 game_id）："
        f"appid {appid_before}→**{appid_after}** · reviews {reviews_before}→**{reviews_after}**",
        f"- ITAD 请求 {itad_calls} · GetItems 请求 {browse_calls}",
        f"- 本轮处理后仍无有效详情 **{still_missing}** 条"
        + ("（再跑一次本 workflow 继续消化）" if still_missing or remaining else "（清单已清空 ✅）"),
        "",
    ])
    print(report)
    write_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
