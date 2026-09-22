# -*- coding: utf-8 -*-
"""报表展示层的单元测试（§3.5 / §7.2 / §7.4 / §7.1）。

重点钉住两件用户明确要求的事：

1. **分组标签是中性描述 + 条件写在旁边**（「优质」这种评价性词会误导 ——
   70% 好评率本来就不等于优质），而且条件文案要**跟着配置变**；
2. **页面必须注明筛选条件**（`conditions()` 的输出要包含真实阈值）。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify, report  # noqa: E402

CFG = {
    "min_positive_ratio": 0.7,
    "min_review_count": 100,
    "notable_review_count": 10000,
    "compare_countries": ["UA", "IN"],
    "absolute_min_positive_ratio": None,
    "page_size_mobile": 10,
    "page_size_desktop": 20,
    "mobile_breakpoint_px": 768,
    "stale_banner_hours": 36,
    "output_dir": "output",
    "sweep_mode": "low_only",
}


class GroupSpecsTest(unittest.TestCase):
    def test_labels_are_neutral_not_evaluative(self):
        specs = {s["key"]: s for s in report.group_specs(CFG)}
        labels = [s["label"] for s in report.group_specs(CFG)]
        self.assertNotIn("优质", labels)
        self.assertNotIn("热门 · 褒贬不一", labels)
        self.assertEqual(specs[classify.TIER_QUALITY]["label"], "好评达标")
        self.assertEqual(specs[classify.TIER_NOTABLE]["label"], "高热度 · 口碑不一")

    def test_tier_labels_single_source(self):
        """分组标题与卡片标签必须来自同一个地方，否则会出现两者不一致。"""
        labels = report.tier_labels(CFG)
        self.assertEqual(labels[classify.TIER_QUALITY], "好评达标")
        self.assertEqual(labels[classify.TIER_NOTABLE], "高热度 · 口碑不一")
        for value in classify.TIER_LABELS.values():
            self.assertNotIn("优质", value)
            self.assertNotIn("褒贬不一」", value)
        # 不传 cfg 时退回 classify 的静态表，值也必须是中性词
        self.assertEqual(report.tier_labels()[classify.TIER_QUALITY], "好评达标")

    def test_criteria_come_from_config(self):
        specs = {s["key"]: s for s in report.group_specs(CFG)}
        self.assertEqual(specs[classify.TIER_QUALITY]["criteria"],
                         "好评率 ≥ 70% 且 评价数 ≥ 100")
        self.assertIn("10,000", specs[classify.TIER_NOTABLE]["criteria"])

    def test_criteria_follow_threshold_changes(self):
        """改阈值时文案必须跟着变，否则页面会撒谎。"""
        cfg = dict(CFG, min_positive_ratio=0.8, min_review_count=250,
                   notable_review_count=50000)
        specs = {s["key"]: s for s in report.group_specs(cfg)}
        self.assertEqual(specs[classify.TIER_QUALITY]["criteria"],
                         "好评率 ≥ 80% 且 评价数 ≥ 250")
        self.assertIn("50,000", specs[classify.TIER_NOTABLE]["criteria"])

    def test_group_collapsed_defaults(self):
        specs = {s["key"]: s for s in report.group_specs(CFG)}
        self.assertFalse(specs[classify.TIER_QUALITY]["collapsed"])   # 主列表展开
        self.assertTrue(specs[classify.TIER_NOTABLE]["collapsed"])
        self.assertTrue(specs[classify.TIER_PENDING]["collapsed"])

    def test_build_groups_carries_criteria_and_skips_empty(self):
        items = [
            {"tier": classify.TIER_QUALITY, "cut": 90, "title": "A"},
            {"tier": classify.TIER_COLD, "cut": 10, "title": "B"},
        ]
        groups = report.build_groups(items, CFG)
        self.assertEqual([g["key"] for g in groups], [classify.TIER_QUALITY])
        self.assertEqual(groups[0]["count"], 1)
        self.assertEqual(groups[0]["criteria"], "好评率 ≥ 70% 且 评价数 ≥ 100")


class ConditionsTest(unittest.TestCase):
    stats = {"sweep": "low_only", "new_today_raw": 105, "new_today_shown": 20,
             "detail_fetched": 300, "detail_backlog": 1800}

    def test_states_real_thresholds(self):
        text = "\n".join(report.conditions(CFG, self.stats))
        self.assertIn("好评率 ≥ 70%", text)
        self.assertIn("评价数 ≥ 100", text)
        self.assertIn("10,000", text)
        self.assertIn("冷门", text)

    def test_states_sweep_and_backlog(self):
        text = "\n".join(report.conditions(CFG, self.stats))
        self.assertIn("low_only", text)
        self.assertIn("105", text)
        self.assertIn("1800", text)      # 详情还差多少条要写出来

    def test_absolute_floor_only_when_enabled(self):
        self.assertNotIn("绝对下限", "\n".join(report.conditions(CFG, self.stats)))
        cfg = dict(CFG, absolute_min_positive_ratio=0.4)
        self.assertIn("好评率低于 40%", "\n".join(report.conditions(cfg, self.stats)))

    def test_fx_line_mentions_date_and_rates(self):
        fx = {"base": "CNY", "date": "2026-09-21",
              "rates": {"UAH": 6.67391, "INR": 14.303287, "USD": 0.149149}}
        text = "\n".join(report.conditions(CFG, self.stats, fx))
        self.assertIn("2026-09-21", text)
        self.assertIn("UAH", text)
        self.assertIn("仅供比价参考", text)
        self.assertIn("以 Steam 实际结算为准", text)

    def test_no_markdown_markup(self):
        """这些字符串直接进 HTML，不能带 markdown 标记。"""
        text = "\n".join(report.conditions(CFG, self.stats))
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)

    def test_no_hardcoded_page_counts(self):
        """页数会随销售节奏变（实测 20~27 页），写死在文案里迟早是错的。"""
        text = "\n".join(report.conditions(CFG, self.stats))
        self.assertNotIn("27 页", text)
        self.assertNotIn("162 页", text)

    def test_title_zh_is_stripped(self):
        """Steam 偶尔把本地化标题存成带尾随空格（例："时之刃 "）。"""
        entry = {"game_id": "u", "title": "Lysfanga", "title_zh": "时之刃 ",
                 "price_int": 100, "currency": "CNY", "tier": classify.TIER_QUALITY}
        card = report.build_card(entry, datetime(2026, 9, 22, tzinfo=classify.zone("Asia/Shanghai")))
        self.assertEqual(card["title_zh"], "时之刃")


class CompareRowsTest(unittest.TestCase):
    def test_formats_price_cny_and_diff(self):
        entry = {"compare": [
            {"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 4500,
             "cny_minor": 674, "diff_pct": -55},
            {"cc": "IN", "label": "印度区", "currency": "INR", "final": 14900,
             "cny_minor": 1042, "diff_pct": -30},
        ]}
        rows = report.compare_rows(entry)
        self.assertEqual(rows[0]["label"], "乌克兰区")
        self.assertEqual(rows[0]["price_text"], "₴45")
        self.assertEqual(rows[0]["cny_text"], "≈ ¥6.74")
        self.assertEqual(rows[0]["diff_text"], "比国区便宜 55%")

    def test_expensive_and_same_and_unknown_diff(self):
        entry = {"compare": [
            {"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 20000,
             "cny_minor": 2997, "diff_pct": 12},
            {"cc": "IN", "label": "印度区", "currency": "INR", "final": 14900,
             "cny_minor": 1042, "diff_pct": 0},
            {"cc": "TR", "label": "土耳其区", "currency": "TRY", "final": 100,
             "cny_minor": None, "diff_pct": None},
        ]}
        rows = report.compare_rows(entry)
        self.assertEqual(rows[0]["diff_text"], "比国区贵 12%")
        self.assertEqual(rows[1]["diff_text"], "与国区同价")
        self.assertIsNone(rows[2]["diff_text"])       # 没汇率就不编差价
        self.assertIsNone(rows[2]["cny_text"])

    def test_empty_compare(self):
        self.assertEqual(report.compare_rows({}), [])
        self.assertEqual(report.compare_rows({"compare": []}), [])


class CardTest(unittest.TestCase):
    now = datetime(2026, 9, 21, 12, 0, tzinfo=classify.zone("Asia/Shanghai"))

    def test_card_uses_title_zh_when_available(self):
        entry = {"game_id": "u", "title": "Cyberpunk 2077", "title_zh": "赛博朋克 2077",
                 "appid": 1091500, "price_int": 14900, "regular_int": 29800, "cut": 50,
                 "currency": "CNY", "flag": "N", "store_low_int": 14900,
                 "history_low_int": 14900, "history_low_1y_int": 14900,
                 "start": "2026-09-21T10:00:00+08:00",
                 "expiry": "2026-09-28T10:00:00+08:00", "tier": classify.TIER_QUALITY}
        card = report.build_card(entry, self.now)
        self.assertEqual(card["title_zh"], "赛博朋克 2077")
        self.assertEqual(card["flag_label"], "新史低")
        self.assertEqual(card["price_text"], "¥149")
        self.assertEqual(card["steam_url"], "https://store.steampowered.com/app/1091500/")
        self.assertEqual(card["xiaoheihe_url"], "https://www.xiaoheihe.cn/games/detail/1091500")
        self.assertEqual(card["tier_label"], classify.TIER_LABELS[classify.TIER_QUALITY])

    def test_card_tier_label_follows_group_label(self):
        """卡片标签不能落在分组标签后面（曾经分组改了、卡片还写着「优质」）。"""
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "tier": classify.TIER_QUALITY}
        card = report.build_card(entry, self.now, report.tier_labels(CFG))
        self.assertEqual(card["tier_label"], "好评达标")

    def test_card_without_appid_has_no_links(self):
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "tier": classify.TIER_PENDING}
        card = report.build_card(entry, self.now)
        self.assertIsNone(card["steam_url"])
        self.assertIsNone(card["xiaoheihe_url"])
        self.assertEqual(card["compare"], [])


if __name__ == "__main__":
    unittest.main()
