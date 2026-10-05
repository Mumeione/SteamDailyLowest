#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S6 一次性迁移：game_meta 动态字段拆分 → dynamic.json + 存量 unlisted 收敛。

用法（**本地一次性跑好再推 data 分支**，Actions 不跑迁移，2026-10-05 用户裁决）：
    python tools/migrate_dynamic_split.py [--state data/state.json] [--dry-run]

做的事（幂等，可重跑）：

1. **拆分**：``State.load()`` 自带代码自迁移 —— game_meta 里混存的
   ``reviews`` / ``stats`` / ``fetched_at`` / ``detail_failed_at`` / ``detail_attempts``
   自动收编进 ``dynamic.json``（dynamic.json 已有的键不覆盖，权威来源）。
2. **存量 unlisted 收敛**（spec 决策 17）：对动态条目逐条重算 tier，
   不进列表（cold/other）的 → 打 ``unlisted{at, start}`` 标记（start 取该游戏在
   seen_deal 里最近一条折扣的开始时间）并删除动态条目 —— 省掉一轮无意义的
   30 天 TTL 等待，迁移完成后 dynamic.json 直接缩到「会进列表」的体量。
3. **落盘 + 对账报告**：stdout 打印前后条数，迁移前后各记一次数（spec §6 验收）。

重跑行为：第 1 步 game_meta 已无动态键 → 跳过；第 2 步动态条目已收敛 → 无 cold/other
可标 → 零改动。报告仍会打印，数字应与上次一致。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import classify  # noqa: E402
from src.config import load_config, resolve_path  # noqa: E402
from src.state import State  # noqa: E402


def latest_start(state: State, game_id: str) -> str | None:
    """该游戏在 seen_deal 里最近一条折扣的 start（unlisted 标记的冻结锚点）。"""
    best: tuple[str, str | None] | None = None   # (last_seen_at, start)
    for entry in state.seen_deal.values():
        if entry.get("game_id") != game_id:
            continue
        key = entry.get("last_seen_at") or ""
        if best is None or key > best[0]:
            best = (key, entry.get("start"))
    return best[1] if best else None


def converge_unlisted(state: State, cfg: dict, now: datetime, *,
                      apply: bool = True) -> dict:
    """存量 unlisted 收敛（spec 决策 17）。

    动态条目逐条重算 tier：不进列表（cold/other）的打 ``unlisted{at, start}``
    （start 取该游戏 seen_deal 最近一条折扣的开始时间）并删除动态条目 ——
    省掉一轮无意义的 TTL 等待，dynamic.json 直接缩到「会进列表」的体量。
    ``apply=False`` 只统计不落库（dry-run）。返回 ``{"marked", "kept"}``。
    """
    marked = kept = 0
    for gid in list(state.dynamic.entries):
        dyn = state.dynamic.entries.get(gid) or {}
        if classify.is_shown(classify.tier_of(dyn.get("reviews"), cfg)):
            kept += 1
            continue
        if apply:
            state.set_unlisted(gid, now, latest_start(state, gid))
            state.drop_dyn(gid)
        marked += 1
    return {"marked": marked, "kept": kept}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S6 一次性迁移：三层存储拆分 + 存量收敛")
    ap.add_argument("--state", default=None,
                    help="state.json 路径（默认取配置 state_path）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计不落盘（load 阶段自迁移新产生的 dynamic/cache 文件会被清掉）")
    args = ap.parse_args(argv)

    cfg = load_config()
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    state_path = Path(args.state) if args.state else resolve_path(cfg, "state_path")

    # dry-run 语义修复（review-s6 P0-2）：State.load() 的代码自迁移在 load 阶段
    # 就会写出 dynamic.json / cache.json（收编旧键时立即落盘）—— 记录 load 前
    # 已存在的文件，dry-run 时把「本次 load 新出现的」删掉，保证盘上无副作用。
    side_files = (state_path.with_name("dynamic.json"), state_path.with_name("cache.json"))
    pre_existing = {p for p in side_files if p.exists()}

    state = State(state_path, tz=tz).load()
    if args.dry_run:
        for p in side_files:
            if p.exists() and p not in pre_existing:
                p.unlink()

    meta_n = len(state.game_meta)
    dyn_before = len(state.dynamic.entries)
    legacy_left = sum(1 for e in state.game_meta.values()
                      if any(k in e for k in ("reviews", "stats", "fetched_at",
                                              "detail_failed_at", "detail_attempts")))
    print(f"[1] 拆分：game_meta {meta_n} 条；迁移后仍混存动态键的条目 {legacy_left} 条"
          f"（0 = 自迁移完成）")

    # ---- 存量 unlisted 收敛 ----
    listing = converge_unlisted(state, cfg, now, apply=not args.dry_run)
    marked, kept = listing["marked"], listing["kept"]

    dyn_after = len(state.dynamic.entries)
    unlisted_n = sum(1 for e in state.game_meta.values() if e.get("unlisted"))
    print(f"[2] 收敛：动态条目 {dyn_before} → {dyn_after} 条"
          f"；标记 unlisted {marked} 条（累计 {unlisted_n}）；达标保留 {kept} 条")
    print(f"[3] seen_deal {len(state.seen_deal)} 条 · game_meta {len(state.game_meta)} 条")

    if not args.dry_run:
        state.save(now)
        print(f"落盘完成：{state_path}（+ dynamic.json / cache.json）")
        print("下一步：核对数字后把三份文件一起提交 data 分支（本地跑好再上传，勿在 Actions 跑）。")
    else:
        print("dry-run：未落盘（load 自迁移新产生的 side 文件已清除，盘上无副作用）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
