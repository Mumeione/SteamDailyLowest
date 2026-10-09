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
import re
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
    "list_batch": 30,
    "list_auto_max": 300,
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


def _load_shards(out: Path) -> dict[str, list[dict]]:
    """读出 ``output/all/`` 下的分片，按板块拼回完整卡片列表。

    分片文件是 ``(window.ALL_S = window.ALL_S || {})["<slot>"] = {...};`` 形态
    （与 all.js 一样不是纯 JSON），按 ``"] = "`` 切开再剥尾部分号。
    返回 ``{板块 key: 按分片顺序拼好的卡片列表}`` —— 板块顺序就是分片顺序。
    """
    out_dir = out / "all"
    if not out_dir.exists():
        return {}
    buckets: dict[str, list[dict]] = {}
    for path in sorted(out_dir.glob("*.js")):
        payload = json.loads(
            path.read_text(encoding="utf-8").split("] = ", 1)[1].rstrip().rstrip(";"))
        buckets.setdefault(payload["key"], []).extend(payload["items"])
    return buckets


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

    def test_specs_carry_only_key_and_label(self):
        """2026-10-09（卡片 08）：`criteria` / `collapsed` 已删 —— 生产一个都不读。

        阈值文案的唯一来源是「关于网站」页的 :func:`report.criteria_notes`。
        """
        for spec in report.group_specs(CFG):
            self.assertEqual(set(spec), {"key", "label"}, spec)


class PoolItemsTest(unittest.TestCase):
    """「全部折扣」分片的池顺序（2026-10-09：由 build_groups 收敛成扁平列表）。"""

    def test_group_order_follows_specs_and_skips_empty_tiers(self):
        items = [
            {"tier": classify.TIER_QUALITY, "cut": 90, "title": "A"},
            {"tier": classify.TIER_COLD, "cut": 10, "title": "B"},     # 空白档位：不进池
            {"tier": classify.TIER_NOTABLE, "cut": 50, "title": "N"},
        ]
        pool = report.pool_items(items, CFG)
        self.assertEqual([i["title"] for i in pool], ["N", "A"])

    def test_within_group_sorted_by_cut_then_title(self):
        """组内折扣降序、同分按标题 —— 与「全部折扣」列表页看到的一致。"""
        items = [
            {"tier": classify.TIER_QUALITY, "cut": 50, "title": "B"},
            {"tier": classify.TIER_QUALITY, "cut": 90, "title": "A"},
            {"tier": classify.TIER_QUALITY, "cut": 50, "title": "A"},
        ]
        self.assertEqual([i["title"] for i in report.pool_items(items, CFG)],
                         ["A", "A", "B"])

    def test_missing_cut_and_title_are_safe(self):
        items = [{"tier": classify.TIER_QUALITY, "cut": None, "title": None},
                 {"tier": classify.TIER_QUALITY, "cut": 80, "title": "X"}]
        self.assertEqual([i["title"] for i in report.pool_items(items, CFG)],
                         ["X", None])


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
        # steam_url / xiaoheihe_url 已删（2026-10-08 问题1）—— 前端由 appid 现拼，
        # 服务端不再下发（现拼结果由 jsdom 冒烟测锁定）
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
        """无 appid 的卡片：前端不会渲染 Steam / 小黑盒链接（现拼依据 appid 缺失）。
        2026-10-08 问题1 起链接 URL 不再由服务端下发 —— 这里改锁「appid 为 None」这个依据。"""
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "tier": classify.TIER_PENDING}
        card = report.build_card(entry, self.now)
        self.assertIsNone(card["appid"])
        self.assertNotIn("steam_url", card)
        self.assertNotIn("xiaoheihe_url", card)
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

    def test_latest_json_shown_order_matches_sort_key(self):
        """端到端锁定：latest.json 的 ``shown`` 顺序 == 按 featured_sort_key 排。
        （原来的断言查的是已删的 featured 组；这里改为查真实产物。）"""
        import tempfile
        now = datetime(2026, 9, 21, 12, 0, tzinfo=classify.zone("Asia/Shanghai"))

        def full_card(low_class, cut, count, title, appid):
            return {"tier": classify.TIER_QUALITY, "tier_label": "好评达标",
                    "cut": cut, "title": title, "title_zh": None, "appid": appid,
                    "low_class": low_class,
                    "low_label": "新史低" if low_class == "new" else "平史低",
                    "price_text": "¥1",
                    "reviews": {"score": 80, "count": count}}

        cards = [full_card("tie", 90, 99999, "A", 1),   # 平史低（层 1）
                 full_card("new", 10, 1, "B", 2),       # 新史低，折扣最小
                 full_card("new", 50, 10, "C", 3)]      # 新史低，折扣居中
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        report.render(dict(CFG, output_dir=str(out)), cards, _STATS, now)
        latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual([s["title"] for s in latest["shown"]], ["C", "B", "A"])

    # build_featured_group 已随 2026-10-08 payload 瘦身删除（S9 前端不读分组键；
    # featured 组是死数据，见 report.render 的 docstring）。
    # 分层排序键本身由上面几条 featured_sort_key 单测锁定。


