# -*- coding: utf-8 -*-
"""S3 回填脚本 ``tools/backfill_queue.py`` 的单元测试。

只测选择逻辑与覆盖率统计（纯函数，不发网络）；fetch_details 管线本体
已有 tests/test_detail_pipeline.py 覆盖。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from backfill_queue import coverage, select_targets  # noqa: E402

from src import classify  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 10, 4, 16, 0, tzinfo=TZ)
EXPIRY = "2026-10-10T10:00:00+08:00"


def deal(game_id: str, cut: int = 90) -> dict:
    return {
        "game_id": game_id,
        "title": f"Game {game_id}",
        "price_int": 1000,
        "regular_int": 10000,
        "cut": cut,
        "currency": "CNY",
        "flag": "N",
        "start": "2026-10-04T10:00:00+08:00",
        "expiry": EXPIRY,
        "store_low_int": 1000,
        "history_low_int": 1000,
        "history_low_1y_int": 1000,
        "boxart": "https://x/b.jpg",
        "low_kind": "N",
    }


class SelectTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rules(self):
        """孤儿 / 已有有效详情 / 冷却中 → 跳过；其余按清单顺序进目标。"""
        for gid in ("g-valid", "g-failed", "g-backlog"):
            self.state.record_seen(deal(gid), NOW)
        self.state.set_meta("g-valid", 111, {"score": 80, "count": 500}, NOW)
        self.state.set_detail_failed("g-failed", NOW)
        self.state.game_meta["g-cooldown-unknown"] = {}   # 不在 seen_deal → 孤儿
        entries = [
            deal("g-orphan"),        # 不在 seen_deal（留存清理吃掉）→ 跳过
            deal("g-valid"),         # 已有有效详情 → 跳过（幂等）
            deal("g-failed"),        # 3 天冷却内失败 → 跳过
            deal("g-backlog"),       # 欠账 → 进目标
            {"title": "no gid"},     # 缺 game_id → 忽略
        ]
        targets, stats = select_targets(entries, self.state, NOW, 7, 3)
        self.assertEqual([e["game_id"] for e in targets], ["g-backlog"])
        self.assertEqual(stats, {"orphan_skipped": 1, "valid_skipped": 1,
                                 "cooldown_skipped": 1})

    def test_idempotent_after_full_fill(self):
        """全部补完后重跑：selected=0（清单消化完，零请求）。"""
        for gid in ("g-a", "g-b"):
            self.state.record_seen(deal(gid), NOW)
            self.state.set_meta(gid, 100, {"score": 80, "count": 500}, NOW)
        targets, stats = select_targets([deal("g-a"), deal("g-b")],
                                        self.state, NOW, 7, 3)
        self.assertEqual(targets, [])
        self.assertEqual(stats["valid_skipped"], 2)

    def test_cooldown_expires_allows_retry(self):
        """冷却（empty_ttl 天）过后，失败过的条目重新进目标。"""
        self.state.record_seen(deal("g-failed"), NOW)
        self.state.set_detail_failed("g-failed",
                                     datetime(2026, 10, 2, 16, 0, tzinfo=TZ))
        later = datetime(2026, 10, 6, 17, 0, tzinfo=TZ)
        targets, _ = select_targets([deal("g-failed")], self.state, later, 7, 3)
        self.assertEqual([e["game_id"] for e in targets], ["g-failed"])


class CoverageTest(unittest.TestCase):
    def test_counts_distinct_game_ids(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            state = State(Path(tmp.name) / "state.json", tz=TZ).load()
            state.record_seen(deal("g-a"), NOW)
            state.record_seen(deal("g-b"), NOW)
            dup = deal("g-a")
            dup["price_int"] = 900   # 同 game_id 的另一折扣版本
            state.record_seen(dup, NOW)
            state.set_meta("g-a", 100, {"score": 80, "count": 500}, NOW)
            state.set_appid("g-b", 101)
            gids, with_appid, with_reviews = coverage(state)
            self.assertEqual(gids, 2)
            self.assertEqual(with_appid, 2)
            self.assertEqual(with_reviews, 1)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
