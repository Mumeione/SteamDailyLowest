# -*- coding: utf-8 -*-
"""Steam 官方客户端（对应 docs/DEVELOPMENT.md §2.2 / §2.5）。

职责：**中文名 + 各区价格**。不消耗 ITAD 配额。limit 与「五种响应」策略在
:mod:`src.httpclient`（`store.steampowered.com` 全站按同一个预算合并计数，
`appdetails` 与 `appreviews` 同 host，保守假设共享 per-IP 预算）。

## ⚠️ 实测出来的 appdetails 批量规则（与早前文档的说法不同）

| 场景 | `filters` | 结果 |
|---|---|---|
| **单** appid | 任意值 / 不带 | 200，`data` 里有 `name` + `price_overview` |
| **多** appid | **只有 `price_overview`** | 200，每个 appid 都有 `price_overview`，**但没有 `name`** |
| **多** appid | 其他任何值（`basic` / `release_date` / …）/ 不带 | **400** |

→ 所以：**批量只能拿各区价格；中文名只能逐游戏取**。
早前文档写的「`appdetails` 批量能同时给中文名与各区价格」是**错的**
（那次验证用的是单 appid）。见 `data/probe/summary27_steam_batch.txt`。

`name` 是否中文只取决于 `l=schinese` 与「Steam 上这个游戏**有没有**中文标题」——
没有中文标题的游戏返回英文名，属正常，不能当失败（§11「Steam 没中文名的回落英文名」）。
"""

from __future__ import annotations

import time
from typing import Callable

import requests

from .httpclient import BaseHttpClient, Blocked, HttpError
from .ratelimit import RateLimiter

BASE = "https://store.steampowered.com"
USER_AGENT = "SteamDailyLowest/1.0 (+https://github.com/)"

#: 多 appid 时唯一可用的 filters 值（其他值一律 400）
BATCH_FILTERS = "price_overview"
#: 单 appid 时用它同时拿 `name` 与 `price_overview`
SINGLE_FILTERS = "basic,price_overview"
#: 实测一次 20 个 appid 正常（文档说的 50 未复核，保守取 20）
DEFAULT_BATCH_SIZE = 20


class SteamError(HttpError):
    """Steam 请求失败。"""


class SteamBlocked(Blocked, SteamError):
    """连续 403 —— 中止本轮。"""


class SteamClient(BaseHttpClient):
    BASE = BASE

    def __init__(self, limiter: RateLimiter, timeout: float = 25, pause: float = 0.0,
                 session: requests.Session | None = None, max_attempts: int = 4,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda msg: None,
                 lang: str = "schinese", batch_size: int = DEFAULT_BATCH_SIZE):
        self.lang = lang
        self.batch_size = max(1, int(batch_size))
        self.parse_errors = 0
        super().__init__(
            limiter=limiter,
            timeout=timeout,
            pause=pause,
            session=session,
            max_attempts=max_attempts,
            sleep=sleep,
            log=log,
            user_agent=USER_AGENT,
        )

    def _endpoint(self, path: str) -> str:
        return "steam"

    # ------------------------------------------------------------------
    # 底层：appdetails
    # ------------------------------------------------------------------
    def _appdetails(self, appids: list[int], cc: str, filters: str) -> dict[int, dict]:
        """调一次 `appdetails`，返回 ``{appid: data}``（拿不到的不在字典里）。"""
        params = {
            "appids": ",".join(str(a) for a in appids),
            "cc": cc.lower(),
            "l": self.lang,
            "filters": filters,
        }
        data = self.request("GET", "/api/appdetails", params=params)
        if not isinstance(data, dict):
            return {}
        out: dict[int, dict] = {}
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            # `success: false` → appid 不存在 / 该区不售（**不是限流**，§10）
            if entry.get("success") is not True:
                continue
            payload = entry.get("data")
            # 永久免费游戏（CS2 / Dota2 / TF2）返回 `data: []`，属正常
            if not isinstance(payload, dict):
                self.parse_errors += 1
                continue
            try:
                out[int(key)] = payload
            except (TypeError, ValueError):
                continue
        return out

    def info(self, appid: int, cc: str = "CN") -> dict | None:
        """**单个** appid：拿 `name`（`l=schinese` 时就是中文名）与该区价格。

        返回 ``{"name": ..., "price": {"currency","initial","final","discount_percent"}}``；
        拿不到返回 None。
        """
        data = self._appdetails([appid], cc, SINGLE_FILTERS)
        payload = data.get(appid)
        if not payload:
            return None
        price = payload.get("price_overview")
        return {
            "name": payload.get("name"),
            "is_free": payload.get("is_free"),
            "currency": (price or {}).get("currency"),
            "initial": (price or {}).get("initial"),
            "final": (price or {}).get("final"),
            "discount_percent": (price or {}).get("discount_percent"),
        }

    def prices(self, appids: list[int], cc: str) -> dict[int, dict]:
        """**批量** appid：只拿该区价格（`name` 拿不到，见模块 docstring）。

        自动按 :attr:`batch_size` 切片；返回 ``{appid: price_overview}``。
        """
        out: dict[int, dict] = {}
        unique = [a for a in dict.fromkeys(appids) if a]
        for start in range(0, len(unique), self.batch_size):
            chunk = unique[start:start + self.batch_size]
            for appid, payload in self._appdetails(chunk, cc, BATCH_FILTERS).items():
                price = payload.get("price_overview")
                if price:
                    out[appid] = price
        return out

    def reviews(self, appid: int, language: str = "all", purchase_type: str = "all") -> dict | None:
        """Steam 官方好评率（§2.5 的**兜底**路径，首选仍是 ITAD `info/v2`）。

        ``query_summary.total_reviews`` 为 0 时返回 None —— 没有样本就没有参考价值。
        """
        data = self.request(
            "GET",
            f"/appreviews/{appid}",
            params={"json": 1, "num_per_page": 0, "language": language,
                    "purchase_type": purchase_type},
        )
        if not isinstance(data, dict):
            return None
        summary = data.get("query_summary") or {}
        total = int(summary.get("total_reviews") or 0)
        if total <= 0:
            return None
        positive = int(summary.get("total_positive") or 0)
        return {"score": int(round(positive / total * 100)), "count": total}


__all__ = ["BASE", "BATCH_FILTERS", "DEFAULT_BATCH_SIZE", "SINGLE_FILTERS",
           "SteamBlocked", "SteamClient", "SteamError"]
