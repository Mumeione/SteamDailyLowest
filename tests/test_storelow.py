# -*- coding: utf-8 -*-
"""「上次史低时间」（§3.6 storelow/v2）的单元测试。

不发任何网络请求：解析层用桩替换 ``ItadClient.request``，
编排层与渲染层注入假客户端 / 直接构造条目。
覆盖：批量分批与解析（取 Steam 店 low、混合时区偏移时间戳）、
state 写入、build_card 显示规则（N=本次刷新历史记录 / H/S=X 天前 / 缺数据不渲染）、
失败不阻断。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import fetch_last_low_times  # noqa: E402
from src import classify  # noqa: E402
from src.itad import ItadClient  # noqa: E402
from src.report import build_card  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=TZ)


def storelow_row(game_id: str, amount_int: int, ts: str, shop_id: int = 61) -> dict:
    """一条与 2026-09-23 实测同构的 storelow/v2 响应元素。"""
    return {
        "id": game_id,
        "lows": [{
            "shop": {"id": shop_id, "name": "Steam"},
            "price": {"amount": amount_int / 100, "amountInt": amount_int, "currency": "CNY"},
            "regular": {"amount": amount_int / 50, "amountInt": amount_int * 2, "currency": "CNY"},
            "cut": 50,
            "timestamp": ts,
        }],
    }


class FakeRequestClient(ItadClient):
    """替换掉 request() 的 ItadClient：按批返回预设响应，记录每次的 body。"""

    def __init__(self, responses: list[list[dict]]):
        super().__init__(api_key="k", limiter=SimpleNamespace(
            acquire=lambda: None, wait=lambda s: None, slow_down=lambda: None,
            stats=lambda: {}))
        self._responses = responses
        self.bodies: list[list[str]] = []

    def request(self, method, path, params=None, json_body=None):
        assert method == "POST" and path == "/games/storelow/v2"
        assert params["country"] == "CN" and params["shops"] == 61
        self.bodies.append(list(json_body))
        idx = len(self.bodies) - 1
        return self._responses[idx] if idx < len(self._responses) else []


class FetchStorelowParsing(unittest.TestCase):
    def test_batches_of_200_and_picks_steam_low(self):
        ids = [f"uuid-{i:03d}" for i in range(201)]
        responses = [
            [storelow_row(g, 1000, f"2021-06-24T21:52:22+02:00") for g in ids[:200]],
            [storelow_row(ids[200], 500, "2025-09-18T03:00:00+08:00")],
        ]
        client = FakeRequestClient(responses)
        lows_map = client.fetch_storelow("CN", ids)
        self.assertEqual(len(client.bodies), 2)          # 201 个 → 分 2 批
        self.assertEqual(len(client.bodies[0]), 200)
        self.assertEqual(client.bodies[1], [ids[200]])
        self.assertEqual(lows_map[ids[0]], "2021-06-24T21:52:22+02:00")
        self.assertEqual(lows_map[ids[200]], "2025-09-18T03:00:00+08:00")

    def test_missing_ids_not_in_result_and_nonsteam_fallback(self):
        client = FakeRequestClient([[
            storelow_row("uuid-a", 1000, "2021-06-24T21:52:22+02:00"),
            {"id": "uuid-b", "lows": []},                       # 无 low → 跳过
            storelow_row("uuid-c", 100, "2020-01-01T00:00:00Z", shop_id=15),  # 非 Steam 店
        ]])
        lows_map = client.fetch_storelow("CN", ["uuid-a", "uuid-b", "uuid-c", "uuid-d"])
        self.assertEqual(set(lows_map), {"uuid-a", "uuid-c"})
        self.assertEqual(lows_map["uuid-c"], "2020-01-01T00:00:00Z")  # 无 61 号店时回落第一条


class FetchLastLowTimes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ).load()
        self.addCleanup(self.tmp.cleanup)

    def test_writes_state_and_counts(self):
        candidates = [{"game_id": "uuid-a", "title": "A"},
                      {"game_id": "uuid-b", "title": "B"}]
        client = SimpleNamespace(
            fetch_storelow=lambda country, ids: {"uuid-a": "2021-06-24T21:52:22+02:00"})
        n = fetch_last_low_times(client, self.state, candidates,
                                 {"country": "CN", "fetch_last_low_time": True}, NOW)
        self.assertEqual(n, 1)
        self.assertEqual(self.state.meta("uuid-a")["last_low_at"],
                         "2021-06-24T21:52:22+02:00")
        self.assertIsNone(self.state.meta("uuid-b"))

    def test_disabled_or_empty_noop(self):
        client = SimpleNamespace(fetch_storelow=lambda *a, **k: self.fail("不应发请求"))
        self.assertEqual(fetch_last_low_times(client, self.state, [{"game_id": "x"}],
                                              {"country": "CN", "fetch_last_low_time": False}, NOW), 0)
        self.assertEqual(fetch_last_low_times(client, self.state, [],
                                              {"country": "CN", "fetch_last_low_time": True}, NOW), 0)

    def test_itad_error_swallowed(self):
        class Boom:
            def fetch_storelow(self, *a, **k):
                from src.itad import ItadError
                raise ItadError("挂了")
        n = fetch_last_low_times(Boom(), self.state, [{"game_id": "x"}],
                                 {"country": "CN", "fetch_last_low_time": True}, NOW)
        self.assertEqual(n, 0)  # 失败不阻断本轮


class BuildCardLastLowText(unittest.TestCase):
    def entry(self, flag: str, last_low_at: str | None) -> dict:
        return {"game_id": "g", "title": "T", "flag": flag,
                "last_low_at": last_low_at, "price_int": 1000, "currency": "CNY"}

    def test_new_low_shows_record_text(self):
        card = build_card(self.entry("N", "2021-06-24T21:52:22+02:00"), NOW)
        self.assertEqual(card["last_low_text"], "本次刷新历史记录")

    def test_equal_low_shows_days_and_date(self):
        card = build_card(self.entry("H", "2026-02-21T10:00:00+08:00"), NOW)
        # 2026-02-21 → 2026-09-23 = 214 天
        self.assertEqual(card["last_low_text"], "214 天前（2026-02-21）")

    def test_missing_data_renders_none(self):
        for entry in (self.entry("H", None), self.entry("S", ""),
                      self.entry(None, "2026-02-21T10:00:00+08:00")):
            self.assertIsNone(build_card(entry, NOW)["last_low_text"])


if __name__ == "__main__":
    unittest.main()
