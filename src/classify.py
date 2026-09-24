# -*- coding: utf-8 -*-
"""判定规则（对应 docs/DEVELOPMENT.md §4 / §3.2 / §3.5 / §4.6）。

本模块是**纯函数**、无 IO，便于对判定规则写单元测试（§6 职责边界）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

try:  # Windows 上若无 IANA 时区库则回落到固定 +08:00（Asia/Shanghai 无夏令时）
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

#: `deal.flag` 取值 → 报表标签（§4.1）
FLAG_LABELS = {"N": "新史低", "H": "平史低", "S": "店史低"}
FLAG_ORDER = ("N", "H", "S")

#: Steam 口径的史低分类（批 E spec E1）—— 报表页面**只展示这两个**，与此处的
#: ITAD `flag` 是两套东西，别混用：
#: - `new` = Steam 首次到达该价
#: - `tie` = Steam 以前到过该价（**含 ITAD 的 H 与 S**）
#: - `unknown` = storeLow 缺失，与 §4.1 的 `low_kind` 同义，如实标记
#:
#: 为什么要多这一层：`deal.flag` 是**全商店口径**，存在「Steam 首次到某价、
#: 别家更早更便宜过」的条目被 ITAD 标成 H/S，而对只看 Steam 的买家那是新史低。
#: 判定依据来自 `storelow/v2` 的「Steam 店内史低被记录的时间」（§3.6）——
#: 若它与本次折扣开始时刻重合，说明这个 Steam 史低就是这次创下的。
#: 该接口批量且每天已在跑，**不新增任何请求**。
STEAM_LOW_NEW = "new"
STEAM_LOW_TIE = "tie"
STEAM_LOW_UNKNOWN = "unknown"

#: 判定窗口（小时）。24 小时是给 ITAD 的记录延迟留余量 —— 实测 51 条里
#: `last_low_at` 与 `start` 的间隔**非 0 即 ≥37 天**，取 1h~72h 结果完全一致，
#: 这里不存在调参问题（依据 data/steam_flag_probe.txt）。
#: 批 F5：定为**常量**、不再暴露 `window_hours` 参数 —— 24h 容错已经很宽，
#: 再放大会把「其实不是本次创下」的旧纪录误判成新史低。
STEAM_LOW_WINDOW_HOURS = 24

STEAM_LOW_LABELS = {
    STEAM_LOW_NEW: "新史低",
    STEAM_LOW_TIE: "平史低",
    STEAM_LOW_UNKNOWN: "史低待确认",
}

#: 好评分档（§3.5）
TIER_QUALITY = "quality"        # 优质：好评率 ≥70% 且评价数 ≥100
TIER_NOTABLE = "notable"        # 热门·褒贬不一：评价数 ≥10000
TIER_COLD = "cold"              # 冷门 / 无数据：评价数 <100 或压根没有好评率，不展示
TIER_OTHER = "other"            # 其余：不展示
TIER_PENDING = "pending"        # 详情待补：这一轮没抓到详情（网络失败等），下次自动补

TIER_LABELS = {
    #: 用中性描述而不是「优质」—— 70% 好评率是 Steam 的「多半好评」档，
    #: 叫「优质」会让人误判（用户明确提过）。真正的门槛写在页面的分组标题旁。
    TIER_QUALITY: "好评达标",
    TIER_NOTABLE: "高热度 · 口碑不一",
    TIER_COLD: "冷门",
    TIER_OTHER: "未达标",
    TIER_PENDING: "详情待补",
}

_SHANGHAI_FALLBACK = timezone(timedelta(hours=8))


def zone(name: str) -> tzinfo:
    """取时区对象；拿不到 IANA 数据时对 Asia/Shanghai 回落到固定 +08:00。"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if name in ("Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "PRC"):
        return _SHANGHAI_FALLBACK
    return timezone.utc


