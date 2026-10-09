# -*- coding: utf-8 -*-
"""小黑盒客户端（``src/heybox.py``，2026-10-09）单测。

不发任何网络请求（``session`` 注入假对象）。覆盖：

- :func:`is_cn_name` 质量闸：单字《茧》过 / 假名拒 / 繁体过 / 纯英文拒；
- :func:`parse_name` 响应收敛（status ok / error / 缺 result / 空 name）；
- :meth:`HeyboxClient.fetch_name` 走底座 ``request``（200 正常 / 403×2 →
  :class:`HeyboxBlocked`）—— **架构红线**：``error_cls`` / ``blocked_cls``
  必须挂对，否则调用方 ``except HeyboxError`` 静默失效（卡片 01 的教训）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.heybox import (  # noqa: E402
    HeyboxBlocked,
    HeyboxClient,
    HeyboxError,
    has_cjk,
    is_cn_name,
    parse_name,
)
from src.httpclient import Blocked, HttpError  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402


def _limiter() -> RateLimiter:
    return RateLimiter(name="test", max_calls=1000, window_seconds=60.0,
                       min_interval=0.0, clock=lambda: 0.0, sleep=lambda s: None)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ("null" if payload is None else "x")
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, timeout=None, **kw):
        self.calls.append({"method": method, "url": url, "params": params})
        return self.responses.pop(0)


class IsCnNameTest(unittest.TestCase):
    def test_single_char_cocoon_passes(self):
        # 用户举例：茧（COCOON）—— 中文名可以只有一个字
        self.assertTrue(is_cn_name("茧"))

    def test_normal_and_traditional_pass(self):
        self.assertTrue(is_cn_name("文明6"))
        self.assertTrue(is_cn_name("隻狼"))                 # 繁体原样接受
        self.assertTrue(is_cn_name("艾尔登法环"))

    def test_kana_rejected_even_with_hanzi(self):
        self.assertFalse(is_cn_name("魔女の旅"))            # 含汉字也有假名 → 拒
        self.assertFalse(is_cn_name("ここのつ"))            # 纯假名 → 拒
        self.assertFalse(is_cn_name("ｷﾞﾙﾄﾞ"))              # 半角片假名 → 拒

    def test_pure_hanzi_japanese_original_passes_harmlessly(self):
        # 纯汉字日文原名过闸 —— 无害：本来就是原名，展示等同英文回落
        self.assertTrue(is_cn_name("東方Project"))

    def test_english_and_empty_rejected(self):
        self.assertFalse(is_cn_name("Elden Ring"))
        self.assertFalse(is_cn_name(""))
        self.assertFalse(is_cn_name(None))
        self.assertFalse(is_cn_name("   "))

    def test_has_cjk(self):
        self.assertTrue(has_cjk("茧"))
        self.assertTrue(has_cjk("ab 明 cd"))
        self.assertFalse(has_cjk("Elden Ring"))
        self.assertFalse(has_cjk("ここのつ"))               # 假名不是汉字


class ParseNameTest(unittest.TestCase):
    def test_ok_shape(self):
        payload = {"status": "ok", "msg": "", "result": {"name": "文明6"}}
        self.assertEqual(parse_name(payload), "文明6")

    def test_strips_whitespace(self):
        payload = {"status": "ok", "result": {"name": " 文明6 "}}
        self.assertEqual(parse_name(payload), "文明6")

    def test_status_error_missing_result_and_empty_name(self):
        self.assertIsNone(parse_name({"status": "error", "msg": "game not found"}))
        self.assertIsNone(parse_name({"status": "ok", "result": None}))
        self.assertIsNone(parse_name({"status": "ok", "result": {}}))
        self.assertIsNone(parse_name({"status": "ok", "result": {"name": "  "}}))
        self.assertIsNone(parse_name(None))
        self.assertIsNone(parse_name("junk"))


class HeyboxClientTest(unittest.TestCase):
    def _client(self, responses):
        return HeyboxClient(limiter=_limiter(), session=FakeSession(responses))

    def test_error_cls_hooks_are_wired(self):
        # 架构红线（卡片 01）：传输出口只抛宿主异常类
        self.assertTrue(issubclass(HeyboxError, HttpError))
        self.assertTrue(issubclass(HeyboxBlocked, Blocked))
        self.assertTrue(issubclass(HeyboxBlocked, HeyboxError))
        self.assertIs(HeyboxClient.error_cls, HeyboxError)
        self.assertIs(HeyboxClient.blocked_cls, HeyboxBlocked)

    def test_fetch_name_hits_detail_endpoint_without_hkey(self):
        session = FakeSession([FakeResponse(payload={
            "status": "ok", "result": {"name": "文明6"}})])
        client = self._client([])
        client.session = session
        self.assertEqual(client.fetch_name(289070), "文明6")
        url = session.calls[0]["url"]
        self.assertTrue(url.startswith("https://api.xiaoheihe.cn/game/web/get_game_detail/"))
        # 免 hkey 形态：只有 _time + appid 两个参数，绝不能带 os_type/version
        self.assertEqual(sorted(session.calls[0]["params"]), ["_time", "appid"])
        self.assertEqual(session.calls[0]["params"]["appid"], "289070")

    def test_fetch_name_returns_none_on_miss(self):
        client = self._client([FakeResponse(payload={
            "status": "error", "msg": "not found"})])
        self.assertIsNone(client.fetch_name(999999))

    def test_single_403_raises_blocked_immediately(self):
        # 风控零容忍（abort_on_risk_control=True）：403 第一次就抛 Blocked，
        # 不等 5 分钟、不等第二次 —— 底座默认策略（403×2）只对其他客户端生效
        client = self._client([FakeResponse(status_code=403)])
        with self.assertRaises(HeyboxBlocked):
            client.fetch_name(289070)

    def test_single_429_raises_blocked_immediately(self):
        client = self._client([FakeResponse(status_code=429)])
        with self.assertRaises(HeyboxBlocked):
            client.fetch_name(289070)

    def test_exhausted_retries_raise_host_error(self):
        client = self._client([FakeResponse(status_code=500)] * 4)
        with self.assertRaises(HeyboxError):
            client.fetch_name(289070)


if __name__ == "__main__":
    unittest.main()
