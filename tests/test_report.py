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

    def test_group_start_uses_the_majority_time(self):
        """组内开始时刻不一致时按**多数派**上提。

        2026-09-25 实测：quality 组 121 张卡里 113 张同是 01:20，另有 01:21/01:03/00:49/
        00:15/06:16 共 8 张真的不同。原先「唯一才上提」会让整组退回 null，
        前端就给每张卡插一行「折扣开始」，详情区从两栏变三栏。
        """
        items = (
            [{"tier": classify.TIER_QUALITY, "cut": 90, "title": f"A{i}",
              "start_text": "2026-09-25 01:20"} for i in range(5)]
            + [{"tier": classify.TIER_QUALITY, "cut": 10, "title": "B",
                "start_text": "2026-09-25 06:16"}]
        )

        groups = report.build_groups(items, CFG)

        self.assertEqual(groups[0]["start_text"], "2026-09-25 01:20")

    def test_group_start_none_when_no_card_has_a_time(self):
        items = [{"tier": classify.TIER_QUALITY, "cut": 90, "title": "A"}]

        groups = report.build_groups(items, CFG)

        self.assertIsNone(groups[0]["start_text"])

    def test_group_start_breaks_tie_toward_the_later_time(self):
        """多数派并列时取较晚的那个 —— 组头不会显示得比实际更早。

        比较的是 `%Y-%m-%d %H:%M` 字符串（定宽，故字典序即时序）。
        """
        items = [
            {"tier": classify.TIER_QUALITY, "cut": 90, "title": "A",
             "start_text": "2026-09-25 03:40"},
            {"tier": classify.TIER_QUALITY, "cut": 80, "title": "B",
             "start_text": "2026-09-25 01:20"},
        ]

        groups = report.build_groups(items, CFG)

        self.assertEqual(groups[0]["start_text"], "2026-09-25 03:40")


class FormatAmountTest(unittest.TestCase):
    """金额展示。演进：rstrip("0") 砍尾零（12.70→12.7）→ 2026-09-25 改为
    「整元才省小数」→ S9-1（2026-10-06）用户反馈 `¥127` / `¥12.7` / `¥12.70`
    三种长度混排、价格区参差，**统一成一律两位小数**。"""

    def test_keeps_two_decimals_when_cents_nonzero(self):
        self.assertEqual(report.format_amount(1270, "CNY"), "¥12.70")
        self.assertEqual(report.format_amount(12050, "CNY"), "¥120.50")
        self.assertEqual(report.format_amount(3976, "CNY"), "¥39.76")

    def test_whole_yuan_also_two_decimals(self):
        self.assertEqual(report.format_amount(1200, "CNY"), "¥12.00")
        self.assertEqual(report.format_amount(12700, "CNY"), "¥127.00")

    def test_missing_amount_stays_dash(self):
        self.assertEqual(report.format_amount(None, "CNY"), "—")


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
        self.assertEqual(rows[0]["price_text"], "₴45.00")   # S9-1：一律两位小数
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
        self.assertEqual(card["price_text"], "¥149.00")   # S9-1：一律两位小数
        self.assertEqual(card["steam_url"], "https://store.steampowered.com/app/1091500/")
        self.assertEqual(card["xiaoheihe_url"], "https://www.xiaoheihe.cn/games/detail/1091500")
        self.assertEqual(card["tier_label"], classify.TIER_LABELS[classify.TIER_QUALITY])

    def test_card_tier_label_follows_group_label(self):
        """卡片标签不能落在分组标签后面（曾经分组改了、卡片还写着「优质」）。"""
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "tier": classify.TIER_QUALITY}
        card = report.build_card(entry, self.now, report.tier_labels(CFG))
        self.assertEqual(card["tier_label"], "好评达标")

    def test_days_left_calendar_diff(self):
        """「剩 X 天」按日历天差：09-21 中午看 09-28 上午结束 → 剩 7 天。"""
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "expiry": "2026-09-28T10:00:00+08:00", "tier": classify.TIER_QUALITY}
        self.assertEqual(report.build_card(entry, self.now)["days_left"], 7)

    def test_days_left_early_morning_expiry_counts_as_previous_day(self):
        """凌晨收摊宽容（2026-09-27 定案，阈值 3:00）：明早 01:00 过期 = 「今天结束」
        （剩 0 天），不能显示成「剩 1 天」。03:00 起主跑已进新一天，不再宽容。"""
        for hour, expected in ((1, 0), (2, 0)):
            entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                     "expiry": f"2026-09-22T0{hour}:00:00+08:00", "tier": classify.TIER_QUALITY}
            self.assertEqual(report.build_card(entry, self.now)["days_left"], expected,
                             f"明天 0{hour}:00 过期应为 {expected} 天")
        # 03:00 起不再宽容：明天 05:00 / 10:00 过期就是正经「剩 1 天」
        for hour in ("05", "10"):
            entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                     "expiry": f"2026-09-22T{hour}:00:00+08:00", "tier": classify.TIER_QUALITY}
            self.assertEqual(report.build_card(entry, self.now)["days_left"], 1)

    def test_days_left_never_negative(self):
        """expiry 已过（清理边缘/时钟漂移）时至少是 0，不出现负数。"""
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "expiry": "2026-09-21T01:00:00+08:00", "tier": classify.TIER_QUALITY}
        self.assertEqual(report.build_card(entry, self.now)["days_left"], 0)

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


