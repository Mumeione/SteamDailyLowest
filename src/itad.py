# -*- coding: utf-8 -*-
"""ITAD 客户端（对应 docs/DEVELOPMENT.md §2.1 / §2.2 / §10）。

只负责网络与解析，不做判定。**限流与「五种响应分开处理」的策略在
:mod:`src.httpclient` 里**（与 Steam 侧共用同一份实现，避免两处漂移）。

**绝不轮换 IP 规避限流。**
"""

from __future__ import annotations

import json
import time
from typing import Callable

import requests

from .httpclient import BaseHttpClient, Blocked, HttpError
from .ratelimit import RateLimiter

BASE = "https://api.isthereanydeal.com"
STEAM_SHOP_ID = 61
USER_AGENT = "SteamDailyLowest/1.0 (+https://github.com/)"

#: ``filter.type`` 里「本体游戏」的取值（1=Game / 2=DLC / 3=Package / 7=Software / 9=Hardware）
GAME_TYPE_ID = 1

#: ``filter.flag`` 的「最宽松」档。**⚠️ flag 是层级语义，不是等值匹配**（实测）：
#: ``N ⊂ H ⊂ S`` —— 传 ``S`` 才会返回**全部史低**（N+H+S）。
FLAG_ANY_LOW = "S"

#: 抓取口径
#: - ``low_only``：服务端只返回「本体游戏 + 史低」→ 27 页 / 5242 条
#: - ``full``：不加 filter，全量 162 页 / 32364 条（体检用）
SWEEP_LOW_ONLY = "low_only"
SWEEP_FULL = "full"
SWEEP_MODES = (SWEEP_LOW_ONLY, SWEEP_FULL)

#: ``/lookup/shop/{shop}/id/v1`` 的保守批大小（实测 ≥20000 也通、1.86s，spec §9 P4）
LOOKUP_BATCH_SIZE = 5000
#: 二分降级最大递归深度：实测单批 20000 也通（spec §9 P4）→ ⌈log2 20000⌉ = 15 层
_LOOKUP_MAX_DEPTH = 15


class ItadError(HttpError):
    """ITAD 请求失败（不可重试或重试耗尽）。"""


class ItadBlocked(Blocked, ItadError):
    """连续 403 —— 滥用封禁，中止本轮并告警。"""


def _party_list(raw) -> list[dict]:
    """把 ``info/v2`` 的 ``publishers`` / ``developers`` 收敛成 ``[{"id", "name"}]``。

    ``id`` 是 ITAD 侧的稳定标识 —— **用它做「同厂商 / 同系列」判定，不要用名字**
    （实测同一厂商有多种写法：``Ubisoft`` / ``Ubisoft Entertainment`` /
    ``Ubisoft Montreal``；只有 id 是同一个）。

    单项缺 ``name`` 时**保留**（``name`` 置空串）—— 消费方按 id 匹配，id 不能丢；
    只有 ``id`` 和 ``name`` 都缺失的项才丢掉。``raw`` 缺键 / null 一律收敛成 ``[]``。
    """
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        pid = item.get("id")
        if not name and pid is None:
            continue
        out.append({"id": pid, "name": str(name) if name else ""})
    return out


def _pick_appid(shop_ids) -> int | None:
    """从 ``['sub/589578', 'app/1658920']`` 里挑 ``app/`` 前缀的 appid。

    lookup 返回值 ``app/`` 与 ``sub/`` **混排**（约 10~15% 的 uuid 只有 ``sub/``，
    拿不到 appid，属正常 —— 命中率实测 85~90.4%，spec §9 P4）→ 只挑 ``app/`` 转 int，
    挑不到返回 None，由调用方识别未命中并决定回落。
    """
    for sid in shop_ids or []:
        if isinstance(sid, str) and sid.startswith("app/"):
            try:
                return int(sid.split("/", 1)[1])
            except (IndexError, ValueError):
                continue
    return None


def sweep_filter(sweep: str) -> dict | None:
    """把抓取口径翻译成 ``/deals/v2`` 的 ``filter`` 参数。

    ``{"type":[1],"flag":"S"}`` 一次查询就等于「本体游戏 + 全部史低」，
    第 24 轮实测 **27 页 / 5242 条**，而无 filter 是 162 页 / 32364 条；
    且这**不影响攒库** —— 状态库本来就只记录史低条目。

    三个实测踩过的坑：
    - ``flag`` 传数组（``["N","H","S"]``）会被服务端**静默忽略**（不报错、不过滤），
      所以只能传单值 ``"S"``；
    - ``steamPerc`` 文档写 0~100，实测**必须传 0~1**（传 70 会静默返回 0 条）；
      而且它与 ``steamCount`` 组合时会返回 0 条 —— **本项目不使用它**；
    - ``sort`` 只接受网站折扣列表的值（``-cut`` / ``price``），
      ``-timestamp`` / ``added`` / ``release`` 一律 400。
    """
    if sweep == SWEEP_FULL:
        return None
    if sweep != SWEEP_LOW_ONLY:
        raise ValueError(f"未知的抓取口径 sweep={sweep!r}，可选 {SWEEP_MODES}")
    return {"type": [GAME_TYPE_ID], "flag": FLAG_ANY_LOW}


