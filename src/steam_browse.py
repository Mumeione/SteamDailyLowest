# -*- coding: utf-8 -*-
"""Steam 批量元数据客户端：``IStoreBrowseService/GetItems/v1``（重构 S1）。

职责：**主元数据源** —— 一次批量拿「中文名 + 好评率 + 评价数 + 厂商 + 发行日」，
替代逐条 ``info/v2``（重构方案 `.scratch/refactor-2026q4/spec.md` §3.1，决策 3）。
⚠️ 2026-10-09（卡片 08）：原先把价格 / 平台 / 标签也一起拉下来，但**没有一个生产
消费方**（价格用 ITAD 现价、报表不展示平台与标签）—— 已从 :data:`DATA_REQUEST`
与解析层一并裁掉，响应体随之变小。要恢复先看 :data:`DATA_REQUEST` 的注释。
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

#: Steam 资产 CDN 基址 —— ``assets.asset_url_format`` 是相对它的模板
#: （如 ``steam/apps/1299510/${FILENAME}?t=...``）。封面 URL = 基址 + 相对路径。
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
#: GetItems 的 ``country_code``：**只影响可售性 / 资产可见性**，不影响我们要的
#: name（由 language 定）/ reviews（全球口径）/ release。取 **US**（覆盖面最广），
#: 而不是线上下单用的 ``country``（CN）—— 否则**国区不可售的游戏整条返回
#: success=15、连 assets 都没有**（2026-10-10 实测：瘟疫公司 246620、
#: 奥日1终极版 387290 在 CN 下 success=15，US 下 success=1 + 资产齐全）。
ITEMS_COUNTRY = "US"
#: 封面取 ``assets`` 里的哪个键 = **小封面**。选 ``library_capsule``（实测 300×450 竖版，
#: 与退役的 ITAD boxart 同尺寸，卡片 3:4 容器正好吻合，单图 57~100KB）。
#: ⚠️ 别换成 ``library_capsule_2x``（600×900，约 3 倍体积）或横版的 ``header`` /
#: ``main_capsule`` / ``small_capsule`` —— 后者塞进 3:4 竖版容器会被 ``object-fit: cover``
#: 裁掉大半宽度（实测 header 460×215 → 只剩约 28% 宽度）。
COVER_ASSET_KEY = "library_capsule"

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

#: data_request 开关 —— **只开有生产消费方的**（2026-10-09 裁剪，架构检查卡片 08）。
#:
#: 消费者是 ``run._write_browse_meta``：它只取 ``appid`` / ``name`` / ``reviews`` /
#: ``publishers`` / ``developers`` / ``release_date``，别的字段没人读 ——
#: 开着等于给每批 250 条白背响应体（GetItems 是 GET，250 条/批，一天几十批）。
#:
#: ⚠️ 按需返回：没开的字段在响应里是 null，不是空对象。
#: ⚠️ **必须显式写 False / 0，不能直接删键** —— 服务端对缺省键的默认值不可假设
#:    （删掉 `include_platforms` 反而可能让它按默认开着）。测试锁了「这几个键存在
#:    且为关闭态」，防的是「顺手加回来」与「漏写变成默认开」两种反向漂移。
DATA_REQUEST = {
    "include_basic_info": True,          # → basic_info.publishers / developers
    "include_reviews": True,             # → reviews.summary_filtered（好评率 + 评价数）
    "include_release": True,             # → release.steam_release_date（新游 TTL 判定）
    # ---- 以下四个曾开着但**零消费方**（只有字段锁定测试在维护），已关 ----
    "include_all_purchase_options": False,   # → price（卡片价格一律用 ITAD 的现价）
    "include_platforms": False,              # → platforms（报表不展示平台）
    "include_tag_count": 0,                  # → tags（报表不展示标签）
    "include_ratings": False,                # → ratings（GameMeta 里压根没这个字段）
    # ---- 一直关着的（体积大 / 有替代来源）----
    # ⚠️ 2026-10-10 起开着：封面改走 Steam（``assets.library_capsule`` 小封面）。
    # 实测 ``include_assets=False`` 时响应里**连 header_image 都没有**（不是 None 是缺键），
    # 任何封面都得开它。代价 = 每条约 +600B 的 assets 对象；日跑几十批可接受。
    "include_assets": True,              # → assets.library_capsule（见 COVER_ASSET_KEY）
    "include_screenshots": False,        # 体积大
    "include_trailers": False,           # 体积大
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
    #: Steam 发行时间戳（秒）
    release_date: int | None
    #: 封面**相对路径**（Steam ``assets.library_capsule`` 小封面，相对 :data:`ASSET_BASE`，
    #: 如 ``steam/apps/570/library_600x900.jpg``）；服务端没给该资产 → None。
    #: 只存相对路径不存整条 URL：**只为「ITAD 无 boxart」的少数条目用**（约 9.5%），
    #: 前端/渲染层拼 :data:`ASSET_BASE` 即得。
    cover: str | None = None
    # 注：原 ``price`` / ``platforms`` / ``tags`` 三个字段已删（2026-10-09 卡片 08）——
    # 生产零消费方（价格一律用 ITAD 的现价、报表不展示平台与标签），
    # 对应的 ``data_request`` 开关也已关掉，留着只会是「永远是 None/[]」的死数据。


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
    # 只解析 data_request 里开着的字段（见 DATA_REQUEST 注释）；
    # 响应里即使带了别的内容（老请求 / 服务端补全）也一律无视。
    return GameMeta(
        appid=appid,
        name=item.get("name"),
        reviews=_reviews(item.get("reviews")),
        publishers=_party_list(basic.get("publishers")),
        developers=_party_list(basic.get("developers")),
        release_date=_release_date(item.get("release")),
        cover=_cover_tail(item.get("assets")),
    )


def _cover_tail(assets) -> str | None:
    """从 ``assets`` 取**小封面**的相对路径（相对 :data:`ASSET_BASE`）。

    ``asset_url_format`` 是相对 :data:`ASSET_BASE` 的模板，把 ``${FILENAME}``
    换成该资产的文件名即得（如 ``steam/apps/570/library_600x900.jpg``）。
    ⚠️ 文件名/子目录**不固定**（2026-10-10 抽 500 条实测：``library_600x900.jpg`` 219 ·
    ``library_600x900_schinese.jpg`` 50 · ``portrait.png`` 13 · 带哈希子目录的
    ``<hash>/library_capsule.jpg`` 约 218，各唯一）—— **必须用服务端给的真实值，
    不能按 appid 现拼**（猜错即 404 破图）。``?t=`` 缓存参数去掉：实测带不带完全一致。
    """
    if not isinstance(assets, dict):
        return None
    fmt = assets.get("asset_url_format")
    filename = assets.get(COVER_ASSET_KEY)
    if not isinstance(fmt, str) or not filename or "${FILENAME}" not in fmt:
        return None
    return fmt.split("?", 1)[0].replace("${FILENAME}", str(filename))


def cover_url(tail: str | None) -> str | None:
    """封面相对路径 → 完整 URL（渲染层唯一入口）。

    相对路径由 :func:`_cover_tail` 产出、存 ``game_meta.cover``；渲染层只认这一个
    拼装点 —— 别在别处再写一遍 :data:`ASSET_BASE`（改了基址就漏）。
    """
    return ASSET_BASE + tail if tail else None


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


def _release_date(raw) -> int | None:
    ts = (raw or {}).get("steam_release_date")
    try:
        return int(ts) if ts is not None else None
    except (TypeError, ValueError):
        return None


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
    error_cls = SteamBrowseError
    blocked_cls = SteamBrowseBlocked

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
    def fetch(self, appids, *, country_code: str = ITEMS_COUNTRY,
              language: str = "schinese") -> dict[int, GameMeta]:
        """批量取元数据，返回 ``{appid: GameMeta}``。

        * 自动按 :attr:`batch_size`（≤250）切片，逐批请求后合并；
        * appid 去重（保序）；``success != 1`` **静默跳过** —— 既含无效 appid，
          也含**请求区不可售**（实测 ``country_code=CN`` 下返回 ``success=15`` +
          ``visible=False`` + 无 assets）；故默认走 :data:`ITEMS_COUNTRY` 取覆盖面。
          结果字典里没有 = 没拿到，由调用方决定回落（ITAD ``info/v2``）；
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
    "ASSET_BASE",
    "BASE",
    "COVER_ASSET_KEY",
    "DATA_REQUEST",
    "ITEMS_COUNTRY",
    "cover_url",
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