class LazyViewsTest(unittest.TestCase):
    """板块完整列表的数据源 = ``output/all/`` 下的分片（按需懒加载）。

    - 未传 all_cards：不产出分片（列表页没有数据源）
    - 传了 all_cards：按板块切出分片，每条带 views / sections 标志

    ⚠️ 2026-10-08：单文件 all.js（5.81MB / gzip 729KB，分类页要等 13~18 秒）
    已换成按板块顺序切的 ``all/<key>_<n>.js`` —— 见 :func:`report.write_all_shards`。
    ⚠️ 视图按钮（payload["views"]）已整段删除，本类不再断言它。
    """

    now = datetime(2026, 9, 21, 12, 0, tzinfo=classify.zone("Asia/Shanghai"))

    def _entry(self, game_id, appid, expiry, start=None):
        return {"game_id": game_id, "title": game_id, "appid": appid,
                "price_int": 100, "regular_int": 200, "cut": 50,
                "currency": "CNY", "flag": "N",
                "start": start or "2026-09-21T10:00:00+08:00",
                "expiry": expiry, "tier": classify.TIER_QUALITY,
                "reviews": {"score": 80, "count": 500}}

    def test_all_json_absent_without_all_cards(self):
        """不传 all_cards → 不产出分片目录；payload 也不再带 views 键（2026-10-08 删）。"""
        out = _render([], self.now)
        self.assertFalse((out / "all").exists())
        self.assertNotIn("views", _load_payload(out))

    def test_all_json_written_with_all_cards(self):
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
        cfg = dict(CFG, output_dir=str(out))
        report.render(cfg, [], _STATS, self.now, all_cards=all_cards)

        items = {i["game_id"]: i for i in _load_shards(out)["__all__"]}
        self.assertEqual(set(items), {"g-active", "g-expired"})
        self.assertIn("active", items["g-active"]["views"])
        self.assertIn("week", items["g-active"]["views"])
        self.assertNotIn("active", items["g-expired"]["views"])
        self.assertNotIn("week", items["g-expired"]["views"])

    def test_all_shards_keep_tier_group_order(self):
        """「全部折扣」没有板块排序语义，沿用**池顺序** = tier 分组（notable →
        quality → pending）+ 组内折扣降序 —— 分片顺序必须与之一致，否则点进
        「全部折扣」看到的顺序会和改动前不一样。"""
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        cards = []
        for game_id, appid, tier, cut in (
            ("g-quality", 1, classify.TIER_QUALITY, 50),
            ("g-notable", 2, classify.TIER_NOTABLE, 90),
        ):
            entry = self._entry(game_id, appid, "2026-09-28T10:00:00+08:00")
            entry["tier"] = tier
            entry["cut"] = cut
            card = report.build_card(entry, self.now)
            card["views"] = ["week", "active", "new_today", "upcoming"]
            cards.append(card)
        cfg = dict(CFG, output_dir=str(out))
        report.render(cfg, [], _STATS, self.now, all_cards=cards)
        order = [c["game_id"] for c in _load_shards(out)["__all__"]]
        self.assertEqual(order, ["g-notable", "g-quality"])

    def test_all_json_cards_are_slimmed(self):
        """分片卡片瘦身（2026-10-08 问题1，分类页 6.3MB）：剔除前端不读
        （tier_label/low_label/last_low_days）或能现拼（steam_url/xiaoheihe_url/banner）
        的字段；`banner` 换成紧凑 `art` 扩展名码；`game_id` 必须保留（art 现拼的依据）。"""
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        entry = self._entry("g-art", 1, "2026-09-28T10:00:00+08:00")
        entry["boxart"] = "https://assets.isthereanydeal.com/g-art/boxart.png?t=123"
        card = report.build_card(entry, self.now)
        cfg = dict(CFG, output_dir=str(out))
        report.render(cfg, [], _STATS, self.now, all_cards=[card])
        item = _load_shards(out)["__all__"][0]
        for dropped in ("tier_label", "low_label", "last_low_days",
                        "steam_url", "xiaoheihe_url", "banner"):
            self.assertNotIn(dropped, item)
        self.assertEqual(item["art"], "png")
        self.assertIn("game_id", item)          # art 现拼的依据，不能删

    def test_boxart_code(self):
        """封面 URL → 紧凑扩展名码：只下发不可现拼的扩展名（.png 不能一律当 .jpg，实测 403）。
        未知后缀回落 None（code-review 2026-10-08）：真出现 webp 说明资产形态变了，
        猜 jpg 会 403 出破图，不如灰块占位。"""
        self.assertIsNone(report.boxart_code(None))
        self.assertEqual(report.boxart_code("https://x/a/boxart.jpg?t=1"), "jpg")
        self.assertEqual(report.boxart_code("https://x/a/boxart.png?t=1"), "png")
        self.assertIsNone(report.boxart_code("https://x/a/weird.webp"))

    # featured 开关与旧的 groups 分组已随 2026-10-08 payload 瘦身删除 ——
    # data.js 不再带 groups/view_groups（S9 前端不读分组键）。