def parse_time(value: str | None, tz: tzinfo | None = None) -> datetime | None:
    """解析 ITAD 的 ISO 时间串；无时区信息时按 tz 解释，解析不了返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return dt


def to_int(amount_int, amount) -> int | None:
    """ITAD 金额统一折算成分（优先用官方 ``amountInt``）。"""
    if isinstance(amount_int, bool):
        amount_int = None
    if isinstance(amount_int, (int, float)):
        return int(round(amount_int))
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        return int(round(amount * 100))
    return None


def normalize_item(item: dict) -> dict:
    """把 ``/deals/v2`` 的一条 item 归一化成状态库里用的扁平结构（§5）。"""
    deal = item.get("deal") or {}
    price = deal.get("price") or {}
    regular = deal.get("regular") or {}
    assets = item.get("assets") or {}
    return {
        "game_id": item.get("id"),
        "slug": item.get("slug"),
        "title": item.get("title"),
        "type": item.get("type"),
        "mature": item.get("mature"),
        "price_int": to_int(price.get("amountInt"), price.get("amount")),
        "regular_int": to_int(regular.get("amountInt"), regular.get("amount")),
        "cut": deal.get("cut"),
        "currency": price.get("currency"),
        "flag": deal.get("flag"),
        "start": deal.get("timestamp"),
        "expiry": deal.get("expiry"),
        "store_low_int": to_int(
            (deal.get("storeLow") or {}).get("amountInt"), (deal.get("storeLow") or {}).get("amount")
        ),
        "history_low_int": to_int(
            (deal.get("historyLow") or {}).get("amountInt"),
            (deal.get("historyLow") or {}).get("amount"),
        ),
        "history_low_1y_int": to_int(
            (deal.get("historyLow_1y") or {}).get("amountInt"),
            (deal.get("historyLow_1y") or {}).get("amount"),
        ),
        "shop_id": (deal.get("shop") or {}).get("id"),
        "boxart": assets.get("boxart"),
    }


def deal_key(game_id: str, price_int: int | None, expiry: str | None) -> str:
    """幂等键：``<itad_uuid>|<price_int>|<expiry>``（§5）。"""
    return f"{game_id}|{price_int}|{expiry}"


#: 写进状态库时保留的字段（§5 的「精简落库」）。
#:
#: 实测原始 22 个字段共 5.09 MB，其中 `banner`(483KB) / `boxart`(467KB) /
#: `slug`(111KB) 最占地方，而
#: `banner` 与 `boxart` 重复（卡片封面只用小图 boxart，R8）、
#: `shop_id` 恒 61、`type` 恒 `game`、`mature` 恒 False、`currency` 恒 CNY ——
#: 这几个都是「存了也不会变」的常量，落库没有意义。
#: `itad_url` 一并砍掉（report-ui spec R2，用户实测 302 直跳 Steam，信息冗余；
#: 旧 state.json 里已落的该字段留存不迁移，下次该条目更新时自然消失）。
#: 保留的字段覆盖：幂等键 / 报表卡片 / §4.6 全部五个视图窗口 / 留存清理。
SEEN_KEEP = (
    "game_id",
    "title",
    "price_int",
    "regular_int",
    "cut",
    "currency",
    "flag",
    "start",
    "expiry",
    "store_low_int",
    "history_low_int",
    "history_low_1y_int",
    "boxart",
    "low_kind",
    "first_seen_at",
    "last_seen_at",
)


def slim_deal(deal: dict) -> dict:
    """按 :data:`SEEN_KEEP` 裁剪一条折扣，用于落库（§5）。

    只影响**写盘**的内容；报表渲染用的是当前这一轮的完整条目，不受影响。
    """
    return {key: deal[key] for key in SEEN_KEEP if key in deal}


def low_kind(deal: dict) -> str | None:
    """史低分类，直接套用 ``deal.flag``（§4.1）。

    返回 ``"N"`` / ``"H"`` / ``"S"``；不是史低返回 None；
    ``storeLow`` 缺失时返回 ``"unknown"``（如实记录，不静默当成非史低，§10）。
    """
    if deal.get("store_low_int") is None:
        return STEAM_LOW_UNKNOWN
    flag = deal.get("flag")
    if flag in FLAG_ORDER:
        return flag
    return None


def low_label(kind: str | None) -> str:
    return FLAG_LABELS.get(kind or "", "—")


def steam_low_class(deal: dict, tz: tzinfo | None = None) -> str | None:
    """史低分类的 **Steam 口径**（批 E spec E1）。

    返回 ``"new"`` / ``"tie"`` / ``"unknown"``；不是史低返回 ``None``。

    ``low_kind()``（ITAD 口径）负责兜住「是不是史低」这道门，本函数只在其之上
    再判「新还是平」，两者职责不重叠。

    时间容差固定为 :data:`STEAM_LOW_WINDOW_HOURS`（批 F5 起不再可传参）。
    """
    kind = low_kind(deal)
    if kind is None or kind == STEAM_LOW_UNKNOWN:
        return kind
    if deal.get("flag") == "N":
        return STEAM_LOW_NEW  # 全网首次 ⇒ 必定 Steam 首次，不必比时间
    start = parse_time(deal.get("start"), tz)
    last = parse_time(deal.get("last_low_at"), tz)
    if start is None or last is None:
        return STEAM_LOW_TIE  # 取不到时间 ⇒ 回落 ITAD flag（走到这里的只剩 H/S）
    delta = abs((last - start).total_seconds())
    return STEAM_LOW_NEW if delta <= STEAM_LOW_WINDOW_HOURS * 3600 else STEAM_LOW_TIE


def steam_low_label(cls: str | None) -> str:
    return STEAM_LOW_LABELS.get(cls or "", "—")


def flag_price_mismatch(deal: dict) -> bool:
    """交叉校验（§4.1）：``flag is None`` 却 ``price <= storeLow`` → 异常。"""
    if deal.get("flag") is not None:
        return False
    price, low = deal.get("price_int"), deal.get("store_low_int")
    return price is not None and low is not None and price <= low


def timestamp_is_today(deal: dict, today, tz: tzinfo) -> bool:
    """主口径（§4.2）：``deal.timestamp`` 的日期 == 运行当天（Asia/Shanghai）。"""
    start = parse_time(deal.get("start"), tz)
    return start is not None and start.astimezone(tz).date() == today


def timestamp_missing(deal: dict, tz: tzinfo) -> bool:
    """timestamp 缺失/不可解析 —— 只有这种情况才允许走「首次见到」兜底（§4.2）。"""
    return parse_time(deal.get("start"), tz) is None


def cheaper(price_a: int | None, price_b: int | None) -> bool:
    """a 是否比 b 便宜（None 视为最贵）。"""
    pa = price_a if price_a is not None else float("inf")
    pb = price_b if price_b is not None else float("inf")
    return pa < pb


def dedupe_by_appid(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一 appid 只保留价格最低的那条（§4.4）。

    返回 ``(保留的条目, 被合并的记录)``；被合并的记录按 appid 汇总，
    便于事后核对是否误合并（§4.4）。没有 appid 的条目无法合并，原样保留。
    """
    kept: list[dict] = []
    index_by_appid: dict[int, int] = {}
    dropped_by_appid: dict[int, list[dict]] = {}
    for entry in entries:
        appid = entry.get("appid")
        if not appid:
            kept.append(entry)
            continue
        pos = index_by_appid.get(appid)
        if pos is None:
            index_by_appid[appid] = len(kept)
            kept.append(entry)
            continue
        current = kept[pos]
        if cheaper(entry.get("price_int"), current.get("price_int")):
            winner, loser = entry, current
            kept[pos] = entry
        else:
            winner, loser = current, entry
        dropped_by_appid.setdefault(appid, []).append(loser)

    merged = []
    for appid, losers in dropped_by_appid.items():
        winner = kept[index_by_appid[appid]]
        merged.append(
            {
                "appid": appid,
                "kept": winner.get("title"),
                "kept_price_int": winner.get("price_int"),
                "dropped": [
                    {"title": item.get("title"), "price_int": item.get("price_int")}
                    for item in losers
                ],
            }
        )
    return kept, merged


