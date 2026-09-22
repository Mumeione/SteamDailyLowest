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
from src import classify  # noqa: E402
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

    def _run(self, **stats_overrides):
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
                           announce_merges=False)
        payload = json.loads(
            (self.out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";")
        )
        return info, payload

    def test_groups_and_card_labels_agree(self):
        _, payload = self._run()
        group_labels = {g["key"]: g["label"] for g in payload["groups"]}
        card_labels = {i["tier"]: i["tier_label"]
                       for g in payload["groups"] for i in g["items"]}
        self.assertEqual(group_labels, card_labels)
        self.assertEqual(group_labels.get(classify.TIER_QUALITY), "好评达标")
        self.assertEqual(group_labels.get(classify.TIER_NOTABLE), "高热度 · 口碑不一")

    def test_no_evaluative_wording_anywhere(self):
        self._run()
        text = (self.out / "data.js").read_text(encoding="utf-8")
        self.assertNotIn("优质", text)
        self.assertNotIn("热门 · 褒贬不一", text)

    def test_groups_carry_criteria(self):
        _, payload = self._run()
        for group in payload["groups"]:
            self.assertTrue(group["criteria"], group["label"])
        quality = next(g for g in payload["groups"] if g["key"] == classify.TIER_QUALITY)
        self.assertIn("好评率 ≥ 70%", quality["criteria"])

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

    def test_title_zh_and_conditions_in_payload(self):
        _, payload = self._run()
        self.assertIn("好评率 ≥ 70%", "\n".join(payload["conditions"]))
        self.assertEqual(payload["page_size"]["breakpoint"], 768)

    def test_html_rendered_with_criteria_block(self):
        self._run()
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("筛选条件", html)
        self.assertIn("好评率 ≥ 70%", html)

    def test_pending_group_when_details_missing(self):
        """没抓到详情的条目要进「详情待补」，而不是被丢掉（§3.3）。"""
        candidates = [deal("g-missing", 999, 60, "No Meta")]
        stats_holder = {}

        def stats_of(info):
            stats_holder.update(info)
            return {"sweep": "low_only", "new_today_shown": info["shown"],
                    "detail_pending": info["tier"].get(classify.TIER_PENDING, 0),
                    "detail_backlog": 1}

        info = render_pass(self.state, candidates, self.cfg, NOW, stats_of,
                           announce_merges=False)
        self.assertEqual(info["shown"], 1)
        self.assertEqual(info["tier"][classify.TIER_PENDING], 1)
        payload = json.loads(
            (self.out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";")
        )
        self.assertEqual([g["key"] for g in payload["groups"]], [classify.TIER_PENDING])
        self.assertIn("下次运行会自动补上", payload["groups"][0]["criteria"])


if __name__ == "__main__":
    unittest.main()