class AllShardsTest(unittest.TestCase):
    """板块完整列表的分片（2026-10-08，分类页加载 20 秒）。

    锁三件事：
      ① 分片顺序 == 板块排序（顺序就是分片的物理顺序，前端不再需要 section_order）
      ② 第 0 片带 total/shards/agg（只加载一片就能给出精确的「共 N 条」）
      ③ agg 计数与「暴力筛一遍」完全对得上（前端判据的服务端镜像，不能漂）
    """

    now = datetime(2026, 9, 21, 12, 0, tzinfo=classify.zone("Asia/Shanghai"))

    @staticmethod
    def card(game_id, appid, *, ago=1, cut=50, count=500, live=True,
             upcoming=False, low="new", tier=None):
        views = [k for k, ok in (("active", live), ("upcoming", upcoming)) if ok]
        return {
            "game_id": game_id, "title": game_id, "title_zh": game_id, "appid": appid,
            "price_int": 100, "price_text": "¥1.00", "regular_int": 200,
            "regular_text": "¥2.00", "cut": cut, "low_class": low,
            "start_days_ago": ago, "start_text": "2026-09-20 10:00",
            "days_left": 3 if live else 0, "days_tier": "soon", "rate_tier": "mid",
            "last_low_text": None, "last_low_date": None, "expiry_text": "2026-09-28 10:00",
            "reviews": {"score": 90, "count": count} if count else None,
            "tier": tier or classify.TIER_QUALITY, "views": views,
            "sections": [], "compare": [],
        }

    def cards(self):
        """四张性质不同的卡，让每个板块都不空。"""
        out = [
            self.card("a", 1, ago=0, cut=95, count=50000, upcoming=True),
            self.card("b", 2, ago=1, cut=80, count=10000, low="tie"),
            self.card("c", 3, ago=3, cut=50, count=500, live=False),
            self.card("d", 4, ago=None, cut=40, count=0),
        ]
        for c in out:
            c["sections"] = report.section_keys(c, CFG)
        return out

    def test_filter_dim_values_matches_filter_specs(self):
        """预聚合表的查表键必须和筛选面板的选项**逐项一致** —— 那边加/改一个档位
        这边没跟上，用户选到那个档位就会查不到、退回已加载条数。"""
        specs = {g["key"]: [o["value"] for o in g["options"]]
                 for g in report.filter_specs(CFG)}
        dims = report.filter_dim_values(CFG)
        for key in ("date", "cut", "reviews", "only_new"):
            self.assertEqual(dims[key], specs[key], f"维度 {key} 与筛选选项不一致")

    def test_shard_order_equals_section_order(self):
        """顺序 = 分片的物理顺序：每个板块的分片拼起来，必须等于该板块自己的排序。"""
        cards = self.cards()
        orders = report.all_section_orders(cards, CFG)
        for key in ("new_low", "expiring", "popular", "big_cut"):
            expected = sorted(report._section_members(key, cards, CFG),
                              key=report._section_sort(key, CFG))
            self.assertEqual([c["appid"] for c in orders[key]],
                             [c["appid"] for c in expected], f"板块 {key} 顺序不符")

    def test_shards_on_disk_preserve_order_and_meta(self):
        """落盘的分片：顺序不变、第 0 片带 total/shards/agg。"""
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        report.render(dict(CFG, output_dir=str(out)), [], _STATS, self.now,
                      all_cards=self.cards())
        shards = _load_shards(out)
        orders = report.all_section_orders(self.cards(), CFG)
        for key, expected in orders.items():
            self.assertEqual([c["appid"] for c in shards[key]],
                             [c["appid"] for c in expected], f"板块 {key} 分片顺序不符")
        first = json.loads(
            (out / "all" / "__all___0.js").read_text(encoding="utf-8")
            .split("] = ", 1)[1].rstrip().rstrip(";"))
        self.assertEqual(first["total"], len(orders["__all__"]))
        self.assertEqual(first["shards"], 1)
        self.assertEqual(first["size"], report.ALL_SHARD_SIZE)
        self.assertIn("counts", first["agg"])
        self.assertEqual(len(first["agg"]["counts"]),
                         len(report.filter_dim_values(CFG)["date"])
                         * len(report.filter_dim_values(CFG)["cut"])
                         * len(report.filter_dim_values(CFG)["reviews"])
                         * len(report.filter_dim_values(CFG)["only_new"]))

    def test_agg_counts_match_brute_force(self):
        """agg 的每一个组合都必须和「照 app.js 判据暴力筛一遍」一致。

        判据镜像自 app.js 的 dateOk / filterOk / liveOk + cardsFor 的板块分支：
          · 四板块：sections 命中 + liveOk +（除「即将到期」外）dateOk + filterOk
          · 全部折扣：dateOk + filterOk（不叠加 liveOk，本就要看全量）
        """
        cards = self.cards()
        dims = report.filter_dim_values(CFG)

        def brute(members, key):
            result = {}
            for d in dims["date"]:
                for cu in dims["cut"]:
                    for rv in dims["reviews"]:
                        for on in dims["only_new"]:
                            n = 0
                            for c in members:
                                live = (not c["views"]) or ("active" in c["views"])
                                if key != "__all__" and not live:
                                    continue
                                if key != "expiring":
                                    ago = c["start_days_ago"]
                                    if d == "all":
                                        pass
                                    elif ago is None:
                                        continue
                                    elif d.startswith("d"):
                                        if ago > int(d[1:]):
                                            continue
                                    elif ago != int(d):
                                        continue
                                if cu != "all" and (c["cut"] or 0) < int(cu):
                                    continue
                                if rv != "all" and report.review_count(c) < int(rv):
                                    continue
                                if on == "new" and c["low_class"] != "new":
                                    continue
                                n += 1
                            result["|".join((d, cu, rv, on))] = n
            return result

        for key, members in report.all_section_orders(cards, CFG).items():
            self.assertEqual(report.section_agg(members, CFG, key)["counts"],
                             brute(members, key), f"板块 {key} 预聚合计数不符")



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

    def test_section_preview_slots_dynamic(self):
        """**栏位统一、随池量动态**（2026-10-08 用户定案）：四板块共用同一个
        预览条数 K = clamp(最小池量, 最低栏位 5, 上限 10) —— 平时与节日之间
        自动调整，个别板块池子不足 K 时如实显示池量（不凑数）。

        （大促时三板块共用 featured_score 曾让热门/大额折扣头 10 逐项相同 ——
          排序已分语义，见 test_section_sort_follows_board_semantics。）"""
        cfg = dict(self.CFG, home_section_preview=10)

        def pool(n_new, n_pop, n_big, n_exp):
            cards = ([self.card(f"新{i}", count=200) for i in range(n_new)]
                     + [self.card(f"热{i}", count=20000) for i in range(n_pop)]
                     + [self.card(f"折{i}", cut=90, count=200) for i in range(n_big)]
                     + [self.card(f"临{i}", count=200, upcoming=True) for i in range(n_exp)])
            return {s["key"]: s for s in report.build_sections(cards, cfg)}

        # 平时：最小池量 3 → K = 5；不足 K 的板块（热门 3、临期 2）如实显示池量
        got = pool(8, 3, 12, 2)
        self.assertEqual([len(got[k]["items"]) for k in ("new_low", "popular", "big_cut", "expiring")],
                         [5, 3, 5, 2])
        # 节日：各池都 ≥ 上限 → K = 10 满额
        got = pool(12, 10, 12, 10)
        self.assertEqual([len(got[k]["items"]) for k in ("new_low", "popular", "big_cut", "expiring")],
                         [10, 10, 10, 10])

    def test_pick_page_sent_via_payload(self):
        """大卡「一页几张」由服务端下发（payload `pick_page`），前端 app.js 只读
        不抄 —— 双源口径（服务端整数页取数 ↔ 前端一排最多几张）经它对齐。"""
        out = _render([], datetime(2026, 10, 6, 5, 14))
        self.assertEqual(_load_payload(out)["pick_page"], report.HOME_PICKS_PAGE)

    def test_big_cut_sorted_by_discount_desc(self):
        got = self.sections([self.card("低", cut=85), self.card("高", cut=95)])
        self.assertEqual([i["title"] for i in got["big_cut"]["items"]], ["高", "低"])

    def test_section_sort_follows_board_semantics(self):
        """**行为断言（2026-10-08 重定）**：板块排序跟着板块语义走，不再三个板块
        共用一套推荐公式 —— 大促时名气头部全是 ≥80% 折扣，共用公式曾让「热门」与
        「大额折扣」的头 10 张逐项相同（实测），两个板块等于一个板块。

        · 热门 = 评价数降序（名气榜）；平手比好评率。
        · 大额折扣 = 折扣降序（力度榜）；平手比评价数。
        · 新史低 = 推荐公式（名气优先），见 test_section_order_uses_featured_score。
        """
        pool = [
            {"title": "名气大折浅", "low_class": "new", "cut": 30,
             "reviews": {"score": 90, "count": 500000}, "start_days_ago": 1},
            {"title": "名气小折深", "low_class": "new", "cut": 95,
             "reviews": {"score": 90, "count": 200}, "start_days_ago": 1},
        ]
        pop = sorted(pool, key=report._section_sort("popular", self.CFG))
        self.assertEqual([c["title"] for c in pop], ["名气大折浅", "名气小折深"],
                         "热门按评价数：50 万评压过 95% 折扣的小游戏")
        big = sorted(pool, key=report._section_sort("big_cut", self.CFG))
        self.assertEqual([c["title"] for c in big], ["名气小折深", "名气大折浅"],
                         "大额折扣按折扣：95% 压过 30%")
        # 平手键：折扣相同比评价数；评价数相同比好评率；都相同比 title（deterministic）
        tie = [
            {"title": "乙", "low_class": "new", "cut": 90,
             "reviews": {"score": 80, "count": 30000}, "start_days_ago": 1},
            {"title": "甲", "low_class": "new", "cut": 90,
             "reviews": {"score": 95, "count": 30000}, "start_days_ago": 1},
        ]
        self.assertEqual([c["title"] for c in sorted(tie, key=report._section_sort("big_cut", self.CFG))],
                         ["乙", "甲"], "折扣同 → 评价数多的在前")
        self.assertEqual([c["title"] for c in sorted(tie, key=report._section_sort("popular", self.CFG))],
                         ["甲", "乙"], "评价数同 → 好评率高的在前")

    def test_picks_include_tie_but_not_expired_nor_unknown(self):
        """推荐位池子 = **新史低 + 平史低**（用户 2026-10-07：「大卡不止新史低，
        平史低也有机会进大卡」），但**必须还在折扣期内**、且史低类型已知。

        前置门槛（好评率 ≥70% 且 评价数 ≥100）照旧 —— 所以这里带上评价数。
        ⚠️ **史低类型不参与打分**（用户同一轮：「别改大卡的公式…你测一下效果就行了」），
        所以这里只断言「谁在池子里」，不断言两类之间的先后顺序。
        """
        picks = report.pick_top([self.card("A", count=1000),
                                 self.card("B", count=1000, live=False),
                                 self.card("C", count=1000, low="tie"),
                                 self.card("D", count=1000, low="unknown")], self.CFG)
        self.assertEqual(sorted(i["title"] for i in picks), ["A", "C"],
                         "平史低进池子、过期与未知类型不进")

    def test_picks_pool_low_classes(self):
        """池子口径与**分层顺序**写成常量，改它要连着改测试 ——
        顺序反了（先平后新）90% off 的老 3A 会把新史低全挤掉。"""
        self.assertEqual(list(report.PICK_LOW_CLASSES), ["new", "tie"])

    def test_filter_implied_flags(self):
        """筛选菜单里「选了也不会变」的选项要标 `implied`（用户 2026-10-07：
        「新史低里面还能再选新史低选项，大额折扣里面还能选折扣降序，置灰」）。

        对照表（依据 = 各板块的口径 `_in_section` 与默认顺序 `_section_sort`）：
          新史低：全是新史低 → 仅新史低无效；板块顺序在该板块内等价折扣降序 → 那个排序无效
          大额折扣：已要求折扣 ≥ big_cut → 折扣区间 ≤ big_cut 的档位无效（含同级）
          热门游戏：已要求评价数 ≥ notable → 好评数量 ≤ notable 的档位无效
        """
        groups = {g["key"]: g for g in report.filter_specs(self.CFG)}
        val = lambda g: {o["value"]: o.get("implied") for o in groups[g]["options"]}

        self.assertEqual(val("only_new")["new"], ["new_low"])
        # 2026-10-08 起板块排序回到各自维度：大额折扣板块「精选」== 折扣降序
        # ⇒ 该板块内选「折扣降序」不改变顺序，重新被隐含；其余板块仍有区分度。
        self.assertEqual(val("sort")["cut"], ["big_cut"])
        self.assertIsNone(val("sort")["rate"], "好评率降序在热门板块之外仍有区分度")
        self.assertIsNone(val("sort")["price"])
        self.assertIsNone(val("sort")["featured"])
        self.assertEqual(val("cut")["50"], ["big_cut"])
        self.assertEqual(val("cut")["80"], ["big_cut"])      # 与板块下限同档 = 筛不掉
        self.assertEqual(val("cut")["90"], None)             # 比下限高 = 有区分度
        self.assertEqual(val("reviews")["500"], ["popular"])
        self.assertEqual(val("reviews")["5000"], ["popular"])
        self.assertEqual(val("reviews")["10000"], ["popular"])
        # 「全部 / 不限」是复位键，永远不会被隐含（排序组没有这一项，跳过）
        for g in ("cut", "reviews", "only_new"):
            self.assertIsNone(val(g)["all"], g)

    def test_filter_implied_follows_config(self):
        """implied 跟着配置走：把「大额折扣」的线降到 50 后，≥50 就成了同级（被隐含）、
        而比新下限高的 ≥90 依旧有区分度（不被隐含）。"""
        cfg = dict(self.CFG, big_cut_percent=50)
        groups = {g["key"]: g for g in report.filter_specs(cfg)}
        val = {o["value"]: o.get("implied") for o in groups["cut"]["options"]}
        self.assertEqual(val["50"], ["big_cut"])
        self.assertIsNone(val["90"])

    def test_section_order_uses_featured_score(self):
        """**行为断言**：新史低板块的「精选」顺序 = 推荐公式（featured_score）降序。

        名气优先是「新史低」板块与列表精选共同的口径（refs §10.2；⚠️ 顶部大卡
        2026-10-09 起已拆到折扣优先档，见 test_picks_order_differs_from_new_low_section）：
        高名气 50% 折扣的游戏要排在低名气 90% 折扣的游戏前面（改 `_section_sort` 键序时这里会红）。"""
        pool = [
            {"title": "大作小折", "low_class": "new", "cut": 50, "price_int": 100,
             "reviews": {"score": 90, "count": 200000}, "start_days_ago": 1, "days_left": 5},
            {"title": "小作大折", "low_class": "new", "cut": 90, "price_int": 20,
             "reviews": {"score": 90, "count": 150}, "start_days_ago": 1, "days_left": 5},
        ]
        ordered = sorted(pool, key=report._section_sort("new_low", self.CFG))
        self.assertEqual([c["title"] for c in ordered], ["大作小折", "小作大折"])

    def test_featured_score_same_formula_as_recommend(self):
        """featured_score 与 recommend_score 用**同一套公式**（只是不打门槛）——
        满足门槛的卡片两者必须完全相等，保证列表精选与「新史低」板块口径一致
        （⚠️ 顶部大卡 2026-10-09 起改用 PICKS_WEIGHTS，不在这个等式里）。"""
        card = {"title": "G", "cut": 80, "days_left": 2, "start_days_ago": 1,
                "reviews": {"score": 88, "count": 12000}}
        self.assertAlmostEqual(report.featured_score(card, self.CFG),
                               report.recommend_score(card, self.CFG), places=9)

    def test_featured_score_has_no_gate(self):
        """featured_score **不打门槛**：详情待补（无评价数）的条目也有分
        （只剩「折扣」一项计分、按剩余权重归一），不能像大卡那样返回 None。"""
        card = {"title": "无详情", "cut": 90, "reviews": None, "days_left": 5}
        self.assertIsNotNone(report.featured_score(card, self.CFG))
        self.assertIsNone(report.recommend_score(card, self.CFG))

    def test_picks_prefer_new_over_higher_scoring_tie(self):
        """**优先新史低**：哪怕平史低的分高得多，也要排在新史低后面
        （用户 2026-10-07：「优先新史低，没有才显示平史低」）。"""
        picks = report.pick_top([self.card("新", count=200),
                                 self.card("平", count=500000, low="tie")], self.CFG)
        self.assertEqual(picks[0]["title"], "新", "分再高也排在后面")
        self.assertEqual([i["title"] for i in picks], ["新", "平"])

    def test_picks_default_cap_is_fifteen(self):
        """默认上限 = **三页 15 张**（2026-10-08 用户澄清：最低 5、最多 15、
        新史低为主、没有才替补平史低、不强行凑 15 —— 上一版默认 5 导致
        **大促也只有 5 张**，`home_picks` 配置从未设置）。新史低 9 张 →
        2 整页（10 张），末页用平史低补满。"""
        cfg = {k: v for k, v in self.CFG.items() if k != "home_picks"}
        cards = ([self.card(f"新{i}", count=200) for i in range(9)]
                 + [self.card(f"平{i}", count=200, low="tie") for i in range(3)])
        picks = report.pick_top(cards, cfg)
        self.assertEqual(len(picks), 10)
        self.assertEqual([i["title"] for i in picks],
                         [f"新{i}" for i in range(9)] + ["平0"])

    def test_picks_floor_is_one_page(self):
        """最低一页 5 张：新史低只有 3 张时也要放满一页（平史低补位）——
        「最低展示五个」。新史低 + 平史低都不足 5 时如实显示池量（不凑数）。"""
        cfg = {k: v for k, v in self.CFG.items() if k != "home_picks"}
        picks = report.pick_top([self.card(f"新{i}", count=200) for i in range(3)]
                                + [self.card(f"平{i}", count=200, low="tie")
                                   for i in range(4)], cfg)
        self.assertEqual(len(picks), 5)
        self.assertEqual(sum(1 for p in picks if p["low_class"] == "new"), 3)

    def test_picks_count_rounds_to_whole_pages_when_cap_raised(self):
        """把上限调大（15）时按**整数页**取：新史低 7 张 → 2 页（10 张），
        余下的用平史低补满，而不是硬凑 15。"""
        cfg = dict(self.CFG, home_picks=15)
        cards = ([self.card(f"新{i}", count=200) for i in range(7)]
                 + [self.card(f"平{i}", count=200, low="tie") for i in range(9)])
        picks = report.pick_top(cards, cfg)
        self.assertEqual(len(picks), 10)
        self.assertEqual([i["title"] for i in picks], [f"新{i}" for i in range(7)]
                         + ["平0", "平1", "平2"])

    def test_picks_order_differs_from_new_low_section(self):
        """大卡与「新史低」板块**不再同序**（2026-10-09 用户定案拆开）：
        大卡用折扣优先档（:data:`PICKS_WEIGHTS`），板块保持名气优先 ——
        「大名气浅折」排板块前面、「小名气深折」排大卡前面，两边正好相反。"""
        cards = [self.card("大作小折", count=200_000, cut=30),
                 self.card("小作大折", count=100, cut=95)]
        picks = report.pick_top(cards, self.CFG)
        section = report._section_sort("new_low", self.CFG)
        self.assertEqual([c["title"] for c in picks], ["小作大折", "大作小折"])
        self.assertEqual([c["title"] for c in sorted(cards, key=section)],
                         ["大作小折", "小作大折"])

    def test_picks_weights_config_override_and_no_low_item(self):
        """``picks_weights`` 配置可覆盖（缺项沿用 :data:`PICKS_WEIGHTS`、未知项
        忽略）；⚠️ 同样**不许有「史低类型」项** —— 分层取已保证新史低优先，
        往权重表里加 low 是重复计分（RECOMMEND_WEIGHTS 同款红线）。"""
        self.assertNotIn("low", report.PICKS_WEIGHTS)
        w = report.picks_weights({"picks_weights": {"cut": 99, "bogus": 5}})
        self.assertEqual(w["cut"], 99.0)
        self.assertEqual(w["fame"], report.PICKS_WEIGHTS["fame"])
        self.assertNotIn("bogus", w)

    def test_picks_gap_weight_ranks_older_low_first(self):
        """大卡档 2026-10-09 新增「间隔」10 分：其余条件相同时，距上次史低更久的
        排前面。锁住 ``PICKS_WEIGHTS["gap"] == 10`` 且 gap 真的进分（不是只写在
        注释/文档里）。这里用**平史低**卡（取上次同价）；新史低同样有该值
        （取史低期记忆，冷启动未建立时才有可能是 None）。"""
        self.assertEqual(report.picks_weights({})["gap"], 10)
        pw = report.picks_weights(self.CFG)

        def tie(title, gap):
            return {"title": title, "title_zh": title, "cut": 50,
                    "low_class": "tie", "start_days_ago": 1, "days_left": 3,
                    "reviews": {"score": 90, "count": 500},
                    "last_low_days": gap, "views": ["active"]}

        ordered = sorted([tie("近", 5), tie("久", 300)],
                         key=lambda c: report.recommend_sort_key(c, self.CFG, weights=pw))
        self.assertEqual([c["title"] for c in ordered], ["久", "近"])

    def test_recommend_weights_have_no_low_item(self):
        """权重表里**没有**「史低类型」这一项 —— 用户 2026-10-07 明确要求把它删掉
        （「别改大卡的公式，你怎么乱动公式」，有史低项的是 refs §10.2 的**订阅端版**）。"""
        self.assertNotIn("low", report.RECOMMEND_WEIGHTS)
        self.assertFalse(hasattr(report, "RECOMMEND_LOW_PARTS"))


