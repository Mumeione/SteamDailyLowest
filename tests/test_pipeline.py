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
            "page_size_mobile": 10,
            "page_size_desktop": 20,
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

    def test_featured_single_group_with_card_labels(self):
        """重构 S5：当日新增 = 扁平精选列表（单一 featured 组）；
        档位信息降级为卡片 tier_label，不得丢失或漂移。"""
        _, payload = self._run()
        self.assertEqual([g["key"] for g in payload["groups"]], ["featured"])
        card_labels = {i["tier"]: i["tier_label"]
                       for g in payload["groups"] for i in g["items"]}
        self.assertEqual(card_labels.get(classify.TIER_QUALITY), "好评达标")
        self.assertEqual(card_labels.get(classify.TIER_NOTABLE), "高热度 · 口碑不一")

    def test_featured_order_is_layered_dict_order(self):
        """精选列表顺序 = 分层字典序：新史低在前、平史低在后（验收 §6）。"""
        _, payload = self._run()
        items = payload["groups"][0]["items"]
        layers = [report.FEATURED_LAYERS[i["low_class"]] for i in items]
        self.assertEqual(layers, sorted(layers),
                         "新史低必须整体排在平史低 / 待确认之前")

    def test_no_evaluative_wording_anywhere(self):
        self._run()
        text = (self.out / "data.js").read_text(encoding="utf-8")
        self.assertNotIn("优质", text)
        self.assertNotIn("热门 · 褒贬不一", text)

    def test_featured_group_carries_criteria(self):
        _, payload = self._run()
        for group in payload["groups"]:
            self.assertTrue(group["criteria"], group["label"])
        self.assertEqual(payload["groups"][0]["criteria"],
                         "新史低 → 折扣力度 → 评价数")

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
        items = [i for g in payload["groups"] for i in g["items"]]
        good = next(i for i in items if i["game_id"] == "g-good")
        self.assertEqual(good["title_zh"], "好游戏")

    def test_no_conditions_block_in_payload_or_html(self):
        """批 F：筛选条件框已从页面删除，判定口径只在 README —— 别又跑回来。"""
        _, payload = self._run()
        self.assertNotIn("conditions", payload)
        self.assertNotIn("criteria_digest", payload)
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("criteria-box", html)
        self.assertEqual(payload["page_size"]["breakpoint"], 768)

    def test_upcoming_view_renders_separately(self):
        """「即将过期」视图：独立进 view_groups，按钮 count 对应，暂不带比价。"""
        expiring = deal("g-expiring", 444, 90, "Expiring Game")
        expiring["expiry"] = "2026-09-23T10:00:00+08:00"  # NOW + 18h → 48h 窗口内
        self.state.set_meta("g-expiring", 444, {"score": 85, "count": 5000}, NOW)
        info, payload = self._run(upcoming=[expiring])

        groups = payload["view_groups"]["upcoming"]
        items = [i for g in groups for i in g["items"]]
        self.assertEqual([i["game_id"] for i in items], ["g-expiring"])
        self.assertEqual(info["upcoming_shown"], 1)
        upcoming_view = next(v for v in payload["views"] if v["key"] == "upcoming")
        self.assertTrue(upcoming_view["enabled"])
        self.assertEqual(upcoming_view["count"], 1)
        # 比价数据由 enrich 阶段填（本测试不跑 enrich）→ 详情区由前端回落单栏
        self.assertEqual(items[0]["compare"], [])

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

    def test_upcoming_view_absent_when_not_produced(self):
        """不传 upcoming 时 payload 不带 view_groups（旧产物兼容口径）。"""
        _, payload = self._run()
        self.assertEqual(payload["view_groups"], {})
        upcoming_view = next(v for v in payload["views"] if v["key"] == "upcoming")
        self.assertTrue(upcoming_view["enabled"])
        self.assertEqual(upcoming_view["count"], 0)

    def test_overview_is_one_summary_line(self):
        """S9（2026-10-06，用户定案）：首页撤掉 6 个小框概览，只留一行
        「今日新增：新史低 X · 平史低 Y」；其余数字搬到「关于网站」（尚未做）。
        批 F 那套 stat-box 随 S9 退场 —— 别再让它回来。"""
        self._run()
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="summary"', html)
        self.assertIn("今日新增：新史低", html)
        self.assertNotIn("stat-box", html)

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
        # 重构 S5：扁平精选列表里「详情待补」不丢 —— 卡片仍在（tier 标签标注），
        # 排在评价数维度之后（无 reviews），但不被吞掉
        self.assertEqual([g["key"] for g in payload["groups"]], ["featured"])
        items = payload["groups"][0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tier"], classify.TIER_PENDING)
        self.assertEqual(items[0]["tier_label"], "详情待补")


if __name__ == "__main__":
    unittest.main()
