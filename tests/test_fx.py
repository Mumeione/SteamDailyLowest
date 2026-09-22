# -*- coding: utf-8 -*-
"""汇率换算的单元测试（docs/DEVELOPMENT.md §2.3）。

只测纯函数 `to_base_minor` —— 它的方向（除以 rate）最容易写反，
而且写反了不会报错、只会静默给出荒唐的比价结论。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import fx  # noqa: E402

#: 2026-09-21 实测值（open.er-api.com，基准 CNY）
REAL = {"base": "CNY", "date": "2026-09-21",
        "rates": {"UAH": 6.67391, "INR": 14.303287, "USD": 0.149149}}


class ToBaseMinorTest(unittest.TestCase):
    def test_ua_hryvnia_converted(self):
        """UA 45₴ = 4500 戈比 → 4500 / 6.67391 ≈ 674 分 = ¥6.74。"""
        self.assertEqual(fx.to_base_minor(4500, "UAH", REAL), 674)

    def test_inr_paise_converted(self):
        """IN ₹149 = 14900 paise → 14900 / 14.303287 ≈ 1042 分 = ¥10.42。"""
        self.assertEqual(fx.to_base_minor(14900, "INR", REAL), 1042)

    def test_base_currency_passthrough(self):
        self.assertEqual(fx.to_base_minor(1490, "CNY", REAL), 1490)

    def test_unknown_currency_returns_none_instead_of_guessing(self):
        """缺少汇率时如实返回 None，不猜、不按 1:1 顶替（§10 的口径）。"""
        self.assertIsNone(fx.to_base_minor(1000, "XYZ", REAL))

    def test_missing_input_returns_none(self):
        self.assertIsNone(fx.to_base_minor(None, "UAH", REAL))
        self.assertIsNone(fx.to_base_minor(1000, None, REAL))

    def test_no_rates_at_all(self):
        self.assertIsNone(fx.to_base_minor(1000, "UAH", None))

    def test_direction_is_divide_not_multiply(self):
        """回归：写反方向会得到 30000 分（¥300）这种荒唐值。"""
        result = fx.to_base_minor(4500, "UAH", REAL)
        self.assertLess(result, 10000)


class FxDisplayTest(unittest.TestCase):
    def test_display_only_keeps_wanted_currencies(self):
        from src import report

        shown = report.fx_display({"compare_countries": ["UA", "IN"]}, REAL)
        codes = [item["code"] for item in shown["rates"]]
        self.assertEqual(codes, ["USD", "UAH", "INR"])   # 去重 + 稳定顺序
        self.assertEqual(shown["date"], "2026-09-21")
        self.assertEqual(shown["base"], "CNY")

    def test_display_none_when_no_fx(self):
        from src import report

        self.assertIsNone(report.fx_display({"compare_countries": ["UA"]}, None))


if __name__ == "__main__":
    unittest.main()