class RecommendScoreTest(unittest.TestCase):
    """refs.md §10.2 权重公式 v2（轮播版，名气优先）——
    现为**「新史低」板块 / 列表精选**的口径（顶部大卡 2026-10-09 起拆到 `PICKS_WEIGHTS`）。

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

    def test_gap_has_no_weight_by_default(self):
        """**轮播版默认不再给「间隔」权重**（2026-10-07）：池子里大量条目没有这个数
        （新史低在 2026-10-09 前一律无值；平史低才有）→ 那 15 分时常不参与打分（总分按
        剩余权重归一化），等于把名气/折扣悄悄放大，和文档写的权重对不上（用户：「怎么
        感觉排出来结果有点不同」）。所以默认权重里 gap = 0，有/没有间隔数据的结果**完全一样**。"""
        self.assertEqual(report.recommend_weights({})["gap"], 0)
        self.assertEqual(report.recommend_score(self.card("有间隔", gap=300)),
                         report.recommend_score(self.card("没间隔", gap=None)))

    def test_missing_gap_is_normalized_when_config_asks_for_it(self):
        """谁要是把间隔权重配回来，缺数据的项仍然**不计分**、总分按剩余权重归一化 ——
        不能因为「数据更全」就系统性吃亏，也不能缺项就当 0 分。"""
        cfg = {"recommend_weights": {"gap": 20}}
        no_gap = report.recommend_score(self.card("没间隔", gap=None), cfg)
        zero_gap = report.recommend_score(self.card("间隔 0 天", gap=0), cfg)
        self.assertGreater(no_gap, zero_gap)     # 缺 ≠ 0 分
        best = report.recommend_score(self.card("满分", cut=95, count=200_000,
                                                rate=95, days_left=0, ago=0, gap=None), cfg)
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
        # ⚠️ 用键值断言而不是整个 dict：选项上还挂着 `implied` / `implied_note`
        #    （见 test_implied_flags），逐键比对才不会被新增字段打破。
        cut75 = next(o for o in groups["cut"]["options"] if o["value"] == "75")
        self.assertEqual(cut75["label"], "≥ 75%")
        rev = next(o for o in groups["reviews"]["options"] if o["value"] == "50000")
        self.assertEqual(rev["label"], "≥ 50,000")
        self.assertIn({"value": "d3", "label": "近 3 天", "disabled": False},
                      groups["date"]["options"])

    def test_cut_options_have_no_70(self):
        """折扣区间**没有 ≥70%**（用户 2026-10-07：「折扣区间去掉 70%」）——
        它夹在 50 与 80 之间、区分度最低。留下的三档是 50 / big_cut（80）/ 90。"""
        values = [o["value"] for o in
                  {g["key"]: g for g in report.filter_specs(self.CFG)}["cut"]["options"]]
        self.assertEqual(values, ["all", "50", "80", "90"])
        self.assertNotIn("70", values)

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
        # 「完成」按钮已删（选项点一下就生效，再来一个「完成」是重复）
        self.assertNotIn('id="filter-done"', html)

    def test_empty_days_are_disabled(self):
        """refs §11.5 Q2：没有数据的天数**置灰不可选**。

        池子里只有"今天"（ago=0）与"5 天前"两种开始时间 →
        「昨天」「前天」必须被置灰，而「今天」「近 7 天」「全部」不能置灰。
        """
        cards = [{"start_days_ago": 0}, {"start_days_ago": 5}]
        groups = {g["key"]: g for g in report.filter_specs(self.CFG, cards)}
        date_by_value = {o["value"]: o for o in groups["date"]["options"]}
        self.assertTrue(date_by_value["0"]["disabled"] is False)
        self.assertTrue(date_by_value["1"]["disabled"])     # 昨天：没数据
        self.assertTrue(date_by_value["2"]["disabled"])     # 前天：没数据
        self.assertFalse(date_by_value["d7"]["disabled"])   # 近 7 天：有 0 和 5
        self.assertFalse(date_by_value["all"]["disabled"])

    def test_no_cards_means_nothing_disabled(self):
        """没给池子（cards=None）就不置灰 —— 宁可全可选，别把有数据的天误置灰。"""
        groups = {g["key"]: g for g in report.filter_specs(self.CFG)}
        for opt in groups["date"]["options"]:
            self.assertFalse(opt.get("disabled"), opt["value"])

    def test_section_specs_have_no_hardcoded_criteria(self):
        """板块不再带写死的「入组条件」文案 —— 阈值写死 = 改了配置页面还在撒谎，
        而且前端从来没渲染过它（review-s9-01 补充审查 #1）。"""
        for spec in report.HOME_SECTIONS:
            self.assertNotIn("criteria", spec)
        sections = report.build_sections(
            [{"low_class": "new", "start_days_ago": 1, "views": ["active"],
              "reviews": {"score": 90, "count": 1000}}], self.CFG)
        for sec in sections:
            self.assertNotIn("criteria", sec)


