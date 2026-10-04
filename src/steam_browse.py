# -*- coding: utf-8 -*-
"""Steam 批量元数据客户端：``IStoreBrowseService/GetItems/v1``（重构 S1）。

职责：**主元数据源** —— 一次批量拿「中文名 + 好评率 + 评价数 + 厂商 + 现价 +
发行日 + 平台 + 标签」，替代逐条 ``info/v2``
（重构方案 `.scratch/refactor-2026q4/spec.md` §3.1，决策 3）。
限流与「五种响应分开处理」在 :mod:`src.httpclient`。

⚠️ **api.steampowered.com 与 store.steampowered.com 是否共用限流桶判定不了**
（2026-10-04 P6 探针关闭该项）→ 本客户端用**独立限流窗口**：
配置键 ``steam_browse_rate_limit`` / ``steam_browse_min_interval``，
与 ``SteamClient``（store 域）的 ``steam_rate_limit`` 互不影响。

⚠️ 这是 Valve **未公开**接口（不在 partner Web API 列表里）：无 SLA、字段名可能漂移
→ `tests/test_steam_browse.py` 用 mock 响应**锁字段名**，漂移时测试先红。

实测边界（2026-10-04，GitHub Actions 探针，spec §9）：

* 单批上限 **250**：300 起直接 HTTP 400（硬拒绝，不是截断）→ 批大小 clamp 到 250；
  若 Steam 将来调低上限，400 会以 :class:`HttpError` 响亮抛出
  —— **参数错必须当错误，绝不能当成「没数据」**。
* UA 非必需但更快（0.14s vs 0.41s）→ 沿用底座默认 UA。
* ``summary_unfiltered`` / ``summary_language_specific`` 实测**恒为 None**
  → 只读 ``summary_filtered``，**不做**「三套依次尝试」（资料库 steam_batch_api.py
  的那段回退逻辑不要照抄）。
* 无效 appid 返回 ``success != 1`` → **跳过而不是抛错**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from .httpclient import BaseHttpClient, Blocked, HttpError
from .ratelimit import RateLimiter

BASE = "https://api.steampowered.com"
#: GetItems 端点路径（相对 :data:`BASE`）
PATH = "/IStoreBrowseService/GetItems/v1"

#: 实测单批条数上限的**历史落点**（250 完整通过，300 → HTTP 400）
MAX_BATCH_SIZE = 250
DEFAULT_BATCH_SIZE = MAX_BATCH_SIZE
#: 编码后 URL 的保守字符预算（第二道闸）。GetItems 走 GET，input_json 经 URL 编码
#: （``{ " : ,`` 每字符膨胀 3 倍）→ **服务端上限是请求长度而非固定条数**
#: （2026-10-04 用户实测反馈；与 P1 数据吻合：~250 条 ≈6.9KB 通过、300 条 ≈7.9KB 被拒）
#: —— appid 越长单批装得越少，按条数死切会撞 400。取 7000 留余量：宁多一批，不赌边界。
URL_BUDGET = 7000
#: context.steam_realm（全球商店区，探针实测值）
STEAM_REALM = 1

#: data_request 开关（与探针实测通过的形态一致）。
#: ⚠️ 按需返回：没开的字段返回里是 null，不是空对象。
DATA_REQUEST = {
    "include_basic_info": True,
    "include_reviews": True,
    "include_release": True,
    "include_all_purchase_options": True,
    "include_platforms": True,
    "include_tag_count": 8,
    "include_assets": False,
    "include_ratings": True,
    "include_screenshots": False,   # 体积大，关
    "include_trailers": False,      # 体积大，关
    "include_full_description": False,
}


class SteamBrowseError(HttpError):
    """GetItems 请求失败（不可重试或重试耗尽）。"""


class SteamBrowseBlocked(Blocked, SteamBrowseError):
    """连续 403 —— 滥用封禁，中止本轮并告警。"""


@dataclass
class GameMeta:
    """单个 appid 的解析结果。

    :attr:`reviews` 与 :meth:`ItadClient.fetch_info` 的 ``reviews`` **同形**
    （``{"score": 0~100 整数, "count": int}``），方便 S2 接入状态库时无缝替换。
    ``publishers`` / ``developers`` 里的 ``id`` 是 Steam 侧
    ``creator_clan_account_id``，**与 ITAD info/v2 的厂商 id 不同源**
    （落库前由 S2 决定映射，不在客户端层拍板）。
    """

    appid: int
    #: 商店标题（``language=schinese`` 时有中文标题的游戏即中文名，
    #: 没有中文标题的回落英文名 —— 与 appdetails 同款行为，属正常）
    name: str | None
    #: ``{"score": 0~100, "count": int}``，取 ``reviews.summary_filtered``；无有效评测 None
    reviews: dict | None
    publishers: list
    developers: list
    #: 最低现价 ``{"final": 分, "initial": 分}``（context 国区货币的「分」单位，
    #: 来自 purchase_options 里 final 最低的一项）；无价格信息 None
    price: dict | None
    #: Steam 发行时间戳（秒）
    release_date: int | None
    #: 原样透传（如 ``{"windows": true, "steam_deck_compat_category": 3}``）
    platforms: dict | None
    #: ``[{"tagid": int, "weight": int}]``，条数由 ``include_tag_count`` 控制
    tags: list


def parse_store_item(item) -> GameMeta | None:
    """把一条 ``store_items`` 元素解析成 :class:`GameMeta`。

    ``success != 1``（无效 appid）、appid 缺失/非法、条目非 dict → 返回 None（跳过）。
    """
    if not isinstance(item, dict) or item.get("success") != 1:
        return None
    try:
        appid = int(item.get("appid"))
    except (TypeError, ValueError):
        return None
    if appid <= 0:
        return None
    basic = item.get("basic_info") if isinstance(item.get("basic_info"), dict) else {}
    return GameMeta(
        appid=appid,
        name=item.get("name"),
        reviews=_reviews(item.get("reviews")),
        publishers=_party_list(basic.get("publishers")),
        developers=_party_list(basic.get("developers")),
        price=_lowest_price(item.get("purchase_options")),
        release_date=_release_date(item.get("release")),
        platforms=item.get("platforms") if isinstance(item.get("platforms"), dict) else None,
        tags=_tags(item.get("tags")),
    )


def _reviews(raw) -> dict | None:
    """只读 ``summary_filtered``（另两套 summary 实测恒为 None，不回退）。

    **count 为 0（或缺失）→ 返回 None**，与 :meth:`ItadClient.fetch_info`
    的「无有效 Steam 评测 → None」口径对齐（P7 实测：无评测游戏 GetItems 回
    0/0、ITAD 回 None，两边语义相同）。若保留 0/0，S2 的派生欠账按
    「有 reviews」判定时会把无评测游戏误当成已抓到详情。
    """
    summary = (raw or {}).get("summary_filtered")
    if not isinstance(summary, dict) or summary.get("percent_positive") is None:
        return None
    count = int(summary.get("review_count") or 0)
    if count <= 0:
        return None
    return {"score": int(summary["percent_positive"]), "count": count}


def _party_list(raw) -> list:
    """``[{"name", "creator_clan_account_id"}]`` → ``[{"id", "name"}]``。

    收敛规则与 itad._party_list 对称：id / name 至少有一个就保留（缺 name 置空串），
    全缺才丢。
    """
    out: list = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        pid = entry.get("creator_clan_account_id")
        if not name and pid is None:
            continue
        out.append({"id": pid, "name": str(name) if name else ""})
    return out


def _lowest_price(raw) -> dict | None:
    """``purchase_options`` 混排本体/豪华版/捆绑包 → 取 ``final_price_in_cents`` 最低的一项。

    价格字段实测是**字符串**（如 ``"1730"``），缺原价的按现价兜底。
    """
    best: dict | None = None
    for opt in raw or []:
        if not isinstance(opt, dict):
            continue
        try:
            final = int(opt["final_price_in_cents"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_initial = opt.get("original_price_in_cents")
        try:
            initial = int(raw_initial) if raw_initial is not None else final
        except (TypeError, ValueError):
            initial = final
        if best is None or final < best["final"]:
            best = {"final": final, "initial": initial}
    return best


def _release_date(raw) -> int | None:
    ts = (raw or {}).get("steam_release_date")
    try:
        return int(ts) if ts is not None else None
    except (TypeError, ValueError):
        return None


def _tags(raw) -> list:
    out: list = []
    for tag in raw or []:
        if not isinstance(tag, dict) or tag.get("tagid") is None:
            continue
        try:
            out.append({"tagid": int(tag["tagid"]), "weight": tag.get("weight")})
        except (TypeError, ValueError):
            continue
    return out


def _encoded_len(s: str) -> int:
    """`urllib.quote` 编码后的长度：ASCII 保留字符 1 字节，其余（``{ } " : , [ ]``）3 字节。"""
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-~/"
    return len(s) + 2 * sum(1 for c in s if c not in safe)


