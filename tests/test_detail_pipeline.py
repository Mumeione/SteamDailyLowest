# -*- coding: utf-8 -*-
"""重构 S2 批量详情管线的单元测试：``detail_targets`` 派生欠账 + ``fetch_details`` 三段管线。

不发任何网络请求：ITAD / GetItems 都注入假客户端（同 ``tests/test_prefetch.py`` 手法）。
锁定行为（spec §3.2 决策 5/6 + §6 验收标准）：

* 派生式欠账：不建队列文件、无条数上限、TTL 命中跳过、近期失败冷却排除、
  排序 = 新史低 → 折扣力度；折扣结束清理后欠账自动收缩；
* 大促模拟：中断/截断后重跑，第二轮目标**仍然包含**未抓完的那批；
* 批量两跳：lookup 映射 → GetItems 写库；厂商 **name-only 只填空白**（id 语义保护）；
* 降级：GetItems 失败/缺条/lookup 未命中 → ``info/v2``，``detail_fallback_budget``
  截断，仍失败才写失败标记；连续 3 批 GetItems 失败熔断。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import apply_listing, detail_targets, entry_needs_detail, fetch_details, merge_details  # noqa: E402
from src import classify  # noqa: E402
from src.state import State  # noqa: E402
from src.steam_browse import GameMeta  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 10, 4, 16, 0, tzinfo=TZ)
EXPIRY = "2026-10-10T10:00:00+08:00"

CFG = {
    "new_game_days": 30,
    "new_game_refresh_days": 1,
    "discount_refresh_days": 3,
    "expiry_refresh_days": 1,
    "upcoming_expiry_hours": 48,
    "detail_retry_cooldown_days": 3,
    "min_positive_ratio": 0.7,
    "min_review_count": 100,
    "notable_review_count": 10000,
    "detail_fallback_budget": 1000,
}


def deal(game_id: str, cut: int = 90, low_kind: str = "N", title: str = "") -> dict:
    """一条能过 record_seen 的最小史低条目（字段按 classify.SEEN_KEEP）。"""
    return {
        "game_id": game_id,
        "title": title or f"Game {game_id}",
        "price_int": 1000,
        "regular_int": 10000,
        "cut": cut,
        "currency": "CNY",
        "flag": low_kind,
        "start": "2026-10-04T10:00:00+08:00",
        "expiry": EXPIRY,
        "store_low_int": 1000,
        "history_low_int": 1000,
        "history_low_1y_int": 1000,
        "boxart": "https://x/b.jpg",
        "low_kind": low_kind,
    }


def game_meta(appid: int, name: str = "某游戏", score: int | None = 80,
              publishers: list | None = None) -> GameMeta:
    reviews = {"score": score, "count": 500} if score is not None else None
    return GameMeta(
        appid=appid, name=name, reviews=reviews,
        publishers=publishers if publishers is not None else [{"id": 7, "name": "Pub"}],
        developers=[{"id": None, "name": "Dev"}],
        price={"final": 1000, "initial": 10000},
        release_date=1700000000,
        platforms={"windows": True}, tags=[],
    )


class FakeItad:
    def __init__(self, lookup: dict | None = None, info_map: dict | None = None):
        self._lookup = lookup or {}
        self._info_map = info_map or {}
        self.asked_lookup: list[str] = []
        self.asked_info: list[str] = []
        self.events: list[dict] = []
        self.limiter = SimpleNamespace(stats=lambda: {})

    def fetch_appid_batch(self, uuids, shop=61, batch_size=5000):
        self.asked_lookup.extend(uuids)
        return {u: a for u, a in self._lookup.items() if u in set(uuids)}

    def fetch_info(self, game_id):
        self.asked_info.append(game_id)
        return self._info_map.get(game_id)


class FakeBrowse:
    def __init__(self, metas: dict | None = None, fail: bool = False, batch_size: int = 250):
        self.metas = metas or {}
        self.fail = fail
        self.batch_size = batch_size
        self.batches: list[list[int]] = []

    def fetch(self, appids, *, country_code="CN", language="schinese"):
        self.batches.append(list(appids))
        if self.fail:
            from src.httpclient import HttpError
            raise HttpError("HTTP 400：too many ids")
        wanted = set(appids)
        return {a: m for a, m in self.metas.items() if a in wanted}


class DetailTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, game_id: str, cut: int = 90, low_kind: str = "N",
              last_seen: str = "2026-10-04T12:00:00+08:00"):
        entry = deal(game_id, cut=cut, low_kind=low_kind)
        key, _ = self.state.record_seen(entry, NOW)
        self.state.seen_deal[key]["last_seen_at"] = last_seen
        return entry

    def test_new_today_plus_derived_backlog(self):
        """当日新增 + 派生欠账；有有效详情的、近期失败的都不进目标。"""
        self._seed("g-valid")
        self.state.set_meta("g-valid", 111, {"score": 80, "count": 500}, NOW)  # 7 天内有效
        self._seed("g-missing")
        self._seed("g-failed")
        self.state.set_detail_failed("g-failed", NOW)   # 刚失败 → 冷却排除
        new_today = [deal("g-new")]
        targets, info = detail_targets(new_today, self.state, CFG, NOW)
        self.assertEqual([e["game_id"] for e in targets], ["g-new", "g-missing"])
        self.assertEqual(info, {"new_today": 1, "backlog": 1, "rejudge": 0, "total": 2})

    def test_no_cap_on_backlog(self):
        """决策 6：无条数上限 —— 300 条欠账全部进目标。"""
        for i in range(300):
            self._seed(f"g-{i:03d}")
        targets, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual(info, {"new_today": 0, "backlog": 300, "rejudge": 0, "total": 300})
        self.assertEqual(len(targets), 300)

    def test_ordering_new_low_first_then_cut(self):
        """欠账排序：新史低（N）优先 → 折扣力度降序（spec §3.3 决策 8 的管线版）。"""
        self._seed("g-h90", cut=90, low_kind="H")
        self._seed("g-n50", cut=50, low_kind="N")
        self._seed("g-n80", cut=80, low_kind="N")
        targets, _ = detail_targets([], self.state, CFG, NOW)
        self.assertEqual([e["game_id"] for e in targets], ["g-n80", "g-n50", "g-h90"])

    def test_failed_enters_backlog_after_cooldown(self):
        """失败冷却（empty_ttl 天）过后重新进欠账。"""
        self._seed("g-failed")
        failed_at = datetime(2026, 10, 2, 16, 0, tzinfo=TZ)   # 2 天前 → 冷却中
        self.state.set_detail_failed("g-failed", failed_at)
        self.assertTrue(self.state.detail_recently_failed("g-failed", NOW, 3))
        self.assertEqual(detail_targets([], self.state, CFG, NOW)[1]["backlog"], 0)
        # 4 天后冷却已过
        later = datetime(2026, 10, 6, 17, 0, tzinfo=TZ)
        self.assertFalse(self.state.detail_recently_failed("g-failed", later, 3))
        targets, _ = detail_targets([], self.state, CFG, later)
        self.assertEqual([e["game_id"] for e in targets], ["g-failed"])

    def test_backlog_shrinks_after_retention_cleanup(self):
        """验收：折扣结束 7 天后 seen_deal 被清理 → 欠账自动收缩，无需手工干预。"""
        self._seed("g-old")
        # 手工把 expiry 挪到很久以前，触发留存清理
        for key in self.state.seen_deal:
            self.state.seen_deal[key]["expiry"] = "2026-09-01T10:00:00+08:00"
        dropped = self.state.cleanup_expired(NOW, 7)
        self.assertEqual(dropped, 1)
        _, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual(info["backlog"], 0)

    def test_multi_version_single_target(self):
        """同 game_id 多版本折扣只派生一个目标（取最近出现的为代表）。"""
        self._seed("g-multi", last_seen="2026-10-04T08:00:00+08:00")
        # 不同 price_int → 不同幂等键，是同一游戏的两个折扣版本
        entry2 = deal("g-multi", cut=70)
        entry2["price_int"] = 900
        key2, _ = self.state.record_seen(entry2, NOW)
        self.state.seen_deal[key2]["last_seen_at"] = "2026-10-04T12:00:00+08:00"
        _, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual(info["backlog"], 1)
        self.assertEqual(
            max(self.state.seen_deal.values(),
                key=lambda e: e["last_seen_at"])["cut"], 70)


class FetchDetailsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()
        self.entries = [deal(f"uuid-{i}") for i in range(3)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_pipeline_full_hit(self):
        """两跳全命中：lookup 映射 → GetItems → 写库，**零 info/v2 请求**。"""
        itad = FakeItad(lookup={f"uuid-{i}": 100 + i for i in range(3)})
        browse = FakeBrowse(metas={100 + i: game_meta(100 + i, name=f"游戏{i}")
                                   for i in range(3)})
        stats = fetch_details(itad, self.state, self.entries, CFG, NOW, browse=browse)
        self.assertEqual(stats["fetched"], 3)
        self.assertEqual(stats["fallback_fetched"], 0)
        self.assertEqual(itad.asked_info, [])
        self.assertEqual(len(browse.batches), 1)
        self.assertEqual(browse.batches[0], [100, 101, 102])
        for i in range(3):
            meta = self.state.meta(f"uuid-{i}")
            self.assertEqual(meta["appid"], 100 + i)
            self.assertEqual(meta["reviews"], {"score": 80, "count": 500})
            # 中文名顺带写入（GetItems 的 name）
            self.assertEqual(self.state.title_zh(f"uuid-{i}"), f"游戏{i}")

    def test_publishers_name_only_fills_blank_only(self):
        """厂商 name-only：空白条目填 {id: None, name}；已有 ITAD id 的存量不覆盖。"""
        # 两个都造成「无 fetched_at」的欠账态（否则 meta_valid 会把它们跳过）：
        # uuid-0 只有 appid、没有厂商；uuid-1 预置 ITAD 口径厂商 + stats
        self.state.set_appid("uuid-0", 100)
        self.state.game_meta["uuid-1"] = {
            "appid": 101,
            "publishers": [{"id": 369, "name": "SEGA"}],
            "stats": {"rank": 5},
        }
        itad = FakeItad(lookup={})
        browse = FakeBrowse(metas={
            100: game_meta(100), 101: game_meta(101)})
        fetch_details(itad, self.state, self.entries[:2], CFG, NOW, browse=browse)
        self.assertEqual(self.state.meta("uuid-0")["publishers"],
                         [{"id": None, "name": "Pub"}])
        # ITAD 口径存量原样保留（id 空间不被 Steam id 污染）
        self.assertEqual(self.state.meta("uuid-1")["publishers"],
                         [{"id": 369, "name": "SEGA"}])
        self.assertEqual(self.state.meta("uuid-1")["stats"], {"rank": 5})

    def test_lookup_miss_goes_info_v2(self):
        """lookup 未命中的 uuid 降级走 info/v2（ITAD 可能自己给映射）。"""
        itad = FakeItad(
            lookup={"uuid-0": 100},   # uuid-1 未命中
            info_map={"uuid-1": {"appid": 101, "reviews": {"score": 70, "count": 200},
                                 "publishers": [{"id": 9, "name": "ITAD Pub"}],
                                 "developers": [], "stats": None}})
        browse = FakeBrowse(metas={100: game_meta(100)})
        stats = fetch_details(itad, self.state, self.entries[:2], CFG, NOW, browse=browse)
        self.assertEqual(stats["fetched"], 2)
        self.assertEqual(itad.asked_info, ["uuid-1"])
        # 降级路径写完整 ITAD 口径（带 id）
        self.assertEqual(self.state.meta("uuid-1")["publishers"],
                         [{"id": 9, "name": "ITAD Pub"}])
        self.assertEqual(self.state.meta("uuid-1")["appid"], 101)

    def test_getitems_miss_falls_back(self):
        """GetItems 响应缺条（无效 appid 等）→ 该 uuid 转降级。"""
        itad = FakeItad(lookup={"uuid-0": 100, "uuid-1": 101},
                        info_map={"uuid-1": {"appid": 101,
                                             "reviews": {"score": 70, "count": 200}}})
        browse = FakeBrowse(metas={100: game_meta(100)})   # 101 缺条
        stats = fetch_details(itad, self.state, self.entries[:2], CFG, NOW, browse=browse)
        self.assertEqual(stats["fetched"], 2)
        self.assertEqual(itad.asked_info, ["uuid-1"])
        self.assertEqual(self.state.meta("uuid-1")["appid"], 101)

    def test_circuit_breaker_after_three_batch_failures(self):
        """连续 3 批 GetItems 失败即熔断：剩余批次不再重试，直接进降级池。"""
        # 预置 appid（跳过 lookup），7 条 → batch_size=2 → 2+2+2 失败后熔断
        for i in range(7):
            self.state.set_appid(f"uuid-{i}", 200 + i)
        itad = FakeItad()
        browse = FakeBrowse(fail=True, batch_size=2)
        many = [deal(f"uuid-{i}") for i in range(7)]
        stats = fetch_details(itad, self.state, many, {**CFG, "detail_fallback_budget": 0},
                              NOW, browse=browse)
        self.assertEqual(len(browse.batches), 3)   # 3 批后熔断（第 4 批不再试）
        self.assertEqual(stats["fetched"], 0)
        self.assertEqual(itad.asked_info, [])      # 预算 0 → 降级不发生
        self.assertEqual(itad.asked_lookup, [])    # 都有 appid，lookup 也不发

    def test_fallback_budget_truncates(self):
        """降级预算截断：超出的留给下轮（派生欠账会自动重派）。"""
        itad = FakeItad(lookup={},
                        info_map={f"uuid-{i}": {"appid": 100 + i,
                                                "reviews": {"score": 80, "count": 500}}
                                  for i in range(3)})
        browse = FakeBrowse()
        cfg = {**CFG, "detail_fallback_budget": 1}
        stats = fetch_details(itad, self.state, self.entries, cfg, NOW, browse=browse)
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(stats["fallback_skipped"], 2)
        self.assertEqual(len(itad.asked_info), 1)

    def test_blocked_propagates_without_degradation(self):
        """连续 403（滥用封禁）必须中止本轮：三段管线任何一段都不许吞掉降级硬扛。"""
        from src.httpclient import Blocked

        class BlockedItad(FakeItad):
            def fetch_appid_batch(self, uuids, shop=61, batch_size=5000):
                raise Blocked("连续收到 403")

        itad = BlockedItad()
        with self.assertRaises(Blocked):
            fetch_details(itad, self.state, self.entries, CFG, NOW, browse=FakeBrowse())

        class BlockedBrowse(FakeBrowse):
            def fetch(self, appids, **kwargs):
                raise Blocked("连续收到 403")

        for i in range(2):
            self.state.set_appid(f"uuid-{i}", 200 + i)
        itad2 = FakeItad()
        with self.assertRaises(Blocked):
            fetch_details(itad2, self.state, self.entries[:2], CFG, NOW,
                          browse=BlockedBrowse())
        class BlockedInfoItad(FakeItad):
            def fetch_info(self, game_id):
                raise Blocked("连续收到 403")

        itad3 = BlockedInfoItad()
        with self.assertRaises(Blocked):
            fetch_details(itad3, self.state, self.entries,
                          {**CFG, "detail_fallback_budget": 5},
                          NOW, browse=FakeBrowse())

    def test_failure_marked_and_respected_by_derivation(self):
        """降级仍失败 → 写失败标记；下一轮派生欠账时被冷却排除。"""
        itad = FakeItad(lookup={})   # 全部未命中
        browse = FakeBrowse()
        fetch_details(itad, self.state, self.entries, CFG, NOW, browse=browse)
        for i in range(3):
            meta = self.state.meta(f"uuid-{i}")
            self.assertIsNotNone(meta.get("detail_failed_at"))
            self.assertEqual(meta["detail_attempts"], 1)
        self.assertEqual(itad.asked_info, [e["game_id"] for e in self.entries])
        # 大促模拟（验收）：中断/失败后重跑，第二轮派生不再包含冷却中的失败项
        targets, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual(info["backlog"], 0)

    def test_idempotent_second_run(self):
        """第二轮全部命中 TTL：零请求（lookup / GetItems / info/v2 都不发）。"""
        itad = FakeItad(lookup={f"uuid-{i}": 100 + i for i in range(3)})
        browse = FakeBrowse(metas={100 + i: game_meta(100 + i) for i in range(3)})
        fetch_details(itad, self.state, self.entries, CFG, NOW, browse=browse)
        itad2, browse2 = FakeItad(), FakeBrowse()
        stats = fetch_details(itad2, self.state, self.entries, CFG, NOW, browse=browse2)
        self.assertEqual(stats["fetched"], 0)
        self.assertEqual(itad2.asked_lookup, [])
        self.assertEqual(itad2.asked_info, [])
        self.assertEqual(browse2.batches, [])

    def test_stale_backlog_refetched_after_interrupt(self):
        """大促模拟：GetItems 只成功一半且降级被截断 → 重跑第二轮目标仍含未抓完的。"""
        for entry in self.entries:
            self.state.record_seen(entry, NOW)   # 欠账派生自 seen_deal，先落库
        itad = FakeItad(lookup={f"uuid-{i}": 100 + i for i in range(3)})
        # 第一轮：GetItems 只回 uuid-0 的，其余缺条；降级预算 0 → 全部欠着
        browse1 = FakeBrowse(metas={100: game_meta(100)})
        cfg0 = {**CFG, "detail_fallback_budget": 0}
        stats = fetch_details(itad, self.state, self.entries, cfg0, NOW, browse=browse1)
        self.assertEqual(stats["fetched"], 1)
        # 第二轮（新进程/新客户端）：派生目标仍包含没抓到的 uuid-1 / uuid-2
        targets, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual([e["game_id"] for e in targets], ["uuid-1", "uuid-2"])
        self.assertEqual(info["backlog"], 2)
        itad2 = FakeItad(info_map={f"uuid-{i}": {"appid": 100 + i,
                                                 "reviews": {"score": 80, "count": 500}}
                                   for i in (1, 2)})
        browse2 = FakeBrowse(metas={101: game_meta(101), 102: game_meta(102)})
        stats2 = fetch_details(itad2, self.state, targets, CFG, NOW, browse=browse2)
        self.assertEqual(stats2["fetched"], 2)


class S6RefreshPolicyTest(unittest.TestCase):
    """S6 折扣感知刷新（spec 决策 16/17）：非折扣不刷 / unlisted 冻结与翻案 /
    四档 TTL / 分档落库（apply_listing）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, game_id: str, **overrides) -> dict:
        entry = deal(game_id)
        entry.update(overrides)
        self.state.record_seen(entry, NOW)
        return entry

    def test_non_discount_not_refreshed(self):
        """验收：非折扣期（expiry 已过）条目不再进入详情目标 —— 即使从未抓过详情。"""
        self._seed("g-old", expiry="2026-10-01T00:00:00+08:00")
        targets, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual(targets, [])
        self.assertFalse(entry_needs_detail(
            next(iter(self.state.seen_deal.values())), self.state, CFG, NOW))

    def test_unlisted_frozen_within_same_discount(self):
        """决策 17：unlisted 条目同一折扣期内（start 相同）不重抓、不入目标。"""
        self._seed("g-cold")
        self.state.set_unlisted("g-cold", NOW, "2026-10-04T10:00:00+08:00")
        targets, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual([e["game_id"] for e in targets if e["game_id"] == "g-cold"], [])
        self.assertEqual(info["rejudge"], 0)

    def test_unlisted_rejudge_on_new_start(self):
        """验收：unlisted 条目下次折扣（start 变化）→ 重抓一次重判（翻案入口）。"""
        self._seed("g-return")
        self.state.set_unlisted("g-return", NOW, "2026-09-20T10:00:00+08:00")
        targets, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual([e["game_id"] for e in targets], ["g-return"])
        self.assertEqual(targets[0]["_scope"], "rejudge")
        self.assertEqual(info["rejudge"], 1)

    def test_rejudge_future_start_survives_fetch_filter(self):
        """review-s6 P0-1 回归：rejudge 条目新折扣 start 在未来（跨时区时间戳）
        不得被 fetch_details 内层 discount_active 闸门静默剔除 ——
        否则 run_log 的 detail_rejudge 计数与实际请求数不符。"""
        self._seed("g-return", start="2026-10-05T09:00:00+08:00")   # NOW +17h
        self.state.set_unlisted("g-return", NOW, "2026-09-20T10:00:00+08:00")
        targets, info = detail_targets([], self.state, CFG, NOW)
        self.assertEqual(info["rejudge"], 1)
        itad = FakeItad(lookup={"g-return": 100},
                        info_map={"g-return": {"appid": 100,
                                               "reviews": {"score": 85, "count": 5000}}})
        browse = FakeBrowse(metas={100: game_meta(100)})
        stats = fetch_details(itad, self.state, targets, CFG, NOW, browse=browse)
        self.assertEqual(stats["fetched"], 1)
        self.assertEqual(itad.asked_lookup, ["g-return"])   # GetItems 命中，info/v2 不该发生

    def test_new_game_daily_ttl(self):
        """新游（release_date ≤30 天）TTL 1 天：12h 前抓过不重抓，26h 前抓过要重抓。"""
        self._seed("g-young")
        self.state.set_release_date("g-young", int(NOW.timestamp()) - 5 * 86400)
        entry = next(iter(self.state.seen_deal.values()))
        from datetime import timedelta
        self.state.set_meta("g-young", 100, {"score": 80, "count": 500},
                            NOW - timedelta(hours=12))
        self.assertFalse(entry_needs_detail(entry, self.state, CFG, NOW))
        self.state.dyn("g-young")["fetched_at"] = (
            NOW - timedelta(hours=26)).isoformat(timespec="seconds")
        self.assertTrue(entry_needs_detail(entry, self.state, CFG, NOW))

    def test_old_game_three_day_ttl(self):
        """普通折扣条目 TTL 3 天：2 天前抓过不重抓，4 天前抓过要重抓。"""
        self._seed("g-oldgame")
        self.state.set_release_date("g-oldgame", int(NOW.timestamp()) - 300 * 86400)
        entry = next(iter(self.state.seen_deal.values()))
        from datetime import timedelta
        self.state.set_meta("g-oldgame", 100, {"score": 80, "count": 500},
                            NOW - timedelta(days=2))
        self.assertFalse(entry_needs_detail(entry, self.state, CFG, NOW))
        self.state.dyn("g-oldgame")["fetched_at"] = (
            NOW - timedelta(days=4)).isoformat(timespec="seconds")
        self.assertTrue(entry_needs_detail(entry, self.state, CFG, NOW))

    def test_expiry_window_one_day_ttl(self):
        """到期窗口（expiry −48h 起）TTL 1 天：2 天前的数据在窗口内已算陈旧。"""
        self._seed("g-ending", expiry="2026-10-05T10:00:00+08:00")   # NOW +18h，进 48h 窗口
        entry = next(iter(self.state.seen_deal.values()))
        from datetime import timedelta
        self.state.set_meta("g-ending", 100, {"score": 80, "count": 500},
                            NOW - timedelta(days=2))
        self.assertTrue(entry_needs_detail(entry, self.state, CFG, NOW))

    def test_apply_listing_marks_cold_and_drops_dynamic(self):
        """验收：抓完详情后分档落库 —— COLD 打 unlisted 并丢动态数据。"""
        self._seed("g-cold")
        self.state.set_meta("g-cold", 100, {"score": 90, "count": 12}, NOW)  # count<100
        listing = apply_listing(self.state, [deal("g-cold")], CFG, NOW)
        self.assertEqual(listing, {"unlisted": 1, "relisted": 0})
        self.assertIsNotNone(self.state.unlisted("g-cold"))
        self.assertIsNone(self.state.dyn("g-cold"))   # 动态数据已丢（省空间）
        # merge_details：unlisted 归「冷门 / 无数据」而不是「详情待补」（PENDING 会展示）
        item = merge_details(self.state, [deal("g-cold")], CFG)[0]
        self.assertEqual(item["tier"], classify.TIER_COLD)

    def test_apply_listing_relists_recovered_game(self):
        """验收：翻案 —— 下次折扣重判达标后摘标记、转正常记录。"""
        self._seed("g-back")
        self.state.set_unlisted("g-back", NOW, "2026-09-20T10:00:00+08:00")
        self.state.set_meta("g-back", 100, {"score": 85, "count": 5000}, NOW)  # 达标
        listing = apply_listing(self.state, [deal("g-back")], CFG, NOW)
        self.assertEqual(listing, {"unlisted": 0, "relisted": 1})
        self.assertIsNone(self.state.unlisted("g-back"))
        self.assertIsNotNone(self.state.dyn("g-back"))   # 正常记录，动态数据保留


if __name__ == "__main__":
    unittest.main()