class AutoescapeGuardTest(unittest.TestCase):
    """转义守卫（code-audit-2026-10-09 第一节 #1）。

    病根：Environment 用 ``select_autoescape(["html"])``，而模板名是 ``*.html.j2`` ——
    jinja2 按「扩展名结尾」匹配，``.j2`` 不以 ``.html`` 结尾 → **autoescape 全程为 False**。
    这里往**服务端渲染的插值**里塞一段 ``<script>``，锁死它必须被转义成实体；
    否则将来任何人把 API 侧字符串（游戏标题 / 错误信息）插进模板就是存储型 XSS。
    """

    def test_server_rendered_values_are_escaped(self):
        import tempfile

        evil = "<script>alert(1)</script>"
        out = Path(tempfile.mkdtemp(prefix="sdl-esc-"))
        # 两个页面各插一处：about 页走服务端渲染的 overview.sweep；
        # index 页走页脚链接 site_repo_url（两页都要锁，见可自动化检查项 #1）。
        report.render(dict(CFG, output_dir=str(out), site_repo_url=evil), [],
                      dict(_STATS, sweep=evil), datetime(2026, 10, 6, 5, 14))
        for name in ("index.html", "about.html"):
            html = (out / name).read_text(encoding="utf-8")
            self.assertNotIn(evil, html, name)
            self.assertIn("&lt;script&gt;", html, name)


