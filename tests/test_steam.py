# -*- coding: utf-8 -*-
"""SteamClient 单 appid 中文名的单元测试（docs/DEVELOPMENT.md §2.2 / §2.5）。

不发任何网络请求：子类覆盖 `request()` 返回预设 JSON（同 `tests/test_storelow.py` 的
`FakeRequestClient` 手法），并顺带断言打到的是生产用的「单 appid + `SINGLE_FILTERS`」组合。

背景（2026-09-25 实测，见 DEVELOPMENT.md §2.2）：`filters` 含 `basic` 时响应的 key 是
**店铺页 id**、不是 appid —— 请求 412020（Metro Exodus）返回 `{"1952352": {...}}`，
而 `data.steam_appid` 仍是 412020。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ratelimit import RateLimiter  # noqa: E402
from src.steam import SINGLE_FILTERS, SteamClient  # noqa: E402

#: Metro Exodus：请求 412020，响应 key 却是店铺页 id 1952352
METRO_REDIRECTED = {
    "1952352": {
        "success": True,
        "data": {
            "steam_appid": 412020,
            "name": "地铁：离乡",
            "price_overview": {
                "currency": "CNY", "initial": 19900,
                "final": 3976, "discount_percent": 80,
            },
        },
    },
}


class FakeSteamRequestClient(SteamClient):
    """替换掉 `request()` 的 SteamClient：返回预设响应，并校验请求形状。"""

    def __init__(self, response: dict):
        super().__init__(RateLimiter("steam", 150, 300, min_interval=0))
        self._response = response

    def request(self, method, path, params=None, json_body=None):
        assert method == "GET" and path == "/api/appdetails"
        assert params["cc"] == "cn" and params["l"] == "schinese"
        # 生产用的就是这一组 —— 也正是触发重定向的那组
        assert params["filters"] == SINGLE_FILTERS
        return self._response


class InfoRedirectTest(unittest.TestCase):
    def test_name_survives_appid_redirect(self):
        """Steam 把请求 appid 重定向后，单 appid 请求仍应取到中文名与该区价格。"""
        client = FakeSteamRequestClient(METRO_REDIRECTED)

        info = client.info(412020)

        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "地铁：离乡")
        self.assertEqual(info["final"], 3976)


class InfoMissingTest(unittest.TestCase):
    def test_unsuccessful_entry_returns_none(self):
        """该区不售 / appid 不存在（`success: false`）时返回 None，不编造数据。"""
        client = FakeSteamRequestClient({"1952352": {"success": False}})

        self.assertIsNone(client.info(412020))


if __name__ == "__main__":
    unittest.main()