class FeaturedSortTest(unittest.TestCase):
    """重构 S5：默认精选排序 = 分层字典序（验收 §6：单测锁定）。

    键序：①新史低在前、平史低在后（史低待确认殿后）②折扣力度降序
    ③评价数降序 ④标题（稳定收尾键）。
    """

    def card(self, low_class, cut, count, title):
        return {
            "low_class": low_class, "cut": cut, "title": title,
            "reviews": {"score": 80, "count": count} if count is not None else None,
        }

    def test_new_layer_before_tie_before_unknown(self):
        cards = [self.card("tie", 90, 5000, "B"),
                 self.card("unknown", 95, 9000, "C"),
                 self.card("new", 10, 10, "A")]
        ordered = sorted(cards, key=report.featured_sort_key)
        self.assertEqual([c["low_class"] for c in ordered],
                         ["new", "tie", "unknown"],
                         "折扣力度再大、评价再多，平史低也排不到新史低前面")

    def test_cut_desc_within_layer(self):
        cards = [self.card("new", 30, 9000, "B"),
                 self.card("new", 80, 10, "A")]
        ordered = sorted(cards, key=report.featured_sort_key)
        self.assertEqual([c["cut"] for c in ordered], [80, 30])

    def test_review_count_desc_breaks_cut_tie(self):
        cards = [self.card("new", 50, 300, "B"),
                 self.card("new", 50, 9000, "A")]
        ordered = sorted(cards, key=report.featured_sort_key)
        self.assertEqual([c["title"] for c in ordered], ["A", "B"])

    def test_missing_reviews_sort_last_within_same_cut(self):
        """无 reviews（详情待补）在同层同折扣力度下按 0 条计，排有数据的后面。"""
        cards = [self.card("new", 50, None, "PENDING"),
                 self.card("new", 50, 100, "RATED")]
        ordered = sorted(cards, key=report.featured_sort_key)
        self.assertEqual([c["title"] for c in ordered], ["RATED", "PENDING"])

    def test_title_is_stable_final_key(self):
        cards = [self.card("new", 50, 100, "B"),
                 self.card("new", 50, 100, "A")]
        ordered = sorted(cards, key=report.featured_sort_key)
        self.assertEqual([c["title"] for c in ordered], ["A", "B"])

    def test_build_featured_group_shape(self):
        cards = [self.card("tie", 90, 5000, "B"),
                 self.card("new", 20, 100, "A")]
        cards[0]["start_text"] = "2026-10-05 01:20"
        cards[1]["start_text"] = "2026-10-05 01:20"
        group = report.build_featured_group(cards)
        self.assertEqual(group["key"], "featured")
        self.assertEqual(group["criteria"], "新史低 → 折扣力度 → 评价数")
        self.assertFalse(group["collapsed"])
        self.assertEqual([c["low_class"] for c in group["items"]], ["new", "tie"])
        self.assertEqual(group["start_text"], "2026-10-05 01:20")