class SectionRegistryGuardTest(unittest.TestCase):
    """板块口径单源（code-audit-2026-10-09 #5 + 可自动化检查项 #4）。

    「加/改一个板块」从前要散着改：HOME_SECTIONS 条目 + `_in_section` +
    `_section_sort` + `_section_members`（日期窗口）+ `section_agg` 的 expiring 位，
    前端还要改 app.js 的板块 key 清单。现在判据 / 排序 / label / 日期窗口都出自
    `report.SECTIONS` 一处，nav 与前端清单由它派生 —— 这条测试锁死这个单源。
    """

    def test_registry_shape_and_home_sections_share_source(self):
        for spec in report.SECTIONS:
            for field in ("key", "label", "match", "sort", "date_window"):
                self.assertIn(field, spec, spec["key"])
        self.assertEqual([s["key"] for s in report.HOME_SECTIONS],
                         [s["key"] for s in report.SECTIONS])
        self.assertEqual([s["label"] for s in report.HOME_SECTIONS],
                         [s["label"] for s in report.SECTIONS])

    def test_nav_is_derived_from_registry(self):
        html = (_render([], datetime(2026, 10, 6, 5, 14)) / "index.html").read_text(
            encoding="utf-8")
        for spec in report.SECTIONS:
            self.assertIn('data-section="%s"' % spec["key"], html)
            self.assertIn(">%s<" % spec["label"], html)
        self.assertIn('data-section="__all__"', html)
        self.assertIn("全部折扣", html)

    def test_app_js_hardcodes_no_section_labels_or_key_list(self):
        js = (Path(report.TEMPLATES_DIR) / "static" / "app.js").read_text(encoding="utf-8")
        # 先剥注释再扫 —— 注释里可以自由提到板块名，不该触发
        code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        for spec in report.SECTIONS:
            self.assertNotIn('"%s"' % spec["label"], code, spec["key"])
            self.assertNotIn('"%s"' % spec["key"], code, spec["key"])
        # 「全部折扣」这一个常量 label 仍允许（伪板块，无服务端来源）
        self.assertIn('__all__: "全部折扣"', code)

    def test_implied_filter_keys_are_real_sections(self):
        """filter_specs 里 `implied` 引用的板块 key 必须真实存在。

        `implied` 是 #5 收敛后**仅存的一处板块 key 字面量**（filter_specs 组装
        「本板块已隐含」的筛选项）—— 改板块 key 时它会静默失配，这条把它锁住。
        """
        keys = {s["key"] for s in report.SECTIONS} | {"__all__"}
        cards = [{"low_class": "new", "start_days_ago": 1, "cut": 90,
                  "reviews": {"score": 90, "count": 20000}, "views": ["active"]}]
        seen = 0
        for group in report.filter_specs(CFG, cards):
            for opt in group["options"]:
                for key in (opt.get("implied") or []):
                    self.assertIn(key, keys, (group["key"], opt["value"]))
                    seen += 1
        # 空循环恒真 = 没锁（smoke_s9 钉过的反模式）：`implied` 一个都没扫到就说明
        # filter_specs 改了形状、这条守卫已经悄悄失效。
        self.assertGreater(seen, 0, "没扫到任何 implied —— 守卫已失效（filter_specs 形状变了？）")


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


