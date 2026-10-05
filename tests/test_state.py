# -*- coding: utf-8 -*-
"""state 幂等 / 留存清理 / 详情缓存 TTL 的单元测试（docs/DEVELOPMENT.md §5）。"""

from __future__ import annotations

import json
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

    def test_set_meta_splits_layers(self):
        """S6 分层：appid/厂商 → game_meta（不变层）；reviews/stats/fetched_at → dynamic。"""
        self.state.set_meta("uuid-1", 1658920, {"score": 61, "count": 472}, NOW,
                            publishers=[{"id": 1, "name": "P"}], stats={"rank": 9})
        # 不变层：只有 appid / publishers（reviews 等动态键不再混存）
        base = self.state.game_meta["uuid-1"]
        self.assertEqual(base["appid"], 1658920)
        self.assertNotIn("reviews", base)
        self.assertNotIn("fetched_at", base)
        self.assertIn("publishers", base)
        # 动态层
        dyn = self.state.dyn("uuid-1")
        self.assertEqual(dyn["reviews"], {"score": 61, "count": 472})
        self.assertEqual(dyn["stats"], {"rank": 9})
        self.assertEqual(dyn["fetched_at"], NOW.isoformat(timespec="seconds"))
        # 合并视图对读方无感
        merged = self.state.meta("uuid-1")
        self.assertEqual(merged["appid"], 1658920)
        self.assertEqual(merged["reviews"], {"score": 61, "count": 472})
        self.assertTrue(self.state.has_appid("uuid-1"))

    def test_set_appid_does_not_touch_dynamic(self):
        self.state.set_meta("uuid-1", 123, {"score": 80, "count": 500}, NOW)
        self.state.set_appid("uuid-1", 456)
        self.assertEqual(self.state.dyn("uuid-1")["fetched_at"],
                         NOW.isoformat(timespec="seconds"))   # fetched_at 没被刷掉
        self.assertEqual(self.state.meta("uuid-1")["appid"], 456)

    def test_never_fetched_has_no_dynamic(self):
        self.assertIsNone(self.state.dyn("uuid-missing"))
        self.assertFalse(self.state.detail_fetched_recently("uuid-missing", NOW, 7))

    def test_legacy_game_meta_auto_migrates(self):
        """S6 代码自迁移：旧版 game_meta 混存的动态键 → load 时收编进 dynamic.json。"""
        legacy = {
            "version": 1, "updated_at": None, "seen_deal": {}, "run_log": [],
            "game_meta": {
                "uuid-old": {
                    "appid": 999,
                    "reviews": {"score": 70, "count": 100},
                    "stats": {"rank": 3},
                    "fetched_at": "2026-10-01T00:00:00+08:00",
                    "detail_failed_at": "2026-09-30T00:00:00+08:00",
                    "detail_attempts": 2,
                }
            },
        }
        self.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        state = State(self.path, tz=TZ).load()
        # game_meta 只剩不变层；动态键进了 dynamic
        self.assertEqual(state.game_meta["uuid-old"], {"appid": 999})
        dyn = state.dyn("uuid-old")
        self.assertEqual(dyn["reviews"], {"score": 70, "count": 100})
        self.assertEqual(dyn["stats"], {"rank": 3})
        self.assertEqual(dyn["detail_attempts"], 2)
        # 合并视图与迁移前读感一致
        self.assertEqual(state.meta("uuid-old")["reviews"], {"score": 70, "count": 100})
        # dynamic.json 已落盘；幂等：再 load 一次结果一致
        self.assertTrue(self.path.with_name("dynamic.json").exists())
        again = State(self.path, tz=TZ).load()
        self.assertEqual(again.meta("uuid-old")["reviews"], {"score": 70, "count": 100})
        self.assertEqual(again.game_meta["uuid-old"], {"appid": 999})

    def test_unlisted_mark_roundtrip(self):
        self.state.set_unlisted("uuid-1", NOW, "2026-09-21T00:30:00+02:00")
        mark = self.state.unlisted("uuid-1")
        self.assertEqual(mark["start"], "2026-09-21T00:30:00+02:00")
        self.assertTrue(self.state.clear_unlisted("uuid-1"))
        self.assertIsNone(self.state.unlisted("uuid-1"))
        self.assertFalse(self.state.clear_unlisted("uuid-1"))   # 再摘一次：False

    def test_cleanup_drops_dynamic_for_gone_games(self):
        """验收：游戏离开 seen_deal（留存清理）→ 动态条目一并删除。"""
        self.state.record_seen(entry(expiry=(NOW - timedelta(days=10)).isoformat()), NOW)
        self.state.record_seen(entry(game_id="uuid-3",
                                     expiry=(NOW - timedelta(days=3)).isoformat()), NOW)
        self.state.set_meta("uuid-1", 111, {"score": 80, "count": 500}, NOW)
        self.state.set_meta("uuid-3", 222, {"score": 80, "count": 500}, NOW)
        self.state.cleanup_expired(NOW, retention_days=7)
        self.assertIsNone(self.state.dyn("uuid-1"))
        self.assertIsNotNone(self.state.dyn("uuid-3"))

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

    # ------------------------------------------------------------------
    # 比价缓存（2026-09-27 方案 B）：appid|expiry 键，同一折扣期内不重拉
    # ------------------------------------------------------------------
    def test_compare_cache_roundtrip(self):
        rows = [{"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 4500}]
        self.state.set_compare(111, "2026-09-28T19:00:00+02:00", rows, NOW)
        cached = self.state.compare_cached(111, "2026-09-28T19:00:00+02:00")
        self.assertEqual(cached[0]["cc"], "UA")
        self.assertEqual(cached[0]["final"], 4500)

    def test_compare_cache_only_keeps_raw_fields(self):
        """缓存只存原币种数据；cny_minor/diff_pct 依赖当天汇率，不能进缓存。"""
        rows = [{"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 4500,
                 "cny_minor": 674, "diff_pct": -22}]
        self.state.set_compare(111, "2026-09-28T19:00:00+02:00", rows, NOW)
        self.state.save(NOW)
        cached = State(self.path, tz=TZ).load().compare_cached(111, "2026-09-28T19:00:00+02:00")
        self.assertNotIn("cny_minor", cached[0])
        self.assertNotIn("diff_pct", cached[0])
        self.assertEqual(cached[0]["final"], 4500)

    def test_compare_cache_empty_rows_not_stored(self):
        """拉取失败（空 rows）不落缓存 —— 下轮自然重试，宁可留空不猜。"""
        self.state.set_compare(111, "2026-09-28T19:00:00+02:00", [], NOW)
        self.assertIsNone(self.state.compare_cached(111, "2026-09-28T19:00:00+02:00"))

    def test_compare_cache_expiry_is_part_of_key(self):
        """同一游戏新折扣（expiry 变了）不算命中 —— 旧折扣价不能串到新折扣上。"""
        rows = [{"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 4500}]
        self.state.set_compare(111, "2026-09-28T19:00:00+02:00", rows, NOW)
        self.assertIsNone(self.state.compare_cached(111, "2026-10-28T19:00:00+02:00"))
        self.assertIsNone(self.state.compare_cached(222, "2026-09-28T19:00:00+02:00"))

    def test_cleanup_expired_drops_stale_compare_cache(self):
        rows = [{"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 4500}]
        self.state.set_compare(111, (NOW - timedelta(days=10)).isoformat(), rows, NOW)
        self.state.set_compare(222, (NOW - timedelta(days=3)).isoformat(), rows, NOW)
        self.state.cleanup_expired(NOW, retention_days=7)
        self.assertIsNone(self.state.compare_cached(111, (NOW - timedelta(days=10)).isoformat()))
        self.assertIsNotNone(self.state.compare_cached(222, (NOW - timedelta(days=3)).isoformat()))

    # ------------------------------------------------------------------
    # 上次史低时间（2026-09-27 起为折扣期暂存，键 game_id|expiry）
    # ------------------------------------------------------------------
    def test_low_time_cache_roundtrip(self):
        expiry = "2026-09-28T19:00:00+02:00"
        self.state.set_last_low_at("uuid-1", expiry, "2026-06-01T12:00:00+02:00")
        self.assertEqual(self.state.last_low_at("uuid-1", expiry),
                         "2026-06-01T12:00:00+02:00")
        # expiry 是键的一部分：同一游戏的新折扣不继承旧值
        self.assertIsNone(self.state.last_low_at("uuid-1", "2026-10-28T19:00:00+02:00"))

    def test_low_time_cache_cleanup_with_seen_deal(self):
        expiry_old = (NOW - timedelta(days=10)).isoformat()
        expiry_keep = (NOW - timedelta(days=3)).isoformat()
        self.state.set_last_low_at("uuid-old", expiry_old, "ts-old")
        self.state.set_last_low_at("uuid-keep", expiry_keep, "ts-keep")
        self.state.cleanup_expired(NOW, retention_days=7)
        self.assertIsNone(self.state.last_low_at("uuid-old", expiry_old))
        self.assertEqual(self.state.last_low_at("uuid-keep", expiry_keep), "ts-keep")

    def test_old_meta_last_low_at_no_longer_read(self):
        """game_meta 里的旧 last_low_at 存量字段不再被读取（改走折扣期暂存）。"""
        self.state.set_meta("uuid-1", 999, {"score": 80, "count": 500}, NOW)
        self.state.game_meta["uuid-1"]["last_low_at"] = "2026-01-01T00:00:00+00:00"
        self.assertIsNone(self.state.last_low_at("uuid-1", "2026-09-28T19:00:00+02:00"))

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

    # ------------------------------------------------------------------
    # 重构 S4：状态库切分 state.json（不可重建）/ cache.json（可重建）
    # ------------------------------------------------------------------
    def test_split_save_writes_two_files(self):
        """缓存落到 cache.json，state.json 不再含缓存键；重载后两族方法照常工作。"""
        rows = [{"cc": "UA", "label": "乌克兰区", "currency": "UAH", "final": 4500}]
        self.state.set_compare(111, "2026-09-28T19:00:00+02:00", rows, NOW)
        self.state.set_last_low_at("uuid-1", "2026-09-28T19:00:00+02:00", "2026-06-01T12:00:00+02:00")
        self.state.record_seen(entry(), NOW)
        self.state.save(NOW)

        cache_path = self.path.with_name("cache.json")
        self.assertTrue(cache_path.exists())
        state_raw = json.loads(self.path.read_text(encoding="utf-8"))
        cache_raw = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertNotIn("compare_cache", state_raw)
        self.assertNotIn("low_time_cache", state_raw)
        self.assertIn("seen_deal", state_raw)
        self.assertEqual(len(cache_raw["compare_cache"]), 1)
        self.assertEqual(len(cache_raw["low_time_cache"]), 1)

        reloaded = State(self.path, tz=TZ).load()
        self.assertEqual(reloaded.compare_cached(111, "2026-09-28T19:00:00+02:00")[0]["cc"], "UA")
        self.assertEqual(reloaded.last_low_at("uuid-1", "2026-09-28T19:00:00+02:00"),
                         "2026-06-01T12:00:00+02:00")
        self.assertEqual(len(reloaded.seen_deal), 1)
        self.assertEqual(reloaded.cache.data["updated_at"], NOW.isoformat(timespec="seconds"))

    def test_split_legacy_state_cache_keys_adopted(self):
        """旧版 state.json（带缓存键）加载时自动收编进 cache.json（代码自迁移）。"""
        legacy = {
            "version": 1,
            "updated_at": "2026-10-01T03:00:00+08:00",
            "seen_deal": {},
            "game_meta": {},
            "compare_cache": {"111|e": {"rows": [{"cc": "UA", "final": 1}], "fetched_at": "t"}},
            "low_time_cache": {"uuid-old|e": "ts"},
            "run_log": [],
        }
        self.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        state = State(self.path, tz=TZ).load()

        # 收编进 Cache 并立即落盘 cache.json；state.json 里不再有缓存键
        self.assertEqual(state.compare_cached(111, "e")[0]["final"], 1)
        self.assertEqual(state.last_low_at("uuid-old", "e"), "ts")
        cache_raw = json.loads(self.path.with_name("cache.json").read_text(encoding="utf-8"))
        self.assertEqual(cache_raw["compare_cache"]["111|e"]["rows"][0]["final"], 1)
        state.save(NOW)
        state_raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("compare_cache", state_raw)
        self.assertNotIn("low_time_cache", state_raw)
        # 幂等：再 load 一次结果一致
        again = State(self.path, tz=TZ).load()
        self.assertEqual(again.compare_cached(111, "e")[0]["final"], 1)

    def test_split_cache_json_priority_over_legacy(self):
        """cache.json 已有的键不被旧 state.json 的遗留键覆盖（cache 是权威来源）。"""
        cache = {"version": 1, "updated_at": None,
                 "compare_cache": {"111|e": {"rows": [{"cc": "UA", "final": 2}], "fetched_at": "new"}},
                 "low_time_cache": {}}
        self.path.with_name("cache.json").write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        legacy = {
            "version": 1, "updated_at": None, "seen_deal": {}, "game_meta": {},
            "compare_cache": {"111|e": {"rows": [{"cc": "UA", "final": 1}], "fetched_at": "old"},
                              "222|e": {"rows": [{"cc": "IN", "final": 3}], "fetched_at": "old"}},
            "low_time_cache": {}, "run_log": [],
        }
        self.path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        state = State(self.path, tz=TZ).load()
        # 111：cache.json 优先（final=2）；222：cache.json 缺，从遗留键收编
        self.assertEqual(state.compare_cached(111, "e")[0]["final"], 2)
        self.assertEqual(state.compare_cached(222, "e")[0]["final"], 3)

    def test_split_missing_cache_json_starts_empty(self):
        """cache.json 缺失：按空缓存起步（可重建，丢了重拉），state 不受影响。"""
        self.state.record_seen(entry(), NOW)
        self.state.save(NOW)
        self.path.with_name("cache.json").unlink()
        reloaded = State(self.path, tz=TZ).load()
        self.assertEqual(reloaded.compare_cache, {})
        self.assertEqual(reloaded.low_time_cache, {})
        self.assertEqual(len(reloaded.seen_deal), 1)

    def test_split_corrupt_cache_json_starts_empty(self):
        """cache.json 损坏：与 State 不同（State 抛 ValueError），Cache 容错重建。"""
        self.state.save(NOW)
        self.path.with_name("cache.json").write_text("{broken", encoding="utf-8")
        reloaded = State(self.path, tz=TZ).load()
        self.assertEqual(reloaded.compare_cache, {})

    def test_split_corrupt_dynamic_json_raises(self):
        """dynamic.json 损坏：与 state.json 同等对待抛错（review-s6 P1-3）——
        它是 reviews 的唯一副本，静默按空起步会让整轮无谓重抓且无人知晓。"""
        self.state.save(NOW)
        self.path.with_name("dynamic.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            State(self.path, tz=TZ).load()

    def test_split_missing_dynamic_json_starts_empty(self):
        """dynamic.json 缺失（≠损坏）：按空起步，可重抓自愈。"""
        self.state.save(NOW)
        self.path.with_name("dynamic.json").unlink()
        reloaded = State(self.path, tz=TZ).load()
        self.assertEqual(reloaded.dynamic.entries, {})


if __name__ == "__main__":
    unittest.main()