def tier_of(reviews: dict | None, cfg: dict) -> str:
    """好评分档（§3.5）。``reviews`` = ``{"score": 0-100, "count": n}`` 或 None。

    注意：``reviews is None`` 是「无数据」（§3.5 归入冷门，不展示），
    与「详情待补」（这一轮没抓到详情）是两件不同的事 ——
    后者由调用方根据抓取状态标记为 :data:`TIER_PENDING`。
    """
    if not reviews or reviews.get("score") is None:
        return TIER_COLD
    count = int(reviews.get("count") or 0)
    ratio = float(reviews["score"]) / 100.0

    min_count = int(cfg.get("min_review_count", 100))
    notable_count = int(cfg.get("notable_review_count", 10000))
    min_ratio = float(cfg.get("min_positive_ratio", 0.7))
    absolute_min = cfg.get("absolute_min_positive_ratio")

    if count < min_count:
        return TIER_COLD
    if absolute_min is not None and ratio < float(absolute_min):
        return TIER_OTHER
    if ratio >= min_ratio:
        return TIER_QUALITY
    if count >= notable_count:
        return TIER_NOTABLE
    return TIER_OTHER


def is_shown(tier: str) -> bool:
    """第一版只展示「优质」「热门·褒贬不一」两档，外加「详情待补」（§3.5 / §7.2）。"""
    return tier in (TIER_QUALITY, TIER_NOTABLE, TIER_PENDING)


