# -*- coding: utf-8 -*-
"""ITAD ``GET /games/info/v2`` 解析的单元测试（快照 v3 新字段的来源）。

不发真实请求：注入假 session 与固定时钟的限流器。
钉住三个解析契约（code-review 2026-09-30 之后的行为）：

1. ``publishers`` / ``developers`` 收敛成 ``[{"id", "name"}]``；
   单项缺 name 保留空串（id 是同厂商判据，不能丢），id/name 全缺才丢；
2. ``stats`` 整体缺失返回 ``None``（让 ``set_meta`` 保留旧值），
   而不是全 null 的 dict —— 后者会把已回填的有效 stats 覆盖掉；
3. 响应不是 dict（异常形态）返回 ``None``。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.itad import ItadClient  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402


class FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls += 1
        return FakeResp(self.payload)


def make_client(payload) -> tuple[ItadClient, FakeSession]:
    limiter = RateLimiter("itad", 800, 300, min_interval=0,
                          clock=lambda: 0.0, sleep=lambda s: None)
    session = FakeSession(payload)
    client = ItadClient(api_key="test-key", limiter=limiter, timeout=1,
                        session=session, sleep=lambda s: None)
    return client, session


FULL = {
    "appid": "570",
    "type": "game",
    "reviews": [{"source": "Steam", "score": 92, "count": 12345}],
    "publishers": [{"id": 369, "name": "SEGA"}, {"id": 1}],
    "developers": [{"id": 366, "name": "ATLUS"}, {"name": "NoId"}],
    "stats": {"rank": 385, "waitlisted": 16847, "collected": 6601},
}


class FetchInfoTest(unittest.TestCase):
    def test_parses_parties_reviews_stats(self):
        client, _ = make_client(FULL)
        info = client.fetch_info("uuid-1")
        self.assertEqual(info["appid"], 570)
        self.assertEqual(info["reviews"], {"score": 92, "count": 12345})
        # 只有 id 的厂商项保留、name 置空串；只有 name 的项同理保留、id 为 None
        # （对称规则：id / name 至少有一个就保留，id 与 name 全缺才丢）
        self.assertEqual(
            info["publishers"], [{"id": 369, "name": "SEGA"}, {"id": 1, "name": ""}]
        )
        self.assertEqual(
            info["developers"],
            [{"id": 366, "name": "ATLUS"}, {"id": None, "name": "NoId"}],
        )
        self.assertEqual(
            info["stats"],
            {"rank": 385, "waitlisted": 16847, "collected": 6601},
        )

    def test_missing_stats_is_none_not_all_null_dict(self):
        """stats 整体缺失 ⇒ None。全 null dict 会绕过 set_meta 的「保留旧值」保护。"""
        payload = {k: v for k, v in FULL.items() if k != "stats"}
        client, _ = make_client(payload)
        info = client.fetch_info("uuid-1")
        self.assertIsNone(info["stats"])

    def test_missing_parties_become_empty_lists(self):
        payload = {k: v for k, v in FULL.items()
                   if k not in ("publishers", "developers", "stats")}
        client, _ = make_client(payload)
        info = client.fetch_info("uuid-1")
        self.assertEqual(info["publishers"], [])
        self.assertEqual(info["developers"], [])
        self.assertIsNone(info["stats"])

    def test_non_dict_body_returns_none(self):
        client, _ = make_client([1, 2, 3])
        self.assertIsNone(client.fetch_info("uuid-1"))

    def test_no_steam_review_is_none(self):
        payload = {k: v for k, v in FULL.items() if k != "reviews"}
        client, _ = make_client(payload)
        self.assertIsNone(client.fetch_info("uuid-1")["reviews"])


if __name__ == "__main__":
    unittest.main()
