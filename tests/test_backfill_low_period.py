# -*- coding: utf-8 -*-
"""史低期回填（``tools/backfill_low_period.py``，2026-10-09 改用 ITAD history/v2）单测。

不发任何网络请求：``fetch`` 注入假函数。覆盖：

- :func:`low_period_starts` 从价格流水推史低期（新史低 ``<`` / 平史低 ``==`` /
  正常价不算 / 乱序输入 / 缺字段跳过）；
- :func:`derive_low_period` 的时间线合并（记忆 cur 与流水同日去重、记忆 cur 更新时
  prev 取流水最新那段）；
- :func:`select_targets` 只取「能进列表」（``classify.is_shown``）并去重；
- :func:`backfill` 只填空不覆盖（已有 prev 跳过 / 推不出上一次不写 / ``--limit`` 限量）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.state import State  # noqa: E402
from tools.backfill_low_period import (  # noqa: E402
    backfill,
    derive_low_period,
    low_period_starts,
    select_targets,
)

TZ = classify.zone("Asia/Shanghai")
CFG = {"min_review_count": 100, "notable_review_count": 10000, "min_positive_ratio": 0.7}


def row(ts: str, amount_int: int) -> dict:
    """一条与 history/v2 同构的价格变更流水元素。"""
    return {"timestamp": ts, "shop": {"id": 61},
            "deal": {"price": {"amountInt": amount_int}, "regular": {"amountInt": 14800}, "cut": 50}}


class LowPeriodStartsTest(unittest.TestCase):
    def test_new_and_tie_lows_count_others_do_not(self):
        rows = [row("2026-01-01T00:00:00Z", 10000),
                row("2026-02-01T00:00:00Z", 5000),    # 新低 → 记
                row("2026-03-01T00:00:00Z", 10000),   # 回升 → 不记
                row("2026-04-01T00:00:00Z", 5000),    # 同价（平史低）→ 记
                row("2026-05-01T00:00:00Z", 4000)]    # 再创新低 → 记
        self.assertEqual(low_period_starts(rows),
                         ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
                          "2026-04-01T00:00:00Z", "2026-05-01T00:00:00Z"])

    def test_sorts_unordered_and_skips_malformed(self):
        rows = [row("2026-05-01T00:00:00Z", 4000),
                row("2026-02-01T00:00:00Z", 5000),
                {"timestamp": "2026-03-01T00:00:00Z", "deal": None},    # 缺 price → 跳过
                {"deal": {"price": {"amountInt": 1}}}]                  # 缺 timestamp → 跳过
        self.assertEqual(low_period_starts(rows),
                         ["2026-02-01T00:00:00Z", "2026-05-01T00:00:00Z"])


class DeriveLowPeriodTest(unittest.TestCase):
    def test_two_periods(self):
        rows = [row("2024-08-31T12:00:00Z", 5000), row("2026-09-17T12:00:00Z", 740)]
        self.assertEqual(derive_low_period(rows, None, TZ),
                         ("2026-09-17T12:00:00Z", "2024-08-31T12:00:00Z"))

    def test_single_period_has_no_prev(self):
        self.assertIsNone(derive_low_period([row("2026-09-17T12:00:00Z", 740)], None, TZ))

    def test_memory_cur_same_day_is_deduped_keeping_memory_spelling(self):
        rows = [row("2024-08-31T12:00:00Z", 5000), row("2026-09-17T19:21:06Z", 740)]
        self.assertEqual(derive_low_period(rows, "2026-09-17T09:00:00Z", TZ),
                         ("2026-09-17T09:00:00Z", "2024-08-31T12:00:00Z"))

    def test_memory_cur_newer_than_history_takes_history_latest_as_prev(self):
        """日常刚入账的周期（cur=10-09）ITAD 还没记进流水，prev 必须取流水最新那段。"""
        rows = [row("2024-08-31T12:00:00Z", 5000), row("2026-09-17T12:00:00Z", 740)]
        self.assertEqual(derive_low_period(rows, "2026-10-09T02:00:00Z", TZ),
                         ("2026-10-09T02:00:00Z", "2026-09-17T12:00:00Z"))


class SelectTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def _game(self, gid, score, count):
        self.state.data["seen_deal"][gid] = {"game_id": gid}
        self.state.dynamic.set_entry(gid, {"reviews": {"score": score, "count": count}})

    def test_only_shown_tiers_and_dedup(self):
        self._game("q", 90, 500)       # quality
        self._game("n", 60, 20000)     # notable（高热度·口碑不一）
        self._game("c", 90, 50)        # cold（评价太少）→ 排除
        self._game("o", 60, 500)       # other（口碑低且不够热）→ 排除
        self.state.data["seen_deal"]["dup"] = {"game_id": "q"}   # 同 gid 第二条
        self.assertEqual(select_targets(self.state, CFG), ["n", "q"])


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ)
        self.state.data["seen_deal"]["g"] = {"game_id": "g"}
        self.state.dynamic.set_entry("g", {"reviews": {"score": 90, "count": 500}})

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _fetch(rows):
        return lambda gid: rows

    def test_fills_prev_from_history(self):
        rows = [row("2024-08-31T12:00:00Z", 5000), row("2026-09-17T12:00:00Z", 740)]
        stats = backfill(self.state, self._fetch(rows), CFG, TZ)
        self.assertEqual(stats["filled"], 1)
        self.assertEqual(self.state.game_meta["g"]["low_period"],
                         {"cur": "2026-09-17T12:00:00Z", "prev": "2024-08-31T12:00:00Z"})

    def test_skips_when_prev_present(self):
        # 用受控写口（game_meta 已是只读视图，卡片 04）
        self.state.set_low_period("g", "2026-09-17T12:00:00Z", "2024-01-01T12:00:00Z")
        rows = [row("2024-08-31T12:00:00Z", 5000), row("2026-09-17T12:00:00Z", 740)]
        stats = backfill(self.state, self._fetch(rows), CFG, TZ)
        self.assertEqual((stats["filled"], stats["skipped_complete"]), (0, 1))
        self.assertEqual(self.state.game_meta["g"]["low_period"]["prev"],
                         "2024-01-01T12:00:00Z")     # 不被覆盖

    def test_no_prev_when_single_period(self):
        stats = backfill(self.state, self._fetch([row("2026-09-17T12:00:00Z", 740)]), CFG, TZ)
        self.assertEqual((stats["filled"], stats["no_prev"]), (0, 1))
        self.assertNotIn("g", [g for g, m in self.state.game_meta.items()
                               if (m or {}).get("low_period")])

    def test_cold_games_are_not_fetched(self):
        self.state.data["seen_deal"]["cold"] = {"game_id": "cold"}
        self.state.dynamic.set_entry("cold", {"reviews": {"score": 90, "count": 5}})
        seen = []
        backfill(self.state, lambda gid: seen.append(gid) or [], CFG, TZ)
        self.assertEqual(seen, ["g"])                # 冷门条目一条都不查

    def test_limit_caps_fetches(self):
        for i in range(5):
            gid = f"g{i}"
            self.state.data["seen_deal"][gid] = {"game_id": gid}
            self.state.dynamic.set_entry(gid, {"reviews": {"score": 90, "count": 500}})
        seen = []

        def fetch(gid):
            seen.append(gid)
            return [row("2024-08-31T12:00:00Z", 5000), row("2026-09-17T12:00:00Z", 740)]

        backfill(self.state, fetch, CFG, TZ, limit=3)
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
