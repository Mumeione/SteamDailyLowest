# -*- coding: utf-8 -*-
"""tools/render_report.py 的池子口径。

预览工具的「全部折扣」池必须是 seen_deal 的**折扣期内子集**（近似线上「当日
ITAD deals」口径）—— 曾经用全量把过期已久的历史条目全灌进页面（回归形状与
实测数字见 docs/CHANGELOG.md 2026-10-10 条目）。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import render_report  # noqa: E402

TZ = render_report.classify.zone("Asia/Shanghai")
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=TZ)


def entry(gid, *, days_left=5):
    return {
        "game_id": gid,
        "start": "2026-10-02 01:00",
        "expiry": (NOW + timedelta(days=days_left)).isoformat(),
    }


class PoolEntriesTest(unittest.TestCase):
    def test_only_active_entries(self):
        """池子只收折扣期内的条目 —— 过期已久的历史留存不进预览页。"""
        pool = render_report.pool_entries(
            [entry("还挂着"), entry("早过期了", days_left=-30)], NOW)
        self.assertEqual([e["game_id"] for e in pool], ["还挂着"])

    def test_missing_expiry_dropped(self):
        """expiry 缺失（解析不出时间）的条目按「不在期内」丢掉 —— 不猜。"""
        pool = render_report.pool_entries([{"game_id": "无时间"}, entry("正常")], NOW)
        self.assertEqual([e["game_id"] for e in pool], ["正常"])


if __name__ == "__main__":
    unittest.main()
