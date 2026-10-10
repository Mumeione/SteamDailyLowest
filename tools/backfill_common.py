#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填脚本共用的输出 / 留痕小工具（``tools/backfill_*.py`` 共用，2026-10-10）。

backfill.yml 的定位是「以后所有一次性回填都走这里」，于是两个回填脚本
（low_period / cn_names）各自长出来的输出助手在这里收敛成一份；
新回填脚本直接复用，别再抄第三遍（backfill 日志审查 Standards #1）。

``write_step_summary`` 与 ``run.py`` 的同名函数是同一套 S8 ③ 样板
（往 ``$GITHUB_STEP_SUMMARY`` 追加关键指标表，非 Actions 环境静默跳过）。
刻意**不**从 run.py import：tools 不 import 1400 行的 CLI 入口
（2026-10-09 渲染层抽离时定下的边界），宁可留这份十行内的薄封装。
"""

from __future__ import annotations

import os
from collections import Counter


def fmt_mmss(seconds: float) -> str:
    """秒数 → ``mm:ss``（心跳与收尾汇总共用）。"""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def event_kinds(events: list[dict]) -> dict[str, int]:
    """客户端 ``events`` 按类型计数（``run_log`` 的 ``event_kinds`` 字段用）。"""
    return dict(Counter(e.get("kind") for e in events))


def summarize_events(events: list[dict]) -> str:
    """把客户端 ``events`` 汇总成一行 ``429×2 · network×1``（空列表 → 无）。"""
    kinds = event_kinds(events)
    return " · ".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "无"


def write_step_summary(title: str, rows: list[tuple[str, object]]) -> None:
    """往 ``$GITHUB_STEP_SUMMARY`` 追加一张关键指标表（非 Actions 环境静默跳过）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n## {title}\n\n| 指标 | 值 |\n|---|---|\n")
            for key, val in rows:
                fh.write(f"| {key} | {val} |\n")
    except OSError as exc:
        print(f"[warn] 运行摘要写入失败：{exc}", flush=True)
