# -*- coding: utf-8 -*-
"""判定规则（对应 docs/DEVELOPMENT.md §4 / §3.2 / §3.5 / §4.6）。

本模块是**纯函数**、无 IO，便于对判定规则写单元测试（§6 职责边界）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

#: 默认配置的单表（2026-10-09 收编）：函数签名的默认值 / 读点兜底一律从这里取，
#: 不再各写一个字面量 —— 否则「改一个 48 要动 5 处」（架构检查卡片 06）。
from .config import DEFAULTS

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
#: 这里不存在调参问题（2026-09-24 一次性离线探针实测，结论沉淀于 DEVELOPMENT §4.1）。
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

#: 「剩 X 天」的凌晨宽容（小时）：Steam 折扣与特卖全球统一收摊，换算到北京时间落在
#: 01:00~02:00，买家语义上就是「今天结束」——多出的几小时可忽略（2026-09-27 用户定案）。
#: 03:00 起主跑（03:14）已进入新的一天，之后的过期时刻按正常日历天算（6:00 容差过大弃用）。
#: ⚠️ 卡片的「剩 X 天」与顶部活动条（`src/announcements.py`）的「还有 X 天」共用它，
#: 别再各写一份常量。
EARLY_MORNING_EXPIRY_HOUR = 3


def days_until(end_dt: datetime, now: datetime) -> int:
    """从 ``now`` 到 ``end_dt`` 的**日历天**差，含凌晨宽容，负数钳到 0。

    卡片「剩 X 天」与活动条「还有 X 天」共用这一份口径（原先两处各写一遍，
    改一处漏一处 = 页面两处数字打架）。两个参数应当是**同一时区**的时刻；
    任一侧没有时区信息时不做换算（测试里会传 naive 的 now）。
    """
    end = end_dt
    if end.tzinfo is not None and now.tzinfo is not None:
        end = end.astimezone(now.tzinfo)
    days = (end.date() - now.date()).days
    if end.hour < EARLY_MORNING_EXPIRY_HOUR:
        days -= 1
    return max(0, days)


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

    min_count = int(cfg.get("min_review_count", DEFAULTS["min_review_count"]))
    notable_count = int(cfg.get("notable_review_count", DEFAULTS["notable_review_count"]))
    min_ratio = float(cfg.get("min_positive_ratio", DEFAULTS["min_positive_ratio"]))
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


def is_new_today(entry: dict, now: datetime) -> bool:
    """条目级「当日新增」判定（双口径的原子版，§4.2）。

    折扣开始日是今天，或**首次见到**是今天 —— 与 :func:`timestamp_is_today` +
    ``first_seen_at`` 兜底那两条主口径一致，供拿不到本轮 candidates 的调用方
    （欠账计数、预抓）按条目自己判。

    原先叫 ``run._is_new_today``，长在 orchestrator 里（卡片 07/05：它是一条**判定**，
    不是编排）。``now`` 必须带时区（比较的是本地日历日）。
    """
    start = parse_time(entry.get("start"), now.tzinfo)
    if start is not None and start.astimezone(now.tzinfo).date() == now.date():
        return True
    first = parse_time(entry.get("first_seen_at"), now.tzinfo)
    return first is not None and first.astimezone(now.tzinfo).date() == now.date()


def merge_tier(meta: dict | None, cfg: dict) -> tuple[str, int | None, dict | None]:
    """由 ``game_meta`` 条目决定**进列表的档位**与卡片要用的 ``appid`` / ``reviews``。

    返回 ``(tier, appid, reviews)``。这条判定原先长在 ``run.merge_details`` 里 ——
    orchestrator 里长着「进列表」语义的另一半，每个新消费方（回填脚本等）都得
    重新理解一遍边界（架构检查卡片 07）。现在只有这一份：

    · **unlisted**（决策 17）→ :data:`TIER_COLD`：它的动态数据已被主动丢弃，
      归 :data:`TIER_PENDING` 会让它**以展示档重新进列表**（PENDING 是展示档）；
    · 没抓过详情（无 meta / 无 ``fetched_at``）→ :data:`TIER_PENDING`（下轮自动补）；
    · 其余按 :func:`tier_of` 判档。

    ⚠️ unlisted 必须排在「没抓过详情」**前面**判：unlisted 条目的动态数据已被删，
    ``fetched_at`` 可能为空 —— 顺序反了就会把它错归 PENDING。
    """
    if meta and meta.get("unlisted"):
        return TIER_COLD, meta.get("appid"), None
    if not meta or not meta.get("fetched_at"):
        return TIER_PENDING, None, None
    reviews = meta.get("reviews")
    return tier_of(reviews, cfg), meta.get("appid"), reviews


def is_shown_meta(meta: dict | None, cfg: dict) -> bool:
    """``game_meta`` 条目**按已知评价数据**是否够格进列表（§3.5）。

    与 :func:`merge_tier` 的区别是**不看抓取状态**：这里只回答「按已抓到的评价数
    它够不够格」，:data:`TIER_PENDING`（这一轮没抓到）不是档位而是抓取状态，
    所以**不能**把「没抓过详情」当 PENDING 放行 —— 那会让回填给永远不展示的
    冷门条目白花配额。

    调用方：``pipeline.count_backlog`` 与 ``tools/backfill_low_period.select_targets``
    （原先两边各写一遍 ``is_shown(tier_of(...))``，口径靠注释维系）。
    """
    return is_shown(tier_of((meta or {}).get("reviews"), cfg))


# ----------------------------------------------------------------------
# 视图窗口（§4.6）—— 第一版只上线「当日新增」，其余已按定义实现备用
# ----------------------------------------------------------------------
def week_window(now: datetime, days: int = DEFAULTS["week_window_days"]) -> tuple[datetime, datetime]:
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


def in_week(start: str | None, now: datetime, days: int = DEFAULTS["week_window_days"]) -> bool:
    """折扣**开始时间**是否落在「本周(14天)」窗口内（§4.6）。"""
    dt = parse_time(start, now.tzinfo)
    if dt is None:
        return False
    begin, end = week_window(now, days)
    return begin <= dt.astimezone(now.tzinfo) <= end


def is_active(expiry: str | None, now: datetime) -> bool:
    dt = parse_time(expiry, now.tzinfo)
    return dt is not None and dt > now


def is_upcoming(expiry: str | None, now: datetime,
                hours: int = DEFAULTS["upcoming_expiry_hours"]) -> bool:
    dt = parse_time(expiry, now.tzinfo)
    if dt is None:
        return False
    return timedelta(0) < dt - now <= timedelta(hours=hours)


def is_expired(expiry: str | None, now: datetime) -> bool:
    dt = parse_time(expiry, now.tzinfo)
    return dt is not None and dt <= now


#: 视图键（写入每张卡片的 ``views`` 成员标志，供前端 ``liveOk`` 与板块筛选使用）
#: 批 F2：`expired` 已移出 —— 折扣过期后对买家没有意义，不再作为一个视图
#: （`is_expired()` 保留：留存清理与判定仍要它）
VIEW_KEYS = ("new_today", "week", "active", "upcoming")


# ----------------------------------------------------------------------
# 详情刷新口径（S6 决策 16 / 17）—— 「要不要给这个条目发详情请求」的领域判定
# ----------------------------------------------------------------------
# 这四个原先长在 run.py（orchestrator）里，每个新消费方都得回 orchestrator 里
# 找一遍口径（架构检查卡片 07）。它们**不自己做 IO**，需要状态库时由调用方把
# ``state`` 传进来（鸭子类型：只需 unlisted / dyn / detail_recently_failed 三个方法）。
def discount_active(entry: dict, now: datetime) -> bool:
    """条目是否折扣活跃（决策 16：``start ≤ now ≤ expiry``）。

    边界解析不了的按**活跃**处理 —— 宁多刷不漏刷；非折扣期一律不刷新
    （用户 2026-10-05 裁决：不进列表就没有消费方）。
    """
    expiry = parse_time(entry.get("expiry"), now.tzinfo)
    if expiry is not None and expiry < now:
        return False
    start = parse_time(entry.get("start"), now.tzinfo)
    if start is not None and start > now:
        return False
    return True


def refresh_ttl_days(entry: dict, state, cfg: dict, now: datetime) -> int:
    """折扣感知的刷新 TTL（决策 16，四档）：

    新游（release_date ≤ ``new_game_days``）1 天 → 到期窗口（expiry −
    ``upcoming_expiry_hours`` 起）1 天 → 折扣期普通条目 ``discount_refresh_days``
    （3 天）。调用方保证条目折扣活跃（非折扣期根本不进派生）。
    game_id 从 ``entry`` 派生（review-s6 P2 Data Clumps：它总是结伴出现，
    不该单独占一个参数）。
    """
    ng_days = int(cfg.get("new_game_days", DEFAULTS["new_game_days"]) or 0)
    meta = state.meta(entry.get("game_id")) or {}
    rd = meta.get("release_date")
    if ng_days > 0 and rd:
        try:
            released = datetime.fromtimestamp(int(rd), tz=now.tzinfo)
            if abs((now - released).total_seconds()) <= ng_days * 86400:
                return int(cfg.get("new_game_refresh_days", DEFAULTS["new_game_refresh_days"]))
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    expiry = parse_time(entry.get("expiry"), now.tzinfo)
    if expiry is not None:
        window = timedelta(hours=int(cfg.get("upcoming_expiry_hours",
                                             DEFAULTS["upcoming_expiry_hours"])))
        if now >= expiry - window:
            return int(cfg.get("expiry_refresh_days", DEFAULTS["expiry_refresh_days"]))
    return int(cfg.get("discount_refresh_days", DEFAULTS["discount_refresh_days"]))


def unlisted_frozen(entry: dict, mark: dict | None) -> bool:
    """unlisted 冻结判定（决策 17）：标记存在且仍是**同一折扣期**（start 一致）。

    :func:`entry_needs_detail` 与 ``run.detail_targets`` 的 backlog 循环共用这一份
    —— 「同折扣期内不重抓，``start`` 变了才重判」这句口径只能有一处实现。
    """
    return mark is not None and (entry.get("start") or "") == (mark.get("start") or "")


def entry_needs_detail(entry: dict, state, cfg: dict, now: datetime,
                       *, is_new: bool = False) -> bool:
    """单条目级「要不要发详情请求」（S6 折扣感知口径，detail_targets 的原子判定）。

    - unlisted 冻结（同一折扣期内，``start`` 与标记一致）→ False；
    - 非当日新增且**非折扣活跃** → False（非折扣期不刷新）；
    - 冷却期内失败过 → False（**当日新增不冷却**，报表核心每轮重试）；
    - 其余按动态数据年龄 vs :func:`refresh_ttl_days` 判定（没抓过 → True）。
    """
    game_id = entry.get("game_id")
    if not game_id:
        return False
    mark = state.unlisted(game_id)
    if unlisted_frozen(entry, mark):
        return False   # 同一折扣期内冻结（决策 17）
    if not is_new:
        if not discount_active(entry, now):
            return False
        if state.detail_recently_failed(
                game_id, now, int(cfg.get("detail_retry_cooldown_days",
                                          DEFAULTS["detail_retry_cooldown_days"]))):
            return False
    dyn = state.dyn(game_id)
    if not dyn or dyn.get("fetched_at") is None:
        return True
    fetched = parse_time(dyn.get("fetched_at"), now.tzinfo)
    if fetched is None:
        return True
    ttl_days = refresh_ttl_days(entry, state, cfg, now)
    return (now - fetched) >= timedelta(days=ttl_days)


# ----------------------------------------------------------------------
# 史低期记忆（§3.6 扩展，2026-10-09）——
# **「时刻 t 是否开启一段史低期」的判定唯一出处**（架构检查卡片 02）。
#
# 两条时间线各自喂进来，但判定与合并规则只有这一份：
#   · 日常增量 —— :meth:`src.state.State.record_low_period` 每次入账一个 start；
#   · 一次性回填 —— ``tools/backfill_low_period`` 从 ITAD ``/games/history/v2``
#     的完整流水推（:func:`low_period_starts`）。
# 从前这两条路各写一套、互不知情（也没有交叉测试），一改概念就要在 5 个 module
# 里同步 —— 现在只剩这两个 adapter，判定在 classify。
# ----------------------------------------------------------------------
#: 时间戳解析不了时排序用的「最早」占位（aware，避免 naive/aware 混比）
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def low_period_starts(points: list[tuple[str, int]]) -> list[str]:
    """从「价格变更流水」推史低期的开始时刻（升序）。

    ``points`` = ``[(timestamp, price_amount_int), ...]``（ITAD ``history/v2`` 的
    形态，由调用方拍平）。按**真实时刻**升序走一遍，价格 **≤ 此前历史最低** 的
    变更即一段史低期的开始（``<`` 是新史低、``==`` 是平史低，两者都算「在史低」）；
    价格回升不算。解析不了时间戳的行直接丢掉（宁缺勿猜）。
    """
    ordered = sorted(
        ((parse_time(ts), ts, price) for ts, price in points if ts and price is not None),
        key=lambda p: p[0] or _EPOCH,
    )
    starts: list[str] = []
    seen_min: int | None = None
    for dt, ts, price in ordered:
        if dt is None:
            continue
        if seen_min is None or price <= seen_min:
            starts.append(ts)
            seen_min = price
    return starts


def roll_low_period(cur: str | None, new_start: str | None,
                    tz: tzinfo | None = None) -> str | None:
    """增量口径：新入账一个史低期开始 —— 返回**新的 cur**；不更新时返回 ``None``。

    同 start（重复入账 / 折扣期被 Steam 延长）不更新；乱序写入只认**更晚**的开始
    时间；解析不了不更新。调用方拿到新值时才滚动 prev（``prev = 旧 cur``）——
    首次史低（旧 cur 为空）会得到 ``prev = None``，该卡维持「本次新史低」文案。
    """
    if not new_start or cur == new_start:
        return None
    parsed_new = parse_time(new_start, tz)
    parsed_cur = parse_time(cur, tz) if cur else None
    if parsed_new is None or (parsed_cur is not None and parsed_new <= parsed_cur):
        return None
    return new_start


def low_period_pair(starts: list[str], cur: str | None = None,
                    tz: tzinfo | None = None) -> tuple[str, str] | None:
    """把一条史低期时间线合成 ``(cur, prev)``；不足两段返回 ``None``。

    时间线 = 流水推出的 ``starts`` ＋ 记忆里已有的 ``cur``（**同日去重、保留记忆的
    拼写**）。``cur`` = 时间线最后一段、``prev`` = 它前一段 —— 于是「记忆里的 cur
    比流水还新」（日常刚入账、ITAD 还没记进流水）时，prev 自然取流水最新那段，
    正是「上一次史低期」。
    """
    timeline = [t for t in starts if parse_time(t, tz) is not None]
    cur_dt = parse_time(cur, tz) if cur else None
    if cur_dt is not None:
        same_day = [t for t in timeline
                    if (parse_time(t, tz) or cur_dt).date() == cur_dt.date()]
        if same_day:
            timeline = [cur if t in same_day else t for t in timeline]
        else:
            timeline.append(cur)
    ordered = sorted(((parse_time(t, tz), t) for t in timeline),
                     key=lambda p: p[0] or _EPOCH)
    ordered = [(d, t) for d, t in ordered if d is not None]
    if len(ordered) < 2:
        return None
    return ordered[-1][1], ordered[-2][1]


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
        return in_week(deal.get("start"), now,
                       int(cfg.get("week_window_days", DEFAULTS["week_window_days"])))
    if view == "active":
        return is_active(deal.get("expiry"), now)
    if view == "upcoming":
        return is_upcoming(deal.get("expiry"), now,
                           int(cfg.get("upcoming_expiry_hours", DEFAULTS["upcoming_expiry_hours"])))
    raise ValueError(f"未知视图 {view!r}，可选 {VIEW_KEYS}")