# ----------------------------------------------------------------------
# 视图窗口（§4.6）—— 第一版只上线「当日新增」，其余已按定义实现备用
# ----------------------------------------------------------------------
def week_window(now: datetime, days: int = 14) -> tuple[datetime, datetime]:
    """「本周」窗口：**本周一 00:00:00 ~ 下周日 23:59:59**（§4.6）。

    ⚠️ 这里**必须按自然周对齐**，不能写成「now 往前推 14 天」的滚动窗口 ——
    滚动窗口在周中运行时会给出与定义不同的结果，§11 那条
    「视图筛选结果与 §4.6 的定义逐条对得上」就会不过。
    """
    local = now.astimezone(now.tzinfo)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=days) - timedelta(seconds=1)


def in_week(start: str | None, now: datetime, days: int = 14) -> bool:
    """折扣**开始时间**是否落在「本周(14天)」窗口内（§4.6）。"""
    dt = parse_time(start, now.tzinfo)
    if dt is None:
        return False
    begin, end = week_window(now, days)
    return begin <= dt.astimezone(now.tzinfo) <= end


def is_active(expiry: str | None, now: datetime) -> bool:
    dt = parse_time(expiry, now.tzinfo)
    return dt is not None and dt > now


def is_upcoming(expiry: str | None, now: datetime, hours: int = 48) -> bool:
    dt = parse_time(expiry, now.tzinfo)
    if dt is None:
        return False
    return timedelta(0) < dt - now <= timedelta(hours=hours)


def is_expired(expiry: str | None, now: datetime) -> bool:
    dt = parse_time(expiry, now.tzinfo)
    return dt is not None and dt <= now


#: 视图键（与 `report.VIEWS` 的 key 一一对应）
#: 批 F2：`expired` 已移出 —— 折扣过期后对买家没有意义，不再作为一个视图
#: （`is_expired()` 保留：留存清理与判定仍要它）
VIEW_KEYS = ("new_today", "week", "active", "upcoming")


def in_view(view: str, deal: dict, now: datetime, cfg: dict) -> bool:
    """判断一条史低是否落在某个视图里（§4.6 的唯一入口）。

    **所有视图都先过「史低」这道门** —— 非史低一律不进任何视图。
    阈值一律从 `cfg` 取，不再在函数签名里写死默认值（那会让 §4.6 与代码再次漂移）。
    """
    if deal.get("flag") is None and deal.get("low_kind") is None:
        return False
    if view == "new_today":
        return timestamp_is_today(deal, now.date(), now.tzinfo)
    if view == "week":
        return in_week(deal.get("start"), now, int(cfg.get("week_window_days", 14)))
    if view == "active":
        return is_active(deal.get("expiry"), now)
    if view == "upcoming":
        return is_upcoming(deal.get("expiry"), now, int(cfg.get("upcoming_expiry_hours", 48)))
    raise ValueError(f"未知视图 {view!r}，可选 {VIEW_KEYS}")
