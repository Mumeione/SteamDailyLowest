# -*- coding: utf-8 -*-
"""报表渲染（对应 docs/DEVELOPMENT.md §7）。

产出 ``index.html`` + ``data.js`` + ``latest.json`` + ``static/``（§6）。
第一版只有「当日新增」一个视图，其余视图位置预留但不可点（§1.1）。
"""

from __future__ import annotations

import json
import math
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import announcements, classify
from .config import DEFAULTS

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = TEMPLATES_DIR / "static"

#: ⚠️ 2026-10-08：**视图按钮已整段退场** —— `VIEWS` / `LAZY_VIEWS` / `payload["views"]`
#: 连同 `render(upcoming_items=…, extra_counts=…)` 一起删除。S9 首页四板块 + 导航栏
#: 已取代视图切换，这组「按钮 count」没有任何前端消费者（app.js 读的是**卡片级**
#: `card["views"]` 成员标志，见 run.py；与本组无关）。
#: 沿革留档：批 F2 删「已过期」（过期折扣对买家无价值）；2026-09-27 上线「即将过期」；
#: S5（2026-10-05）开放「本周 / 折扣中 / 全部」并把大数据拆到 ``all.js`` 懒加载。

#: 「剩 X 天」的凌晨宽容阈值已挪到 `classify.EARLY_MORNING_EXPIRY_HOUR`
#: （2026-10-07：卡片与顶部活动条共用 `classify.days_until`，不再各留一份常量）


def group_specs(cfg: dict) -> list[dict]:
    """档位顺序与标签（§3.5）—— 「高热度」在前、然后「好评达标」、「详情待补」。

    标签刻意用**中性描述**而不是「优质」—— 70% 好评率是 Steam 的「多半好评」档，
    叫「优质」会让人误判。

    ⚠️ 2026-10-09（卡片 08）：原先这里还带 ``criteria`` / ``collapsed`` 两个字段
    （给已退场的分组页做组标题与折叠态），**生产一个都不读**，只有测试在维护它们 ——
    阈值文案的唯一来源本来就是「关于网站」页的 :func:`criteria_notes`。已删。
    ``cfg`` 参数保留：调用方（tier_labels / pool_items）签名一致，改起来更省事。
    """
    del cfg
    return [
        {"key": classify.TIER_NOTABLE, "label": "高热度 · 口碑不一"},
        {"key": classify.TIER_QUALITY, "label": "好评达标"},
        {"key": classify.TIER_PENDING, "label": "详情待补"},
    ]


def fx_display(cfg: dict, fx: dict | None) -> dict | None:
    """页脚要展示的汇率信息（§7.4：必须标注汇率数值与取数日期）。"""
    if not fx:
        return None
    currencies = ["USD"] + [c for c in (cfg.get("compare_countries") or [])]
    currency_of = {"UA": "UAH", "IN": "INR", "CN": "CNY", "US": "USD",
                   "TR": "TRY", "BR": "BRL", "RU": "RUB"}
    rates = fx.get("rates") or {}
    picked = []
    for cc in currencies:
        code = currency_of.get(cc, cc)
        value = rates.get(code)
        if value:
            picked.append({"code": code, "rate": round(float(value), 4)})
    return {"base": fx.get("base") or "CNY", "date": fx.get("date"), "rates": picked}


# 批 F（2026-09-24）：页面上的「筛选条件」折叠框已删除 —— 判定口径属于文档，
# 不该占手机屏幕高度。原本由 conditions() / conditions_digest() 生成的那几行
# 现在写在 README 的「筛选条件」一节；改阈值时记得同步那里。


#: 卡片的两个**分档配色**判据（S9-卡片，用户 2026-10-07）：
#: 「60 好评和 90 好评一个颜色、剩 7 天和剩 2 天也是一个颜色」→ 都要分档。
#: 阈值仍集中在配置（`good_positive_ratio` / `min_positive_ratio` / `bad_positive_ratio`
#: / `upcoming_expiry_hours` / `home_new_low_days`），前端只挂类名，不再自己写一份。
#: ⚠️ 三个阈值都只是 :data:`src.config.DEFAULTS` 的**别名**，不是第二张表
#: （2026-10-09 收编）：改默认值只改 config.py 一处。
GOOD_POSITIVE_RATIO = DEFAULTS["good_positive_ratio"]
#: 「差评」档的门槛（Steam 商店口径：40~69% 是「褒贬不一」、<40% 是「差评」）。
#: ⚠️ 这两档**只可能由「高热度 · 口碑不一」组（评价数 ≥ notable，不看好评率）的卡产生**
#: （「好评达标」组被 ≥70% 展示门槛挡住）；但那张卡**可以同时是**新史低 / 临期 / 大额折扣，
#: 所以 mid / low **可能出现在任何板块** —— 配色的意义就是在任何位置都能认出来。
#: （2026-10-07 review 纠正：旧注释写「只会出现在热门游戏板块」，那是**分组**口径不是
#: **板块**口径 —— COD 类大作踩新史低时照样进「新史低」板块，好感分档不硬砍。）
BAD_POSITIVE_RATIO = DEFAULTS["bad_positive_ratio"]


def rate_tier(reviews: dict | None, cfg: dict | None = None) -> str | None:
    """好评率配色档（Steam 商店口径，用户 2026-10-07 拍板）：

    ``high`` ≥ `good_positive_ratio`（默认 90%）· ``ok`` ≥ `min_positive_ratio`（70%）
    · ``mid`` ≥ `bad_positive_ratio`（40%，褒贬不一）· ``low`` < 40%（差评）；
    没有详情返回 ``None``。

    ⚠️ ``high`` 与 ``ok`` **同色**（都挂 `--steam-blue`），只靠**字重**分层 ——
    用户明确不要「给 90% 再发明一个更深的蓝」（那会让蓝色变两个号）。
    """
    if not reviews or reviews.get("score") is None:
        return None
    score = float(reviews["score"])
    good = float((cfg or {}).get("good_positive_ratio", GOOD_POSITIVE_RATIO)) * 100
    floor = float((cfg or {}).get("min_positive_ratio", 0.7)) * 100
    bad = float((cfg or {}).get("bad_positive_ratio", BAD_POSITIVE_RATIO)) * 100
    if score >= good:
        return "high"
    if score >= floor:
        return "ok"
    return "mid" if score >= bad else "low"


