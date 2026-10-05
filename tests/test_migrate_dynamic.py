# -*- coding: utf-8 -*-
"""S6 迁移脚本 ``tools/migrate_dynamic_split.py`` 的单元测试（review-s6 P1-4）。

覆盖：收敛逻辑（latest_start 锚点、cold/other 标记 + 动态条目丢弃、达标保留）、
幂等复跑、dry-run 只统计不落库、--dry-run 无盘上副作用（load 自迁移产物被清除）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from migrate_dynamic_split import converge_unlisted, latest_start, main  # noqa: E402

from src import classify  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 16, 0, tzinfo=TZ)

# tier_of 直接吃 reviews，不需要 min_review_count 之外的门槛（默认 100 / 0.7）
CFG = {"min_positive_ratio": 0.7, "min_review_count": 100,
       "notable_review_count": 10000}


def deal(game_id: str, start: str, last_seen: str) -> dict:
    return {"game_id": game_id, "title": f"G {game_id}", "price_int": 100,
            "regular_int": 1000, "cut": 90, "currency": "CNY", "flag": "N",
            "start": start, "expiry": "2026-10-20T00:00:00+08:00",
            "low_kind": "N", "first_seen_at": last_seen, "last_seen_at": last_seen}


class LatestStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_picks_most_recent_seen(self):
        # 两条必须是不同 deal_key（同 game_id 同价同 expiry 会被 record_seen 合并）；
        # last_seen_at 由 record_seen 的 now 参数写入，写在 deal 字典里会被覆盖
        self.state.record_seen(deal("g1", "2026-09-20T10:00:00+08:00", ""),
                               NOW - timedelta(days=9))
        d2 = deal("g1", "2026-10-01T10:00:00+08:00", "")
        d2["price_int"] = 200
        self.state.record_seen(d2, NOW)
        self.assertEqual(latest_start(self.state, "g1"), "2026-10-01T10:00:00+08:00")

    def test_missing_game_returns_none(self):
        self.assertIsNone(latest_start(self.state, "nope"))


class ConvergeUnlistedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()
        self.state.record_seen(deal("g-cold", "2026-09-20T10:00:00+08:00",
                                    "2026-10-04T12:00:00+08:00"), NOW)
        # 三档：cold（count<100）/ other（差评但高热度）/ good（达标）
        self.state.set_meta("g-cold", 100, {"score": 90, "count": 12}, NOW)
        self.state.set_meta("g-other", 101, {"score": 55, "count": 5000}, NOW)
        self.state.set_meta("g-good", 102, {"score": 85, "count": 5000}, NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def test_marks_cold_and_other_drops_dynamic(self):
        listing = converge_unlisted(self.state, CFG, NOW)
        self.assertEqual(listing, {"marked": 2, "kept": 1})
        self.assertIsNone(self.state.dyn("g-cold"))
        self.assertIsNone(self.state.dyn("g-other"))
        self.assertIsNotNone(self.state.dyn("g-good"))
        # 冻结锚点 = seen_deal 最近一条折扣的 start
        self.assertEqual(self.state.unlisted("g-cold")["start"],
                         "2026-09-20T10:00:00+08:00")

    def test_dry_run_counts_only(self):
        listing = converge_unlisted(self.state, CFG, NOW, apply=False)
        self.assertEqual(listing, {"marked": 2, "kept": 1})
        self.assertIsNone(self.state.unlisted("g-cold"))   # 未打标记
        self.assertIsNotNone(self.state.dyn("g-cold"))     # 动态数据未丢

    def test_idempotent_second_run(self):
        converge_unlisted(self.state, CFG, NOW)
        again = converge_unlisted(self.state, CFG, NOW)
        self.assertEqual(again, {"marked": 0, "kept": 1})
        self.assertEqual(sum(1 for e in self.state.game_meta.values()
                             if e.get("unlisted")), 2)


class DryRunNoSideEffectsTest(unittest.TestCase):
    def test_dry_run_cleans_load_artifacts(self):
        """--dry-run 不得有盘上副作用（review-s6 P0-2）：
        load 自迁移新产生的 dynamic.json 必须被清掉，state.json 原样。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            state_path = Path(tmp.name) / "state.json"
            legacy = {
                "version": 1, "updated_at": None, "seen_deal": {}, "run_log": [],
                "game_meta": {"uuid-old": {"appid": 1,
                                           "reviews": {"score": 90, "count": 12},
                                           "fetched_at": "2026-10-01T00:00:00+08:00"}},
            }
            state_path.write_text(json.dumps(legacy, ensure_ascii=False),
                                  encoding="utf-8")
            before = state_path.read_text(encoding="utf-8")
            main(["--state", str(state_path), "--dry-run"])
            self.assertFalse(state_path.with_name("dynamic.json").exists())
            self.assertFalse(state_path.with_name("cache.json").exists())
            self.assertEqual(state_path.read_text(encoding="utf-8"), before)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
