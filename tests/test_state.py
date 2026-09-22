# -*- coding: utf-8 -*-
"""state 幂等 / 留存清理 / 详情缓存 TTL 的单元测试（docs/DEVELOPMENT.md §5）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 9, 21, 3, 0, tzinfo=TZ)


def entry(**kwargs) -> dict:
    base = {
        "game_id": "uuid-1",
        "title": "Crown Wars: The Black Prince",
        "price_int": 865,
        "regular_int": 17300,
        "cut": 95,
        "currency": "CNY",
        "flag": "N",
        "start": "2026-09-21T00:30:00+02:00",
        "expiry": "2026-09-28T19:00:00+02:00",
    }
    base.update(kwargs)
    return base


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.state = State(self.path, tz=TZ).load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_same_day(self):
        _, first = self.state.record_seen(entry(), NOW)
        _, again = self.state.record_seen(entry(), NOW + timedelta(minutes=5))
        self.assertTrue(first)
        self.assertFalse(again)
        self.assertEqual(len(self.state.seen_deal), 1)

    def test_has_seen_key_contains_price_and_expiry(self):
        self.state.record_seen(entry(), NOW)
        self.assertTrue(self.state.has_seen("uuid-1", 865, "2026-09-28T19:00:00+02:00"))
        # 价格变了就是另一条组合（§4.2 兜底口径）
        self.assertFalse(self.state.has_seen("uuid-1", 999, "2026-09-28T19:00:00+02:00"))

    def test_cleanup_expired_keeps_recent(self):
        self.state.record_seen(entry(expiry=(NOW - timedelta(days=10)).isoformat()), NOW)
        self.state.record_seen(entry(game_id="uuid-2", expiry=(NOW - timedelta(days=8)).isoformat()), NOW)
        self.state.record_seen(entry(game_id="uuid-3", expiry=(NOW - timedelta(days=3)).isoformat()), NOW)
        dropped = self.state.cleanup_expired(NOW, retention_days=7)
        self.assertEqual(dropped, 2)
        self.assertEqual(list(self.state.seen_deal.values())[0]["game_id"], "uuid-3")

    def test_meta_ttl(self):
        self.state.set_meta("uuid-1", 1658920, {"score": 61, "count": 472}, NOW)
        self.assertTrue(self.state.meta_valid("uuid-1", NOW, ttl_days=7, empty_ttl_days=3))
        self.assertFalse(self.state.meta_valid("uuid-1", NOW + timedelta(days=8), 7, 3))
        self.assertTrue(self.state.has_appid("uuid-1"))

    def test_appid_survives_but_reviews_expire(self):
        self.state.set_meta("uuid-1", 123, {"score": 80, "count": 500}, NOW - timedelta(days=30))
        self.assertFalse(self.state.meta_valid("uuid-1", NOW, 7, 3))
        self.assertTrue(self.state.has_appid("uuid-1"))

    def test_no_data_entry_uses_empty_ttl_not_forever(self):
        """抓过但没有好评率/appid 的条目：按 empty_ttl 重试，不是每轮都重抓。"""
        self.state.set_meta("uuid-noappid", None, None, NOW)
        self.assertTrue(self.state.meta_valid("uuid-noappid", NOW, 7, 3))
        self.assertTrue(self.state.meta_valid("uuid-noappid", NOW + timedelta(days=2), 7, 3))
        self.assertFalse(self.state.meta_valid("uuid-noappid", NOW + timedelta(days=4), 7, 3))
        # 关键：判定不能依赖「有没有 appid」—— 该条目 appid 就是拿不到
        self.assertFalse(self.state.has_appid("uuid-noappid"))

    def test_never_fetched_is_invalid(self):
        self.assertFalse(self.state.meta_valid("uuid-missing", NOW, 7, 3))

    def test_record_seen_trims_fields(self):
        """落库只保留 classify.SEEN_KEEP 里的字段（§5 精简）。"""
        deal = entry(
            banner="https://x/banner.jpg",
            boxart="https://x/boxart.jpg",
            slug="crown-wars",
            shop_id=61,
            type="game",
            mature=False,
            itad_url="https://itad.link/abc",
        )
        self.state.record_seen(deal, NOW)
        stored = next(iter(self.state.seen_deal.values()))
        self.assertTrue(set(stored).issubset(set(classify.SEEN_KEEP)))
        # 保留：报表与后续视图需要的
        for kept in ("game_id", "title", "price_int", "flag", "expiry", "boxart"):
            self.assertIn(kept, stored)
        # 裁掉：常量字段、重复的封面图，以及 R2 砍掉的 itad_url（302 直跳 Steam）
        for dropped in ("banner", "itad_url", "slug", "shop_id", "type", "mature"):
            self.assertNotIn(dropped, stored)
        self.assertEqual(stored["first_seen_at"], NOW.isoformat(timespec="seconds"))

    def test_save_slims_legacy_entries(self):
        """存量状态文件（早期版本写下的全字段）在保存时自动收敛为精简格式。"""
        self.state.data["seen_deal"] = {
            "uuid-1|865|e": {
                "game_id": "uuid-1", "title": "T", "price_int": 865, "expiry": "e",
                "first_seen_at": "x", "last_seen_at": "y",
                "banner": "https://x/banner.jpg", "slug": "t", "shop_id": 61,
                "type": "game", "mature": False,
            }
        }
        self.state.save(NOW)
        stored = next(iter(State(self.path, tz=TZ).load().seen_deal.values()))
        self.assertNotIn("banner", stored)
        self.assertNotIn("slug", stored)
        self.assertEqual(stored["game_id"], "uuid-1")
        self.assertEqual(stored["first_seen_at"], "x")   # 时间戳不能丢

    def test_save_is_compact_json(self):
        """状态文件用紧凑 JSON 落盘（省 19%）。"""
        self.state.record_seen(entry(), NOW)
        self.state.save(NOW)
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(raw.count("\n"), 0)          # 单行：没有缩进换行
        self.assertNotIn('": "', raw)                 # 值里没有分隔空格
        self.assertTrue(raw.startswith('{"version":1') or raw.startswith('{"version": 1'))
        self.assertEqual(State(self.path, tz=TZ).load().data["updated_at"],
                         NOW.isoformat(timespec="seconds"))


    def test_run_log_keeps_tail(self):
        for i in range(35):
            self.state.add_run_log({"run_at": str(i)}, keep=30)
        self.assertEqual(len(self.state.run_log), 30)
        self.assertEqual(self.state.run_log[0]["run_at"], "5")

    def test_save_load_roundtrip(self):
        self.state.set_meta("uuid-1", 999, {"score": 70, "count": 100}, NOW)
        self.state.record_seen(entry(), NOW)
        self.state.add_run_log({"run_at": NOW.isoformat(), "mode": "daily"}, keep=30)
        self.state.save(NOW)

        reloaded = State(self.path, tz=TZ).load()
        self.assertEqual(len(reloaded.seen_deal), 1)
        self.assertEqual(reloaded.meta("uuid-1")["appid"], 999)
        self.assertEqual(reloaded.last_run()["mode"], "daily")
        self.assertEqual(reloaded.data["updated_at"], NOW.isoformat(timespec="seconds"))


if __name__ == "__main__":
    unittest.main()