class LazyViewsTest(unittest.TestCase):
    """重构 S5：本周 / 折扣中 / 全部 走 all.js 懒加载。

    - 未传 all_cards：三个视图按钮禁用（数据不可用时不给可点的空按钮）
    - 传了 all_cards：按钮启用 + 带服务端预统计 count + lazy 标志；
      all.js 落盘（分组形态与 data.js 同构，条目带 views 成员标志）
    """

    now = datetime(2026, 9, 21, 12, 0, tzinfo=classify.zone("Asia/Shanghai"))

    def _entry(self, game_id, appid, expiry, start=None):
        return {"game_id": game_id, "title": game_id, "appid": appid,
                "price_int": 100, "regular_int": 200, "cut": 50,
                "currency": "CNY", "flag": "N",
                "start": start or "2026-09-21T10:00:00+08:00",
                "expiry": expiry, "tier": classify.TIER_QUALITY,
                "reviews": {"score": 80, "count": 500}}

    def test_lazy_views_disabled_without_all_cards(self):
        out = _render([], self.now)
        payload = _load_payload(out)
        lazy = {v["key"]: v for v in payload["views"] if v["key"] in report.LAZY_VIEWS}
        self.assertEqual(set(lazy), {"week", "active", "all"})
        for view in lazy.values():
            self.assertFalse(view["enabled"])
            self.assertIsNone(view["count"])
            self.assertFalse(view["lazy"])

    def test_lazy_views_enabled_with_counts_and_all_json(self):
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        entries = [
            # 折扣中（9-28 过期）+ 本周（9-21 开始，落在本周窗口）
            self._entry("g-active", 1, "2026-09-28T10:00:00+08:00"),
            # 已过期：不 active 不 week，只进「全部」
            self._entry("g-expired", 2, "2026-09-20T10:00:00+08:00",
                        start="2026-09-13T10:00:00+08:00"),
        ]
        all_cards = []
        for entry in entries:
            card = report.build_card(entry, self.now)
            card["views"] = [key for key in classify.VIEW_KEYS
                             if classify.in_view(key, entry, self.now, CFG)]
            all_cards.append(card)
        extra = {"week": sum(1 for c in all_cards if "week" in c["views"]),
                 "active": sum(1 for c in all_cards if "active" in c["views"]),
                 "all": len(all_cards)}
        cfg = dict(CFG, output_dir=str(out))
        report.render(cfg, [], _STATS, self.now, featured=True,
                      all_cards=all_cards, extra_counts=extra)
        payload = _load_payload(out)
        by_key = {v["key"]: v for v in payload["views"]}
        self.assertTrue(by_key["week"]["enabled"])
        self.assertTrue(by_key["week"]["lazy"])
        self.assertEqual(by_key["week"]["count"], 1)     # 只有 g-active 在本周窗口
        self.assertEqual(by_key["active"]["count"], 1)   # 只有 g-active 未过期
        self.assertEqual(by_key["all"]["count"], 2)

        # all.js 是 window.ALL_DATA = {...} 形态（非纯 JSON），解析时剥前缀
        all_payload = json.loads(
            (out / "all.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip(";\n"))
        items = {i["game_id"]: i for g in all_payload["groups"] for i in g["items"]}
        self.assertEqual(set(items), {"g-active", "g-expired"})
        self.assertIn("active", items["g-active"]["views"])
        self.assertIn("week", items["g-active"]["views"])
        self.assertNotIn("active", items["g-expired"]["views"])
        self.assertNotIn("week", items["g-expired"]["views"])

    def test_all_json_groups_by_tier(self):
        """「全部」沿用现有分组交互：all.js 里仍是口碑分档组（组头带条件文案）。"""
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        entry = self._entry("g-good", 1, "2026-09-28T10:00:00+08:00")
        card = report.build_card(entry, self.now)
        card["views"] = ["week", "active", "new_today", "upcoming"]
        cfg = dict(CFG, output_dir=str(out))
        report.render(cfg, [], _STATS, self.now, featured=True,
                      all_cards=[card], extra_counts={"week": 1, "active": 1, "all": 1})
        all_payload = json.loads(
            (out / "all.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip(";\n"))
        self.assertEqual([g["key"] for g in all_payload["groups"]],
                         [classify.TIER_QUALITY])
        self.assertEqual(all_payload["groups"][0]["criteria"],
                         "好评率 ≥ 70% 且 评价数 ≥ 100")

    def test_featured_flag_switches_group_shape(self):
        """featured=True：当日新增变单一精选组；False：维持口碑分档分组（兼容旧调用）。"""
        import tempfile
        card = {"tier": classify.TIER_QUALITY, "cut": 50, "title": "A",
                "title_zh": None, "appid": 1, "low_class": "new",
                "low_label": "新史低", "price_text": "¥1", "start_text": None}
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        cfg = dict(CFG, output_dir=str(out))
        report.render(cfg, [card], _STATS, self.now, featured=True)
        payload = _load_payload(out)
        self.assertEqual([g["key"] for g in payload["groups"]], ["featured"])

        out2 = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        report.render(dict(CFG, output_dir=str(out2)), [card], _STATS, self.now)
        payload2 = _load_payload(out2)
        self.assertEqual([g["key"] for g in payload2["groups"]], [classify.TIER_QUALITY])


if __name__ == "__main__":
    unittest.main()


class HomeSectionsTest(unittest.TestCase):
    """S9 首页四板块（2026-10-06）。锁定口径：近 7 天窗口、必须还在折扣期内、
    「即将到期」不叠加近 7 天、预览条数、以及各板块的排序。"""

    CFG = {
        "home_new_low_days": 7,
        "big_cut_percent": 80,
        "notable_review_count": 10000,
        "home_section_preview": 3,
        "home_picks": 15,
    }

    @staticmethod
    def card(title, *, ago=1, cut=50, count=0, live=True, upcoming=False, low="new"):
        views = [k for k, ok in (("active", live), ("upcoming", upcoming)) if ok]
        return {
            "title": title, "title_zh": title, "cut": cut,
            "low_class": low, "start_days_ago": ago, "days_left": 3 if live else 0,
            "reviews": {"score": 90, "count": count} if count else None,
            "views": views,
        }

    def sections(self, cards):
        return {s["key"]: s for s in report.build_sections(cards, self.CFG)}

    def test_seven_day_window(self):
        got = self.sections([self.card("在窗口内", ago=7), self.card("刚出窗口", ago=8)])
        titles = [i["title"] for i in got["new_low"]["items"]]
        self.assertIn("在窗口内", titles)
        self.assertNotIn("刚出窗口", titles)

    def test_expired_is_excluded(self):
        """首页不能摆「已经买不到」的折扣（all_shown 里含过期留存）。"""
        got = self.sections([self.card("还活着"), self.card("已过期", live=False)])
        self.assertEqual([i["title"] for i in got["new_low"]["items"]], ["还活着"])

    def test_expiring_does_not_stack_seven_day_window(self):
        """20 天前开始、但马上到期的老折扣必须进「即将到期」。"""
        got = self.sections([self.card("老折扣快到期", ago=20, upcoming=True)])
        self.assertEqual([i["title"] for i in got["expiring"]["items"]], ["老折扣快到期"])

    def test_popular_threshold_and_big_cut_threshold(self):
        got = self.sections([self.card("够热", count=10000),
                             self.card("不够热", count=9999)])
        self.assertEqual([i["title"] for i in got["popular"]["items"]], ["够热"])
        got = self.sections([self.card("够深", cut=80), self.card("不够深", cut=79)])
        self.assertEqual([i["title"] for i in got["big_cut"]["items"]], ["够深"])

    def test_preview_limit_and_count(self):
        cards = [self.card(f"G{i}", cut=90 - i) for i in range(10)]
        got = self.sections(cards)
        self.assertEqual(len(got["new_low"]["items"]), 3)   # home_section_preview
        self.assertEqual(got["new_low"]["count"], 10)       # 完整条数照实报

    def test_big_cut_sorted_by_discount_desc(self):
        got = self.sections([self.card("低", cut=85), self.card("高", cut=95)])
        self.assertEqual([i["title"] for i in got["big_cut"]["items"]], ["高", "低"])

    def test_picks_only_live_new_lows(self):
        # 推荐位有前置门槛（好评率 ≥70% 且 评价数 ≥100），所以这里要带上评价数
        picks = report.pick_top([self.card("A", count=1000),
                                 self.card("B", count=1000, live=False),
                                 self.card("C", count=1000, low="tie")], self.CFG)
        self.assertEqual([i["title"] for i in picks], ["A"])


class RecommendScoreTest(unittest.TestCase):
    """refs.md §10.2 权重公式 v2（轮播版，名气优先）。

    钉四件事：① 门槛挡掉没数据/低口碑的；② 名气优先但**不是线性**（对数列压，
    156 万评价不能把其余项压成噪声）；③ **缺间隔分时归一化**，不是当 0 分；
    ④ 平手顺序稳定可复现（折扣% → 评价数 → 价格低 → appid）。
    """

    CFG = {"home_new_low_days": 7, "home_picks": 15}

    @staticmethod
    def card(title, *, cut=50, count=1000, rate=90, days_left=3, ago=1,
             gap=None, price=10000, appid=1):
        return {
            "title": title, "title_zh": title, "cut": cut,
            "low_class": "new", "start_days_ago": ago, "days_left": days_left,
            "reviews": {"score": rate, "count": count},
            "last_low_days": gap, "price_int": price, "appid": appid,
            "views": ["active"],
        }

    def test_gate_blocks_no_detail_and_low_score(self):
        self.assertIsNone(report.recommend_score(self.card("没详情", count=0)))
        self.assertIsNone(report.recommend_score(self.card("评价太少", count=99)))
        self.assertIsNone(report.recommend_score(self.card("口碑不够", rate=69)))
        self.assertIsNotNone(report.recommend_score(self.card("合格")))

    def test_fame_is_logarithmic_not_linear(self):
        """1 万评价不该只拿 200 万评价的 1/200 —— 对数列压过之后大约是七成。"""
        small = report.recommend_score(self.card("小", count=10_000))
        huge = report.recommend_score(self.card("大", count=2_000_000))
        # 名气项：log10(1w+1)/log10(20w+1) ≈ 0.75；200 万封顶 = 1.0
        self.assertGreater(small, 60)
        self.assertLess(small, huge)
        self.assertLess((huge - small) / huge, 0.15)   # 差不到 15%，不是数量级差

    def test_missing_gap_is_normalized_not_zero(self):
        """间隔缺数据时该项**不计分**，并把总分按剩余权重归一化回 100 ——
        否则「有数据」的那批会被凭空扣掉 15 分，等于变相惩罚数据更全的游戏。"""
        no_gap = report.recommend_score(self.card("没间隔数据", gap=None))
        zero_gap = report.recommend_score(self.card("间隔 0 天", gap=0))
        self.assertGreater(no_gap, zero_gap)   # 缺 ≠ 0 分
        # 归一化后满分仍是 100：各项拉满、无间隔时应该接近满分
        best = report.recommend_score(self.card("满分", cut=95, count=200_000,
                                                rate=95, days_left=0, ago=0, gap=None))
        self.assertAlmostEqual(best, 100.0, places=1)

    def test_score_prefers_bigger_cut_when_fame_ties(self):
        same = [self.card("七折", cut=70), self.card("九折", cut=90)]
        same.sort(key=lambda c: report.recommend_sort_key(c, self.CFG))
        self.assertEqual([c["title"] for c in same], ["九折", "七折"])

    def test_tie_break_is_stable(self):
        """全同分时按 评价数 → 价格低 → appid 收尾，保证每次跑出来顺序一样。"""
        a = self.card("便宜的", price=1000, appid=2)
        b = self.card("贵的", price=9000, appid=1)
        pair = [a, b]
        pair.sort(key=lambda c: report.recommend_sort_key(c, self.CFG))
        self.assertEqual([c["title"] for c in pair], ["便宜的", "贵的"])

    def test_pick_top_uses_weights_not_lexicographic(self):
        """老字典序会让 95% 的小众游戏顶掉 GTFO 这类 —— 公式不能这样。"""
        niche = self.card("冷门九五折", cut=95, count=120)
        famous = self.card("热门大作", cut=73, count=46_256, rate=86)
        picks = report.pick_top([niche, famous], self.CFG)
        self.assertEqual(picks[0]["title"], "热门大作")

    def test_pick_top_falls_back_when_nothing_passes_gate(self):
        """整池都没过门槛时退回字典序 —— 大卡这块不能整块消失。"""
        pool = [self.card("没详情", count=0), self.card("也没详情", count=0)]
        self.assertEqual(len(report.pick_top(pool, self.CFG)), 2)


class CleanTitleZhTest(unittest.TestCase):
    """S9：剥掉中文名里夹带的英文原名（Steam schinese 标题常带，实测 946/7380）。"""

    def test_strips_english_prefix(self):
        self.assertEqual(
            report.clean_title_zh("Lords of the Fallen 堕落之主", "Lords of the Fallen"),
            "堕落之主")
        self.assertEqual(
            report.clean_title_zh("Forza Motorsport 极限竞速", "Forza Motorsport"),
            "极限竞速")

    def test_strips_english_suffix_and_wrapped(self):
        self.assertEqual(
            report.clean_title_zh("Cities: Skylines II 都市：天际线2", "Cities: Skylines II"),
            "都市：天际线2")
        self.assertEqual(
            report.clean_title_zh("Wo Long: Fallen Dynasty （卧龙：苍天陨落）",
                                  "Wo Long: Fallen Dynasty"),
            "卧龙：苍天陨落")
        self.assertEqual(
            report.clean_title_zh("大富翁10 (RichMan 10)", "Richman 10"), "大富翁10")

    def test_keeps_when_english_not_exactly_matched(self):
        """对不上原名就不动 —— 不猜、不乱切。"""
        self.assertEqual(
            report.clean_title_zh("《镜之边缘：Catalyst》", "Mirror's Edge™ Catalyst"),
            "《镜之边缘：Catalyst》")
        self.assertEqual(report.clean_title_zh("Agent A - 伪装游戏",
                                               "Agent A: A puzzle in disguise"),
                         "Agent A - 伪装游戏")

    def test_does_not_leave_only_english_tail(self):
        """英文原名是前缀时，剥完只剩英文尾巴 —— 必须原样返回，宁可不动。"""
        self.assertEqual(report.clean_title_zh("Pure Farming 2018", "Pure Farming"),
                         "Pure Farming 2018")
        self.assertEqual(report.clean_title_zh("Kingdom Rush  - Tower Defense",
                                               "Kingdom Rush"),
                         "Kingdom Rush  - Tower Defense")
        self.assertEqual(report.clean_title_zh("MX Nitro: Unleashed", "MX Nitro"),
                         "MX Nitro: Unleashed")

    def test_passthrough_cases(self):
        self.assertEqual(report.clean_title_zh("赛博朋克 2077", "Cyberpunk 2077"), "赛博朋克 2077")
        self.assertEqual(report.clean_title_zh("Gotham Knights", "Gotham Knights"),
                         "Gotham Knights")   # 同名（没中文名）原样
        self.assertIsNone(report.clean_title_zh(None, "X"))
        self.assertEqual(report.clean_title_zh("只有中文", None), "只有中文")

class SectionKeysTest(unittest.TestCase):
    """S9：单卡板块归属（写进 all.js 的 sections 字段，前端按它筛完整列表）。
    注意它**不判日期窗口** —— 窗口交给调用方，这样前端「全部」胶囊能看全量。"""

    CFG = HomeSectionsTest.CFG

    def test_keys_ignore_date_window(self):
        old = HomeSectionsTest.card("20 天前的新史低", ago=20)
        self.assertIn("new_low", report.section_keys(old, self.CFG))

    def test_multi_section_membership(self):
        card = HomeSectionsTest.card("又新又便宜", cut=90, count=20000)
        self.assertEqual(report.section_keys(card, self.CFG), ["new_low", "popular", "big_cut"])

    def test_expired_belongs_to_nothing(self):
        self.assertEqual(
            report.section_keys(HomeSectionsTest.card("过期了", live=False), self.CFG), [])


class FilterSpecsTest(unittest.TestCase):
    """S9-2 底部抽屉筛选（refs.md §6.7）：排序 / 日期 / 折扣区间 / 好评数量 / 仅新史低。

    钉两件事：① 五个维度齐全；② **阈值从配置来** —— 改 `big_cut_percent`
    / `notable_review_count` / `home_new_low_days`，选项与默认值跟着变，
    不会出现「选项写着 ≥80%、实际按 75% 筛」这种撒谎。
    """

    CFG = {"home_new_low_days": 7, "big_cut_percent": 80,
           "notable_review_count": 10000}

    def test_five_dimensions(self):
        keys = [g["key"] for g in report.filter_specs(self.CFG)]
        self.assertEqual(keys, ["sort", "date", "cut", "reviews", "only_new"])

    def test_thresholds_follow_config(self):
        cfg = dict(self.CFG, big_cut_percent=75, notable_review_count=50000,
                   home_new_low_days=3)
        groups = {g["key"]: g for g in report.filter_specs(cfg)}
        self.assertIn({"value": "75", "label": "≥ 75%"}, groups["cut"]["options"])
        self.assertIn({"value": "50000", "label": "≥ 50,000"},
                      groups["reviews"]["options"])
        self.assertIn({"value": "d3", "label": "近 3 天"}, groups["date"]["options"])

    def test_defaults_follow_config(self):
        self.assertEqual(report.filter_defaults(self.CFG)["date"], "d7")
        self.assertEqual(
            report.filter_defaults(dict(self.CFG, home_new_low_days=3))["date"], "d3")

    def test_rendered_html_has_drawer(self):
        html = (_render([], datetime(2026, 10, 6, 5, 14)) / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('id="drawer"', html)
        self.assertIn('id="filter-open"', html)
        self.assertIn('data-group="sort"', html)
        self.assertIn('data-value="d7"', html)
        self.assertNotIn('data-range="7"', html)   # 旧的两个胶囊已退场


class TopbarTest(unittest.TestCase):
    """S9-2 顶栏：站点名标识 + 手机端导航折叠按钮（refs.md B1）。

    钉两件事：① 站点名是「小图标 + 双色分段」的结构，**不是**一行纯文字
    （用户要"艺术字体"，而页面必须能离线打开 → 只能纯 CSS，不引字体文件）；
    ② 有一个汉堡按钮，且默认 aria-expanded=false（收起态），
    否则手机上又变回横向滚动那条。
    """

    def test_index_has_brand_mark_and_split_text(self):
        html = (_render([], datetime(2026, 10, 6, 5, 14)) / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('class="brand-mark"', html)
        self.assertIn('class="brand-a">Steam<', html)
        self.assertIn('class="brand-b">DailyLowest<', html)
        self.assertIn("SteamDailyLowest 首页", html)     # 图标站名的无障碍名
        self.assertNotIn('id="brand">SteamDailyLowest<', html)   # 老的一行纯文字已退场

    def test_index_has_nav_toggle_collapsed_by_default(self):
        html = (_render([], datetime(2026, 10, 6, 5, 14)) / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('id="nav-toggle"', html)
        self.assertIn('aria-controls="nav"', html)
        self.assertIn('aria-expanded="false"', html)

    def test_about_shares_the_same_brand_without_toggle(self):
        """关于页复用同一套站点名标识，但不需要汉堡按钮（它只有一个回首页链接）。"""
        html = (_render([], datetime(2026, 10, 6, 5, 14)) / "about.html").read_text(
            encoding="utf-8")
        self.assertIn('class="brand-mark"', html)
        self.assertIn('class="brand-b">DailyLowest<', html)
        self.assertNotIn('id="nav-toggle"', html)
