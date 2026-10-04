# -*- coding: utf-8 -*-
"""``ItadClient.fetch_appid_batch``（POST /lookup/shop/61/id/v1）单元测试（重构 S1）。

不发真实请求，两种手法：

* 覆写 ``request()``：校验请求形状（POST 路径、批大小 5000、去重）与
  **二分降级**（批失败 → 对半拆 → 单条仍失败丢弃）；
* ``CaptureSession``（假 session 走真实 BaseHttpClient 管线）：锁
  **「key 不进 URL、走 ITAD-API-Key 头」**的契约（lookup 免鉴权、不计额度，
  key 出现在 URL 里毫无必要 —— 也与探针红线一致）。

来源形态对照（spec §9 P4/P5）：请求 body 是 uuid 数组；响应是
``{uuid: ["sub/589578", "app/1658920"]}``，``app/`` 与 ``sub/`` 混排。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.httpclient import Blocked, HttpError  # noqa: E402
from src.itad import LOOKUP_BATCH_SIZE, ItadClient  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402


def make_limiter() -> RateLimiter:
    return RateLimiter("itad", 800, 300, min_interval=0,
                       clock=lambda: 0.0, sleep=lambda s: None)


class JsonResp:
    def __init__(self, payload):
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return json.loads(self.text)


class CaptureSession:
    """走真实 BaseHttpClient 管线：记录 method/url/params/json/headers。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": headers})
        return JsonResp(self.payload)


class FakeLookupClient(ItadClient):
    """覆写 request() 的客户端：``fail`` 里的 uuid 出现在批次中就抛 HttpError。"""

    def __init__(self, fail=()):
        super().__init__(api_key="test-key", limiter=make_limiter(), timeout=1,
                         session=CaptureSession({}), sleep=lambda s: None)
        self.fail = set(fail)
        self.calls: list[dict] = []

    def request(self, method, path, params=None, json_body=None, headers=None):
        self.calls.append({"method": method, "path": path, "body": json_body,
                           "params": params, "headers": headers})
        if set(json_body) & self.fail:
            raise HttpError("HTTP 500，重试 4 次仍失败")
        return {uid: BODY.get(uid, ["app/100"]) for uid in json_body}


BODY = {
    "uuid-app": ["sub/589578", "app/1658920"],
    "uuid-sub-only": ["sub/123"],
    "uuid-empty": [],
    "uuid-app-only": ["app/447040"],
}


class MappingTest(unittest.TestCase):
    def test_only_app_prefix_becomes_int(self):
        """app/ 与 sub/ 混排：只挑 app/ 转 int；sub-only / 空列表 = 未命中，不在返回里。"""
        client = FakeLookupClient()
        out = client.fetch_appid_batch(list(BODY.keys()))
        self.assertEqual(out, {"uuid-app": 1658920, "uuid-app-only": 447040})

    def test_garbage_shop_ids_ignored(self):
        client = FakeLookupClient()
        client.request = lambda *a, **k: {"u": [None, 42, "app/", "app/x", "app/570"]}
        self.assertEqual(client.fetch_appid_batch(["u"]), {"u": 570})

    def test_dedup_and_blank_filtered(self):
        client = FakeLookupClient()
        client.fetch_appid_batch(["u1", "u1", "", "u2"])
        self.assertEqual(client.calls[0]["body"], ["u1", "u2"])

    def test_empty_input_makes_no_request(self):
        client = FakeLookupClient()
        self.assertEqual(client.fetch_appid_batch([]), {})
        self.assertEqual(client.fetch_appid_batch(["", None]), {})  # type: ignore[list-item]
        self.assertEqual(client.calls, [])


