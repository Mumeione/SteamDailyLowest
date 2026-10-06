# -*- coding: utf-8 -*-
"""run.py --prefetch（批 B 预抓，.scratch/prefetch/spec.md）的单元测试。

S8 起 prefetch **不扫描折扣列表**：目标纯 state 派生（复用 detail_targets 公式 +
「只缺中文名」特有分支），测试改为先给 state 预攒库（模拟主跑已完成），再跑预抓。
不发任何网络请求：ITAD / Steam 都注入假客户端，覆盖预算截断、最近优先排序、
幂等、无报表输出、中文名补齐。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import prefetch_targets, run_prefetch  # noqa: E402
from src import classify  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW_STAMP = "2026-09-22T15:00:00+08:00"
# S6 起非折扣期（expiry 已过）不刷新 —— expiry 必须永远落在「未来」测试才有意义
EXPIRY = (datetime.now(TZ) + timedelta(days=30)).isoformat(timespec="seconds")


def raw_item(game_id: str, start: str, title: str = "T") -> dict:
    """一条能通过 normalize_item + funnel 的最小 ITAD item（本体 / 付费 / 新史低）。"""
    return {
        "id": game_id, "slug": game_id, "title": title,
        "type": "game", "mature": False,
        "assets": {"boxart": f"https://x/{game_id}.jpg"},
        "deal": {
            "shop": {"id": 61, "name": "Steam"},
            "price": {"amountInt": 1000, "amount": 10.0, "currency": "CNY"},
            "regular": {"amountInt": 10000, "amount": 100.0},
            "cut": 90, "flag": "N",
            "timestamp": start,
            "expiry": EXPIRY,
            "storeLow": {"amountInt": 1000, "amount": 10.0},
            "historyLow": {"amountInt": 1000, "amount": 10.0},
            "historyLow_1y": {"amountInt": 1000, "amount": 10.0},
        },
    }


class FakeItadClient:
    def __init__(self, info_map: dict | None = None, lookup: dict | None = None):
        self._info_map = info_map or {}
        self._lookup = lookup or {}
        self.calls = 0
        self.asked_info: list[str] = []
        self.asked_lookup: list[str] = []
        self.events: list[dict] = []
        self.limiter = SimpleNamespace(stats=lambda: {})

    def fetch_deals(self, country, shops=61, limit=200, sort="-cut",
                    max_deals=None, sweep="low_only", progress=None):
        raise AssertionError("S8 起 prefetch 不扫描折扣列表，fetch_deals 不应被调用")

    def fetch_appid_batch(self, uuids, shop=61, batch_size=5000):
        self.asked_lookup.extend(uuids)
        return {u: a for u, a in self._lookup.items() if u in set(uuids)}

    def fetch_info(self, game_id):
        self.calls += 1
        self.asked_info.append(game_id)
        return self._info_map.get(game_id)


class FakeSteamClient:
    def __init__(self, info_map: dict | None = None):
        self._info_map = info_map or {}
        self.calls = 0
        self.asked_info: list[int] = []
        self.events: list[dict] = []
        self.limiter = SimpleNamespace(stats=lambda: {})

    def info(self, appid, cc="CN"):
        self.calls += 1
        self.asked_info.append(int(appid))
        return self._info_map.get(int(appid))


class PrefetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = State(self.root / "state.json", tz=TZ).load()
        self.now = datetime.now(TZ)
        self.cfg = {
            "timezone": "Asia/Shanghai",
            "country": "CN",
            "state_path": str(self.root / "state.json"),
            "output_dir": str(self.root / "output"),
            "expired_retention_days": 7,
            "sweep_mode": "low_only",
            "prefetch_daily_budget": 300,
            "new_game_days": 30,
            "new_game_refresh_days": 1,
            "discount_refresh_days": 3,
            "expiry_refresh_days": 1,
            "detail_retry_cooldown_days": 3,
            "min_cut": 0, "max_price": None,
            "only_type": "game", "exclude_mature": True, "exclude_free": True,
            "run_log_keep": 30,
        }
        # 5 个缺详情的游戏，折扣开始时间各不相同（测「最近优先」）。
        # S8 起由测试预攒库（模拟主跑已把当天条目写进 seen_deal）。
        self.starts = {
            "uuid-0": "2026-09-20T10:00:00+08:00",
            "uuid-1": "2026-09-22T09:00:00+08:00",
            "uuid-2": "2026-09-21T12:00:00+08:00",
            "uuid-3": "2026-09-19T08:00:00+08:00",
            "uuid-4": "2026-09-22T14:00:00+08:00",
        }
        self.info_map = {
            f"uuid-{i}": {"appid": 100 + i, "reviews": {"score": 80, "count": 500}}
            for i in range(5)
        }
        self.steam_info = {100 + i: {"name": f"游戏{i}"} for i in range(5)}

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        """模拟主跑攒库：把 5 个游戏写进 seen_deal（first_seen = 现在 → 当日新增）。"""
        for gid, start in self.starts.items():
            self.state.record_seen(
                classify.normalize_item(raw_item(gid, start)), self.now)

    def _run(self, client: FakeItadClient, steam: FakeSteamClient):
        return run_prefetch(self.cfg, state=self.state, client=client, steam=steam)

    # ---- 预算截断 ----
    def test_budget_truncation(self):
        self._seed()
        self.cfg["prefetch_daily_budget"] = 2
        client = FakeItadClient(self.info_map)
        self._run(client, FakeSteamClient(self.steam_info))
        self.assertEqual(len(client.asked_info), 2)
        # 只有 2 个游戏的详情进了缓存
        self.assertEqual(
            sum(1 for m in self.state.game_meta.values() if m.get("appid")), 2)
        # 没抓到的条目依然留在 seen_deal（主跑已攒库），等下轮预算
        self.assertEqual(len(self.state.seen_deal), 5)

    def test_budget_zero_disables(self):
        self._seed()
        self.cfg["prefetch_daily_budget"] = 0
        client = FakeItadClient(self.info_map)
        self._run(client, FakeSteamClient(self.steam_info))
        self.assertEqual(client.asked_info, [])
        self.assertEqual(len(self.state.seen_deal), 5)  # 攒库不受影响

    # ---- 最近优先排序 ----
    def test_recent_first_ordering(self):
        self._seed()
        client = FakeItadClient(self.info_map)
        self._run(client, FakeSteamClient(self.steam_info))
        # start 降序：uuid-4 (09-22 14:00) → uuid-1 (09-22 09:00) → uuid-2 → uuid-0 → uuid-3
        self.assertEqual(
            client.asked_info,
            ["uuid-4", "uuid-1", "uuid-2", "uuid-0", "uuid-3"])

    def test_prefetch_targets_missing_start_last(self):
        """没有 start 时间戳的条目排最后（但仍会进目标）。"""
        for gid, start in (("uuid-x", ""), ("uuid-y", NOW_STAMP)):
            self.state.record_seen(
                classify.normalize_item(raw_item(gid, start)), self.now)
        targets, info = prefetch_targets(self.state, self.cfg, self.now)
        self.assertEqual(info["total"], 2)
        self.assertEqual([e["game_id"] for e in targets], ["uuid-y", "uuid-x"])

    def test_ordering_by_instant_across_offsets(self):
        """start 带不同时区偏移时按绝对时刻排序，而不是字符串字典序（审查修正回归）。

        uuid-a：本地钟 14:00（+08:00）= 06:00 UTC；uuid-b：本地钟 09:00（+02:00）= 07:00 UTC
        → b 更晚发生，应排前面；若按字典序会比错。
        """
        for gid, start in (("uuid-a", "2026-09-22T14:00:00+08:00"),
                           ("uuid-b", "2026-09-22T09:00:00+02:00")):
            self.state.record_seen(
                classify.normalize_item(raw_item(gid, start)), self.now)
        targets, _ = prefetch_targets(self.state, self.cfg, self.now)
        self.assertEqual([e["game_id"] for e in targets], ["uuid-b", "uuid-a"])

    # ---- 幂等：第二次几乎零请求 ----
    def test_idempotent_second_run(self):
        self._seed()
        self._run(FakeItadClient(self.info_map), FakeSteamClient(self.steam_info))
        itad2 = FakeItadClient(self.info_map)
        steam2 = FakeSteamClient(self.steam_info)
        self._run(itad2, steam2)
        # 详情与中文名全部命中缓存：零请求
        self.assertEqual(itad2.asked_info, [])
        self.assertEqual(steam2.asked_info, [])
        # 状态库不产生重复条目
        self.assertEqual(len(self.state.seen_deal), 5)

    # ---- 无报表输出 ----
    def test_no_report_output(self):
        out_dir = self.root / "output"
        out_dir.mkdir()
        (out_dir / "index.html").write_text("old", encoding="utf-8")
        self._seed()
        self._run(FakeItadClient(self.info_map), FakeSteamClient(self.steam_info))
        # 不生成 / 不触碰 output/ 下任何文件
        self.assertEqual([p.name for p in out_dir.iterdir()], ["index.html"])
        self.assertEqual((out_dir / "index.html").read_text(encoding="utf-8"), "old")
        # 但 state 写回且 run_log 有 prefetch 记录
        self.assertTrue((self.root / "state.json").exists())
        self.assertEqual(self.state.last_run().get("mode"), "prefetch")

    def test_run_log_marks_state_derived(self):
        """S8 起 run_log 记录 derived_from_state，且不再有 sweep/翻页字段。"""
        self._seed()
        self._run(FakeItadClient(self.info_map), FakeSteamClient(self.steam_info))
        rec = self.state.last_run()
        self.assertTrue(rec.get("derived_from_state"))
        self.assertNotIn("deals_fetched", rec)
        self.assertNotIn("hist_low_total", rec)
        self.assertEqual(rec.get("detail_new_today"), 5)

    # ---- 中文名补齐（Steam 逐游戏，两条独立缓存）----
    def test_title_zh_fetched_via_steam(self):
        self._seed()
        client = FakeItadClient(self.info_map)
        steam = FakeSteamClient(self.steam_info)
        self._run(client, steam)
        # 每个游戏详情补齐后都顺带补了中文名（顺序同样按最近优先：start 降序）
        self.assertEqual(steam.asked_info, [104, 101, 102, 100, 103])
        self.assertEqual(self.state.title_zh("uuid-2"), "游戏2")
        # run_log 记录了中文名抓取数
        self.assertEqual(self.state.last_run().get("title_zh_fetched"), 5)

    def test_title_only_target_skips_itad(self):
        """已有详情、只缺中文名的条目：不发 ITAD 请求，只发 Steam 请求。"""
        self.state.set_meta("uuid-0", 100, {"score": 80, "count": 500}, self.now)
        self._seed()
        client = FakeItadClient(self.info_map)
        steam = FakeSteamClient(self.steam_info)
        self._run(client, steam)
        self.assertNotIn("uuid-0", client.asked_info)
        self.assertIn(100, steam.asked_info)
        self.assertEqual(self.state.title_zh("uuid-0"), "游戏0")


if __name__ == "__main__":
    unittest.main()
