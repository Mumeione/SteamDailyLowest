# -*- coding: utf-8 -*-
"""「即将过期」快照导出的单元测试（``.scratch/expiring-snapshot/spec.md`` §2）。

快照是跨仓库的数据契约：字段集合与键顺序、generated_at 带时区偏移、
窗口小时数都写死成字面量来断言 —— 数据消费方靠它拼结构、判新鲜度。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.snapshot import build_fx, build_snapshot, write_snapshot  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 9, 27, 3, 14, 7, tzinfo=TZ)


def entry(**kwargs) -> dict:
    """已 merge_details + 已按 appid 去重的条目（快照的输入形态）。"""
    base = {
        "game_id": "uuid-1",
        "title": "Some Game",
        "title_zh": "某游戏",
        "appid": 570,
        "flag": "N",
        "price_int": 2990,
        "regular_int": 9990,
        "cut": 70,
        "currency": "CNY",
        "start": "2026-09-25T07:00:00+02:00",
        "expiry": "2026-09-29T07:00:00+02:00",
        "reviews": {"score": 92, "count": 12345},
        # 以下字段不属于快照契约，写出时必须被裁掉
        "tier": "X",
        "last_low_at": "2026-09-20T10:00:00+08:00",
        "low_kind": "N",
    }
    base.update(kwargs)
    return base


class BuildSnapshotTest(unittest.TestCase):
    def test_item_field_order_and_key_last(self):
        """字段契约：KEEP 顺序 + key 追加在末尾（键顺序即写出顺序，便于人工 diff）。"""
        snap = build_snapshot([entry()], NOW, {"upcoming_expiry_hours": 48})
        self.assertEqual(snap["version"], 2)
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["window_hours"], 48)
        # generated_at 必须带时区偏移（消费方判断数据新鲜度的唯一依据）
        self.assertEqual(snap["generated_at"], "2026-09-27T03:14:07+08:00")
        item = snap["items"][0]
        self.assertEqual(
            list(item.keys()),
            [
                "game_id", "title", "title_zh", "appid", "flag",
                "price_int", "regular_int", "cut", "currency",
                "start", "expiry", "reviews", "compare", "key",
            ],
        )
        # 未过口碑分档的条目没有比价 → compare 为 null 是预期状态（消费方降级省略比价行）
        self.assertIsNone(item["compare"])
        # 内部字段不得泄漏进快照（口径归主仓库，消费方不需要）
        self.assertNotIn("tier", item)
        self.assertNotIn("last_low_at", item)

    def test_key_is_deal_key_literal(self):
        """key = <game_id>|<price_int>|<expiry>（§5 契约，字面量断言防实现漂移）。"""
        snap = build_snapshot([entry()], NOW, {"upcoming_expiry_hours": 48})
        self.assertEqual(snap["items"][0]["key"], "uuid-1|2990|2026-09-29T07:00:00+02:00")

    def test_window_hours_from_cfg(self):
        snap = build_snapshot([entry()], NOW, {"upcoming_expiry_hours": 72})
        self.assertEqual(snap["window_hours"], 72)

    def test_appid_and_reviews_may_be_null(self):
        """详情未补齐的条目允许 appid / reviews 为 null（消费方自行排除）。"""
        snap = build_snapshot(
            [entry(appid=None, reviews=None, title_zh=None)], NOW,
            {"upcoming_expiry_hours": 48},
        )
        item = snap["items"][0]
        self.assertIsNone(item["appid"])
        self.assertIsNone(item["reviews"])
        self.assertIsNone(item["title_zh"])

    def test_top_level_fx_null_by_default(self):
        """不传 fx 时顶层 fx 为 null；传了则原样透出（方向 base=CNY，不取倒数）。"""
        snap = build_snapshot([entry()], NOW, {"upcoming_expiry_hours": 48})
        self.assertIsNone(snap["fx"])
        fx = {"date": "2026-09-27", "base": "CNY", "rates": {"UAH": 6.6, "INR": 12.1}}
        snap = build_snapshot([entry()], NOW, {"upcoming_expiry_hours": 48}, fx=fx)
        self.assertEqual(snap["fx"]["date"], "2026-09-27")
        self.assertEqual(snap["fx"]["base"], "CNY")
        self.assertEqual(snap["fx"]["rates"], {"UAH": 6.6, "INR": 12.1})


class BuildFxTest(unittest.TestCase):
    def test_none_when_not_dict(self):
        self.assertIsNone(build_fx(None))

    def test_none_when_no_rates(self):
        self.assertIsNone(build_fx({}))
        self.assertIsNone(build_fx({"date": "2026-09-27"}))
        self.assertIsNone(build_fx({"date": "2026-09-27", "rates": {}}))

    def test_direction_not_inverted(self):
        """方向必须保持「1 CNY 换多少 X」：UAH ≈ 6.6，出现 0.15 就是取了倒数。"""
        fx = {"date": "2026-09-27", "base": "CNY", "rates": {"UAH": "6.6", "INR": "12.1"}}
        out = build_fx(fx)
        self.assertEqual(out["base"], "CNY")
        self.assertEqual(out["rates"]["UAH"], 6.6)
        self.assertEqual(out["rates"]["INR"], 12.1)
        self.assertGreater(out["rates"]["UAH"], 1)

    def test_base_defaults_to_cny(self):
        out = build_fx({"date": "2026-09-27", "rates": {"UAH": 6.6}})
        self.assertEqual(out["base"], "CNY")
        self.assertEqual(out["date"], "2026-09-27")


class WriteSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_compact_json_and_returns_count(self):
        path = self.root / "expiring.json"
        n = write_snapshot(path, [entry(), entry()], NOW, {"upcoming_expiry_hours": 48})
        self.assertEqual(n, 2)
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["items"]), 2)
        # 紧凑 JSON（与 state.json 同一习惯）：不缩进、无多余空格。
        # 重 dumps 对比而非查 ", " 子串——条目 title 恰含 ", " 时后者会误报
        self.assertEqual(raw, json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def test_creates_parent_dir(self):
        path = self.root / "data" / "expiring.json"
        write_snapshot(path, [entry()], NOW, {"upcoming_expiry_hours": 48})
        self.assertTrue(path.exists())

    def test_writes_fx_passthrough(self):
        """write_snapshot 必须把 fx 透传给 build_snapshot（漏转发则文件里 fx 恒 null）。"""
        path = self.root / "expiring.json"
        fx = {"date": "2026-09-27", "base": "CNY", "rates": {"UAH": 6.6}}
        write_snapshot(path, [entry()], NOW, {"upcoming_expiry_hours": 48}, fx=fx)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["fx"]["rates"], {"UAH": 6.6})

    def test_empty_entries_still_writes(self):
        """空快照是合法状态（当天没有 48h 内到期的史低），不能跳过写文件。"""
        path = self.root / "expiring.json"
        n = write_snapshot(path, [], NOW, {"upcoming_expiry_hours": 48})
        self.assertEqual(n, 0)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["items"], [])


if __name__ == "__main__":
    unittest.main()