class RequestShapeTest(unittest.TestCase):
    def test_post_path_and_batch_5000(self):
        """默认 5000/批（实测 20000 也通、保守取 5000）：5001 个 uuid 切成 5000 + 1。"""
        client = FakeLookupClient()
        uuids = [f"u{i}" for i in range(LOOKUP_BATCH_SIZE + 1)]
        client.fetch_appid_batch(uuids)
        self.assertEqual(len(client.calls), 2)
        first, second = client.calls
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["path"], "/lookup/shop/61/id/v1")
        self.assertEqual(len(first["body"]), LOOKUP_BATCH_SIZE)
        self.assertEqual(len(second["body"]), 1)
        self.assertEqual(second["body"][0], "u5000")

    def test_custom_shop_id_in_path(self):
        client = FakeLookupClient()
        client.fetch_appid_batch(["u1"], shop=999)
        self.assertEqual(client.calls[0]["path"], "/lookup/shop/999/id/v1")

    def test_header_and_no_key_in_query(self):
        """key 走 ITAD-API-Key 头（附加 headers），不混进 query。"""
        client = FakeLookupClient()
        client.fetch_appid_batch(["u1"])
        call = client.calls[0]
        self.assertEqual(call["headers"], {"ITAD-API-Key": "test-key"})
        self.assertNotIn("key", call["params"] or {})


class KeyContractTest(unittest.TestCase):
    """走真实 BaseHttpClient 管线锁「key 不进 /lookup/ 的 URL」——
    这条契约由 ItadClient._prepare 实现，覆写 request() 的假客户端测不到它。"""

    def test_lookup_key_in_header_not_url(self):
        session = CaptureSession({"uuid-app": BODY["uuid-app"]})
        client = ItadClient(api_key="secret-key", limiter=make_limiter(),
                            timeout=1, session=session, sleep=lambda s: None)
        out = client.fetch_appid_batch(["uuid-app"])
        call = session.calls[0]
        self.assertTrue(call["url"].startswith(
            "https://api.isthereanydeal.com/lookup/shop/61/id/v1"))
        self.assertNotIn("key", call["params"])
        self.assertEqual(call["headers"].get("ITAD-API-Key"), "secret-key")
        self.assertEqual(out, {"uuid-app": 1658920})

    def test_other_endpoints_keep_key_in_query(self):
        """既有端点（info/v2）key 仍走 query —— 既有行为不被本次改动影响。"""
        session = CaptureSession({"appid": "570", "reviews": [], "stats": None})
        client = ItadClient(api_key="secret-key", limiter=make_limiter(),
                            timeout=1, session=session, sleep=lambda s: None)
        client.fetch_info("uuid-1")
        call = session.calls[0]
        self.assertIn("key", call["params"])
        self.assertEqual(call["params"]["key"], "secret-key")
        self.assertIsNone(call["headers"])


class SplitFallbackTest(unittest.TestCase):
    """二分降级：保留为防御（实测随机无效 uuid 已不复现 500，spec §9 P5）。"""

    def test_binary_split_recovers_good_uuids(self):
        client = FakeLookupClient(fail={"bad"})
        out = client.fetch_appid_batch(["g1", "bad", "g2"])
        self.assertEqual(out, {"g1": 100, "g2": 100})
        # 1 次整批失败 + 对半拆分若干次，肯定不止 1 次请求
        self.assertGreater(len(client.calls), 1)

    def test_single_bad_uuid_dropped_silently(self):
        client = FakeLookupClient(fail={"bad"})
        self.assertEqual(client.fetch_appid_batch(["bad"]), {})
        self.assertEqual(len(client.calls), 1)

    def test_non_dict_response_treated_as_failure(self):
        """响应非对象（异常形态）按批失败处理，同样走二分。"""
        client = FakeLookupClient()

        def non_dict(*args, **kwargs):
            client.calls.append({"body": kwargs.get("json_body")})
            return ["not", "a", "dict"]

        client.request = non_dict
        out = client.fetch_appid_batch(["u1", "u2"])
        self.assertEqual(out, {})
        self.assertGreater(len(client.calls), 1)

    def test_blocked_propagates_without_split(self):
        """连续 403（滥用封禁）不降级、不上抛被吞 —— 直接中止本轮。"""
        class BlockedClient(FakeLookupClient):
            def request(self, method, path, params=None, json_body=None, headers=None):
                raise Blocked("连续收到 403")

        client = BlockedClient()
        with self.assertRaises(Blocked):
            client.fetch_appid_batch(["g1", "g2"])


if __name__ == "__main__":
    unittest.main()
