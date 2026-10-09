# -*- coding: utf-8 -*-
"""史低期判定的**单点**与**两条时间线的交叉**测试（架构检查卡片 02）。

背景：日常增量（``state.record_low_period``）与一次性回填
（``tools/backfill_low_period``）原先各写一套「时刻 t 是否开启一段史低期」的推导、
互不知情、也没有任何交叉测试。现在两者都走 ``classify`` 的共享纯函数，本文件锁：

1. ``roll_low_period`` —— 增量口径本身的规则；
2. ``low_period_starts`` / ``low_period_pair`` —— 完整流水口径的规则；
3. **两条路在同一段历史上给出同一个 ``(cur, prev)``** —— 正是从前缺的那种交叉；
4. ``State.set_low_period`` 受控写口（回填不再直写 game_meta）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.state import State  # noqa: E402
from tools.backfill_low_period import derive_low_period, low_period_starts  # noqa: E402

TZ = classify.zone("Asia/Shanghai")


def row(ts: str, amount_int: int) -> dict:
    """一条与 ITAD ``history/v2`` 同构的价格变更流水元素。"""
    return {"timestamp": ts, "shop": {"id": 61},
            "deal": {"price": {"amountInt": amount_int}}}


class RollLowPeriodTest(unittest.TestCase):
    """增量口径：新入账一个史低期开始 → 是否滚动 cur。"""

    def test_first_period_always_takes(self):
        self.assertEqual(classify.roll_low_period(None, "2026-09-17T12:00:00Z", TZ),
                         "2026-09-17T12:00:00Z")

    def test_same_start_does_not_move(self):
        """重复入账 / 折扣期被 Steam 延长（同 start）—— 不滚动，prev 不会被自己顶掉。"""
        self.assertIsNone(classify.roll_low_period(
            "2026-09-17T12:00:00Z", "2026-09-17T12:00:00Z", TZ))

    def test_older_start_ignored(self):
        """乱序写入只认更晚的开始时间（否则新史低会被老折扣挤掉）。"""
        self.assertIsNone(classify.roll_low_period(
            "2026-09-17T12:00:00Z", "2024-08-31T12:00:00Z", TZ))

    def test_newer_start_moves(self):
        self.assertEqual(
            classify.roll_low_period("2024-08-31T12:00:00Z", "2026-09-17T12:00:00Z", TZ),
            "2026-09-17T12:00:00Z")

    def test_missing_or_unparseable_ignored(self):
        for bad in (None, "", "not-a-time"):
            self.assertIsNone(classify.roll_low_period("2026-09-17T12:00:00Z", bad, TZ))

    def test_compares_real_instants_across_offsets(self):
        """带不同时区偏移的时间戳必须按**绝对时刻**比，不能按字符串字典序。

        这里刻意让两种口径给出相反结论：新值的字符串更小（"02:" < "09:"）却
        实际更晚（UTC 02:00 > UTC 01:00）—— 按字典序会误判成「乱序」而丢弃。
        """
        self.assertEqual(classify.roll_low_period(
            "2026-09-17T09:00:00+08:00", "2026-09-17T02:00:00+00:00", TZ),
            "2026-09-17T02:00:00+00:00")


class LowPeriodStartsTest(unittest.TestCase):
    """完整流水口径：价格 ≤ 此前历史最低的变更即一段史低期的开始。"""

    def test_new_and_tie_count_back_up_does_not(self):
        rows = [row("2026-01-01T00:00:00Z", 10000),
                row("2026-02-01T00:00:00Z", 5000),    # 新低 → 记
                row("2026-03-01T00:00:00Z", 10000),   # 回升 → 不记
                row("2026-04-01T00:00:00Z", 5000),    # 同价（平史低）→ 记
                row("2026-05-01T00:00:00Z", 4000)]    # 再创新低 → 记
        self.assertEqual(low_period_starts(rows),
                         ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
                          "2026-04-01T00:00:00Z", "2026-05-01T00:00:00Z"])

    def test_unordered_input_and_malformed_rows(self):
        rows = [row("2026-05-01T00:00:00Z", 4000),
                {"timestamp": "not-a-time", "deal": {"price": {"amountInt": 1}}},
                row("2026-02-01T00:00:00Z", 5000)]
        # 「not-a-time」既不参与排序也不产生史低期（宁缺勿猜）
        self.assertEqual(low_period_starts(rows),
                         ["2026-02-01T00:00:00Z", "2026-05-01T00:00:00Z"])

    def test_sorts_across_timezone_offsets_by_real_instant(self):
        """流水必须按**绝对时刻**排序：ITAD 的 timestamp 带混合偏移（实测 +02:00 等）。

        两种口径在这里结论相反 —— 字符串字典序会把 ``-05:00`` 那条当成更早，
        于是 400 先成为历史最低、500 那条就不算史低期开始（少记一段）。
        """
        rows = [row("2026-09-16T20:00:00-05:00", 400),   # 实为 09-17T01:00Z
                row("2026-09-17T00:30:00+00:00", 500)]   # 实为 09-17T00:30Z（更早）
        self.assertEqual(low_period_starts(rows),
                         ["2026-09-17T00:30:00+00:00", "2026-09-16T20:00:00-05:00"])


class LowPeriodPairTest(unittest.TestCase):
    def test_needs_two_periods(self):
        self.assertIsNone(classify.low_period_pair([], None, TZ))
        self.assertIsNone(classify.low_period_pair(["2026-09-17T12:00:00Z"], None, TZ))

    def test_memory_cur_same_day_keeps_memory_spelling(self):
        starts = ["2024-08-31T12:00:00Z", "2026-09-17T19:21:06Z"]
        self.assertEqual(classify.low_period_pair(starts, "2026-09-17T09:00:00Z", TZ),
                         ("2026-09-17T09:00:00Z", "2024-08-31T12:00:00Z"))

    def test_memory_cur_newer_than_history_keeps_it_as_cur(self):
        starts = ["2024-08-31T12:00:00Z", "2026-09-17T12:00:00Z"]
        self.assertEqual(classify.low_period_pair(starts, "2026-10-09T02:00:00Z", TZ),
                         ("2026-10-09T02:00:00Z", "2026-09-17T12:00:00Z"))


class IncrementalVsHistoryCrossTest(unittest.TestCase):
    """**交叉测试**：同一段历史上，增量路径与流水路径必须给出同一个 (cur, prev)。"""

    #: 一段价格历史：三个史低期开始（新低 / 同价 / 再创新低），中间夹一次回升
    HISTORY = [row("2024-08-31T12:00:00Z", 5000),   # 史低期 1
               row("2025-01-10T12:00:00Z", 9000),   # 回升 → 不是史低期
               row("2025-06-01T12:00:00Z", 5000),   # 平史低 → 史低期 2
               row("2026-09-17T12:00:00Z", 740)]    # 新史低 → 史低期 3

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()

    def test_history_path(self):
        self.assertEqual(derive_low_period(self.HISTORY, None, TZ),
                         ("2026-09-17T12:00:00Z", "2025-06-01T12:00:00Z"))

    def test_incremental_path(self):
        """日常只喂「史低期开始」（回升那一轮不会入账），逐条入账。"""
        for start in low_period_starts(self.HISTORY):
            self.state.record_low_period("g", start)
        self.assertEqual(self.state.game_meta["g"]["low_period"],
                         {"cur": "2026-09-17T12:00:00Z", "prev": "2025-06-01T12:00:00Z"})

    def test_two_paths_agree(self):
        for start in low_period_starts(self.HISTORY):
            self.state.record_low_period("g", start)
        incremental = self.state.game_meta["g"]["low_period"]
        history = derive_low_period(self.HISTORY, None, TZ)
        self.assertEqual((incremental["cur"], incremental["prev"]), history)

    def test_replay_is_idempotent(self):
        """重跑同一批（含折扣期延长导致的重复入账）不改变记忆。"""
        for _ in range(3):
            for start in low_period_starts(self.HISTORY):
                self.state.record_low_period("g", start)
        self.assertEqual(self.state.game_meta["g"]["low_period"]["prev"],
                         "2025-06-01T12:00:00Z")


class SetLowPeriodWritePortTest(unittest.TestCase):
    """受控写口（卡片 04）：回填把结果交给 State 落，不再直写 game_meta。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()

    def test_writes_pair(self):
        self.state.set_low_period("g", "2026-09-17T12:00:00Z", "2024-08-31T12:00:00Z")
        self.assertEqual(self.state.game_meta["g"]["low_period"],
                         {"cur": "2026-09-17T12:00:00Z", "prev": "2024-08-31T12:00:00Z"})
        self.assertEqual(self.state.prev_low_start("g"), "2024-08-31T12:00:00Z")

    def test_prev_can_be_none(self):
        self.state.set_low_period("g", "2026-09-17T12:00:00Z", None)
        self.assertIsNone(self.state.prev_low_start("g"))

    def test_missing_game_id_is_noop(self):
        self.state.set_low_period("", "2026-09-17T12:00:00Z", None)
        self.assertEqual(self.state.game_meta, {})

    def test_merges_into_existing_meta(self):
        self.state.set_title_zh("g", "某游戏")
        self.state.set_low_period("g", "2026-09-17T12:00:00Z", None)
        self.assertEqual(self.state.title_zh("g"), "某游戏")
        self.assertIn("low_period", self.state.game_meta["g"])


class RecordLowPeriodNoEmptyMetaTest(unittest.TestCase):
    """增量入账被忽略时**不留空 meta 条目**（从前会塞一个 {} 进 game_meta）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()

    def test_noop_does_not_create_entry(self):
        self.state.record_low_period("g", None)
        self.state.record_low_period(None, "2026-09-17T12:00:00Z")
        self.assertEqual(self.state.game_meta, {})


if __name__ == "__main__":
    unittest.main()