class CardTierColourTest(unittest.TestCase):
    """S9-卡片：好评率与剩余天数的**配色档**（用户 2026-10-07 原话：「60 好评和 90 好评
    是一个颜色，剩余 7 天和剩余 2 天也是一个颜色」）。

    档位在服务端算（阈值跟着配置走），前端只挂 `rate-*` / `days-*` 类名 ——
    所以这里钉的是「边界值 + 跟配置联动」，DOM 那侧由 jsdom 冒烟测钉。
    """

    NOW = datetime(2026, 10, 6, 20, 0, tzinfo=classify.zone("Asia/Shanghai"))

    def _card(self, cfg: dict | None = None, **kw) -> dict:
        entry = {"game_id": "u", "title": "X", "price_int": 100, "currency": "CNY",
                 "expiry": "2026-10-08T01:00:00+08:00", "tier": classify.TIER_QUALITY}
        entry.update(kw)
        return report.build_card(entry, self.NOW, report.tier_labels(CFG), cfg or CFG)

    def test_rate_tier_four_levels(self):
        """Steam 商店口径（用户 2026-10-07）：≥90 high · 70~89 ok · 40~69 mid · <40 low。"""
        for score, expect in ((95, "high"), (90, "high"), (89, "ok"), (70, "ok"),
                              (69, "mid"), (40, "mid"), (39, "low"), (0, "low")):
            card = self._card(reviews={"score": score, "count": 500})
            self.assertEqual(card["rate_tier"], expect, f"好评率 {score}%")

    def test_rate_tier_has_no_value_without_reviews(self):
        self.assertIsNone(self._card()["rate_tier"])

    def test_rate_tier_follows_config(self):
        """阈值来自配置：把「高口碑线」降到 80%、「展示门槛」降到 60% 后分档跟着变。"""
        cfg = dict(CFG, good_positive_ratio=0.8, min_positive_ratio=0.6)
        self.assertEqual(self._card(cfg, reviews={"score": 85, "count": 500})["rate_tier"], "high")
        self.assertEqual(self._card(cfg, reviews={"score": 65, "count": 500})["rate_tier"], "ok")
        self.assertEqual(self._card(cfg, reviews={"score": 55, "count": 500})["rate_tier"], "mid")
        self.assertEqual(self._card(cfg, reviews={"score": 30, "count": 500})["rate_tier"], "low")

    def test_rate_tier_bad_line_follows_config(self):
        """「差评」线也可配（默认 40%）：调到 50% 后 45% 就从 mid 掉进 low。"""
        cfg = dict(CFG, bad_positive_ratio=0.5)
        self.assertEqual(self._card(cfg, reviews={"score": 45, "count": 500})["rate_tier"], "low")
        self.assertEqual(self._card(cfg, reviews={"score": 55, "count": 500})["rate_tier"], "mid")

    def test_days_tier_four_levels(self):
        """2026-10-07 起「今天结束」（0 天）单独一档：0 final · ≤2 urgent · ≤6 soon · ≥7 later。"""
        for days, expect in ((0, "final"), (-1, "final"), (1, "urgent"), (2, "urgent"),
                             (3, "soon"), (6, "soon"), (7, "later"), (30, "later")):
            self.assertEqual(report.days_tier(days, CFG), expect, f"剩 {days} 天")

    def test_days_tier_bounds_come_from_config(self):
        """urgent 边界 = 即将过期窗口（48h → 2 天）；later 边界 = 三板块时间窗（7 天）。"""
        cfg = {"upcoming_expiry_hours": 72, "home_new_low_days": 14}
        self.assertEqual(report.days_tier(0, cfg), "final")      # 「今天结束」永远单独一档
        self.assertEqual(report.days_tier(3, cfg), "urgent")     # 72h 窗口 → 3 天内都算急
        self.assertEqual(report.days_tier(4, cfg), "soon")
        self.assertEqual(report.days_tier(13, cfg), "soon")     # 窗口放宽到 14 天
        self.assertEqual(report.days_tier(14, cfg), "later")

    def test_days_tier_none_when_unknown(self):
        self.assertIsNone(report.days_tier(None, CFG))

    def test_card_carries_both_tiers(self):
        card = self._card(reviews={"score": 93, "count": 500})
        self.assertEqual(card["rate_tier"], "high")
        self.assertEqual(card["days_tier"], "urgent")   # 10-08 01:00 收摊按 10-07 结束 → 1 天
        # 服务端的 `reviews_text` 已删：前端要给「93%」单独上色，字符串在两边各拼一份会分叉
        self.assertNotIn("reviews_text", card)

    def test_mid_tier_notable_card_enters_other_sections(self):
        """「褒贬不一 / 差评」并非只出现在热门板块（2026-10-07 review 纠正的口径）：

        mid/low 只可能由「高热度 · 口碑不一」组（评 ≥ notable，不看好评率）的卡产生，
        但那张卡同时是新史低时**照进「新史低」板块**（好感分档不硬砍，COD 类大作
        踩新史低必须上榜）—— 配色档跟着卡走，在任何板块都要能认出来。
        这条钉死「不硬砍」：谁把低口碑卡从非热门板块里剔掉，这里就红。
        """
        card = self._card(reviews={"score": 55, "count": 20000},
                          tier=classify.TIER_NOTABLE,
                          flag="N", store_low_int=80,
                          start="2026-10-05T20:00:00+08:00")
        self.assertEqual(card["rate_tier"], "mid")
        self.assertEqual(card["low_class"], classify.STEAM_LOW_NEW)
        by_key = {s["key"]: s for s in report.build_sections([card], CFG)}
        self.assertGreater(by_key["new_low"]["count"], 0)
        self.assertEqual(by_key["new_low"]["items"][0]["rate_tier"], "mid")
        # 「热门游戏」板块不看好评率，同一张卡也该在（评 ≥10000）
        self.assertGreater(by_key["popular"]["count"], 0)