class ItadClient(BaseHttpClient):
    BASE = BASE

    def __init__(self, api_key: str, limiter: RateLimiter, timeout: float = 25,
                 pause: float = 0.0, session: requests.Session | None = None,
                 max_attempts: int = 4, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda msg: None):
        self.api_key = api_key
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

    def _prepare(self, path: str, params: dict | None) -> tuple[str, dict]:
        query = dict(params or {})
        if not path.startswith("/lookup/"):
            # /lookup/* 免鉴权且**不计入额度**（2026-10-04 用量页实测：三端点相加
            # 正好 100%，没有它的位置）→ key 不进 URL；该端点的 key 走 ITAD-API-Key 头
            query["key"] = self.api_key
        return self.BASE + path, query

    def _endpoint(self, path: str) -> str:
        return "itad"

    # ------------------------------------------------------------------
    # 业务端点
    # ------------------------------------------------------------------
    def fetch_deals(
        self,
        country: str,
        shops: int = STEAM_SHOP_ID,
        limit: int = 200,
        sort: str = "-cut",
        max_deals: int | None = None,
        sweep: str = SWEEP_LOW_ONLY,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """翻页抓取折扣列表，直到 hasMore=False。

        ``sweep`` 决定要不要走服务端过滤（见 :func:`sweep_filter`）。
        """
        flt = sweep_filter(sweep)
        items: list[dict] = []
        offset = 0
        pages = 0
        while True:
            params = {
                "country": country,
                "shops": shops,
                "limit": limit,
                "offset": offset,
                "sort": sort,
            }
            if flt is not None:
                params["filter"] = json.dumps(flt, ensure_ascii=False, separators=(",", ":"))
            data = self.request("GET", "/deals/v2", params=params)
            if not isinstance(data, dict):
                raise ItadError(f"/deals/v2 返回了非对象：{type(data).__name__}")
            batch = data.get("list") or []
            items.extend(batch)
            pages += 1
            if progress:
                progress(pages, len(items))
            next_offset = data.get("nextOffset")
            if not data.get("hasMore"):
                break
            if max_deals is not None and len(items) >= max_deals:
                break
            if not next_offset or next_offset == offset:
                break  # 防死循环
            offset = next_offset
        self._log(f"[itad] deals/v2 翻页 {pages} 页，共 {len(items)} 条（口径 {sweep}）")
        return items

    def fetch_info(self, game_id: str) -> dict | None:
        """``GET /games/info/v2`` —— 一游戏一请求，返回 appid + Steam 好评率 + 厂商 / stats。

        好评率**只取 ``source == "Steam"`` 那条**，并带上 ``count``（§2.1）。
        请求失败/数据不可用时返回 None，由调用方标记「详情待补」。

        **2026-09-30 起顺带取 ``publishers`` / ``developers`` / ``stats``**（快照 v3 需要）：
        这些字段本来就在同一个响应里（实测填充率 100%，52 条分层样本），
        **不增加任何请求**；历史上没存，靠 ``tools/backfill_game_meta.py`` 一次性补齐。
        每一项都可能缺（老游戏 / 特殊条目），缺就给空列表 / None，不编造。
        ⚠️ ``stats`` **整体**缺失时返回 ``None``（而不是全 null 的 dict）——
        ``set_meta`` 以 None 判「本次没取到 → 保留旧值」，全 null dict 会把
        已回填的有效 stats 覆盖掉（code-review 2026-09-30 抓到的坑）。
        """
        data = self.request("GET", "/games/info/v2", params={"id": game_id})
        if not isinstance(data, dict):
            return None
        appid = data.get("appid")
        reviews = None
        for entry in data.get("reviews") or []:
            if entry.get("source") != "Steam":
                continue
            score = entry.get("score")
            if score is None:
                continue
            reviews = {"score": int(score), "count": int(entry.get("count") or 0)}
            break
        stats_raw = data.get("stats")
        return {
            "appid": int(appid) if appid else None,
            "reviews": reviews,
            "type": data.get("type"),
            "publishers": _party_list(data.get("publishers")),
            "developers": _party_list(data.get("developers")),
            "stats": {
                "rank": stats_raw.get("rank"),
                "waitlisted": stats_raw.get("waitlisted"),
                "collected": stats_raw.get("collected"),
            } if isinstance(stats_raw, dict) else None,
        }


    def fetch_storelow(self, country: str, game_ids: list[str],
                       shops: int = STEAM_SHOP_ID, batch_size: int = 200) -> dict[str, str]:
        """``POST /games/storelow/v2`` —— 批量取 Steam 店内史低的记录时间（§3.6）。

        body 为 uuid 数组（实测 200 个/次），返回 ``{game_id: timestamp}``。
        响应结构（2026-09-23 实测）：``[{"id", "lows": [{"shop", "price", "cut",
        "timestamp"}]}]`` —— ``lows[].timestamp`` 是该最低价被记录的时间
        （⚠️ 带混合时区偏移，如 ``+02:00``，与 ``deal.timestamp`` 同款坑）。
        响应里缺的游戏不会出现在返回值中，由调用方决定要不要重试。
        """
        lows_map: dict[str, str] = {}
        ids = [gid for gid in game_ids if gid]
        batches = (len(ids) + batch_size - 1) // batch_size
        for start in range(0, len(ids), batch_size):
            data = self.request(
                "POST", "/games/storelow/v2",
                params={"country": country, "shops": shops},
                json_body=ids[start:start + batch_size],
            )
            for row in data or []:
                gid = row.get("id")
                lows = row.get("lows") or []
                if not gid or not lows:
                    continue
                pick = next(
                    (low for low in lows if (low.get("shop") or {}).get("id") == shops),
                    lows[0],
                )
                ts = (pick or {}).get("timestamp")
                if ts:
                    lows_map[gid] = ts
        if ids:
            self._log(f"[itad] storelow/v2 批量 {batches} 次，命中 {len(lows_map)}/{len(ids)}")
        return lows_map

    # ------------------------------------------------------------------
    # 批量映射（重构 S1：uuid → appid，替代 info/v2 逐条的第一跳）
    # ------------------------------------------------------------------
    def fetch_appid_batch(self, uuids: list[str], shop: int = STEAM_SHOP_ID,
                          batch_size: int = LOOKUP_BATCH_SIZE) -> dict[str, int]:
        """``POST /lookup/shop/{shop}/id/v1`` —— uuid → appid **批量映射**。

        免鉴权、**不计入 ITAD 额度**（spec §4 未知量 1 已确认）→ key 只走
        ``ITAD-API-Key`` 请求头、**不进 URL**（见 :meth:`_prepare`）。

        实测边界（spec §9 P4）：单批 ≥20000 也通，保守默认 5000/批、自动切片。
        返回 ``{uuid: appid}``；未命中的 uuid（只有 ``sub/`` / 完全未知）**不在
        返回值里**，由调用方识别（回落 ``info/v2`` 逐条补）。

        **二分降级保留为防御**：实测随机无效 uuid 不会 500（回 200 + 空条目），
        但调研那次用官方占位符 uuid 触发过整批 500 —— 批次失败（:class:`HttpError`
        或响应非对象）时对半拆到单条，单条仍失败则丢弃该 uuid 并记 ``lookup_drop``
        事件。:class:`Blocked`（连续 403 滥用封禁）**不降级**，照常上抛中止本轮。
        """
        ids = [u for u in dict.fromkeys(uuids) if u]
        if not ids:
            return {}
        size = max(1, int(batch_size))
        out: dict[str, int] = {}
        batches = (len(ids) + size - 1) // size
        for start in range(0, len(ids), size):
            out.update(self._lookup_split(ids[start:start + size], shop))
        self._log(f"[itad] lookup/shop/{shop}/id/v1 批量 {batches} 次，"
                  f"映射命中 {len(out)}/{len(ids)}")
        return out

    def _lookup_split(self, batch: list[str], shop: int, depth: int = 0) -> dict[str, int]:
        """查一个批次；失败（HttpError / 响应非对象）时二分降级到单条。"""
        path = f"/lookup/shop/{shop}/id/v1"
        data = None
        failed = False
        try:
            data = self.request("POST", path, json_body=batch,
                                headers={"ITAD-API-Key": self.api_key})
        except Blocked:
            raise
        except HttpError:
            failed = True
        if failed or not isinstance(data, dict):
            if len(batch) > 1 and depth < _LOOKUP_MAX_DEPTH:
                mid = len(batch) // 2
                merged = self._lookup_split(batch[:mid], shop, depth + 1)
                merged.update(self._lookup_split(batch[mid:], shop, depth + 1))
                return merged
            self._record("lookup_drop", path, uuid=batch[0] if batch else "")
            return {}
        out: dict[str, int] = {}
        for uid, shop_ids in data.items():
            appid = _pick_appid(shop_ids)
            if appid is not None:
                out[uid] = appid
        return out


__all__ = [
    "BASE",
    "LOOKUP_BATCH_SIZE",
    "STEAM_SHOP_ID",
    "FLAG_ANY_LOW",
    "GAME_TYPE_ID",
    "ItadBlocked",
    "ItadClient",
    "ItadError",
    "SWEEP_FULL",
    "SWEEP_LOW_ONLY",
    "SWEEP_MODES",
    "sweep_filter",
]
