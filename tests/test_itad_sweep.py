# -*- coding: utf-8 -*-
"""ITAD `/deals/v2` 抓取口径的单元测试（docs/DEVELOPMENT.md §3.2）。

不发任何真实请求，只钉住「服务端 filter 长什么样」——
这几个取值是第 24 轮用 148 次真实请求测出来的，改动它们必须有实测依据。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import itad  # noqa: E402


class SweepFilterTest(unittest.TestCase):
    def test_low_only_uses_type_and_flag(self):
        flt = itad.sweep_filter(itad.SWEEP_LOW_ONLY)
        self.assertEqual(flt, {"type": [1], "flag": "S"})

    def test_flag_must_be_scalar(self):
        """`flag` 传数组会被服务端静默忽略（不报错、不过滤），必须传单值。"""
        flt = itad.sweep_filter(itad.SWEEP_LOW_ONLY)
        self.assertIsInstance(flt["flag"], str)
        self.assertEqual(flt["flag"], itad.FLAG_ANY_LOW)
        # "S" 是层级里最宽松的一档，等价于「全部史低」（N ⊂ H ⊂ S）
        self.assertNotIn(flt["flag"], ("N", "H"))

    def test_full_sweep_has_no_filter(self):
        self.assertIsNone(itad.sweep_filter(itad.SWEEP_FULL))

    def test_unknown_sweep_raises(self):
        with self.assertRaises(ValueError):
            itad.sweep_filter("something-else")

    def test_low_only_cuts_page_count(self):
        """服务端过滤是这一轮的核心收益：162 页 → 27 页（实测值，见 summary24）。"""
        self.assertEqual(itad.SWEEP_MODES, ("low_only", "full"))


if __name__ == "__main__":
    unittest.main()
