# -*- coding: utf-8 -*-
"""跨区比价 S7 换模型的单元测试：现价真查 + 真查即校准（原价回写/重定价日志）。

验收（spec §6）：跨区原价缓存不再随折扣期失效；区域重定价能被
「真查即校准」捕获并记日志。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.enrich import enrich_steam  # noqa: E402
from src.httpclient import HttpError  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 3, 0, tzinfo=TZ)

CFG = {"compare_countries": ["UA", "IN"], "country": "CN"}


def make_entry(appid=111, game_id="uuid-1", price_int=1000) -> dict:
    return {"game_id": game_id, "appid": appid, "title": "Some Game",
            "price_int": price_int, "expiry": "2026-10-08T19:00:00+02:00"}


class FakeSteam:
    """只实现 enrich_steam 用到的两个方法；price_overview 给 initial/final。"""

    def __init__(self, prices_by_cc=None, info_by_appid=None, fail_ccs=()):
        self.prices_by_cc = prices_by_cc or {}
        self.info_by_appid = info_by_appid or {}
        self.fail_ccs = set(fail_ccs)
        self.price_calls: list[tuple[tuple, str]] = []

    def prices(self, appids, cc):
        if cc in self.fail_ccs:
            raise HttpError(f"boom {cc}")
        self.price_calls.append((tuple(appids), cc))
        return {a: p for a, p in self.prices_by_cc.get(cc, {}).items() if a in appids}

    def info(self, appid, cc="CN"):
        return self.info_by_appid.get(appid)


class EnrichCompareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()
        self.logs: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_enrich(self, client, entries):
        return enrich_steam(client, self.state, entries, CFG, None, NOW,
                            self.logs.append)

    def test_fresh_rows_from_real_query_and_first_write_not_repriced(self):
        """现价行来自真查响应；原价首见写入不算重定价、不记日志。"""
        client = FakeSteam(prices_by_cc={
            "UA": {111: {"currency": "UAH", "initial": 2500, "final": 2000}},
            "IN": {111: {"currency": "INR", "initial": 1800, "final": 1500}},
        })
        entry = make_entry()
        facts = self.run_enrich(client, [entry])

        rows = {r["cc"]: r for r in entry["compare"]}
        self.assertEqual(rows["UA"]["final"], 2000)          # 现价来自真查
        self.assertEqual(rows["UA"]["currency"], "UAH")
        self.assertEqual(rows["IN"]["final"], 1500)
        self.assertEqual(self.state.compare_original(111, "UA"), 2500)   # 原价入库
        self.assertEqual(self.state.compare_original(111, "IN"), 1800)
        self.assertEqual(facts["compare_repriced"], 0)
        self.assertEqual(facts["compare_batches"], 2)
        self.assertEqual(facts["compare_fetched"], 1)
        self.assertFalse(any("重定价" in line for line in self.logs))

    def test_repricing_detected_logged_and_written_back(self):
        """真查响应的原价与缓存不一致 = 区域重定价：记日志 + 回写自愈。"""
        self.state.set_compare_original(111, "UA", 2500, "UAH", NOW)
        client = FakeSteam(prices_by_cc={
            "UA": {111: {"currency": "UAH", "initial": 2100, "final": 1700}},
            "IN": {111: {"currency": "INR", "initial": 1800, "final": 1500}},
        })
        entry = make_entry()
        facts = self.run_enrich(client, [entry])

        self.assertEqual(facts["compare_repriced"], 1)
        self.assertEqual(self.state.compare_original(111, "UA"), 2100)
        self.assertTrue(any("区域重定价（UA）" in line and "2500 → 2100" in line
                            for line in self.logs))

    def test_failed_cc_means_missing_rows_no_fallback(self):
        """真查失败的区域该轮直接缺行（不回落），成功区照常渲染（下轮自愈）。"""
        client = FakeSteam(
            prices_by_cc={"IN": {111: {"currency": "INR", "initial": 1800, "final": 1500}}},
            fail_ccs=("UA",))
        entry = make_entry()
        facts = self.run_enrich(client, [entry])

        self.assertEqual([r["cc"] for r in entry["compare"]], ["IN"])
        self.assertEqual(facts["errors"], 1)
        self.assertIsNone(self.state.compare_original(111, "UA"))   # 没收到就没有
        self.assertTrue(any("缺行" in line for line in self.logs))

    def test_entries_without_appid_get_no_requests(self):
        client = FakeSteam(prices_by_cc={"UA": {}, "IN": {}})
        facts = self.run_enrich(client, [{"game_id": "g", "title": "T", "price_int": 1}])
        self.assertEqual(client.price_calls, [])
        self.assertEqual(facts["compare_batches"], 0)


if __name__ == "__main__":
    unittest.main()
