# -*- coding: utf-8 -*-
"""流水线集成测试：`run.render_pass` → `report.render` → 写出来的 data.js。

不发任何网络请求（用临时的 state + output 目录）。
存在的意义：单测只能证明各模块对，**接起来对不对**要靠这里。
之前踩过一次：分组标题改成了「好评达标」，但卡片标签还是旧的「优质」——
两个来源不一致，页面自相矛盾。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import render_pass  # noqa: E402
from src import classify, report  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 9, 22, 16, 0, tzinfo=TZ)


def deal(game_id: str, appid: int | None, cut: int, title: str) -> dict:
    return {
        "game_id": game_id,
        "title": title,
        "appid": appid,
        "type": "game",
        "price_int": 1000,
        "regular_int": 10000,
        "cut": cut,
        "currency": "CNY",
        "flag": "N",
        "low_kind": "N",
        "start": "2026-09-22T10:00:00+08:00",
        "expiry": "2026-09-29T10:00:00+08:00",
        "store_low_int": 1000,
        "history_low_int": 1000,
        "history_low_1y_int": 1000,
        "boxart": "https://x/b.jpg",
    }


class RenderPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root / "out"
        self.state = State(root / "state.json", tz=TZ).load()
        # 好评达标（好评率 ≥70% 且 评价数 ≥100）
        self.state.set_meta("g-good", 111, {"score": 85, "count": 5000}, NOW)
        # 高热度 · 口碑不一（评价数 ≥10000、好评率低）
        self.state.set_meta("g-hot", 222, {"score": 58, "count": 782040}, NOW)
        # 冷门（评价数 <100）→ 不进任何分组
        self.state.set_meta("g-cold", 333, {"score": 99, "count": 12}, NOW)
        self.state.set_title_zh("g-good", "好游戏", NOW)
        self.cfg = {
            "output_dir": str(self.out),
            "min_positive_ratio": 0.7,
            "min_review_count": 100,
            "notable_review_count": 10000,
            "absolute_min_positive_ratio": None,
            "compare_countries": ["UA", "IN"],
            "list_batch": 30,
            "list_auto_max": 300,
            "mobile_breakpoint_px": 768,
            "stale_banner_hours": 36,
            "sweep_mode": "low_only",
        }
        self.candidates = [
            deal("g-good", 111, 90, "Good Game"),
            deal("g-hot", 222, 80, "Hot Game"),
            deal("g-cold", 333, 70, "Cold Game"),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *, upcoming=None, **stats_overrides):
        stats_holder = {}

        def stats_of(info):
            stats = {
                "sweep": "low_only", "deals_fetched": 3893, "hist_low_total": 3891,
                "new_today_raw": 671, "new_today_shown": info["shown"],
                "detail_fetched": 0, "detail_targets": 971, "detail_backlog": 2816,
                "detail_pending": info["tier"].get(classify.TIER_PENDING, 0),
                "deduped_versions": len(info["deduped"]),
                "last_run_at": NOW.isoformat(timespec="seconds"),
            }
            stats.update(stats_overrides)
            stats_holder.update(stats)
            return stats

        info = render_pass(self.state, self.candidates, self.cfg, NOW, stats_of,
                           announce_merges=False, upcoming=upcoming)
        payload = json.loads(
            (self.out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";")
        )
        return info, payload

    def test_section_cards_carry_tier_labels(self):
        """原「featured 单一组」已随 2026-10-08 payload 瘦身删除（S9 只读
        sections/picks）；档位信息降级为卡片 tier_label，在板块预览卡片上
        不得丢失或漂移。"""
        _, payload = self._run()
        card_labels = {i["tier"]: i["tier_label"]
                       for s in payload["sections"] for i in s["items"]}
        self.assertEqual(card_labels.get(classify.TIER_QUALITY), "好评达标")
        self.assertEqual(card_labels.get(classify.TIER_NOTABLE), "高热度 · 口碑不一")

    def test_featured_sort_key_layers_new_first(self):
        """精选排序键 = 分层字典序：新史低在前、平史低在后（验收 §6）。
        （featured 组已删；排序键现由 latest.json 与 pick_top 兜底复用，
        板块排序用的是另一套 report.featured_score，见 _section_sort。）"""
        new_card = {"low_class": "new", "cut": 50, "title": "A"}
        tie_card = {"low_class": "tie", "cut": 90, "title": "B"}
        self.assertLess(report.featured_sort_key(new_card),
                        report.featured_sort_key(tie_card),
                        "新史低必须整体排在平史低 / 待确认之前")

    def test_no_evaluative_wording_anywhere(self):
        self._run()
        text = (self.out / "data.js").read_text(encoding="utf-8")
        self.assertNotIn("优质", text)
        self.assertNotIn("热门 · 褒贬不一", text)

    # test_featured_group_carries_criteria 已随 featured 组删除（板块口径文案
    # 的唯一来源是「关于网站」页的 criteria_notes，见 HOME_SECTIONS 注释）。

    def test_cold_game_is_not_shown(self):
        info, _ = self._run()
        self.assertEqual(info["tier"][classify.TIER_COLD], 1)
        self.assertEqual(info["shown"], 2)          # 只 好评达标 + 高热度
        self.assertEqual(info["tier"][classify.TIER_QUALITY], 1)
        self.assertEqual(info["tier"][classify.TIER_NOTABLE], 1)

    def test_cached_title_zh_applied_without_enrich(self):
        """缓存里的中文名必须在 merge_details 阶段就用上。

        回归：曾经只在 enrich 阶段读 title_zh，导致首版渲染（不跑 enrich）
        把已经有中文名的游戏退化成英文名。
        """
        info, payload = self._run()
        items = [i for s in payload["sections"] for i in s["items"]]
        good = next(i for i in items if i["game_id"] == "g-good")
        self.assertEqual(good["title_zh"], "好游戏")

    def test_compare_propagates_from_enrich_to_all_cards(self):
        """S9 回归修复（2026-10-08）：`enrich_hook` 就地写的是 `shown`/`upcoming_shown`
        那批 dict，而首页四板块/大卡/all.js 走 `render_pass` 里**另建**的 `all_cards`
        （`merge_details` 的 `item = dict(entry)` 是拷贝）——不按 appid 回灌的话比价
        在所有页面上都是空的（`tools/render_report.py` 的 `graft_compare_from_cache`
        只在本地预览补，掩盖了线上）。这条测试锁定「回灌」这一步。"""
        marker = [{"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 100,
                   "cny_minor": 80, "diff_pct": -20}]

        def enrich_hook(shown, upcoming_shown, info):
            for entry in shown + upcoming_shown:
                entry["compare"] = marker
            return {"title_fetched": 0, "title_cached": 0, "compare_batches": 2,
                    "compare_repriced": 0, "compare_fetched": 2}

        def stats_of(info):
            return {"sweep": "low_only", "new_today_raw": 3, "new_today_shown": info["shown"],
                    "deals_fetched": 0, "hist_low_total": 0,
                    "detail_pending": info["tier"].get(classify.TIER_PENDING, 0),
                    "detail_backlog": 0}

        render_pass(self.state, self.candidates, self.cfg, NOW, stats_of,
                    announce_merges=False, enrich_hook=enrich_hook,
                    all_entries=self.candidates)

        payload = json.loads(
            (self.out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";")
        )
        section_cards = [i for s in payload["sections"] for i in s["items"]]
        good = next(i for i in section_cards if i["game_id"] == "g-good")
        self.assertTrue(good["compare"], "首页板块卡片必须带上 enrich 回灌的比价行")

        all_data = json.loads(
            (self.out / "all.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";")
        )
        all_cards = [c for g in all_data["groups"] for c in g["items"]]
        good_all = next(c for c in all_cards if c["game_id"] == "g-good")
        self.assertTrue(good_all["compare"], "all.js 卡片同样要带上回灌的比价行")

    def test_estimate_compare_fills_history_entries(self):
        """2026-10-08 估算定案：真查只保证「当日新增 + 即将到期」两板块的真实性
        （每日抓取是防打折中途降价），其余板块的历史条目用「外区原价永久缓存 ×
        国区折扣比例」估算 —— **原价永久缓存就是为估算备料的**，生产路径必须补上
        （此前只在本地预览 graft 里有，改着改着丢了）。enrich_hook 不给真查结果
        ⇒ 全部走估算路径，缓存有原价的条目 compare 必须非空。"""
        self.state.set_compare_original(111, "UA", 50000, "UAH", NOW)
        self.state.set_compare_original(111, "IN", 60000, "INR", NOW)
        self.state.save(NOW)

        def stats_of(info):
            return {"sweep": "low_only", "new_today_raw": 3, "new_today_shown": info["shown"],
                    "deals_fetched": 0, "hist_low_total": 0,
                    "detail_pending": info["tier"].get(classify.TIER_PENDING, 0),
                    "detail_backlog": 0}

        render_pass(self.state, self.candidates, self.cfg, NOW, stats_of,
                    announce_merges=False, enrich_hook=None,
                    all_entries=self.candidates)

        all_data = json.loads(
            (self.out / "all.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";")
        )
        all_cards = [c for g in all_data["groups"] for c in g["items"]]
        good = next(c for c in all_cards if c["game_id"] == "g-good")
        # 国区现价 1000 / 原价 10000 → 折扣比例 0.1；UA 原价 50000 → 估算现价 5000
        # （卡片里存的是 compare_rows 格式化后的展示行）
        rows = {r["label"]: r for r in good["compare"]}
        self.assertEqual(rows["乌克兰区"]["price_text"], "₴50.00")
        self.assertEqual(rows["印度区"]["price_text"], "₹60.00")
        # 无真查 → 无汇率 → 不换算 CNY、不出差价%（如实留空，不造假数）
        self.assertIsNone(rows["乌克兰区"]["cny_text"])
        self.assertIsNone(rows["乌克兰区"]["diff_pct"])

    def test_no_conditions_block_in_payload_or_html(self):
        """批 F：筛选条件框已从页面删除，判定口径只在 README —— 别又跑回来。"""
        _, payload = self._run()
        self.assertNotIn("conditions", payload)
        self.assertNotIn("criteria_digest", payload)
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("criteria-box", html)
        self.assertEqual(payload["list"]["breakpoint"], 768)

    def test_upcoming_counted_but_not_inlined(self):
        """「即将过期」：不再内联任何视图数据（2026-10-08 —— ``view_groups`` 与
        ``views`` 按钮组都已删除；大促尾期前者 ≈ 全池，曾把 data.js 撑到 7.4MB）。
        完整列表走 all.js 的 expiring 板块；expiring.json 导出走
        ``upcoming_shown_items``（另一条测试锁定）。"""
        expiring = deal("g-expiring", 444, 90, "Expiring Game")
        expiring["expiry"] = "2026-09-23T10:00:00+08:00"  # NOW + 18h → 48h 窗口内
        self.state.set_meta("g-expiring", 444, {"score": 85, "count": 5000}, NOW)
        info, payload = self._run(upcoming=[expiring])

        self.assertNotIn("view_groups", payload)
        self.assertNotIn("groups", payload)
        self.assertNotIn("views", payload)
        self.assertEqual(info["upcoming_shown"], 1)

    def test_upcoming_shown_items_is_post_tier_deduped(self):
        """upcoming_shown_items：已合并详情、已按 appid 去重、且**已过**口碑分档（is_shown）——
        即「即将过期」视图实际进列表的条目，供 data 分支的 expiring.json 导出
        （口径单点收敛在主仓库，消费方不再自建门槛；.scratch/expiring-snapshot/changelog.md v3）。
        """
        self.state.set_meta("g-good-v2", 111, {"score": 85, "count": 5000}, NOW)
        self.state.set_meta("g-cold2", 444, {"score": 99, "count": 12}, NOW)
        upcoming = [
            deal("g-good", 111, 90, "Good Game"),
            deal("g-good-v2", 111, 80, "Good Game V2"),   # 同 appid → 去重掉
            deal("g-cold2", 444, 70, "Cold Two"),          # 冷门 → 分档滤掉，不进导出
        ]
        info, _ = self._run(upcoming=upcoming)
        self.assertIsInstance(info["upcoming_shown_items"], list)
        self.assertEqual([e["game_id"] for e in info["upcoming_shown_items"]],
                         ["g-good"])
        # upcoming_shown 必须保持数字（run_log / render_report 在用，防类型回归，changelog v3）
        self.assertIsInstance(info["upcoming_shown"], int)
        self.assertEqual(info["upcoming_shown"], 1)

    def test_upcoming_absent_when_not_produced(self):
        """不传 upcoming 时 info.upcoming_shown 为 0；payload 永远不带
        view_groups / views（数据本体只在 all.js / expiring.json）。"""
        info, payload = self._run()
        self.assertNotIn("view_groups", payload)
        self.assertNotIn("views", payload)
        self.assertEqual(info["upcoming_shown"], 0)

    def test_payload_carries_assets_version(self):
        """app.js 懒加载 all.js 时用 payload.assets_version 拼 ?v=（防 Pages 缓存）——
        与模板给 data.js/app.js 的 ?v= 同源（= now 的 epoch 秒）。"""
        _, payload = self._run()
        self.assertEqual(payload["assets_version"], str(int(NOW.timestamp())))

    def test_overview_is_one_summary_line(self):
        """S9（2026-10-06，用户定案）：首页撤掉 6 个小框概览，只留一行摘要
        「今日新增 · 新史低 X · 平史低 Y」；其余数字搬到「关于网站」。
        批 F 那套 stat-box 随 S9 退场 —— 别再让它回来。

        2026-10-07 第二轮：摘要在 CSS 里改成 **S2 结构**（白卡胶囊 + 17px 数字）。
        文案从「今日新增：新史低 X」拆成了标签 + 数字 + 单位，所以断言改成查**结构与两处标签**，
        不再查一个整句。
        ⚠️ 第三轮末尾用户又删掉了数字前的小色条（「中间不要留很粗的竖线，不好看」）——
        所以这里断言**不许**再出现 `s-bar`。"""
        self._run()
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="summary"', html)
        self.assertIn("今日新增", html)
        self.assertIn('<span class="s-item s-new"><b>', html)
        self.assertIn('<span class="s-item s-tie"><b>', html)
        self.assertIn("新史低</span>", html)
        self.assertIn("平史低</span>", html)
        self.assertNotIn("stat-box", html)
        # 摘要里的小色条已删（用户 2026-10-07）；卡片色条仍是唯一图例
        self.assertNotIn('class="s-bar"', html)
        self.assertNotIn('class="hl', html)

    def test_pending_group_when_details_missing(self):
        """没抓到详情的条目要进「详情待补」，而不是被丢掉（§3.3）。"""
        candidates = [deal("g-missing", 999, 60, "No Meta")]
        stats_holder = {}

        def stats_of(info):
            stats_holder.update(info)
            # 模板会做算术（未进列表 = raw - shown），概览与页脚用到的字段都要给全
            return {"sweep": "low_only", "new_today_raw": 1,
                    "new_today_shown": info["shown"],
                    "deals_fetched": 0, "hist_low_total": 0,
                    "detail_pending": info["tier"].get(classify.TIER_PENDING, 0),
                    "detail_backlog": 1}

        info = render_pass(self.state, candidates, self.cfg, NOW, stats_of,
                           announce_merges=False)
        self.assertEqual(info["shown"], 1)
        self.assertEqual(info["tier"][classify.TIER_PENDING], 1)
        payload = json.loads(
            (self.out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";")
        )
        # 「详情待补」不丢 —— 卡片仍进板块预览（tier 标签标注），不被吞掉
        new_low = next(s for s in payload["sections"] if s["key"] == "new_low")
        self.assertEqual(len(new_low["items"]), 1)
        self.assertEqual(new_low["items"][0]["tier"], classify.TIER_PENDING)
        self.assertEqual(new_low["items"][0]["tier_label"], "详情待补")


if __name__ == "__main__":
    unittest.main()