def days_tier(days_left: int | None, cfg: dict | None = None) -> str | None:
    """剩余天数配色档（2026-10-07 起分四档，「今天结束」单独拎出来）：

    ``final`` 今天/已结束（0 天）· ``urgent`` ≤ 即将过期窗口（默认 48h = 2 天）
    · ``soon`` 一周内 · ``later`` 还有一周以上；不知道天数返回 ``None``。

    边界都从配置推：``upcoming_expiry_hours`` 向上取整到天 = urgent，
    ``home_new_low_days``（默认 7）= 「一周内」的分界。

    ⚠️ 「今天结束」原先和「剩 1~2 天」同档。用户 2026-10-07 要它最显眼（单独一档，
    前端给它粉底徽章），但同时提醒「即将到期那一类会显示很多这个色块」——
    所以**只有 0 天这一个字符串**进 final 档，1~2 天仍是普通文字。
    """
    if days_left is None:
        return None
    hours = int((cfg or {}).get("upcoming_expiry_hours", DEFAULT_UPCOMING_HOURS))
    urgent_max = max(0, (hours + 23) // 24)
    later_min = int((cfg or {}).get("home_new_low_days", DEFAULT_HOME_DAYS))
    if days_left <= 0:
        return "final"
    if days_left <= urgent_max:
        return "urgent"
    return "soon" if days_left < later_min else "later"


CURRENCY_SYMBOLS = {
    "CNY": "¥",
    "USD": "$",
    "EUR": "€",
    "UAH": "₴",
    "INR": "₹",
    "GBP": "£",
    "JPY": "¥",
    "KRW": "₩",
    "BRL": "R$",
    "TRY": "₺",
}


def format_amount(amount_int: int | None, currency: str | None) -> str:
    """把「分」格式化成展示金额。

    **一律保留两位小数**：12700 分 → `¥127.00`、1200 分 → `¥12.00`、
    1270 分 → `¥12.70`。

    演进：原先统一 `rstrip("0")` 会把 12.70 砍成 12.7（2026-09-25 改为
    「整元才省小数」）；但那样 `¥127` / `¥12.7` / `¥12.70` 三种长度会混排，
    价格区右边缘参差不齐（S9-1 用户反馈「价格长短不一致」），故统一两位小数。
    """
    if amount_int is None:
        return "—"
    symbol = CURRENCY_SYMBOLS.get(currency or "", "")
    return f"{symbol}{amount_int / 100:,.2f}"


def compare_rows(entry: dict) -> list[dict]:
    """把 `entry["compare"]` 的原始数值格式化成卡片要显示的行（§7.2）。

    原始数据由 `src.enrich.enrich_steam` 填：`final` 是**原币种最小单位**，
    `cny_minor` 是换算成人民币分，`diff_pct` 是相对国区的差价百分比
    （负数 = 比国区便宜；正负号与红绿色由前端渲染）。
    """
    rows = []
    for item in entry.get("compare") or []:
        price_text = format_amount(item.get("final"), item.get("currency"))
        cny = item.get("cny_minor")
        rows.append({
            "label": item.get("label") or item.get("cc"),
            "price_text": price_text,
            "cny_text": f"≈ {format_amount(cny, 'CNY')}" if cny is not None else None,
            "diff_pct": item.get("diff_pct"),
        })
    return rows


def tier_labels(cfg: dict | None = None) -> dict:
    """档位键 → 页面标签。**唯一来源**：分组标题与卡片标签都从这里取，
    免得出现「分组叫好评达标、卡片标签还写着优质」这种不一致。"""
    if cfg is None:
        return {key: classify.TIER_LABELS[key] for key in classify.TIER_LABELS}
    return {spec["key"]: spec["label"] for spec in group_specs(cfg)}


#: Steam schinese 标题里常见的商标符号，比较时先去掉
_TITLE_NOISE_RE = re.compile(r"[\u00ae\u2122\u00a9]")
#: 剥完必须还剩中文（>= 2 个汉字）才认，见 clean_title_zh 里的硬约束
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
#: 剥完英文后可能剩下的吊诡残留：空括号、首尾分隔符、连续空格
_EMPTY_BRACKET_RE = re.compile(r"\s*[\(（\[【]\s*[\)）\]】]")
_SEP_CHARS = "\\s\\-\\u2013\\u2014:：,，、/\\|()（）\\[\\]【】®™©"


def clean_title_zh(title_zh: str | None, title: str | None) -> str | None:
    """去掉中文名里夹带的英文原名（S9，2026-10-06）。

    Steam 的 schinese 标题有时会把英文原名一起塞进来，实测 7380 条里 **946 条**
    是这种，例：``Lords of the Fallen 堕落之主``、``Cities: Skylines II 都市：天际线2``、
    ``大富翁10 (RichMan 10)``、``Wo Long: Fallen Dynasty （卧龙：苍天陨落）``。
    后果是中文名那行被英文撑长截断，而下面「英文名」那行又把同一个英文显示一遍。

    **只在能精确对上英文原名时才剥，对不上就原样返回**（不猜、不乱切）——
    所以 ``《镜之边缘：Catalyst》``（原名 ``Mirror's Edge™ Catalyst``）这种
    中文名里带个英文副标题的会保持原样。
    """
    if not title_zh or not title:
        return title_zh
    zh, en = title_zh.strip(), title.strip()
    if not zh or not en or zh == en:
        return title_zh  # 压根没有中文名（Steam 回退到英文），保持原样
    candidates = {en}
    stripped = _TITLE_NOISE_RE.sub("", en).strip()
    if len(stripped) >= 4:
        candidates.add(stripped)
    low = zh.casefold()
    for variant in sorted(candidates, key=len, reverse=True):
        if len(variant) < 4:
            continue
        idx = low.find(variant.casefold())
        if idx < 0:
            continue
        cleaned = zh[:idx] + zh[idx + len(variant):]
        cleaned = _EMPTY_BRACKET_RE.sub("", cleaned)
        cleaned = re.sub("^[" + _SEP_CHARS + "]+", "", cleaned)
        cleaned = re.sub("[" + _SEP_CHARS + "]+$", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        # ⚠️ 硬约束：剥完**必须还剩中文**，否则一律不动。
        # 反例（不加这条就会被切坏）：`Pure Farming 2018`（原名 Pure Farming）
        # 只剩 "2018"、`Kingdom Rush  - Tower Defense` 只剩 "Tower Defense"、
        # `MX Nitro: Unleashed` 只剩 "Unleashed" —— 这些其实是"本该显示英文的条"，
        # 剥掉前缀只是把尾巴留下，比不剥更糟。
        if cleaned != zh and len(_CJK_RE.findall(cleaned)) >= 2:
            return cleaned
    return title_zh


#: 「参数没传」的哨兵 —— 与「显式传了 None」区分开（见 build_card 的两个史低时刻参数）
_MISSING = object()


def build_card(entry: dict, now: datetime, labels: dict | None = None,
               cfg: dict | None = None, *,
               last_low_at=_MISSING, prev_low_at=_MISSING) -> dict:
    """把状态库条目（已合并详情）拼成卡片数据。

    ``cfg`` 只用于**配色分档的阈值**（好评率 / 剩余天数，见 rate_tier / days_tier）——
    不传就用模块默认值，所以老的调用与测试照常工作。

    ``last_low_at`` / ``prev_low_at``：**显式参数**（架构检查卡片 02）。这两个时刻原
    先只靠「上游按约定塞进 entry 的 dict 键」维系（低层判定读不到就静默降级成
    「平史低 / 无数据」），是本模块最热改动区的隐式契约。生产唯一入口
    ``pipeline.merge_details`` 两个键都会写，所以传不传等价；把它们放到签名上是为了让
    「这两个值从哪来」不再靠注释维系，新调用方也能直接喂。不传时回落到 entry
    同名键（兼容旧调用与测试）。
    """
    if last_low_at is _MISSING:
        last_low_at = entry.get("last_low_at")
    if prev_low_at is _MISSING:
        prev_low_at = entry.get("prev_low_at")
    currency = entry.get("currency")
    appid = entry.get("appid")
    reviews = entry.get("reviews")
    tier = entry.get("tier") or classify.TIER_PENDING
    label_map = labels or tier_labels()
    start_dt = classify.parse_time(entry.get("start"), now.tzinfo)
    expiry_dt = classify.parse_time(entry.get("expiry"), now.tzinfo)
    # 批 E spec E1：史低分类改用 Steam 口径，不再把 ITAD 的 flag 直接搬到页面上
    low_class = classify.steam_low_class(entry, now.tzinfo)
    # 「剩 X 天」按**日期差**算（10-02 结束、今天 09-24 → 剩 8 天），不用小时差：
    # 用户要看的是「还剩几个日历天」这种粗粒度信息，精确时刻放在详情里（批 E spec E2）。
    # 2026-09-27 追加凌晨宽容、2026-10-07 与活动条的「还有 X 天」合并到同一份口径，
    # 两者都走 classify.days_until（原先两处各写一遍，容易改一处漏一处）。
    days_left = None
    if expiry_dt is not None:
        days_left = classify.days_until(expiry_dt, now)
    # §3.6 史低天数（「距上次史低」那一行）：
    #   tie = 上一次 Steam 达到该价的时间（storelow/v2 批量取，存 low_time_cache）；
    #   new = 上一次史低期的开始（自有史低期记忆 game_meta.low_period.prev，
    #   2026-10-09 起）—— storelow/v2 对新史低返回空（这个价从没出现过），
    #   只能自己攒。记忆没建立起来（首次史低 / 冷启动）→ 维持「本次新史低」。
    # 主文本只放天数保证单行（日期太长会把整行挤成两排），具体日期由
    # 前端悬停/点按显示（last_low_date）。取不到就不渲染这一行（JS 对空值自动跳过），不猜。
    last_low_date = None
    last_low_text = None
    # 权重公式「间隔」项要的是**数值天数**（refs.md §10.2）。新史低也有值了
    # （距上次史低期，来自史低期记忆）；记忆未建立时为 None（该项不计分）。
    last_low_days = None
    if low_class == classify.STEAM_LOW_NEW:
        low_at = classify.parse_time(prev_low_at, now.tzinfo)
        if low_at is not None:
            last_low_days = max(0, (now.date() - low_at.date()).days)
            last_low_text = f"{last_low_days} 天"
            last_low_date = low_at.strftime("%Y-%m-%d")
        else:
            # 批 F5 文案（用户定案）：该游戏首次史低 / 记忆未建立时的兜底 ——
            # 有记忆后主文本与平史低一致（「N 天」），标签统一「上次新史低」。
            last_low_text = "本次新史低"
    elif low_class == classify.STEAM_LOW_TIE:
        low_at = classify.parse_time(last_low_at, now.tzinfo)
        if low_at is not None:
            # 批 E 第二轮：主文本就是「N 天」（标签侧已改为「距上次史低」，
            # 再写「N 天前」语义重复）；具体日期由前端悬停/点按显示
            last_low_days = max(0, (now.date() - low_at.date()).days)
            last_low_text = f"{last_low_days} 天"
            last_low_date = low_at.strftime("%Y-%m-%d")
    return {
        "game_id": entry.get("game_id"),
        "title": entry.get("title"),
        # Steam 偶尔会把本地化标题存成带尾随空格（例："时之刃 "），渲染前统一清掉
        # S9：剥掉中文名里夹带的英文原名（Steam 的 schinese 标题常带），
        # 否则中文名那行被撑长截断、下面英文名那行还重复一遍
        "title_zh": clean_title_zh((entry.get("title_zh") or "").strip() or None,
                                   entry.get("title")),
        "appid": appid,
        # R8：卡片缩略图只有几十像素宽，用小图 boxart 即可；banner600/400 不再使用
        "banner": entry.get("boxart") or entry.get("banner"),
        "price_text": format_amount(entry.get("price_int"), currency),
        # R1：前端排序用数值，不用 price_text 字符串
        "price_int": entry.get("price_int"),
        "regular_text": format_amount(entry.get("regular_int"), currency),
        "cut": entry.get("cut"),
        # 批 E spec E6：页面上的史低标签与色条都来自这两个字段（Steam 口径）
        "low_class": low_class,
        "low_label": classify.steam_low_label(low_class),
        "days_left": days_left,
        # S9-卡片：剩余天数与好评率的**配色档**（前端只挂类名，阈值在服务端算）
        "days_tier": days_tier(days_left, cfg),
        "rate_tier": rate_tier(reviews, cfg),
        "last_low_text": last_low_text,
        # 权重公式 v2「间隔」项用的数值天数（平史低取上次同价、新史低取史低期记忆；
        # 记忆未建立时为 None，该项不计分）
        "last_low_days": last_low_days,
        # 有日期才渲染悬停/点按交互（new=新纪录没有具体日期）
        "last_low_date": last_low_date,
        # 批 E spec E3：原来这三条删掉了 —— 进报表的前提就是正处史低、storeLow 又已含
        # 本次折扣，所以「Steam 史低」在数学上恒等于现价（实测 50/51）；
        # 「全周期 / 近一年最低」则是 ITAD 全商店口径，与「本报告只看 Steam」冲突。
        "compare": compare_rows(entry),
        "start_text": start_dt.astimezone(now.tzinfo).strftime("%Y-%m-%d %H:%M") if start_dt else None,
        # S9：首页四板块的「近 7 天新增」判据（按日历天差，与 start_text 同源）
        "start_days_ago": (
            max(0, (now.date() - start_dt.astimezone(now.tzinfo).date()).days)
            if start_dt is not None else None
        ),
        "expiry_text": expiry_dt.astimezone(now.tzinfo).strftime("%Y-%m-%d %H:%M") if expiry_dt else None,
        "reviews": reviews,
        # 注：原 `reviews_text`（"94% · 22,300 条"）已删 —— S9-卡片要给「94%」单独上色，
        # 前端拿 reviews.score / count 自己拼（服务端再拼一遍字符串就是第二份口径来源）
        "tier": tier,
        "tier_label": label_map.get(tier, tier),
        # R2：itad_url 已删（302 直跳 Steam，信息冗余），卡片与 payload 均不再输出。
        # 2026-10-08 问题1：steam_url / xiaoheihe_url 也已删 —— 前端由 appid 现拼
        # （app.js 的 steamUrl / xhhUrl），下发常量前缀纯属浪费（all.js 里两项合计 ~900KB）。
    }



def review_count(card: dict) -> int:
    """卡片的评价数（没有详情 = 0）。

    ``reviews:{score,count}`` 是一组**总结伴出现**的字段，取值的写法散在
    `_in_section` / `_section_sort` / `recommend_*` 里容易写歪
    （漏一层 `or {}` 就是 AttributeError）—— 统一从这里取。
    """
    return ((card.get("reviews") or {}).get("count")) or 0


def review_score(card: dict) -> float | None:
    """卡片的好评率（没有详情 = None）。"""
    return (card.get("reviews") or {}).get("score")


#: 重构 S5 默认精选排序（spec §1 决策 8 / §3.3）：**分层 + 多键字典序**，
#: 不用加权求和 —— 权重没有可解释性，字典序每一键都答得出「为什么排前面」：
#: ① 新史低在前、平史低在后（史低待确认殿后）② 折扣力度降序 ③ 评价数降序。
FEATURED_LAYERS = {
    classify.STEAM_LOW_NEW: 0,
    classify.STEAM_LOW_TIE: 1,
    classify.STEAM_LOW_UNKNOWN: 2,
}


def featured_sort_key(card: dict) -> tuple:
    """分层字典序排序键：新史低 → 折扣力度 → 评价数（验收 §6：单测锁定）。

    ⚠️ 2026-10-08：名字里的「featured」是历史遗留 —— 原「当日新增 · 精选」扁平组
    （``build_featured_group``）已随 data.js 首屏瘦身删除。本键保留，现服务两处：
    ``pick_top`` 的兜底排序（推荐分全部缺席时）与 ``latest.json`` 的 ``shown`` 顺序。
    板块排序用的是另一套（``featured_score``，见 ``_section_sort``），不是本键。
    """
    reviews_count = review_count(card)
    return (
        FEATURED_LAYERS.get(card.get("low_class") or classify.STEAM_LOW_UNKNOWN, 2),
        -(card.get("cut") or 0),
        -reviews_count,
        card.get("title") or "",  # 稳定收尾键：同分时 deterministic，不随字典序漂移
    )


def pool_items(cards: list[dict], cfg: dict) -> list[dict]:
    """「全部折扣」分片的**池顺序**：按 :func:`group_specs` 的档位分组 + 组内折扣降序。

    生产要的一直只有这个**扁平列表**（``all_section_orders`` 的 ``__all__`` 池）。
    从前它由 ``build_groups`` 组装 —— 那个函数额外产出一层带 ``criteria`` /
    ``collapsed`` / ``count`` / ``start_text`` 的分组结构，**生产一个字段都不读**，
    只在测试里被维护（删掉它，复杂度就消失：卡片本身已有 ``start_text``，
    页面上也没有「组标题」这个位置了）。空白档位不进池（老行为，保持一致）。
    """
    pool: list[dict] = []
    for spec in group_specs(cfg):
        group_items = [item for item in cards if item["tier"] == spec["key"]]
        if not group_items:
            continue
        group_items.sort(key=lambda i: (-(i["cut"] or 0), i["title"] or ""))
        pool.extend(group_items)
    return pool


#: all.js 卡片瘦身（2026-10-08，问题1：分类页懒加载的 all.js 达 6.3MB）——
#: 这些字段**前端要么不读、要么能由已有字段现拼**，只从 all.js 剥离；
#: data.js 的 sections/picks 只有 ~42KB，保持原样（`check_payload` 旧口径与单测照旧读它）：
#:   · ``tier_label`` / ``low_label`` / ``last_low_days``：app.js 从不读
#:     （tier_label / low_label 只被 ``tools/check_payload.py`` 当诊断用，见那边的派生改法）；
#:   · ``banner`` → ``art``（见 :func:`boxart_code`）。
#: （``steam_url`` / ``xiaoheihe_url`` 不在这里 —— 已在 :func:`build_card` 源头删除，
#:  前端一律由 ``appid`` 现拼，data.js 里也不该留死数据。）
#: ⚠️ ``game_id`` **必须保留** —— 它是 ``art`` 现拼封面的依据（boxart 资产 uuid == game_id）。
_ALL_JS_DROP = ("tier_label", "low_label", "last_low_days", "banner")


def boxart_code(url: str | None) -> str | None:
    """封面 URL → 紧凑「扩展名」码（all.js 专用，2026-10-08）。

    ITAD 的封面 URL 里只有**扩展名**不可由 `game_id` 现拼 —— 前缀固定
    ``https://assets.isthereanydeal.com/<game_id>/boxart``，``?t=<epoch>`` 缓存参数
    实测去掉后响应完全一致（200 / 同 content-length）。所以下发扩展名即可：
      · ``None`` —— 该条没有封面（ITAD 无资产，实测约 9%）→ 前端显示灰块占位；
      · ``"jpg"`` / ``"png"`` —— 前端拼 ``.../boxart.<ext>``。
    ⚠️ 两种扩展名都真实存在（实测 6573 jpg / 123 png），**不能一律 jpg** ——
    `.png` 资产在 `.jpg` 上是 403（2026-10-08 实测）。
    """
    if not url:
        return None
    path = url.split("?", 1)[0]
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else None
    # ⚠️ 未知后缀回落 **None**（code-review 2026-10-08）：ITAD 实测只有 jpg/png，
    #    真出现 webp 之类说明资产形态变了 —— 猜 jpg 会 403 出破图，不如 None 走
    #    前端灰块占位（诚实且视觉一致）。
    return ext if ext in ("jpg", "png") else None


def slim_for_all_js(cards: list[dict]) -> list[dict]:
    """给列表页卡片瘦身：剔除 :data:`_ALL_JS_DROP`，并把 ``banner`` 换成紧凑的 ``art``。"""
    out = []
    for card in cards:
        slim = {k: v for k, v in card.items() if k not in _ALL_JS_DROP}
        # ⚠️ 幂等：卡片可能**已经被瘦身过**（没有 banner、只有 art）—— 这时别拿
        # 不存在的 banner 去算、把 art 抹成 None（分片路径会连着瘦两次：
        # `all_section_orders` 的池子先瘦一次建组，`write_all_shards` 再瘦一次）。
        if "banner" in card:
            slim["art"] = boxart_code(card.get("banner"))
        elif "art" in card:
            slim["art"] = card["art"]
        out.append(slim)
    return out


# ==================================================================
# 板块完整列表的分片（2026-10-08：分类页加载 20 秒）
# ==================================================================
# 病根：分类页原本**一次性下载整个 all.js** —— 线上 5.81MB raw / 729KB gzip，
# 而且**下完才渲染第一条**，可首屏只需要 30 条（3KB 的量）。国内到 github.io
# 实测只有 40~60KB/s，729KB 就是 13~18 秒，占满那 20 秒的 95%。
# 分片后首屏只要 1 片（200 条 ≈ 20KB gzip），降幅 97%。
#
# ⚠️ **必须按板块自己的顺序切片，不能做「共享池 + 板块索引」** —— 实测
# （2026-10-08，池顺序 = tier 分组 + 折扣降序，与板块排序完全不相关）：
#     new_low 前 30 条散落在 9 片 / expiring 1 片 / popular 18 片 / big_cut 1 片
# 「热门游戏」打开就要拉 18 个分片，等于没优化。卡片因此在板块间**重复存储**
# （发布体积 5.8MB → 约 13.1MB raw），这是可重建产物，换首屏速度值得。
#
# ⚠️ 别再把卡片字段拆成「card / compare 两个文件」来求解：实测 compare 只占
# 24.4%，拆掉也只把 653KB 降到 550KB（-16%），20 秒 → 17 秒，感觉不出来。
# 真正的问题是「一次性全量阻塞」，不是「单卡有多胖」。
ALL_SHARD_SIZE = 200

#: 分片文件写入的全局命名空间 —— app.js 的 :func:`shardGlobal` 读同一个键。
#: 产物是 ``(window.ALL_S = window.ALL_S || {})["expiring_0"] = {...};`` 形态，
#: 每个文件自带命名空间初始化，加载顺序无所谓（与 all.js 一样不受 CORS 限制，
#: 本地 file:// 直开也能用；fetch 会失败）。
ALL_SHARD_GLOBAL = "ALL_S"


def _cut_bands(cfg: dict) -> list[int]:
    """折扣区间档位（升序去重）—— 唯一出处，别再各写一遍 ``sorted({50, big, 90})``。

    50% = 「有点折扣」的直觉线；``big_cut_percent``（默认 80%）是「大额折扣」板块的
    同一根线；90% = 「几乎白送」。**70% 已由用户去掉**（夹在 50 与 80 之间、
    区分度最低）。用 set 去重：把 big_cut 调成 50 或 90 也不会出重复选项。
    """
    return sorted({50, int(cfg.get("big_cut_percent", DEFAULT_BIG_CUT)), 90})


def _review_bands(cfg: dict) -> list[int]:
    """好评数量档位（升序去重）—— 唯一出处，供 agg 与抽屉选项共用。"""
    return sorted({500, 5000, int(cfg.get("notable_review_count", DEFAULT_NOTABLE))})


def _date_bands(cfg: dict) -> list[dict]:
    """日期档位 ``[{value, label}]``（升序）—— 协议值 ``0/1/2/dN/all``。

    **唯一出处**：:func:`filter_dim_values`（前端查表键）与 :func:`filter_specs`
    （抽屉选项）都从这里取，两边不可能再漂移 —— 曾经两处各拼一份，加档位时
    容易只改一边（查表 miss 会静默退回分片遍历）。冒烟另有「抽屉选项值 ==
    payload 的 agg 维度」对拍兜底。
    """
    days = int(cfg.get("home_new_low_days", DEFAULT_HOME_DAYS))
    return [
        {"value": "0", "label": "今天"},
        {"value": "1", "label": "昨天"},
        {"value": "2", "label": "前天"},
        {"value": f"d{days}", "label": f"近 {days} 天"},
        {"value": "all", "label": "全部"},
    ]


def filter_dim_values(cfg: dict) -> dict[str, list[str]]:
    """列表页筛选四个维度的**全部选项值**（前端预聚合计数的查表键）。

    ⚠️ 与 :func:`filter_specs` 的选项**必须逐项一致** —— 现在两边都从
    :func:`_date_bands` / :func:`_cut_bands` / :func:`_review_bands` 取，同源了；
    单测与冒烟各有一道对拍。
    """
    return {
        "date": [o["value"] for o in _date_bands(cfg)],
        "cut": ["all"] + [str(v) for v in _cut_bands(cfg)],
        "reviews": ["all"] + [str(v) for v in _review_bands(cfg)],
        "only_new": ["all", "new"],
    }


def _agg_rows(members: list[dict], cfg: dict, key: str) -> list[tuple]:
    """把每张卡的筛选判据预压成元组，供 :func:`section_agg` 复用。

    不做的话就是 160 个组合 × N 张卡次重复取字段（expiring 一轮 116 万次）。
    """
    days = int(cfg.get("home_new_low_days", DEFAULT_HOME_DAYS))
    cuts = _cut_bands(cfg)          # 档位只在 _cut_bands / _review_bands 里算一次
    revs = _review_bands(cfg)
    rows = []
    for card in members:
        ago = card.get("start_days_ago")
        # 日期位图，位序跟着 :func:`filter_dim_values` 的 date 选项：0/1/2/dN/all
        bits = 16                                   # "all" 恒真
        if ago is not None:
            if ago == 0:
                bits |= 1
            if ago == 1:
                bits |= 2
            if ago == 2:
                bits |= 4
            if ago <= days:
                bits |= 8
        rows.append((
            True if key == "__all__" else _is_live(card),   # 「全部折扣」不叠加 liveOk
            bits,
            sum(1 for t in cuts if (card.get("cut") or 0) >= t),   # 门槛已升序 ⇒ 越大越严
            sum(1 for t in revs if review_count(card) >= t),
            card.get("low_class") == classify.STEAM_LOW_NEW,
        ))
    return rows


#: 预聚合计数表的**键序契约**（唯一出处）：键 = 各维度的值按这个顺序 ``join("|")``。
#: 前端 app.js 的 ``aggCount`` 按同一顺序拼键查表 —— 改顺序等于改协议，
#: 两边必须同步（tools/check_payload.py 有机械对拍：Python 这份顺序 ↔ JS 的数组顺序）。
AGG_KEY_ORDER = ("date", "cut", "reviews", "only_new")


def section_agg(members: list[dict], cfg: dict, key: str) -> dict:
    """板块在**每一种筛选组合**下的精确条数（构建期算好，写进第 0 片）。

    意义：分片之后前端手上只有前几片，本来算不出「共 N 条」——这张表让
    **只加载第 0 片**就能给出精确总数，筛选不必退化成全量加载。
    四个维度都是离散档位（5×4×4×2 = 160 个组合），表本身约 4KB raw / 1KB gzip。

    ⚠️ 判据必须与 app.js 的 ``dateOk`` / ``filterOk`` / ``liveOk`` 同一口径：
      · 「即将到期」**不叠加日期窗口**（refs §11.3：叠加会把最紧急的老折扣漏掉）；
      · 「全部折扣」**不叠加 liveOk**（本来就是要看全量，含过期留存）。
    """
    dims = filter_dim_values(cfg)
    dates, cuts, revs, news = dims["date"], dims["cut"], dims["reviews"], dims["only_new"]
    rows = _agg_rows(members, cfg, key)
    cache: dict[tuple, int] = {}
    counts: dict[str, int] = {}
    for di, d in enumerate(dates):
        # 「即将到期」忽略日期 ⇒ 5 个日期选项共用一份计数（靠 cache 只算一次）
        bit = 16 if key == "expiring" else (1 << di)
        for ci in range(len(cuts)):
            for ri in range(len(revs)):
                for oi in range(len(news)):
                    ck = (bit, ci, ri, oi)
                    n = cache.get(ck)
                    if n is None:
                        n = sum(1 for live, b, ct, rt, isn in rows
                                if live and (b & bit) and ct >= ci and rt >= ri
                                and (oi == 0 or isn))
                        cache[ck] = n
                    combo = {"date": d, "cut": cuts[ci],
                             "reviews": revs[ri], "only_new": news[oi]}
                    counts["|".join(combo[name] for name in AGG_KEY_ORDER)] = n
    return {"dim": dims, "counts": counts}


def all_section_orders(all_cards: list[dict], cfg: dict) -> dict[str, list[dict]]:
    """每个板块**完整列表**的卡片顺序，分片就按它切。

    与首页四板块预览（:func:`build_sections`）同源、同排序 —— 点「查看更多」
    进去看到的顺序和首页预览一致。

    ``"__all__"``（全部折扣）没有板块排序语义，沿用**池顺序**：tier 分组 +
    折扣降序，与前端原本展开 ``groups`` 的顺序一致（分片后就是这个顺序），别改成
    ``all_cards`` 的原序（那是抓取顺序，不稳定）。
    """
    orders = {}
    for spec in HOME_SECTIONS:
        key = spec["key"]
        members = _section_members(key, all_cards, cfg)
        members.sort(key=_section_sort(key, cfg))
        orders[key] = members
    # ⚠️ 这里**不瘦身**：`write_all_shards` 会统一瘦一次，瘦两遍会把 art 抹成 None
    orders["__all__"] = pool_items(all_cards, cfg)
    return orders


def _write_shard(path: Path, slot: str, payload: dict) -> None:
    """写一个分片文件：``(window.ALL_S = window.ALL_S || {})["<slot>"] = {...};``"""
    path.write_text(
        '(window.%s = window.%s || {})[%s] = %s;\n'
        % (ALL_SHARD_GLOBAL, ALL_SHARD_GLOBAL,
           json.dumps(slot), json.dumps(payload, ensure_ascii=False)),
        encoding="utf-8")


def write_all_shards(output_dir: Path, all_cards: list[dict], cfg: dict) -> dict:
    """把板块完整列表切成 ``all/<key>_<n>.js``，**替代**原先的单文件 ``all.js``。

    每片 :data:`ALL_SHARD_SIZE` 条；第 0 片额外带 ``total`` / ``shards`` / ``agg``，
    让前端只加载一片就能给出精确的「共 N 条」。

    关于「换排序」：默认「精选」排序 = 分片顺序，天然对齐、零成本。用户主动换成
    折扣/价格/好评排序是**全局重排**，必须拿到该板块全部卡片，此时并发拉取全部分片
    （≈650KB gzip，与改动前点一次分类的代价相同）。实测另出一份「列式排序键」
    只能在这个次要路径上再省约 40%，却要在 app.js 里复制一份筛选判据
    （卡式 + 列式两套），与「判据只有一份」的约定冲突 —— 不值，故不做。

    ⚠️ 会先清空 ``output/all/``：池量每天变，昨天 37 片、今天 12 片的话，
    残留的旧分片会被一起发布，白占仓库体积。
    """
    out = output_dir / "all"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for key, members in all_section_orders(all_cards, cfg).items():
        slim = slim_for_all_js(members)
        total = len(slim)
        n_shards = max(1, math.ceil(total / ALL_SHARD_SIZE))
        manifest[key] = {"total": total, "shards": n_shards, "size": ALL_SHARD_SIZE}
        for i in range(n_shards):
            payload = {
                "key": key,
                "n": i,
                "size": ALL_SHARD_SIZE,     # 前端按它算「第 k 条落在第几片」
                "total": total,
                "shards": n_shards,
                "items": slim[i * ALL_SHARD_SIZE:(i + 1) * ALL_SHARD_SIZE],
            }
            if i == 0:
                payload["agg"] = section_agg(members, cfg, key)
            _write_shard(out / f"{key}_{i}.js", f"{key}_{i}", payload)
    return manifest


#: 首页四板块（S9 定案 2026-10-06）。顺序由用户拍板：新史低 → 即将到期 → 热门游戏 → 大额折扣。
#: ⚠️ **池子必须是「全部」视图那批卡片（``all_cards`` / ``all_shown``）** ——
#: 绝不能用 ``seen_deal`` 全量（里面有同一游戏的历史价格变体，条数会放大十几倍）。
#: ⚠️ 这里**不再写「入组条件」文案** —— 原先每个板块带一句 `criteria`（"评价数 ≥ 10000"、
#: "折扣 ≥ 80%"），阈值是写死的字面量：改了 `notable_review_count` 之后页面还在说
#: 10000，等于撒谎；而且**前端从头到尾没渲染过它**（refs B6 只要条数）—— 既是谎言
#: 又是死数据（review-s9-01 补充审查 #1）。板块口径改在「关于网站」页由
#: :func:`criteria_notes` 从配置生成，那里才是唯一来源。
#: ⚠️ 加/改板块要同时动三处（这是已知的重复分派，暂时保留、改动面大）：
#: ① :func:`_in_section`（判据）② :func:`_section_sort`（板块内排序）
#: ③ :func:`all_section_orders`（列表页顺序 = 分片的物理顺序）—— 有单测与冒烟锁定。
#:    （2026-10-08 起 `section_order` 不再单独下发：切片的顺序本身就是它。）
HOME_SECTIONS = [
    {"key": "new_low", "label": "新史低"},
    {"key": "expiring", "label": "即将到期"},
    {"key": "popular", "label": "热门游戏"},
    {"key": "big_cut", "label": "大额折扣"},
]


#: 默认阈值的**唯一来源** = :data:`src.config.DEFAULTS`（2026-10-09 收编）。
#: 这几个名字是**只读别名**，保留它们是为了不改动几十处调用点；值一律从单表取，
#: 绝不再在这里写第二遍字面量（曾经 10000 / 80 / 7 / 48 在两处各写一遍，
#: 改一个默认值要散着改，两张表还能悄悄不一致）。
DEFAULT_HOME_DAYS = DEFAULTS["home_new_low_days"]
DEFAULT_BIG_CUT = DEFAULTS["big_cut_percent"]
DEFAULT_NOTABLE = DEFAULTS["notable_review_count"]
DEFAULT_UPCOMING_HOURS = DEFAULTS["upcoming_expiry_hours"]
#: 四板块预览栏位的**上限**（节日满额）与**最低栏位**（平时整齐度）—— 口径见
#: :func:`build_sections`。2026-10-08 用户问「最低设置多少个，3 还是 4 还是 5」
#: → 取 **5**：与大卡单排 5 张同高、页面节奏统一；3 偏空、4 不对称。
DEFAULT_HOME_SECTION_PREVIEW = DEFAULTS["home_section_preview"]
DEFAULT_HOME_SECTION_FLOOR = DEFAULTS["home_section_preview_min"]


def _is_fresh(card: dict, days: int) -> bool:
    """「近 N 天新增」：按折扣开始日距今天数。取不到 start 的一律不算。"""
    ago = card.get("start_days_ago")
    return ago is not None and ago <= days


def _is_live(card: dict) -> bool:
    """折扣是否还在有效期内。

    ⚠️ **首页三板块（新史低/热门/大额折扣）必须叠加这条** —— 「全部」视图的池子
    （``all_shown``）里含已过期留存的条目（实测 new 类里 inactive 有 215 条），
    不筛就会把「已经买不到」的折扣摆到首页。没有 ``views`` 标志时
    （非 ``all_cards`` 的调用路径）按 True 处理，不误杀。
    """
    views = card.get("views")
    return views is None or "active" in views


def _in_section(key: str, card: dict, cfg: dict) -> bool:
    """单卡是否属于某个板块。**只判"性质"，不判日期窗口** ——
    日期窗口（近 N 天）交给调用方，这样前端点「全部」胶囊时能跳过窗口看全量。"""
    if key == "new_low":
        return (card.get("low_class") == classify.STEAM_LOW_NEW) and _is_live(card)
    if key == "expiring":
        # 按「到期」这一维筛，**不叠加近 7 天**：7 天前开始的老折扣照样马上要过期，
        # 按新增日期筛会把最紧急的那批漏掉（实测 107 → 23）。
        views = card.get("views")
        if views is not None:
            return "upcoming" in views
        # 没有 views 标志时（非 all_cards 的调用路径）按「剩 ≤2 天」近似
        left = card.get("days_left")
        return left is not None and left <= 2
    if key == "popular":
        return (review_count(card) >= int(cfg.get("notable_review_count", DEFAULT_NOTABLE))
                and _is_live(card))
    if key == "big_cut":
        return (card.get("cut") or 0) >= int(cfg.get("big_cut_percent", DEFAULT_BIG_CUT)) and _is_live(card)
    return False


def section_keys(card: dict, cfg: dict) -> list[str]:
    """这张卡片属于哪几个板块（同一游戏可跨板块，允许重复）。
    写进每张卡的 ``card["sections"]``（分片 ``all/<key>_<n>.js`` 与 data.js 都带），
    前端按它筛出板块的完整列表。"""
    return [spec["key"] for spec in HOME_SECTIONS if _in_section(spec["key"], card, cfg)]


def _section_members(key: str, cards: list[dict], cfg: dict) -> list[dict]:
    days = int(cfg.get("home_new_low_days", DEFAULT_HOME_DAYS))
    members = [c for c in cards if _in_section(key, c, cfg)]
    # 「即将到期」不叠加日期窗口（见 _in_section 注释），其余三个要
    if key == "expiring":
        return members
    return [c for c in members if _is_fresh(c, days)]


def _section_sort(key: str, cfg: dict | None = None):
    """每个板块「精选」的顺序（都带 title 收尾键，保证 deterministic）。

    ⚠️ **2026-10-08 用户重定：各板块需求不同 ⇒ 排序维度跟着板块语义走**，不再
    「三个板块共用一套推荐公式」—— 大促时名气头部游戏全是 ≥80% 折扣，「热门」与
    「大额折扣」按同一公式（名气 40 分主导）排出的头 10 张**逐项相同**（实测），
    两个板块等于一个板块。现在的口径：

    · **新史低** = 推荐公式（:func:`featured_score`，refs §10.2 名气优先）——
      注意 2026-10-09 起大卡改用专属权重档（:data:`PICKS_WEIGHTS`，折扣优先），
      与本板块**刻意不同**（两处同序 = 大卡复读板块，用户要求拆开）。
    · **即将到期** = 「到期近 → 远」：公式的「紧迫」项只有 5 分，压不过名气（40 分），
      按公式排会把「剩 0 天的小游戏」排到「剩 2 天的大作」后面 —— 这个板块的全部
      意义就是「快没了」，紧迫必须优先。
    · **热门游戏** = **评价数降序**（板块语义就是「名气榜」，refs A-4：热门=10000+）；
      平手比好评率 → title。
    · **大额折扣** = **折扣降序**（板块语义就是「力度榜」，refs A-4：高折扣=80%+）；
      平手比评价数 → title。恢复自然顺序后，板块内「折扣降序」排序选项重新被隐含
      （见 :func:`filter_specs` 的 implied 表）。
    """
    if key == "expiring":
        return lambda c: (c.get("days_left") if c.get("days_left") is not None else 99,
                          -(c.get("cut") or 0), c.get("title") or "")
    if key == "popular":
        return lambda c: (-review_count(c),
                          -((c.get("reviews") or {}).get("score") or 0),
                          c.get("title") or "")
    if key == "big_cut":
        return lambda c: (-(c.get("cut") or 0), -review_count(c), c.get("title") or "")
    return lambda c: (-featured_score(c, cfg), -review_count(c), c.get("title") or "")


def build_sections(cards: list[dict], cfg: dict) -> list[dict]:
    """首页四板块。``items`` 放前 K 条，完整条数放 ``count``，
    前端「查看更多」按需展开（数据在 ``all/<key>_<n>.js`` 分片里，按需懒加载）。

    ⚠️ **栏位统一、随池量动态**（2026-10-08 用户定案）—— 明天大促结束后
    新史低池量回落，固定 10 条会放不满、四板块参差。四板块共用**同一个**
    预览条数 K，随池量在平时与节日之间自动调整：

        K = clamp(四板块最小池量, 最低栏位, 上限)
          = min(home_section_preview(10), max(home_section_preview_min(5), min_pool))

      · 大促（各池都 ≥10）→ K = 10，满额；
      · 平时（最小池量 5~9）→ K = 最低栏位 5 ~ 9，各板块条数一致、整齐；
      · 个别板块池子不足 K 时**如实显示池量**（不凑数 —— 与大卡同一哲学，
        池量本身就是「平时 vs 节日」的信号，不需要额外的节日开关）。
    """
    preview = int(cfg.get("home_section_preview", DEFAULT_HOME_SECTION_PREVIEW))
    floor = int(cfg.get("home_section_preview_min", DEFAULT_HOME_SECTION_FLOOR))
    boards = []
    for spec in HOME_SECTIONS:
        members = _section_members(spec["key"], cards, cfg)
        members.sort(key=_section_sort(spec["key"], cfg))
        boards.append((spec, members))
    min_pool = min((len(m) for _, m in boards), default=0)
    k = min(preview, max(floor, min_pool))
    out = []
    for spec, members in boards:
        out.append({
            "key": spec["key"],
            "label": spec["label"],
            "count": len(members),
            "items": members[:k],
        })
    return out


#: **「新史低」板块 / 列表精选的**推荐权重 —— refs.md **§10.2 的 v2「轮播版」**（名气优先）。
#: ⚠️ **2026-10-09 起顶部大卡已拆到独立档 :data:`PICKS_WEIGHTS`（折扣优先），
#: 这里不再管大卡**（用户裁决：两处同序 = 大卡复读板块前 15 名，见该表注释）。
#: 每项先归一到 0~1 再乘权重，满分 100；改档位优先改 ``config.json`` 的
#: ``recommend_weights``（只写要改的那几项即可），不必动代码。
#: 缺数据的项**不计分、按剩余权重归一化**（见 recommend_score 末尾）。
#: ⚠️ **这里没有「史低类型」这一项，是故意的** —— refs §10.2 的轮播版当初就不给，
#: 理由写在表里：「池子全是新史低，无区分度」。2026-10-07 池子放宽成「新史低 +
#: 平史低」之后我一度补过一项 `low`，用户明确要求**改回来**（原话：「别改大卡的公式，
#: 你怎么乱动公式，你测一下效果就行了」）—— 所以**别再往这里加 low**（:data:`PICKS_WEIGHTS`
#: 同一红线）。
#: 有「史低」那一项的是**订阅端版**（史低 10 分，见 refs §10.2 右列）。
RECOMMEND_WEIGHTS = {
    "fame": 40,     # 名气：对数压缩，20 万评价封顶
    "cut": 30,      # 折扣：95% 满分
    "review": 20,   # 口碑：50% → 0 分，90% → 满分
    # 间隔（距上次史低天数）：refs §10.2 原给轮播版 15 分，但实测它在**新史低为主的池子里
    # 永远是空的**（新史低的「上次史低」就是本次），那 15 分实际上从不参与打分 ——
    # 等于把名气/折扣悄悄放大，与文档写的权重对不上（用户 2026-10-07：「怎么感觉排出来
    # 结果有点不同」）。故默认 0，那 15 分并进名气 / 折扣 / 口碑。要启用就改配置。
    "gap": 0,
    "urgent": 5,    # 紧迫：快到期
    "fresh": 5,     # 新鲜：折扣刚开始
}
#: 顶部大卡**候选池的分层顺序**（用户 2026-10-07：「优先新史低，没有才显示平史低」）
#: —— `pick_top` 就按这个顺序逐层取。
#: ⚠️ 这只是**取候选的先后**，与打分权重无关（权重表里没有史低类型这一项，别加）。
#: ⚠️ 顺序别调换：反过来的话 90% off 的老 3A 平史低会把新史低全挤掉。
PICK_LOW_CLASSES = (classify.STEAM_LOW_NEW, classify.STEAM_LOW_TIE)

#: 大卡「一页几张」—— **口径唯一来源就在这里**，经 payload 的 ``pick_page`` 下发，
#: 前端 app.js 的 `PICK_MAX_COLS`（一排最多几张）从 payload 读、不再自己写死 5
#: （2026-10-07 review：双源靠注释提醒同步迟早漂移，改由 payload 下发）。
HOME_PICKS_PAGE = 5
#: 大卡张数的**默认上限** = **三页 15 张**（2026-10-08 用户澄清定案）：
#: 「最低展示五个，最多展示 15 个，新史低为主，没有才替补平史低，不要强行显示
#: 15 个」—— 上一版默认 5 导致**大促也只有 5 张**（`home_picks` 配置从未设置，
#: 「能凑够再放」的意图没落地）。现在的口径：
#:   · 张数 = min(新史低能凑满的整数页 × 5, 15)，**最低一页 5 张**（不够的页用
#:     平史低补满 ——「没有才替补平史低」）；
#:   · 新史低凑不满 15 张时按整数页回落（10 / 5），**绝不拿平史低硬凑到 15**。
#: 想改上限就改 `config.json` 的 `home_picks`（仍是 5 的整数倍：10 / 15）。
#: ⚠️ 别拿大促期间的数据来"验证"平时条数 —— 大促那几天新史低一天上千条
#: （refs §6.2 实测 09-26→10-05：24/16/13/28/29/47/**1943**/4/2/4），上限天天填满；
#: 平时一天只有个位数，回落到一页 5 张才是常态。
HOME_PICKS_DEFAULT = DEFAULTS["home_picks"]
#: 前置门槛（§10.2）：有评价数 · 好评率 ≥ min_rate · 评价数 ≥ min_count
RECOMMEND_MIN_RATE = DEFAULTS["recommend_min_rate"]
RECOMMEND_MIN_COUNT = DEFAULTS["recommend_min_count"]
#: 名气 / 间隔 的封顶值（达到即满分）
RECOMMEND_FAME_CAP = 200_000
RECOMMEND_GAP_CAP_DAYS = 366


def _clamp01(value: float) -> float:
    return 0.0 if value < 0 else (1.0 if value > 1 else float(value))


def _resolve_weights(defaults: dict, cfg: dict | None, key: str) -> dict:
    """按 ``cfg[key]`` 覆盖 ``defaults`` —— 缺项沿用默认、未知项忽略、``None`` 忽略。"""
    weights = dict(defaults)
    for name, value in ((cfg or {}).get(key) or {}).items():
        if name in weights and value is not None:
            weights[name] = float(value)
    return weights


def recommend_weights(cfg: dict | None) -> dict:
    """「新史低」板块 / 列表精选的权重（``recommend_weights`` 配置覆盖默认档）。"""
    return _resolve_weights(RECOMMEND_WEIGHTS, cfg, "recommend_weights")


#: 顶部大卡（「今日最值」）的**专属权重档**（2026-10-09 用户定案）：
#: 大卡与「新史低」板块此前共用同一套 :data:`RECOMMEND_WEIGHTS`，同一批池子
#: （都是新史低）排出来的**条目和顺序完全一样** —— 大卡等于新史低板块的前 15 名
#: 复读。两处的语义本来就不同：
#:   · **新史低板块** = 今天有什么新史低的榜单 → 名气优先（browse 口径）；
#:   · **大卡** = 今日最值的门面位 → **折扣力度优先**，名气其次；**距上次史低
#:     越久**与快到期的深折一起往前顶（gap / urgent 档）。
#: **「间隔」（gap）2026-10-09 用户定案加入大卡档（10 分：名气 30→25、紧迫
#: 10→5 各让 5）** ——
#: 距上次史低越久 = 这次回到史低越难得。**新史低**取「史低期记忆」的 prev
#: （`game_meta.low_period.prev`，同日补上；冷启动记忆未建立时无值、不计分），
#: **平史低**取 ITAD 的上次同价时刻 —— 两档都有值。
#: 想调大卡就改 ``config.json`` 的 ``picks_weights``（只写要改的项，语义同
#: ``recommend_weights``）；⚠️ 照样**不许加「史低类型」项**（分层取已保证
#: 新史低优先，加 low 项是重复计分，见 RECOMMEND_WEIGHTS 的红色注释）。
PICKS_WEIGHTS = {
    "fame": 25,     # 名气：降到次席（对数压缩口径不变），让出 5 分给间隔
    "cut": 40,      # 折扣：**主导** —— 门面位要的是「一眼值」
    "review": 15,   # 口碑
    "gap": 10,      # 间隔：新史低取史低期记忆、平史低取上次同价（越久越难得，见上）
    "urgent": 5,    # 紧迫：让出 5 分给间隔（原 10）
    "fresh": 5,     # 新鲜
}


def picks_weights(cfg: dict | None) -> dict:
    """大卡权重：``picks_weights`` 配置覆盖（缺项沿用 :data:`PICKS_WEIGHTS`）。"""
    return _resolve_weights(PICKS_WEIGHTS, cfg, "picks_weights")


def _recommend_parts(card: dict, cfg: dict | None = None) -> dict:
    """推荐公式的**各分项**（都已归一到 0~1）。算式与出处见 :func:`recommend_score`。"""
    count = review_count(card)
    rate = review_score(card)
    days_left = card.get("days_left")          # ⚠️ 别写 `or 99`：0 天是合法值
    left = days_left if days_left is not None else 99
    start_ago = card.get("start_days_ago")
    ago = start_ago if start_ago is not None else 99
    parts = {
        "fame": _clamp01(math.log10(count + 1) / math.log10(RECOMMEND_FAME_CAP + 1)),
        "cut": _clamp01((card.get("cut") or 0) / 95.0),
        "review": _clamp01(((rate or 50.0) - 50.0) / 40.0),
        "urgent": 1.0 if left <= 1 else 0.6 if left <= 2 else 0.2 if left <= 7 else 0.0,
        "fresh": 1.0 if ago <= 2 else 0.6 if ago <= 7 else 0.0,
    }
    gap = card.get("last_low_days")
    if gap is not None and gap >= 0:
        parts["gap"] = _clamp01(
            math.log10(gap + 1) / math.log10(RECOMMEND_GAP_CAP_DAYS + 1))
    return parts


def _weighted_total(parts: dict, cfg: dict | None = None,
                    weights: dict | None = None) -> float:
    """按权重加权并**归一化到 0~100** —— 缺数据的项不计分、按「可用权重之和」折算。

    ``weights`` 缺省用 :func:`recommend_weights`（板块/精选口径）；
    大卡传 :func:`picks_weights`（专属档，见 :data:`PICKS_WEIGHTS`）。
    """
    weights = weights if weights is not None else recommend_weights(cfg)
    total = sum(weights[key] for key in parts)
    if total <= 0:
        return 0.0
    return sum(weights[key] * parts[key] for key in parts) / total * 100.0


def recommend_score(card: dict, cfg: dict | None = None) -> float | None:
    """推荐公式的分数（0~100）—— **「新史低」板块 / 列表精选的「名气优先」档**
    (:data:`RECOMMEND_WEIGHTS`)。**不满足前置门槛返回 ``None``** = 不进推荐位。

    打分项归一（refs.md §10.2 轮播版；**默认不含「间隔」** —— 见 RECOMMEND_WEIGHTS 的注释）：

    - 名气 ``log10(评价数+1) / log10(20 万+1)`` —— 用对数压，否则 156 万评价
      的游戏会把其余项压成噪声（§7.1 实测：彩虹六号 35% 折扣霸榜就是这么来的）
    - 折扣 ``折扣% / 95``
    - 口碑 ``(好评率 − 50) / 40``
    - 间隔 ``log10(距上次史低天数 + 1) / log10(367)`` —— 平史低取上次同价、
      新史低取史低期记忆（`low_period.prev`；冷启动未建立时无值）；本档默认给 0，
      等于不参与（配置里配了才会算 —— 见 RECOMMEND_WEIGHTS 的注释）
    - 紧迫 剩 ≤1 天 1.0 / ≤2 天 0.6 / ≤7 天 0.2 / 更久 0
      （原文按小时给档，卡片只有「剩 X 天」的日历天，按天近似）
    - 新鲜 折扣开始 ≤2 天 1.0 / ≤7 天 0.6 / 更早 0

    ⚠️ **缺数据的项不计分，并把总分按"可用权重之和"归一化回 100** ——
    这样「有数据的项」不会被平白稀释，也不会因为缺一项就系统性吃亏。
    （分项与加权拆在 :func:`_recommend_parts` / :func:`_weighted_total`，
    ``featured_score``（板块精选）复用同一套算式；**顶部大卡 2026-10-09 起改用
    :data:`PICKS_WEIGHTS`（折扣优先），两处口径刻意不同**，别再当成同一套。）
    """
    count = review_count(card)
    rate = review_score(card)
    min_count = int((cfg or {}).get("recommend_min_count", RECOMMEND_MIN_COUNT))
    min_rate = float((cfg or {}).get("recommend_min_rate", RECOMMEND_MIN_RATE))
    if not count or rate is None:            # 没有详情 → 不进推荐位（宁可少推，不瞎推）
        return None
    if count < min_count or rate < min_rate:
        return None
    return _weighted_total(_recommend_parts(card, cfg), cfg)


def featured_score(card: dict, cfg: dict | None = None) -> float:
    """「精选」排序的打分（0~100）—— **同一套 refs §10.2 公式，但不设前置门槛**。

    2026-10-07 用户：「精选用公式，refs 文档中有参考公式，根据这个推断修正精选的公式」。
    列表页里「详情待补 / 低口碑」的条目也要能排（不能像大卡那样直接不进），所以
    复用 :func:`_recommend_parts` + :func:`_weighted_total`：缺数据的项照常计 0、
    按剩余权重归一 —— 口径与**「新史低」板块**一致（同一 :data:`RECOMMEND_WEIGHTS` 档，
    改权重两处一起变）；⚠️ **顶部大卡 2026-10-09 起已拆到 :data:`PICKS_WEIGHTS`
    （折扣优先），与本函数不再同口径**。
    """
    return _weighted_total(_recommend_parts(card, cfg), cfg)


def recommend_sort_key(card: dict, cfg: dict | None = None,
                       weights: dict | None = None) -> tuple:
    """推荐位排序键：分高在前；平手比 折扣% → 评价数 → 价格低 → appid。

    最后一项（appid）是为了**结果稳定可复现** —— 不加的话每次跑出来顺序会抖。
    ``weights`` 传 :func:`picks_weights` 时即大卡排序键（分值按大卡专属权重算）。
    """
    score = _weighted_total(_recommend_parts(card, cfg), cfg, weights) \
        if weights is not None else recommend_score(card, cfg)
    return (
        -(score if score is not None else -1.0),
        -(card.get("cut") or 0),
        -review_count(card),
        card.get("price_int") if card.get("price_int") is not None else 10 ** 9,
        card.get("appid") or 0,
    )


def pick_top(cards: list[dict], cfg: dict) -> list[dict]:
    """首页顶部「今日最值」大卡横排的候选（前端每页 5 张、可左右翻页）。

    池子 = 近 N 天（``home_new_low_days``）的 **新史低 + 平史低** ∩ 还在折扣期内，
    **分层取**；打分走 §10.2 权重公式 v2 的**大卡专属档** :data:`PICKS_WEIGHTS`
    （折扣优先，见其注释；门槛照旧，**不含「史低类型」**）。

    ⚠️ **分层取**（用户 2026-10-07 定稿：「优先新史低，没有才显示平史低」）：
    1. 先取近 N 天的**新史低**（过门槛 + 打分排序）；
    2. 只有不够**一整页**时，**才**用同一套规则补**平史低**（把当前那页补满）。

    张数 = 「新史低能凑满几页」× ``HOME_PICKS_PAGE``（5），上限 ``home_picks``
    （**默认 15**，见 :data:`HOME_PICKS_DEFAULT` 的 2026-10-08 澄清）；
    **最低一页 5 张**，新史低凑不满时按整数页回落（10 / 5），末页用平史低补满 ——
    **绝不拿平史低硬凑上限**。

    refs §10.2 原文写的是「**轮播池 = 新史低 ∩ 有详情**」（原文权重表里「史低」那一格
    给轮播版的就是「—（池子全是新史低，无区分度）」）—— 用户确认的口径是它的放宽版：
    平史低**有机会**进，但新史低优先。实测**不分层**的话 15 张里 14 张是平史低
    （90% off 的老 3A 名气分高、全被顶上来），底部色条几乎全灰。

    ⚠️ 门槛（好评率 ≥70% 且评价数 ≥100）把「详情待补」的条目也挡在外面 ——
    推荐位不能推没数据的游戏。每层若全都不过门槛（详情大面积缺失的极端情况），
    该层退回老的字典序，保证大卡这一块不会整块消失。
    """
    days = int(cfg.get("home_new_low_days", DEFAULT_HOME_DAYS))
    # 展示张数 = 「新史低能凑满几页」× 每页张数，上限 home_picks（默认 15）：
    #   大促（新史低 ≥15）→ 3 页 15 张；新史低 7 → 2 页 10 张；个位数 → 1 页 5 张。
    #   末页不够时**只补满这一页**（否则一排会缺格子、右边空一块），绝不硬凑上限。
    # 用户 2026-10-08：「最低展示五个，最多展示 15 个，新史低为主，没有才替补
    # 平史低，不要强行显示 15 个」。
    want = max(HOME_PICKS_PAGE, int(cfg.get("home_picks", HOME_PICKS_DEFAULT)))
    pool = [c for c in cards if _is_fresh(c, days) and _is_live(c)]
    # 大卡专属权重档：算一次给排序键用（别塞进 key 的 lambda 里逐次重算）
    pw = picks_weights(cfg)

    def ranked(low_class: str) -> list[dict]:
        """该史低类型里按**大卡专属权重**排好的候选（门槛挡掉的一律不出现）。

        ⚠️ 打分用 :func:`picks_weights`（折扣优先档），不是板块的推荐档 ——
        否则大卡就是「新史低板块前 N 名」的复读（2026-10-09 用户定案拆开）。
        门槛（好评率/评价数）仍按大卡原口径，与权重无关。
        """
        same = [c for c in pool if c.get("low_class") == low_class]
        scored = [c for c in same if recommend_score(c, cfg) is not None]
        if not scored:
            same.sort(key=featured_sort_key)
            return same
        scored.sort(key=lambda c: recommend_sort_key(c, cfg, weights=pw))
        return scored

    new_ok = ranked(classify.STEAM_LOW_NEW)
    tie_ok = ranked(classify.STEAM_LOW_TIE)
    pages = min(want // HOME_PICKS_PAGE,
                max(1, (len(new_ok) + HOME_PICKS_PAGE - 1) // HOME_PICKS_PAGE))
    take = pages * HOME_PICKS_PAGE
    out = new_ok[:take]
    if len(out) < take:
        out += tie_ok[: take - len(out)]
    return out


#: 站点链接默认值（配置里可覆盖，`config.example.json` 有这两个键）
DEFAULT_REPO_URL = DEFAULTS["site_repo_url"]
DEFAULT_ACTIONS_URL = DEFAULTS["site_actions_url"]


def site_links(cfg: dict) -> dict:
    return {
        "repo": cfg.get("site_repo_url") or DEFAULT_REPO_URL,
        "actions": cfg.get("site_actions_url") or DEFAULT_ACTIONS_URL,
    }


#: 底部抽屉筛选的**排序**维度（refs.md §6.7 推荐方案的第一个分组）。
#: 「精选」= 保持服务端给的顺序（板块自己的排序 / 大卡的权重分），不在这里重排。
FILTER_SORTS = [
    {"value": "featured", "label": "精选"},
    {"value": "cut", "label": "折扣降序"},
    {"value": "price", "label": "价格升序"},
    {"value": "rate", "label": "好评率降序"},
]


def filter_specs(cfg: dict, cards: list[dict] | None = None) -> list[dict]:
    """底部抽屉式筛选的分组与选项（refs.md §6.7 的推荐方案）。

    维度：**排序 / 日期 / 折扣区间 / 好评数量 / 仅新史低**。
    之所以做成抽屉而不是顶栏一排胶囊 —— 维度一多顶栏就挤爆，而且手机上找不到。

    ⚠️ 阈值一律**从配置来**（同 group_specs 的思路）：改 `big_cut_percent`
    / `notable_review_count` / `home_new_low_days`，选项文案跟着变，
    不会出现「选项写着 ≥80%、实际按 75% 筛」这种撒谎。

    ``cards``：给了就按它算「哪些日期没有数据」，该选项标 ``disabled: True``
    （refs §11.5 Q2「没有数据的天数**置灰不可选**」）。池子应与首页四板块同源
    （``all_cards``），否则置灰的口径跟实际列表对不上。

    **日期值的协议**（后端拼、前端 :func:`parseDateSpec` 解，改格式两边必须同步）：

    - ``"dN"`` = 近 N 天（``start_days_ago <= N``）
    - ``"N"``  = 距今第 N 天（``start_days_ago == N``，0 = 今天）
    - ``"all"``= 不限
    """
    days = int(cfg.get("home_new_low_days", DEFAULT_HOME_DAYS))
    big = int(cfg.get("big_cut_percent", DEFAULT_BIG_CUT))
    notable = int(cfg.get("notable_review_count", DEFAULT_NOTABLE))
    # 折扣区间 / 好评数量的档位与 agg 维度共用同一份计算（_cut_bands / _review_bands）
    cuts = _cut_bands(cfg)
    counts = _review_bands(cfg)
    # 置灰统计：只算「开始时间已知」的卡片 —— 前端 dateOk 对未知 start 一律返回 false，
    # 口径必须一致，否则会出现「选项没置灰但点进去是空的」
    agos = [c.get("start_days_ago") for c in (cards or [])]
    agos = [a for a in agos if a is not None]

    def dead(spec_value: str) -> bool:
        if cards is None:
            return False
        if spec_value.startswith("d"):
            return not any(a <= int(spec_value[1:]) for a in agos)
        return int(spec_value) not in agos

    # ---- 哪些选项在哪些板块里「选了也不会变」⇒ 前端置灰（用户 2026-10-07：
    #   「新史低里面还能再选新史低选项，大额折扣里面还能选折扣降序，置灰」）----
    # 依据都是**板块自己的口径**，不是手感：
    #   · 新史低板块：`_in_section` 已要求 `low_class == new` ⇒「仅新史低」无效。
    #   · 大额折扣板块：`_in_section` 已要求 `折扣 ≥ big_cut` ⇒ 折扣区间里 ≤ big_cut 的档位
    #     筛不掉任何东西。
    #   · 热门游戏板块：`_in_section` 已要求 `评价数 ≥ notable` ⇒ 好评数量里 ≤ notable 的档位无效。
    # ⚠️ **「折扣降序」重新进这张表**（2026-10-08）：板块排序已改回各板块自己的维度
    #    （大额折扣 = 折扣降序，见 `_section_sort`），板块内「精选」与「折扣降序」等价 ⇒
    #    在大额折扣板块里选「折扣降序」不会改变顺序，置灰。其余排序选项仍有区分度。
    # ⚠️ 这几条都跟着「板块口径」走 —— 改 `_in_section` / `_section_sort` 时必须回来看一眼；
    #    `tests/test_report.py` 里有对应的断言。
    sort_opts = []
    for o in FILTER_SORTS:
        if o["value"] == "cut":
            o = dict(o, implied=["big_cut"], implied_note="本板块已按折扣降序排列")
        sort_opts.append(o)
    cut_opts = [{"value": "all", "label": "不限"}]
    for v in cuts:
        o = {"value": str(v), "label": f"≥ {v}%"}
        if v <= big:                      # 与板块下限同档或更宽松 → 筛不掉东西
            o["implied"] = ["big_cut"]
            o["implied_note"] = f"本板块已要求 ≥ {big}%"
        cut_opts.append(o)
    rev_opts = [{"value": "all", "label": "不限"}]
    for v in counts:
        o = {"value": str(v), "label": f"≥ {v:,}"}
        if v <= notable:
            o["implied"] = ["popular"]
            o["implied_note"] = f"本板块已要求 ≥ {notable:,}"
        rev_opts.append(o)

    # 日期选项直接从 _date_bands 取（与 filter_dim_values 的 agg 维度同源），
    # 只补各自的 `disabled`（「这一天没有数据」）；「全部」永不留空，恒可选。
    date_opts = [dict(o, disabled=False if o["value"] == "all" else dead(o["value"]))
                 for o in _date_bands(cfg)]

    return [
        {"key": "sort", "label": "排序", "options": sort_opts},
        {"key": "date", "label": "日期", "options": date_opts},
        {"key": "cut", "label": "折扣区间", "options": cut_opts},
        {"key": "reviews", "label": "好评数量", "options": rev_opts},
        {"key": "only_new", "label": "史低类型", "options": [
            {"value": "all", "label": "不限"},
            {"value": "new", "label": "仅新史低", "implied": ["new_low"],
             "implied_note": "本板块已全是新史低"},
        ]},
    ]


def filter_defaults(cfg: dict) -> dict:
    """筛选的默认值 —— 前端 `state.filters` 的初始状态，也是「已选 N 项」角标的基准。

    「日期」默认跟着 `home_new_low_days` 走（现在 7 天），不写死 d7。
    """
    return {
        "sort": "featured",
        "date": f"d{int(cfg.get('home_new_low_days', DEFAULT_HOME_DAYS))}",
        "cut": "all",
        "reviews": "all",
        "only_new": "all",
    }


def criteria_notes(cfg: dict) -> list[dict]:
    """「关于网站」页的筛选口径 —— **从配置生成**，改阈值页面自动跟着变
    （同 group_specs 的思路：别把阈值文案写死在模板里，否则改了配置就对不上）。"""
    pct = int(round(float(cfg.get("min_positive_ratio", 0.7)) * 100))
    min_count = int(cfg.get("min_review_count", 100))
    notable = int(cfg.get("notable_review_count", DEFAULT_NOTABLE))
    scope = "只收 type=game 的本体折扣，排除免费游戏"
    if cfg.get("exclude_mature"):
        scope += "与成人内容"
    return [
        {"k": "收录范围", "v": scope},
        {"k": "史低判定", "v": "用 IsThereAnyDeal 的 flag（N=新史低 / H=平史低）。"
                              "**不靠比价格** —— Steam 店史低已含本次折扣，比价会把新史低也判成相等"},
        {"k": "展示门槛", "v": f"好评率 ≥ {pct}% 且 评价数 ≥ {min_count}；低于此不入列表"},
        {"k": "高热度档", "v": f"评价数 ≥ {notable:,}，不看好评率（热门游戏板块用它）"},
        {"k": "大额折扣档", "v": f"折扣 ≥ {int(cfg.get('big_cut_percent', DEFAULT_BIG_CUT))}%"},
        {"k": "板块时间窗", "v": f"首页三板块（新史低/热门/大额折扣）只看近 "
                                f"{int(cfg.get('home_new_low_days', DEFAULT_HOME_DAYS))} 天新增；"
                                "「即将到期」按到期时间算，不叠加时间窗"},
        {"k": "即将到期", "v": f"距折扣结束 ≤ {int(cfg.get('upcoming_expiry_hours', DEFAULT_UPCOMING_HOURS))} 小时"},
        {"k": "多版本去重", "v": "同一 appid 只保留价格最低的那条"},
        {"k": "留存", "v": f"折扣过期后仍保留 {int(cfg.get('expired_retention_days', 7))} 天"},
    ]


def render(cfg: dict, items: list[dict], stats: dict, now: datetime,
           *, fx: dict | None = None, steam: dict | None = None,
           all_cards: list[dict] | None = None,
           run_log: list[dict] | None = None) -> dict:
    """写出一整套静态文件，返回产出路径。

    ``items`` 为当日新增的原始卡片：计入 low_points，``all_cards`` 缺席时兜底作
    首页板块池子（兼容直接调用 render 的测试与工具）。

    ``all_cards``：「全部」视图的数据 —— 今日筛选链通过的全量卡片，每张带
    ``views`` 列表（week/active/new_today/upcoming 成员标志，run.py 计算）；
    传了就写出板块完整列表的分片 ``all/<key>_<n>.js``（2026-10-08 起取代单文件
    ``all.js`` —— 见 :func:`write_all_shards` 的注释）。

    ⚠️ 2026-10-08：payload 不再下发 ``views`` / ``groups`` / ``view_groups``
    （首屏瘦身）—— 视图按钮与「即将过期」卡片都无前端消费者；「即将到期」板块的
    完整列表走 ``all/`` 分片的 ``expiring`` 板块，expiring.json 快照导出由 run.py 的
    ``upcoming_shown_items`` 负责。
    """
    output_dir = Path(cfg["output_dir"])
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "static").mkdir(parents=True, exist_ok=True)

    for name in ("app.css", "app.js"):
        shutil.copyfile(STATIC_DIR / name, output_dir / "static" / name)

    version = str(int(now.timestamp()))

    # 批 E spec E4：概览的「史低构成」色点 —— 直接数 cards，
    # 保证色点之和 == 进列表条数（不可能出现对不上的情况）
    low_points = {classify.STEAM_LOW_NEW: 0, classify.STEAM_LOW_TIE: 0,
                  classify.STEAM_LOW_UNKNOWN: 0}
    for item in items:
        key = item.get("low_class") or classify.STEAM_LOW_UNKNOWN
        low_points[key] = low_points.get(key, 0) + 1

    # ---- S9 首页四板块 + 顶部大卡：池子优先用「全部」视图那批（all_cards）----
    home_pool = all_cards if all_cards is not None else items
    sections = build_sections(home_pool, cfg)
    picks = pick_top(home_pool, cfg)

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_text": now.strftime("%Y-%m-%d %H:%M"),
        #: 资源版本（= now 的 epoch 秒）——模板给 data.js / app.js 的 ``?v=`` 用它，
        #: app.js 懒加载 ``all/`` 分片时也拼同一个值（2026-10-08：原先的单文件
        #: ``all.js`` 不带 ``?v=``，会吃 Pages 的 ~10 分钟缓存、可能短暂取到旧版）。
        "assets_version": version,
        "stale_banner_hours": int(cfg.get("stale_banner_hours", 36)),
        #: S9-3 顶部消息区：>26h 黄（Actions 延迟）/>36h 红（今天没更新）。
        #: 阈值从配置来，前端不再写死第二份（review-s9-01 确立的约定）——
        #: 留在 payload 里是因为「数据新不新鲜」只能等页面打开时才知道，
        #: 服务端算不了（渲染时刻 ≠ 访客打开时刻）。
        "stale_warn_hours": int(cfg.get("stale_warn_hours", 26)),
        "sweep": stats.get("sweep"),
        "overview": stats,
        "low_points": low_points,
        #: ⚠️ payload 里**没有** ``groups`` / ``view_groups`` / ``views``（2026-10-08 删除）：
        #: S9 首页板块走 sections、大卡走 picks、「即将到期」完整列表走 ``all/`` 分片的
        #: expiring —— 没有前端消费者。这组键曾让 data.js 在大促尾期膨胀到
        #: 7.4MB（首屏几十秒）——别加回来。
        #: S9 首页四板块（池子 = 「全部」视图那批卡片，见 build_sections 注释）
        "sections": sections,
        #: S9 顶部「今日最值」大卡横排候选（每页张数 = pick_page，前端不再自抄一份）
        "picks": picks,
        "pick_page": HOME_PICKS_PAGE,
        "fx": fx_display(cfg, fx),
        "steam": steam or {},
        #: 底部抽屉筛选的分组/选项与默认值（refs.md §6.7）；前端 state.filters 用
        "filter_defaults": filter_defaults(cfg),
        #: 断点必须与 app.css 的 @media 一致，否则「布局按手机、每页按桌面」会错位
        #: 列表加载参数（S9-3 起由「每页条数」换成「每批追加 + 自动追加上限」）：
        #: batch      = 每次追加几条（refs.md §9.2「20~30 条」）
        #: auto_max   = **自动**追加的总上限，到了就只留手动按钮
        #:              —— refs.md §9.2「不做无限追加」（否则「全部折扣」页 7000+ 条会滚不到底）
        #: breakpoint = 必须与 app.css 的 @media 一致，否则「布局按手机、逻辑按桌面」会错位
        "list": {
            "batch": int(cfg.get("list_batch", 30)),
            "auto_max": int(cfg.get("list_auto_max", 300)),
            "breakpoint": int(cfg.get("mobile_breakpoint_px", 600)),
            #: S9-卡片：三档布局的第二个边界（≤ 它是平板档，> 它是 PC 档）。
            #: 与 breakpoint 一样必须与 app.css 的 @media 一致；工具脚本也从这里读。
            "tablet_breakpoint": int(cfg.get("tablet_breakpoint_px", 1100)),
        },
        # ⚠️ 原 `notice`（「本周 / 折扣中 / 全部」那句灰字说明）已随 S9 删除：
        # 页面上唯一的消费者 `<p class="notice">` 没了，而文案讲的又是已经不存在的
        # 五个视图 —— 留着就是死数据（review-s9-01 #4）。
    }

    # ---- data.js 只放**前端真的会读**的键（2026-10-09，卡片 08）----
    # 之前是把整个 payload 塞进去，于是 data.js 里躺着 overview / fx / steam / sweep /
    # low_points / filter_defaults / generated_at_text —— 前端 app.js 一个都不读
    # （概览数字与汇率是**服务端渲染**进 index/about 的；筛选默认值走模板自己的
    # ``window.FILTER_DEFAULTS``）。首屏多背几十 KB 的死数据，且让人误以为改它们有用。
    # 覆盖统计要用的 overview / low_points 已在 latest.json 里（机器读的伴生产物）。
    # ⚠️ 新增键之前先确认 app.js 有消费者，否则又变成死数据。
    data_payload = {
        "generated_at": payload["generated_at"],
        "assets_version": payload["assets_version"],
        "stale_banner_hours": payload["stale_banner_hours"],
        "stale_warn_hours": payload["stale_warn_hours"],
        "sections": payload["sections"],
        "picks": payload["picks"],
        "pick_page": payload["pick_page"],
        "list": payload["list"],
    }
    data_js = "window.REPORT_DATA = " + json.dumps(data_payload, ensure_ascii=False) + ";\n"
    (output_dir / "data.js").write_text(data_js, encoding="utf-8")

    paths = {
        "index": str(output_dir / "index.html"),
        "data_js": str(output_dir / "data.js"),
        "latest_json": str(output_dir / "latest.json"),
        "item_count": len(items),
    }

    # 板块完整列表（首页点「查看更多」进去的那张页）—— 2026-10-08 起**按板块顺序
    # 切成 all/<key>_<n>.js 分片**，取代原先的单文件 all.js（5.81MB raw / 729KB gzip）。
    # 顺序不再单独下发：它就是分片的物理顺序，前端按片号依次读即得到板块顺序，
    # 排序口径仍然只有服务端一份（:func:`_section_sort`）。
    if all_cards is not None:
        manifest = write_all_shards(output_dir, all_cards, cfg)
        paths["all_shards"] = manifest
        paths["all_dir"] = str(output_dir / "all")

    latest = {
        "generated_at": payload["generated_at"],
        "sweep": payload["sweep"],
        "overview": stats,
        #: 史低构成（色点）—— data.js 已不再下发（前端不读，摘要由模板渲染）；
        #: 放在这里给 tools/check_payload.py 做「色点之和 == 进列表条数」的自洽校验。
        "low_points": low_points,
        #: 字段名与含义严格对应：`new_today_raw` 是真实新增量（可能远大于进列表的条数），
        #: 这里列出的是**实际进列表**的条目，故叫 `shown`
        "shown": [
            {
                "title": item["title"],
                "title_zh": item["title_zh"],
                "appid": item["appid"],
                "low_class": item["low_class"],
                "low_label": item["low_label"],
                "price_text": item["price_text"],
                "tier": item["tier"],
            }
            for item in sorted(items, key=featured_sort_key)  # 原 featured 组的顺序（组已删，排序键保留）
        ],
    }
    (output_dir / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # S9 导航栏：四个板块 + 「全部折扣」（首页 = 点站点名，不占导航栏位置）
    nav = ([{"key": spec["key"], "label": spec["label"]} for spec in HOME_SECTIONS]
           + [{"key": "__all__", "label": "全部折扣"}])

    links = site_links(cfg)
    # S9-3 顶部消息区（refs.md §4 A-3 / B2）：**渲染时**按北京时间筛出「正在进行」的
    # 活动与通知 —— 前端不判日期（访客本机时区会把日子算错，见 announcements.py）。
    # 筛不出东西就是 None，模板里那一行整块不渲染（用户口径：没活动就不显示）。
    messages = announcements.current(now, cfg.get("announcements_path"))
    template = env.get_template("index.html.j2")
    # ⚠️ `overview` 不再传：首页模板里最后一个消费者（页脚那行「生成时间 + 状态库最近一次运行」）
    # 已被用户要求删掉，其余概览数字都在 about 页（那里仍然传）。payload["overview"] 保留 ——
    # tools/check_payload.py 还在读它，删不删属于另一轮「payload 拆袋」的事。
    html = template.render(
        payload=payload,
        assets_version=version,
        nav=nav,
        links=links,
        fx=payload["fx"],
        low_points=low_points,
        generated_at_text=payload["generated_at_text"],
        msg_festival=messages["festival"],
        msg_notice=messages["notice"],
        # 置灰统计用与首页四板块**同一个池子**，否则「选项没置灰但点进去是空的」
        filter_groups=filter_specs(cfg, home_pool),
        filter_defaults=payload["filter_defaults"],
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")

    # ---- S9：「关于网站」页（其余概览数字 / 筛选口径 / 数据来源 / 汇率 / 更新日志）----
    about_tpl = env.get_template("about.html.j2")
    about_html = about_tpl.render(
        generated_at_text=payload["generated_at_text"],
        overview=stats,
        low_points=low_points,
        fx=payload["fx"],
        criteria=criteria_notes(cfg),
        run_log=list(reversed((run_log or [])[-10:])),   # 最近 10 次，新的在前
        links=links,
        assets_version=version,
    )
    (output_dir / "about.html").write_text(about_html, encoding="utf-8")
    paths["about"] = str(output_dir / "about.html")

    return paths