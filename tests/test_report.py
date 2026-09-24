# -*- coding: utf-8 -*-
"""报表展示层的单元测试（§3.5 / §7.2 / §7.4 / §7.1）。

重点钉住三件用户明确要求的事：

1. **分组标签是中性描述 + 条件写在旁边**（「优质」这种评价性词会误导 ——
   70% 好评率本来就不等于优质），而且条件文案要**跟着配置变**；
2. **筛选条件只在 README**：页面上那个折叠框已删（批 F），
   口径字段不该再出现在 payload / HTML 里；
3. 概览是多框铺满的响应式布局，不再是并排大框。
"""

from __future__ import annotations

import json
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

#: 渲染用的最小统计块（模板会做算术，概览/页脚读到的字段都要给全）
_STATS = {
    "sweep": "low_only", "new_today_raw": 1, "new_today_shown": 1,
    "deals_fetched": 0, "hist_low_total": 1,
    "detail_fetched": 1, "detail_backlog": 0, "detail_targets": 1,
    "last_run_at": None,
}


def _render(items: list[dict], now: datetime) -> Path:
    """用临时 output_dir 跑一次真实渲染，返回输出目录（测试用）。"""
    import tempfile

    out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
    report.render(dict(CFG, output_dir=str(out)), items, _STATS, now)
    return out


def _load_payload(out: Path) -> dict:
    text = (out / "data.js").read_text(encoding="utf-8")
    return json.loads(text.split("=", 1)[1].rstrip().rstrip(";"))


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

    def test_build_groups_notable_first(self):
        """「高热度 · 口碑不一」排在页面最上面（默认收起），好评达标随后。"""
        items = [
            {"tier": classify.TIER_QUALITY, "cut": 90, "title": "A"},
            {"tier": classify.TIER_NOTABLE, "cut": 50, "title": "N"},
        ]
        groups = report.build_groups(items, CFG)
        self.assertEqual([g["key"] for g in groups],
                         [classify.TIER_NOTABLE, classify.TIER_QUALITY])
        self.assertTrue(groups[0]["collapsed"])


class ConditionsTest(unittest.TestCase):
    """批 F（2026-09-24）：页面上的「筛选条件」折叠框已删除，判定口径搬到
    README「筛选条件」一节 —— 这里钉住「别又跑回 payload / 页面里」。"""

    def test_conditions_helpers_are_gone(self):
        self.assertFalse(hasattr(report, "conditions"))
        self.assertFalse(hasattr(report, "conditions_digest"))

    def test_payload_has_no_criteria_fields(self):
        items = [{"title": "A", "title_zh": None, "appid": 1, "cut": 90,
                  "price_text": "¥10", "low_class": "new", "low_label": "新史低",
                  "tier": classify.TIER_QUALITY}]
        now = datetime(2026, 9, 24, 10, 0, tzinfo=classify.zone("Asia/Shanghai"))
        out = _render(items, now)
        payload = _load_payload(out)
        for field in ("conditions", "criteria_digest"):
            self.assertNotIn(field, payload)
        self.assertNotIn("criteria-box", (out / "index.html").read_text(encoding="utf-8"))

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
        self.assertEqual(rows[0]["diff_pct"], -55)   # 正负号与颜色由前端渲染

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
        self.assertEqual(rows[0]["diff_pct"], 12)
        self.assertEqual(rows[1]["diff_pct"], 0)
        self.assertIsNone(rows[2]["diff_pct"])        # 没汇率就不编差价
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
        # 批 E spec E1/E6：卡片标签改用 Steam 口径的 low_class / low_label
        self.assertEqual(card["low_class"], "new")
        self.assertEqual(card["low_label"], "新史低")
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

    def test_card_drops_itad_url_and_keeps_price_int(self):
        """R2：payload 不再输出 itad_url；R1：前端排序用的 price_int 直接透传。"""
        entry = {"game_id": "u", "title": "X", "price_int": 14900, "currency": "CNY",
                 "itad_url": "https://itad.link/x", "tier": classify.TIER_QUALITY}
        card = report.build_card(entry, self.now)
        self.assertNotIn("itad_url", card)
        self.assertEqual(card["price_int"], 14900)

    def test_card_banner_prefers_boxart(self):
        """R8：缩略图只有几十像素宽，payload 封面优先用小图 boxart。"""
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "boxart": "https://x/boxart.jpg", "banner": "https://x/banner600.jpg",
                 "tier": classify.TIER_QUALITY}
        card = report.build_card(entry, self.now)
        self.assertEqual(card["banner"], "https://x/boxart.jpg")
        # boxart 缺失时保持回落（前端另有无图模式兜底）
        bare = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                "tier": classify.TIER_QUALITY}
        self.assertIsNone(report.build_card(bare, self.now)["banner"])


if __name__ == "__main__":
    unittest.main()
