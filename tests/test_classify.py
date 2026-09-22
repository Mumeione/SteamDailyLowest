# -*- coding: utf-8 -*-
"""classify 纯函数单元测试（docs/DEVELOPMENT.md §4 / §3.5 / §4.6）。"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402

TZ = classify.zone("Asia/Shanghai")


def deal(**kwargs) -> dict:
    base = {
        "game_id": "uuid-1",
        "title": "Test Game",
        "type": "game",
        "price_int": 995,
        "regular_int": 17300,
        "cut": 95,
        "currency": "CNY",
        "flag": "N",
        "start": "2026-09-21T00:30:00+02:00",
        "expiry": "2026-09-28T19:00:00+02:00",
        "store_low_int": 995,
        "history_low_int": 995,
        "history_low_1y_int": 995,
    }
    base.update(kwargs)
    return base


class FlagTest(unittest.TestCase):
    def test_new_and_equal_and_store(self):
        self.assertEqual(classify.low_kind(deal(flag="N")), "N")
        self.assertEqual(classify.low_kind(deal(flag="H")), "H")
        self.assertEqual(classify.low_kind(deal(flag="S")), "S")

    def test_none_flag_is_not_low(self):
        self.assertIsNone(classify.low_kind(deal(flag=None, price_int=1200)))

    def test_missing_store_low_is_unknown(self):
        self.assertEqual(classify.low_kind(deal(store_low_int=None, flag=None)), "unknown")

    def test_labels(self):
        self.assertEqual(classify.low_label("N"), "新史低")
        self.assertEqual(classify.low_label("H"), "平史低")
        self.assertEqual(classify.low_label("S"), "店史低")

    def test_flag_price_mismatch(self):
        # flag is None 但 price <= storeLow → 异常（§4.1 交叉校验）
        self.assertTrue(classify.flag_price_mismatch(deal(flag=None, price_int=900, store_low_int=995)))
        self.assertFalse(classify.flag_price_mismatch(deal(flag="N", price_int=995, store_low_int=995)))


class NormalizeTest(unittest.TestCase):
    def test_amount_int_preferred_and_fallback(self):
        item = {
            "id": "uuid-1",
            "title": "Crown Wars",
            "type": "game",
            "assets": {"boxart": "https://x/boxart.jpg"},
            "deal": {
                "shop": {"id": 61},
                "price": {"amount": 8.65, "currency": "CNY"},
                "regular": {"amountInt": 17300, "currency": "CNY"},
                "cut": 95,
                "flag": "N",
                "storeLow": {"amount": 8.65},
                "historyLow": {"amountInt": 865},
                "historyLow_1y": {"amountInt": 865},
                "timestamp": "2026-09-14T19:21:10+02:00",
                "expiry": "2026-09-28T19:00:00+02:00",
                "url": "https://itad.link/abc",
            },
        }
        norm = classify.normalize_item(item)
        self.assertEqual(norm["price_int"], 865)       # 由 amount 折算
        self.assertEqual(norm["regular_int"], 17300)   # 直接用 amountInt
        self.assertEqual(norm["store_low_int"], 865)
        self.assertEqual(norm["boxart"], "https://x/boxart.jpg")
        self.assertEqual(norm["game_id"], "uuid-1")

    def test_deal_key(self):
        self.assertEqual(classify.deal_key("u", 865, "2026-09-28T19:00:00+02:00"),
                         "u|865|2026-09-28T19:00:00+02:00")


class TodayTest(unittest.TestCase):
    def test_timestamp_is_today_across_timezones(self):
        today = date(2026, 9, 21)
        # 美东 09-20 18:30 → 上海 09-21 06:30
        self.assertTrue(classify.timestamp_is_today(deal(start="2026-09-20T18:30:00-04:00"), today, TZ))
        # 中欧 09-21 20:00 → 上海 09-22 02:00，不是今天
        self.assertFalse(classify.timestamp_is_today(deal(start="2026-09-21T20:00:00+02:00"), today, TZ))

    def test_missing_timestamp(self):
        self.assertTrue(classify.timestamp_missing(deal(start=None), TZ))
        self.assertFalse(classify.timestamp_missing(deal(start="2026-09-21T00:30:00+02:00"), TZ))


class DedupeTest(unittest.TestCase):
    def test_keep_cheapest_per_appid(self):
        entries = [
            {"appid": 447040, "title": "Watch_Dogs® 2 Gold Edition", "price_int": 1990},
            {"appid": 447040, "title": "Watch_Dogs® 2 Deluxe Edition", "price_int": 1740},
            {"appid": 447040, "title": "Watch_Dogs® 2", "price_int": 1490},
            {"appid": 1924170, "title": "TT Isle of Man Fan Edition", "price_int": 990},
            {"appid": 1924170, "title": "TT Isle of Man", "price_int": 840},
            {"appid": None, "title": "无 appid 的条目", "price_int": 500},
        ]
        kept, merged = classify.dedupe_by_appid(entries)
        self.assertEqual([e["title"] for e in kept],
                         ["Watch_Dogs® 2", "TT Isle of Man", "无 appid 的条目"])
        by_appid = {record["appid"]: record for record in merged}
        self.assertEqual(sorted(by_appid), [447040, 1924170])
        wd2 = by_appid[447040]
        self.assertEqual(wd2["kept"], "Watch_Dogs® 2")
        self.assertEqual(wd2["kept_price_int"], 1490)
        self.assertEqual([d["title"] for d in wd2["dropped"]],
                         ["Watch_Dogs® 2 Gold Edition", "Watch_Dogs® 2 Deluxe Edition"])

    def test_all_entries_survive_when_no_appid(self):
        entries = [{"appid": None, "title": "a"}, {"appid": None, "title": "b"}]
        kept, merged = classify.dedupe_by_appid(entries)
        self.assertEqual(len(kept), 2)
        self.assertEqual(merged, [])


class TierTest(unittest.TestCase):
    cfg = {
        "min_positive_ratio": 0.7,
        "min_review_count": 100,
        "notable_review_count": 10000,
        "absolute_min_positive_ratio": None,
    }

    def test_quality(self):
        self.assertEqual(classify.tier_of({"score": 82, "count": 30589}, self.cfg), classify.TIER_QUALITY)
        self.assertEqual(classify.tier_of({"score": 70, "count": 100}, self.cfg), classify.TIER_QUALITY)

    def test_notable_mixed_kept_despite_low_ratio(self):
        # 《使命召唤》58.5% / 782040 条：不能被静默丢掉（§3.5）
        self.assertEqual(classify.tier_of({"score": 58, "count": 782040}, self.cfg), classify.TIER_NOTABLE)

    def test_cold_and_other(self):
        self.assertEqual(classify.tier_of({"score": 100, "count": 10}, self.cfg), classify.TIER_COLD)
        self.assertEqual(classify.tier_of({"score": 60, "count": 500}, self.cfg), classify.TIER_OTHER)

    def test_no_review_data_is_cold(self):
        # §3.5「冷门 / 无数据」不展示；「详情待补」由抓取状态决定，不在这里判定
        self.assertEqual(classify.tier_of(None, self.cfg), classify.TIER_COLD)
        self.assertEqual(classify.tier_of({"score": None, "count": 5}, self.cfg), classify.TIER_COLD)

    def test_absolute_min_ratio(self):
        cfg = dict(self.cfg, absolute_min_positive_ratio=0.4)
        self.assertEqual(classify.tier_of({"score": 30, "count": 50000}, cfg), classify.TIER_OTHER)

    def test_is_shown(self):
        self.assertTrue(classify.is_shown(classify.TIER_QUALITY))
        self.assertTrue(classify.is_shown(classify.TIER_NOTABLE))
        self.assertTrue(classify.is_shown(classify.TIER_PENDING))
        self.assertFalse(classify.is_shown(classify.TIER_COLD))
        self.assertFalse(classify.is_shown(classify.TIER_OTHER))


class ViewWindowTest(unittest.TestCase):
    #: 2026-09-21 是**周一**（用来钉住自然周对齐的边界）
    now = datetime(2026, 9, 21, 12, 0, tzinfo=TZ)

    def test_now_is_monday(self):
        self.assertEqual(self.now.weekday(), 0)

    def test_active_and_expired(self):
        self.assertTrue(classify.is_active("2026-09-22T12:00:00+08:00", self.now))
        self.assertFalse(classify.is_active("2026-09-20T12:00:00+08:00", self.now))
        self.assertTrue(classify.is_expired("2026-09-20T12:00:00+08:00", self.now))

    def test_upcoming_48h(self):
        self.assertTrue(classify.is_upcoming("2026-09-22T12:00:00+08:00", self.now, 48))
        self.assertFalse(classify.is_upcoming("2026-09-25T12:00:00+08:00", self.now, 48))
        self.assertFalse(classify.is_upcoming("2026-09-20T12:00:00+08:00", self.now, 48))

    def test_week_window_is_calendar_aligned(self):
        """§4.6：本周一 00:00:00 ~ 下周日 23:59:59（14 天），不是滚动窗口。"""
        begin, end = classify.week_window(self.now, 14)
        self.assertEqual(begin.isoformat(), "2026-09-21T00:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-10-04T23:59:59+08:00")

    def test_in_week_boundaries(self):
        # 起点前一秒不算（上周日）
        self.assertFalse(classify.in_week("2026-09-20T23:59:59+08:00", self.now, 14))
        # 起点那一刻算
        self.assertTrue(classify.in_week("2026-09-21T00:00:00+08:00", self.now, 14))
        # 终点那一刻算
        self.assertTrue(classify.in_week("2026-10-04T23:59:59+08:00", self.now, 14))
        # 终点后一秒不算
        self.assertFalse(classify.in_week("2026-10-05T00:00:00+08:00", self.now, 14))

    def test_in_week_rejects_rolling_interpretation(self):
        """关键回归：滚动 14 天会把「8 天前」也算进来，自然周不会。"""
        # 09-13（上周日）在滚动窗口内（now-14d = 09-07），但不在自然周窗口内
        self.assertFalse(classify.in_week("2026-09-13T12:00:00+08:00", self.now, 14))

    def test_in_view_gates_on_historical_low(self):
        """所有视图都先过「史低」这道门（§4.6）。"""
        cfg = {"week_window_days": 14, "upcoming_expiry_hours": 48}
        not_low = {"flag": None, "low_kind": None,
                   "start": "2026-09-21T10:00:00+08:00", "expiry": "2026-09-22T10:00:00+08:00"}
        for view in classify.VIEW_KEYS:
            self.assertFalse(classify.in_view(view, not_low, self.now, cfg))
        low = dict(not_low, flag="N", low_kind="N")
        self.assertTrue(classify.in_view("new_today", low, self.now, cfg))
        self.assertTrue(classify.in_view("week", low, self.now, cfg))
        self.assertTrue(classify.in_view("active", low, self.now, cfg))
        self.assertTrue(classify.in_view("upcoming", low, self.now, cfg))
        self.assertFalse(classify.in_view("expired", low, self.now, cfg))

    def test_in_view_rejects_unknown(self):
        with self.assertRaises(ValueError):
            classify.in_view("nope", {"flag": "N"}, self.now, {})



class SlimDealTest(unittest.TestCase):
    def test_keeps_only_whitelisted_fields(self):
        full = {
            "game_id": "uuid-1", "title": "T", "price_int": 100, "regular_int": 200,
            "cut": 50, "currency": "CNY", "flag": "N", "start": "s", "expiry": "e",
            "store_low_int": 100, "history_low_int": 100, "history_low_1y_int": 100,
            "boxart": "https://x/b.jpg", "itad_url": "https://itad.link/a",
            "low_kind": "N",
            # 以下是「存了也不会变」或与别处重复的，应当被裁掉
            "banner": "https://x/banner.jpg", "slug": "t", "shop_id": 61,
            "type": "game", "mature": False,
        }
        slim = classify.slim_deal(full)
        # 裁剪结果只能是白名单的子集（first/last_seen_at 由 state 追加，此时还没有）
        self.assertTrue(set(slim).issubset(set(classify.SEEN_KEEP)))
        self.assertEqual(slim["game_id"], "uuid-1")
        self.assertEqual(slim["boxart"], "https://x/b.jpg")
        # R2：itad_url 302 直跳 Steam，信息冗余，不再落库
        for dropped in ("banner", "itad_url", "slug", "shop_id", "type", "mature"):
            self.assertNotIn(dropped, slim)

    def test_seen_keep_has_no_itad_url(self):
        """R2 验收：SEEN_KEEP 白名单不再含 itad_url。"""
        self.assertNotIn("itad_url", classify.SEEN_KEEP)

    def test_timestamps_added_after_slimming(self):
        """first/last_seen_at 由 state.record_seen 追加，裁剪时还不存在也不该报错。"""
        slim = classify.slim_deal({"game_id": "u", "price_int": 1, "expiry": "e"})
        self.assertEqual(slim, {"game_id": "u", "price_int": 1, "expiry": "e"})


if __name__ == "__main__":
    unittest.main()