class MessageBarTest(unittest.TestCase):
    """S9-3 顶部消息区（refs.md §4 A-3 / B2）：节日条 + 一行提示，最多两行。

    钉四件事：① 有正在进行的活动才渲染那一行、且带上季节主题色类名；
    ② **没内容整块不显示**（用户口径：「没活动时：不显示」）；
    ③ 站点通知走第二行的 `data-notice`，陈旧告警的优先级在 app.js 里判（这里只保证文案到位）；
    ④ 陈旧告警的两档阈值**从配置来**，别在前端写死。
    """

    NOW = datetime(2026, 10, 6, 20, 0, tzinfo=classify.zone("Asia/Shanghai"))

    def _content(self, payload: dict) -> Path:
        import tempfile
        path = Path(tempfile.mkdtemp(prefix="sdl-ann-")) / "announcements.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _render_with(self, content, *, cfg_extra: dict | None = None) -> Path:
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="sdl-test-"))
        cfg = dict(CFG, output_dir=str(out), announcements_path=str(content))
        cfg.update(cfg_extra or {})
        report.render(cfg, [], _STATS, self.NOW)
        return out

    def test_festival_row_rendered_with_season_colour(self):
        content = self._content({"festivals": [{
            "name": "Steam 秋季特卖", "kind": "season",
            "start": "2026-10-01 01:00", "end": "2026-10-08 01:00"}]})
        html = (self._render_with(content) / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="msg msg-fest season-autumn"', html)
        self.assertIn("Steam 秋季特卖", html)
        # 用户 2026-10-07：节日条只要「季节 + 活动名 + 进行中 + 起止 + 还有几天」，
        # 不要补充说明（原 note 字段已从数据与代码里删掉）
        self.assertNotIn("一年四大特卖之一", html)
        self.assertIn("还有 1 天", html)          # 10-08 01:00 收摊按 10-07 结束算
        self.assertNotIn('id="msgbar" hidden', html)

    def test_nothing_active_hides_the_whole_block(self):
        content = self._content({"festivals": [], "notices": []})
        html = (self._render_with(content) / "index.html").read_text(encoding="utf-8")
        self.assertIn('<div class="msgbar" id="msgbar" hidden>', html)
        self.assertNotIn("msg-fest", html)

    def test_missing_content_file_still_renders_and_hides(self):
        """坏数据不能炸报表：路径不存在时照常出页面，只是没有顶部条。"""
        out = self._render_with(Path("no/such/announcements.json"))
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="msgbar"', html)
        self.assertNotIn("msg-fest", html)
        self.assertTrue((out / "about.html").exists())

    def test_notice_text_handed_to_the_frontend(self):
        content = self._content({"notices": [{
            "text": "站点改版说明", "url": "https://example.com/note",
            "start": "2026-10-01", "end": "2026-10-31"}]})
        html = (self._render_with(content) / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-notice="站点改版说明"', html)
        self.assertIn('data-notice-url="https://example.com/note"', html)
        self.assertIn('id="msg-alert"', html)

    def test_stale_thresholds_come_from_config(self):
        payload = _load_payload(self._render_with(self._content({})))
        self.assertEqual(payload["stale_warn_hours"], 26)     # 默认
        self.assertEqual(payload["stale_banner_hours"], 36)
        payload = _load_payload(self._render_with(
            self._content({}), cfg_extra={"stale_warn_hours": 20, "stale_banner_hours": 30}))
        self.assertEqual(payload["stale_warn_hours"], 20)
        self.assertEqual(payload["stale_banner_hours"], 30)

    def test_empty_state_element_is_a_container_for_the_art(self):
        """空状态由 app.js 填「插画 + 文案」，模板里必须是容器而不是写死一句话
        （refs.md B13：三种状态都要有插画）。"""
        html = (self._render_with(self._content({})) / "index.html").read_text(encoding="utf-8")
        self.assertIn('<div id="empty" class="empty" hidden></div>', html)


if __name__ == "__main__":
    unittest.main()