def _static_url_len(country_code: str, language: str) -> int:
    """不含 ids 的固定部分编码长度（域名 + 路径 + query 名 + context/data_request）。"""
    tail = json.dumps(
        {"context": {"language": language, "country_code": country_code,
                     "steam_realm": STEAM_REALM},
         "data_request": DATA_REQUEST},
        separators=(",", ":"),
    )
    full = '{"ids":[' + '],' + tail[1:]   # ids 为空时的完整 input_json 形态
    return len(BASE) + len(PATH) + len("?input_json=") + _encoded_len(full)


class SteamBrowseClient(BaseHttpClient):
    BASE = BASE

    def __init__(self, limiter: RateLimiter, timeout: float = 25, pause: float = 0.0,
                 session=None, max_attempts: int = 4,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda msg: None,
                 batch_size: int = DEFAULT_BATCH_SIZE):
        # 300 起 HTTP 400（spec §9 P1）：宁可 clamp 也别把参数错留给运行时
        self.batch_size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
        super().__init__(
            limiter=limiter,
            timeout=timeout,
            pause=pause,
            session=session,
            max_attempts=max_attempts,
            sleep=sleep,
            log=log,
        )

    def _endpoint(self, path: str) -> str:
        return "steam_browse"

    # ------------------------------------------------------------------
    # 业务端点
    # ------------------------------------------------------------------
    def fetch(self, appids, *, country_code: str = "CN",
              language: str = "schinese") -> dict[int, GameMeta]:
        """批量取元数据，返回 ``{appid: GameMeta}``。

        * 自动按 :attr:`batch_size`（≤250）切片，逐批请求后合并；
        * appid 去重（保序）；无效 appid（``success != 1``）**静默跳过**
          —— 结果字典里没有 = 没拿到，由调用方决定回落（ITAD ``info/v2``）；
        * 单批超限被 Steam 拒（HTTP 400）时 :class:`HttpError` **原样上抛**
          —— 参数错不允许被当成「没数据」。
        """
        unique = [int(a) for a in dict.fromkeys(appids) if a]
        if not unique:
            return {}
        out: dict[int, GameMeta] = {}
        batches = self._pack(unique, country_code, language)
        for chunk in batches:
            body = self.request("GET", PATH, params={
                "input_json": json.dumps(
                    self._payload(chunk, country_code, language),
                    separators=(",", ":"), ensure_ascii=False,
                )})
            items: list = []
            if isinstance(body, dict):
                items = (body.get("response") or {}).get("store_items") or []
            for item in items:
                meta = parse_store_item(item)
                if meta is not None:
                    out[meta.appid] = meta
        self._log(f"[steam_browse] GetItems 批量 {len(batches)} 次，命中 {len(out)}/{len(unique)}")
        return out

    def _pack(self, appids: list[int], country_code: str, language: str) -> list[list[int]]:
        """按「条数 ≤ batch_size」与「编码后 URL ≤ :data:`URL_BUDGET`」双闸切批。

        每条 appid 的编码占用 = ``len(str(appid)) + 21``
        （``{"appid":N}`` 编码 +8、逗号 ``%2C`` +3）—— appid 越长单批装得越少。
        单条超预算也至少装 1 条（让 400 响亮暴露，而不是静默丢 id）。
        """
        budget = URL_BUDGET - _static_url_len(country_code, language)
        batches: list[list[int]] = []
        cur: list[int] = []
        cur_len = 0
        for a in appids:
            item_len = len(str(a)) + 21
            if cur and (len(cur) >= self.batch_size or cur_len + item_len > budget):
                batches.append(cur)
                cur, cur_len = [], 0
            cur.append(a)
            cur_len += item_len
        if cur:
            batches.append(cur)
        return batches

    @staticmethod
    def _payload(appids: list[int], country_code: str, language: str) -> dict:
        return {
            "ids": [{"appid": a} for a in appids],
            "context": {
                "language": language,
                "country_code": country_code,
                "steam_realm": STEAM_REALM,
            },
            "data_request": DATA_REQUEST,
        }


__all__ = [
    "BASE",
    "DATA_REQUEST",
    "DEFAULT_BATCH_SIZE",
    "GameMeta",
    "MAX_BATCH_SIZE",
    "PATH",
    "STEAM_REALM",
    "URL_BUDGET",
    "SteamBrowseBlocked",
    "SteamBrowseClient",
    "SteamBrowseError",
    "parse_store_item",
]
