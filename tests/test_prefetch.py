# -*- coding: utf-8 -*-
"""run.py --prefetch（批 B 预抓，.scratch/prefetch/spec.md）的单元测试。

不发任何网络请求：ITAD / Steam 都注入假客户端，覆盖 spec 验收里的
预算截断、最近优先排序、幂等、无报表输出、中文名补齐。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import prefetch_targets, run_prefetch  # noqa: E402
from src import classify  # noqa: E402
from src.state import State  # noqa: E402

NOW_STAMP = "2026-09-22T15:00:00+08:00"


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
            "expiry": "2026-09-30T19:00:00+08:00",
            "storeLow": {"amountInt": 1000, "amount": 10.0},
            "historyLow": {"amountInt": 1000, "amount": 10.0},
            "historyLow_1y": {"amountInt": 1000, "amount": 10.0},
        },
    }


class FakeItadClient:
    def __init__(self, items: list[dict], info_map: dict | None = None):
        self._items = items
        self._info_map = info_map or {}
        self.calls = 0
        self.asked_info: list[str] = []
        self.events: list[dict] = []
        self.limiter = SimpleNamespace(stats=lambda: {})

    def fetch_deals(self, country, shops=61, limit=200, sort="-cut",
                    max_deals=None, sweep="low_only", progress=None):
        return self._items

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
        self.state = State(self.root / "state.json",
                           tz=classify.zone("Asia/Shanghai")).load()
        self.cfg = {
            "timezone": "Asia/Shanghai",
            "country": "CN",
            "state_path": str(self.root / "state.json"),
            "output_dir": str(self.root / "output"),
            "expired_retention_days": 7,
            "sweep_mode": "low_only",
            "prefetch_daily_budget": 300,
            "reviews_ttl_days": 7,
            "reviews_empty_ttl_days": 3,
            "min_cut": 0, "max_price": None,
            "only_type": "game", "exclude_mature": True, "exclude_free": True,
            "run_log_keep": 30,
        }
        # 5 个缺详情的游戏，折扣开始时间各不相同（测「最近优先」）
        self.starts = {
            "uuid-0": "2026-09-20T10:00:00+08:00",
            "uuid-1": "2026-09-22T09:00:00+08:00",
            "uuid-2": "2026-09-21T12:00:00+08:00",
            "uuid-3": "2026-09-19T08:00:00+08:00",
            "uuid-4": "2026-09-22T14:00:00+08:00",
        }
        self.items = [raw_item(gid, start) for gid, start in self.starts.items()]
        self.info_map = {
            f"uuid-{i}": {"appid": 100 + i, "reviews": {"score": 80, "count": 500}}
            for i in range(5)
        }
        self.steam_info = {100 + i: {"name": f"游戏{i}"} for i in range(5)}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, client: FakeItadClient, steam: FakeSteamClient):
        return run_prefetch(self.cfg, state=self.state, client=client, steam=steam)

    # ---- 预算截断 ----
    def test_budget_truncation(self):
        self.cfg["prefetch_daily_budget"] = 2
        client = FakeItadClient(self.items, self.info_map)
        self._run(client, FakeSteamClient(self.steam_info))
        self.assertEqual(len(client.asked_info), 2)
        # 只有 2 个游戏的详情进了缓存
        self.assertEqual(
            sum(1 for m in self.state.game_meta.values() if m.get("appid")), 2)
        # 没抓到的条目依然攒进 seen_deal（先落盘），等下轮预算
        self.assertEqual(len(self.state.seen_deal), 5)

    def test_budget_zero_disables(self):
        self.cfg["prefetch_daily_budget"] = 0
        client = FakeItadClient(self.items, self.info_map)
        self._run(client, FakeSteamClient(self.steam_info))
        self.assertEqual(client.asked_info, [])
        self.assertEqual(len(self.state.seen_deal), 5)  # 攒库照常

    # ---- 最近优先排序 ----
    def test_recent_first_ordering(self):
        client = FakeItadClient(self.items, self.info_map)
        self._run(client, FakeSteamClient(self.steam_info))
        # start 降序：uuid-4 (09-22 14:00) → uuid-1 (09-22 09:00) → uuid-2 → uuid-0 → uuid-3
        self.assertEqual(
            client.asked_info,
            ["uuid-4", "uuid-1", "uuid-2", "uuid-0", "uuid-3"])

    def test_prefetch_targets_missing_start_last(self):
        """没有 start 时间戳的条目排最后（但仍会进目标）。"""
        from datetime import datetime
        tz = classify.zone("Asia/Shanghai")
        entries = [classify.normalize_item(raw_item("uuid-x", start="")),
                   classify.normalize_item(raw_item("uuid-y", start=NOW_STAMP))]
        targets, info = prefetch_targets(entries, self.state, self.cfg, datetime.now(tz))
        self.assertEqual(info["needed"], 2)
        self.assertEqual([e["game_id"] for e in targets], ["uuid-y", "uuid-x"])

    def test_ordering_by_instant_across_offsets(self):
        """start 带不同时区偏移时按绝对时刻排序，而不是字符串字典序（审查修正回归）。

        uuid-a：本地钟 14:00（+08:00）= 06:00 UTC；uuid-b：本地钟 09:00（+02:00）= 07:00 UTC
        → b 更晚发生，应排前面；若按字典序会比错。
        """
        from datetime import datetime
        tz = classify.zone("Asia/Shanghai")
        entries = [
            classify.normalize_item(raw_item("uuid-a", "2026-09-22T14:00:00+08:00")),
            classify.normalize_item(raw_item("uuid-b", "2026-09-22T09:00:00+02:00")),
        ]
        targets, _ = prefetch_targets(entries, self.state, self.cfg, datetime.now(tz))
        self.assertEqual([e["game_id"] for e in targets], ["uuid-b", "uuid-a"])

    # ---- 幂等：第二次几乎零请求 ----
    def test_idempotent_second_run(self):
        self._run(FakeItadClient(self.items, self.info_map),
                  FakeSteamClient(self.steam_info))
        itad2 = FakeItadClient(self.items, self.info_map)
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
        client = FakeItadClient(self.items, self.info_map)
        self._run(client, FakeSteamClient(self.steam_info))
        # 不生成 / 不触碰 output/ 下任何文件
        self.assertEqual([p.name for p in out_dir.iterdir()], ["index.html"])
        self.assertEqual((out_dir / "index.html").read_text(encoding="utf-8"), "old")
        # 但 state 写回且 run_log 有 prefetch 记录
        self.assertTrue((self.root / "state.json").exists())
        self.assertEqual(self.state.last_run().get("mode"), "prefetch")

    # ---- 中文名补齐（Steam 逐游戏，两条独立缓存）----
    def test_title_zh_fetched_via_steam(self):
        client = FakeItadClient(self.items, self.info_map)
        steam = FakeSteamClient(self.steam_info)
        self._run(client, steam)
        # 每个游戏详情补齐后都顺带补了中文名（顺序同样按最近优先：start 降序）
        self.assertEqual(steam.asked_info, [104, 101, 102, 100, 103])
        self.assertEqual(self.state.title_zh("uuid-2"), "游戏2")
        # run_log 记录了中文名抓取数
        self.assertEqual(self.state.last_run().get("title_zh_fetched"), 5)

    def test_title_only_target_skips_itad(self):
        """已有详情、只缺中文名的条目：不发 ITAD 请求，只发 Steam 请求。"""
        from datetime import datetime
        now = datetime.now(classify.zone("Asia/Shanghai"))
        self.state.set_meta("uuid-0", 100, {"score": 80, "count": 500}, now)
        client = FakeItadClient(self.items, self.info_map)
        steam = FakeSteamClient(self.steam_info)
        self._run(client, steam)
        self.assertNotIn("uuid-0", client.asked_info)
        self.assertIn(100, steam.asked_info)
        self.assertEqual(self.state.title_zh("uuid-0"), "游戏0")


if __name__ == "__main__":
    unittest.main()
