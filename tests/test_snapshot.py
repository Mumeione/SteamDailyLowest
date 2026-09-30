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
        # v3 新增字段的来源：store_low_int 决定「是不是史低」，publishers/developers/stats
        # 来自 game_meta（merge_details 已合进条目）；此处给 v3 之前的形态也不该崩
        "store_low_int": 2990,
        "publishers": [{"id": 369, "name": "SEGA"}],
        "developers": [{"id": 366, "name": "ATLUS"}],
        "stats": {"rank": 385, "waitlisted": 16847, "collected": 6601},
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
        self.assertEqual(snap["version"], 3)
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["window_hours"], 48)
        # generated_at 必须带时区偏移（消费方判断数据新鲜度的唯一依据）
        self.assertEqual(snap["generated_at"], "2026-09-27T03:14:07+08:00")
        item = snap["items"][0]
        self.assertEqual(
            list(item.keys()),
            [
                "game_id", "title", "title_zh", "appid", "flag", "low_class",
                "price_int", "regular_int", "cut", "currency",
                "start", "expiry", "reviews", "publishers", "developers", "stats",
                "compare", "key",
            ],
        )
        # 未过口碑分档的条目没有比价 → compare 为 null 是预期状态（消费方降级省略比价行）
        self.assertIsNone(item["compare"])
        # 内部字段不得泄漏进快照（口径归主仓库，消费方不需要）
        self.assertNotIn("tier", item)
        self.assertNotIn("last_low_at", item)
        self.assertNotIn("store_low_int", item)

    def test_low_class_is_steam_scoped_not_flag(self):
        """v3 核心：low_class 用 Steam 口径，与 ITAD 的 flag 是两套东西。

        `flag="H"`（全网曾到过该价）但 Steam 店内史低就是本次折扣创下的 → ``new``。
        依据：店内史低记录时刻（``last_low_at``）与折扣开始时刻（``start``）重合。
        """
        # flag=N（全网首次）⇒ 必定 Steam 首次，不必比时间
        snap = build_snapshot([entry(flag="N")], NOW, {})
        self.assertEqual(snap["items"][0]["low_class"], "new")
        # flag=H 但 last_low_at == start ⇒ Steam 口径仍是新史低
        snap = build_snapshot(
            [entry(flag="H", last_low_at="2026-09-25T07:00:00+02:00")], NOW, {})
        self.assertEqual(snap["items"][0]["low_class"], "new")
        # flag=H 且上次史低是半年前 ⇒ 平史低
        snap = build_snapshot(
            [entry(flag="H", last_low_at="2026-03-01T07:00:00+02:00")], NOW, {})
        self.assertEqual(snap["items"][0]["low_class"], "tie")
        # storeLow 缺失 ⇒ 如实标 unknown（不静默当「不是史低」，§10）
        snap = build_snapshot([entry(store_low_int=None, flag=None)], NOW, {})
        self.assertEqual(snap["items"][0]["low_class"], "unknown")
        # storeLow 在而 flag 缺失/非法（classify 返回 None 的异常形态）⇒ 也收敛成
        # unknown —— 契约是三值域，快照里不出现 null（code-review 2026-09-30）
        snap = build_snapshot([entry(flag=None)], NOW, {})
        self.assertEqual(snap["items"][0]["low_class"], "unknown")
        snap = build_snapshot([entry(flag="bogus")], NOW, {})
        self.assertEqual(snap["items"][0]["low_class"], "unknown")

    def test_low_class_tz_falls_back_to_cfg(self):
        """``now`` 失去 tzinfo 时按计划书回落 ``classify.zone(cfg["timezone"])``。

        构造让结果依赖时区解释：start = 2026-09-24T16:00Z；
        last_low_at 无偏移 ``2026-09-25T18:00:00`` —— 按 +08 解释距 start 18h（new），
        按 UTC 解释 26h（tie）。24h 容差窗口两侧，用于钉死 tz 来源。
        """
        naive = datetime(2026, 9, 27, 3, 14, 7)
        snap = build_snapshot(
            [entry(flag="H", start="2026-09-24T16:00:00+00:00",
                   last_low_at="2026-09-25T18:00:00")],
            naive, {"timezone": "Asia/Shanghai"},
        )
        self.assertEqual(snap["items"][0]["low_class"], "new")

    def test_party_lists_normalized_to_empty(self):
        """publishers / developers 缺键或 null 都收敛成 []，消费方不用判 null。"""
        snap = build_snapshot([entry(publishers=None, developers=None)], NOW, {})
        item = snap["items"][0]
        self.assertEqual(item["publishers"], [])
        self.assertEqual(item["developers"], [])
        # stats 保持原样：null = 还没回填到，是真信息，不能假装成 {}
        snap = build_snapshot([entry(stats=None)], NOW, {})
        self.assertIsNone(snap["items"][0]["stats"])
        # 老快照（v2 形态）没有这三个键，也不该崩
        old = entry()
        for key in ("publishers", "developers", "stats"):
            old.pop(key)
        snap = build_snapshot([old], NOW, {})
        self.assertEqual(snap["items"][0]["publishers"], [])
        self.assertIsNone(snap["items"][0]["stats"])

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
