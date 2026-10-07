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

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = TEMPLATES_DIR / "static"

#: 分组的默认折叠状态（§7.2：分组默认展开，卡片一律折叠）
#: 标签与「条件」文案一律由 :func:`group_specs` 从配置生成 ——
#: 写死「优质」这类评价性词会误导（70% 好评率本来就不等于"优质"），
#: 而且阈值一改文案就对不上了。
GROUP_COLLAPSED = {
    classify.TIER_QUALITY: False,
    classify.TIER_NOTABLE: True,
    classify.TIER_PENDING: True,
}

#: 视图开关（§7.2）。
#: 批 F2：「已过期」已删除 —— 折扣过期后毫无价值（用户：过期折扣犹如砒霜），
#: 不配占一个分类按钮的位置。相关数据仍照常入库，只是不上页面。
#: 2026-09-27：「即将过期」上线（48h 窗口，``upcoming_expiry_hours``）。
#: 重构 S5（2026-10-05）：开放「本周 / 折扣中」，新增「全部」入口 ——
#: 这三个视图的数据量大（今日筛选链全量，约 5 千条、大促峰值 3 万），
#: 拆到独立 ``all.js`` 首次点击时懒加载（spec §1 决策 9），
#: 服务端只在 data.js 里预置按钮 count。
#: ⚠️ 2026-10-08：「即将过期」的分组数据也移出 data.js（``view_groups`` 键删除，
#: 大促尾期它 ≈ 全池，曾把 data.js 撑到 7.4MB）—— S9 前端本就不读它，
#: 「即将到期」板块的完整列表一直走 all.js 的 ``expiring`` 板块。
#: ``views`` 目前**没有前端消费者**（S9 不渲染这组按钮，app.js 也不读 payload.views），
#: 只有测试在读 —— 是否删它属另一轮「payload 拆袋」，本轮不动。
VIEWS = [
    {"key": "new_today", "label": "当日新增", "enabled": True},
    {"key": "week", "label": "本周(14天)", "enabled": True},
    {"key": "active", "label": "折扣中", "enabled": True},
    {"key": "upcoming", "label": "即将过期", "enabled": True},
    {"key": "all", "label": "全部", "enabled": True},
]

#: 走 all.js 懒加载的视图（数据不在 data.js 里，前端首次点击时 fetch）。
#: 「全部」本身也在其中 —— 它就是 all.js 的原始分组。
LAZY_VIEWS = ("week", "active", "all")

#: 「剩 X 天」的凌晨宽容阈值已挪到 `classify.EARLY_MORNING_EXPIRY_HOUR`
#: （2026-10-07：卡片与顶部活动条共用 `classify.days_until`，不再各留一份常量）


def group_specs(cfg: dict) -> list[dict]:
    """由配置生成分组标题与「入组条件」（§3.5）。

    标题刻意用**中性描述**而不是「优质」—— 70% 好评率是 Steam 的「多半好评」档，
    叫「优质」会让人误判。条件文案直接来自配置，改阈值时页面自动跟着变。
    """
    pct = int(round(float(cfg.get("min_positive_ratio", 0.7)) * 100))
    min_count = int(cfg.get("min_review_count", 100))
    notable = int(cfg.get("notable_review_count", DEFAULT_NOTABLE))
    return [
        {
            "key": classify.TIER_NOTABLE,
            "label": "高热度 · 口碑不一",
            "criteria": f"评价数 ≥ {notable:,}，不看好评率",
            "collapsed": GROUP_COLLAPSED[classify.TIER_NOTABLE],
        },
        {
            "key": classify.TIER_QUALITY,
            "label": "好评达标",
            "criteria": f"好评率 ≥ {pct}% 且 评价数 ≥ {min_count}",
            "collapsed": GROUP_COLLAPSED[classify.TIER_QUALITY],
        },
        {
            "key": classify.TIER_PENDING,
            "label": "详情待补",
            "criteria": "本轮还没取到详情，下次运行会自动补上",
            "collapsed": GROUP_COLLAPSED[classify.TIER_PENDING],
        },
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
GOOD_POSITIVE_RATIO = 0.9
#: 「差评」档的门槛（Steam 商店口径：40~69% 是「褒贬不一」、<40% 是「差评」）。
#: ⚠️ 这两档**只可能由「高热度 · 口碑不一」组（评价数 ≥ notable，不看好评率）的卡产生**
#: （「好评达标」组被 ≥70% 展示门槛挡住）；但那张卡**可以同时是**新史低 / 临期 / 大额折扣，
#: 所以 mid / low **可能出现在任何板块** —— 配色的意义就是在任何位置都能认出来。
#: （2026-10-07 review 纠正：旧注释写「只会出现在热门游戏板块」，那是**分组**口径不是
#: **板块**口径 —— COD 类大作踩新史低时照样进「新史低」板块，好感分档不硬砍。）
BAD_POSITIVE_RATIO = 0.4


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


def build_card(entry: dict, now: datetime, labels: dict | None = None,
               cfg: dict | None = None) -> dict:
    """把状态库条目（已合并详情）拼成卡片数据。

    ``cfg`` 只用于**配色分档的阈值**（好评率 / 剩余天数，见 rate_tier / days_tier）——
    不传就用模块默认值，所以老的调用与测试照常工作。
    """
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
    # §3.6 史低天数（「距上次史低」那一行）：new = 这次就是新纪录（没有具体日期）；
    # tie = 上一次 Steam 达到该价的时间（storelow/v2 批量取，存 low_time_cache）。
    # 主文本只放天数保证单行（日期太长会把整行挤成两排），具体日期由
    # 前端悬停/点按显示（last_low_date）。取不到就不渲染这一行（JS 对空值自动跳过），不猜。
    last_low_date = None
    last_low_text = None
    # 权重公式 v2 的「间隔」项要的是**数值天数**（refs.md §10.2）。
    # 只在平史低时有意义 —— 新史低的「上次史低」就是本次，天数恒等于折扣已开的天数，
    # 不是"间隔"，故留空（留空 = 该项不计分，见 recommend_score 的归一化）。
    last_low_days = None
    if low_class == classify.STEAM_LOW_NEW:
        # 批 F5：文本由「本次刷新历史记录」改为「本次新史低」（用户定案：只改文本、标签仍为「距上次史低」）
        last_low_text = "本次新史低"
    elif low_class == classify.STEAM_LOW_TIE:
        low_at = classify.parse_time(entry.get("last_low_at"), now.tzinfo)
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
        # 权重公式 v2「间隔」项用的数值天数（平史低有值，新史低/取不到为 None）
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
        "steam_url": f"https://store.steampowered.com/app/{appid}/" if appid else None,
        "xiaoheihe_url": f"https://www.xiaoheihe.cn/games/detail/{appid}" if appid else None,
        # R2：itad_url 已删（302 直跳 Steam，信息冗余），卡片与 payload 均不再输出
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


def _majority_start(items: list[dict]) -> str | None:
    """组内「折扣开始」按**多数派**上提（批 E spec E5 / 2026-09-25 实测口径）。

    并列时取较晚的，保证组头不会显示得比实际更早
    （比较的是 ``%Y-%m-%d %H:%M`` 定宽字符串，字典序即时序）。
    """
    starts = [item["start_text"] for item in items if item.get("start_text")]
    counts = Counter(starts)
    return max(counts, key=lambda text: (counts[text], text)) if counts else None


def build_groups(items: list[dict], cfg: dict) -> list[dict]:
    groups = []
    for spec in group_specs(cfg):
        group_items = [item for item in items if item["tier"] == spec["key"]]
        if not group_items:
            continue
        group_items.sort(key=lambda i: (-(i["cut"] or 0), i["title"] or ""))
        groups.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "criteria": spec["criteria"],
                "collapsed": spec["collapsed"],
                "count": len(group_items),
                "start_text": _majority_start(group_items),
                "items": group_items,
            }
        )
    return groups


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
#: ③ :func:`render` 里 all.js 的 `section_order`（列表页顺序）—— 有单测与冒烟锁定。
HOME_SECTIONS = [
    {"key": "new_low", "label": "新史低"},
    {"key": "expiring", "label": "即将到期"},
    {"key": "popular", "label": "热门游戏"},
    {"key": "big_cut", "label": "大额折扣"},
]


#: 默认阈值的**唯一来源**：10000 / 80 / 7 / 48 原先在 `_in_section` /
#: `criteria_notes` / `filter_specs` 三处各写一遍字面量，改默认值要散着改
#: （review-s9-01 补充审查 #4）。配置里没有时才用这些。
DEFAULT_HOME_DAYS = 7
DEFAULT_BIG_CUT = 80
DEFAULT_NOTABLE = 10000
DEFAULT_UPCOMING_HOURS = 48


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
    写进 all.js 的每张卡（``card["sections"]``），前端按它筛出板块的完整列表。"""
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

    2026-10-07 用户：「精选用公式，refs 文档中有参考公式，根据这个推断修正精选的公式」——
    **新史低 / 热门 / 大额折扣** 的「精选」改为按推荐公式打分
    （:func:`featured_score`，refs §10.2 名气优先），不再是各板块自己的自然顺序
    （原：大额折扣=折扣降序、热门=评价数降序、新史低=分层字典序）。

    ⚠️ **「即将到期」保持「到期近 → 远」**：公式的「紧迫」项只有 5 分，压不过名气（40 分），
    按公式排会把「剩 0 天的小游戏」排到「剩 2 天的大作」后面 —— 这个板块的全部意义
    就是「快没了」，紧迫必须优先。
    """
    if key == "expiring":
        return lambda c: (c.get("days_left") if c.get("days_left") is not None else 99,
                          -(c.get("cut") or 0), c.get("title") or "")
    return lambda c: (-featured_score(c, cfg), -review_count(c), c.get("title") or "")


def build_sections(cards: list[dict], cfg: dict) -> list[dict]:
    """首页四板块。``items`` 只放前 N 条（``home_section_preview``），
    完整条数放 ``count``，前端「查看更多」按需展开（数据在 all.js 里）。"""
    preview = int(cfg.get("home_section_preview", 10))
    out = []
    for spec in HOME_SECTIONS:
        members = _section_members(spec["key"], cards, cfg)
        members.sort(key=_section_sort(spec["key"], cfg))
        out.append({
            "key": spec["key"],
            "label": spec["label"],
            "count": len(members),
            "items": members[:preview],
        })
    return out


#: 顶部大卡（轮播）的推荐权重 —— refs.md **§10.2 的 v2「轮播版」**（名气优先）。
#: 每项先归一到 0~1 再乘权重，满分 100；改档位优先改 ``config.json`` 的
#: ``recommend_weights``（只写要改的那几项即可），不必动代码。
#: 缺数据的项**不计分、按剩余权重归一化**（见 recommend_score 末尾）。
#: ⚠️ **这里没有「史低类型」这一项，是故意的** —— refs §10.2 的轮播版当初就不给，
#: 理由写在表里：「池子全是新史低，无区分度」。2026-10-07 池子放宽成「新史低 +
#: 平史低」之后我一度补过一项 `low`，用户明确要求**改回来**（原话：「别改大卡的公式，
#: 你怎么乱动公式，你测一下效果就行了」）—— 所以**别再往这里加 low**。
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
#: 大卡张数的**默认上限** = 一页（5 张）。用户 2026-10-07 定的：
#: 「平时凑不够 15 张内容，平时 5 张就行，能凑够再放，不要凑数」。
#: 想放更多就改 `config.json` 的 `home_picks`（仍按整数页取：10 / 15；平史低只拿来
#: **补末页的空格**，页数永远只由新史低决定 —— 见 :func:`pick_top` 的注释）。
#: ⚠️ 别拿大促期间的数据来"验证"这条 —— 大促那几天新史低一天上千条（refs §6.2 实测
#: 09-26→10-05 的每日新增：24/16/13/28/29/47/**1943**/4/2/4，1943 就是 10-02 那波大促），
#: 上限当然天天填满；**平时一天只有个位数**，一页 5 张才是常态。
#: 反过来说：大促期间候选多，但也没必要把首页顶成三页要翻的大卡。
HOME_PICKS_DEFAULT = HOME_PICKS_PAGE
#: 前置门槛（§10.2）：有评价数 · 好评率 ≥ min_rate · 评价数 ≥ min_count
RECOMMEND_MIN_RATE = 70
RECOMMEND_MIN_COUNT = 100
#: 名气 / 间隔 的封顶值（达到即满分）
RECOMMEND_FAME_CAP = 200_000
RECOMMEND_GAP_CAP_DAYS = 366


def _clamp01(value: float) -> float:
    return 0.0 if value < 0 else (1.0 if value > 1 else float(value))


def recommend_weights(cfg: dict | None) -> dict:
    """配置里的权重覆盖默认档（缺的项沿用默认，不要求写全）。"""
    weights = dict(RECOMMEND_WEIGHTS)
    for key, value in ((cfg or {}).get("recommend_weights") or {}).items():
        if key in weights and value is not None:
            weights[key] = float(value)
    return weights


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


def _weighted_total(parts: dict, cfg: dict | None = None) -> float:
    """按权重加权并**归一化到 0~100** —— 缺数据的项不计分、按「可用权重之和」折算。"""
    weights = recommend_weights(cfg)
    total = sum(weights[key] for key in parts)
    if total <= 0:
        return 0.0
    return sum(weights[key] * parts[key] for key in parts) / total * 100.0


def recommend_score(card: dict, cfg: dict | None = None) -> float | None:
    """顶部大卡的推荐分（0~100）。**不满足前置门槛返回 ``None``** = 不进推荐位。

    打分项归一（refs.md §10.2 轮播版；**默认不含「间隔」** —— 见 RECOMMEND_WEIGHTS 的注释）：

    - 名气 ``log10(评价数+1) / log10(20 万+1)`` —— 用对数压，否则 156 万评价
      的游戏会把其余项压成噪声（§7.1 实测：彩虹六号 35% 折扣霸榜就是这么来的）
    - 折扣 ``折扣% / 95``
    - 口碑 ``(好评率 − 50) / 40``
    - 间隔 ``log10(距上次史低天数 + 1) / log10(367)`` —— **只有平史低有这个数**，
      轮播池全是新史低 → 默认权重给 0，等于不参与（配置里配了才会算）
    - 紧迫 剩 ≤1 天 1.0 / ≤2 天 0.6 / ≤7 天 0.2 / 更久 0
      （原文按小时给档，卡片只有「剩 X 天」的日历天，按天近似）
    - 新鲜 折扣开始 ≤2 天 1.0 / ≤7 天 0.6 / 更早 0

    ⚠️ **缺数据的项不计分，并把总分按"可用权重之和"归一化回 100** ——
    这样「有数据的项」不会被平白稀释，也不会因为缺一项就系统性吃亏。
    （分项与加权拆在 :func:`_recommend_parts` / :func:`_weighted_total`，
    ``featured_score`` 复用同一套算式，保证两处口径永远一致。）
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
    按剩余权重归一 —— 口径与大卡**永远一致**（改权重两处一起变）。
    """
    return _weighted_total(_recommend_parts(card, cfg), cfg)


def recommend_sort_key(card: dict, cfg: dict | None = None) -> tuple:
    """推荐位排序键：分高在前；平手比 折扣% → 评价数 → 价格低 → appid。

    最后一项（appid）是为了**结果稳定可复现** —— 不加的话每次跑出来顺序会抖。
    """
    score = recommend_score(card, cfg)
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
    **分层取**；打分走 §10.2 权重公式 v2（名气/折扣/口碑/紧迫/新鲜 + 前置门槛，
    **不含「史低类型」** —— 见 RECOMMEND_WEIGHTS 的注释）。

    ⚠️ **分层取**（用户 2026-10-07 定稿：「优先新史低，没有才显示平史低」）：
    1. 先取近 N 天的**新史低**（过门槛 + 打分排序）；
    2. 只有不够**一整页**时，**才**用同一套规则补**平史低**（把当前那页补满）。

    张数 = 「新史低能凑满几页」× ``HOME_PICKS_PAGE``（5），上限 ``home_picks``；
    **默认上限就是一页 5 张** —— 用户：「平时凑不够 15 张内容，平时 5 张就行，
    能凑够再放，不要凑数」。（平时一天的新史低只有个位数，一页常有富余；
    大促期间候选上千条，但也没必要把首页顶成三页要翻的大卡。）

    refs §10.2 原文写的是「**轮播池 = 新史低 ∩ 有详情**」（原文权重表里「史低」那一格
    给轮播版的就是「—（池子全是新史低，无区分度）」）—— 用户确认的口径是它的放宽版：
    平史低**有机会**进，但新史低优先。实测**不分层**的话 15 张里 14 张是平史低
    （90% off 的老 3A 名气分高、全被顶上来），底部色条几乎全灰。

    ⚠️ 门槛（好评率 ≥70% 且评价数 ≥100）把「详情待补」的条目也挡在外面 ——
    推荐位不能推没数据的游戏。每层若全都不过门槛（详情大面积缺失的极端情况），
    该层退回老的字典序，保证大卡这一块不会整块消失。
    """
    days = int(cfg.get("home_new_low_days", DEFAULT_HOME_DAYS))
    # 展示张数 = 「新史低能凑满几页」× 每页张数，上限 home_picks：
    #   上限 5（默认）→ 只放一排 · 上限 15 → 新史低 ≥15 放 15、10~14 放 10、其余放 5
    #   末页不够时**只补满这一页**（否则一排会缺格子、右边空一块），绝不多补。
    # 用户 2026-10-07：「平时凑不够 15 张内容，平时 5 张就行，能凑够再放，不要凑数」。
    want = max(HOME_PICKS_PAGE, int(cfg.get("home_picks", HOME_PICKS_DEFAULT)))
    pool = [c for c in cards if _is_fresh(c, days) and _is_live(c)]

    def ranked(low_class: str) -> list[dict]:
        """该史低类型里按推荐分排好的候选（门槛挡掉的一律不出现）。"""
        same = [c for c in pool if c.get("low_class") == low_class]
        scored = [c for c in same if recommend_score(c, cfg) is not None]
        if not scored:
            same.sort(key=featured_sort_key)
            return same
        scored.sort(key=lambda c: recommend_sort_key(c, cfg))
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
DEFAULT_REPO_URL = "https://github.com/Mumeione/SteamDailyLowest"
DEFAULT_ACTIONS_URL = DEFAULT_REPO_URL + "/actions"


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
    # 折扣区间：**没有 70%**（用户 2026-10-07「折扣区间去掉 70%」）——
    # 50% 是「有点折扣」的直觉线，big_cut（默认 80%）是「大额折扣」板块的同一根线，
    # 90% 是「几乎白送」。70% 夹在 50 与 80 之间、区分度最低，故去掉。
    # ⚠️ 用 set 去重：若哪天把 big_cut_percent 调成 50 或 90，这里不会出现重复选项。
    cuts = sorted({50, big, 90})
    counts = sorted({500, 5000, notable})
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
    # ⚠️ **「排序」不再进这张表**（2026-10-07 同日稍晚）—— 「精选」已改为按推荐公式打分
    #    （`featured_score`，见 `_section_sort`），不再等于各板块的自然顺序，
    #    「折扣降序」在哪个板块都与它不同 ⇒ 排序的四个选项在所有板块都有区分度。
    # ⚠️ 这几条都跟着「板块口径」走 —— 改 `_in_section` 时必须回来看一眼；
    #    `tests/test_report.py` 里有对应的断言。
    sort_opts = list(FILTER_SORTS)
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

    return [
        {"key": "sort", "label": "排序", "options": sort_opts},
        {"key": "date", "label": "日期", "options": [
            {"value": "0", "label": "今天", "disabled": dead("0")},
            {"value": "1", "label": "昨天", "disabled": dead("1")},
            {"value": "2", "label": "前天", "disabled": dead("2")},
            {"value": f"d{days}", "label": f"近 {days} 天", "disabled": dead(f"d{days}")},
            {"value": "all", "label": "全部", "disabled": False},
        ]},
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
           upcoming_items: list[dict] | None = None,
           all_cards: list[dict] | None = None,
           extra_counts: dict | None = None,
           run_log: list[dict] | None = None) -> dict:
    """写出一整套静态文件，返回产出路径。

    ``upcoming_items``：「即将过期」视图的卡片数据。⚠️ 2026-10-08 起**只用于
    views 按钮的 count 下发，不再内联进 payload**（``view_groups`` 键已删除）——
    大促尾期「即将过期」≈ 全池（实测 7202 条内联卡片把 data.js 撑到 7.4MB，首屏几十秒）。
    「即将到期」板块的完整列表改走 all.js 的 ``expiring`` 板块（懒加载），
    expiring.json 快照导出仍由 run.py 的 ``upcoming_shown_items`` 负责。

    ``items`` 为当日新增的原始卡片：计入 low_points 与 new_today 的按钮 count，
    ``all_cards`` 缺席时兜底作首页板块池子（兼容直接调用 render 的测试与工具）。

    ``all_cards``：「全部」视图的数据 —— 今日筛选链通过的全量卡片，每张带
    ``views`` 列表（week/active/new_today/upcoming 成员标志，run.py 计算）；
    传了就写出 ``all.js``（本周 / 折扣中 / 全部三个视图懒加载它），
    并启用这三个视图按钮；``extra_counts`` 给出它们的按钮 count。
    """
    output_dir = Path(cfg["output_dir"])
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "static").mkdir(parents=True, exist_ok=True)

    for name in ("app.css", "app.js"):
        shutil.copyfile(STATIC_DIR / name, output_dir / "static" / name)

    version = str(int(now.timestamp()))
    # 视图按钮的 count：new_today / upcoming 从本集合取；week / active / all
    # 由调用方统计好经 ``extra_counts`` 传入（懒加载视图的数据不在本 payload 里）
    view_counts = {
        "new_today": len(items),
        "upcoming": len(upcoming_items or []),
        **(extra_counts or {}),
    }
    lazy_ready = all_cards is not None
    views = []
    for view in VIEWS:
        item = dict(view)
        enabled = view["enabled"] and (
            view["key"] not in LAZY_VIEWS or lazy_ready
        )
        item["enabled"] = enabled
        item["count"] = view_counts.get(view["key"]) if enabled else None
        item["lazy"] = enabled and view["key"] in LAZY_VIEWS
        views.append(item)

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
        "stale_banner_hours": int(cfg.get("stale_banner_hours", 36)),
        #: S9-3 顶部消息区：>26h 黄（Actions 延迟）/>36h 红（今天没更新）。
        #: 阈值从配置来，前端不再写死第二份（review-s9-01 确立的约定）——
        #: 留在 payload 里是因为「数据新不新鲜」只能等页面打开时才知道，
        #: 服务端算不了（渲染时刻 ≠ 访客打开时刻）。
        "stale_warn_hours": int(cfg.get("stale_warn_hours", 26)),
        "sweep": stats.get("sweep"),
        "overview": stats,
        "low_points": low_points,
        "views": views,
        #: ⚠️ payload 里**没有** ``groups`` / ``view_groups``（2026-10-08 删除）：
        #: S9 首页板块走 sections、大卡走 picks、「即将到期」完整列表走 all.js 的
        #: expiring —— 没有前端消费者。这两个键曾让 data.js 在大促尾期膨胀到
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

    data_js = "window.REPORT_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    (output_dir / "data.js").write_text(data_js, encoding="utf-8")

    paths = {
        "index": str(output_dir / "index.html"),
        "data_js": str(output_dir / "data.js"),
        "latest_json": str(output_dir / "latest.json"),
        "item_count": len(items),
    }

    # 重构 S5：「全部」视图的懒加载数据（今日筛选链全量，含视图成员标志）。
    # 与 data.js 同一次渲染写出，保证两份产物的 generated_at 一致。
    # ⚠️ 产物是 all.js（window.ALL_DATA = {...}）而不是 .json —— 前端用动态
    # <script> 标签加载，不受 CORS 限制，本地 file:// 直开也能用；fetch 会失败
    if all_cards is not None:
        # S9-卡片：板块列表页（首页点「查看更多」进去的那张页）的顺序 ——
        # 首页四板块的顺序由 `_section_sort` 决定，而 all.js 的分组顺序是
        # 「tier 分组 + (-cut, title)」，两者本来不一致（现象：点进去顺序变样）。
        # 这里把每个板块的**完整 appid 顺序**一起下发，前端 featured 模式下按它排 ——
        # 排序口径仍然只有服务端一份，前端不复制规则。
        all_payload = {
            "generated_at": payload["generated_at"],
            "generated_at_text": payload["generated_at_text"],
            "groups": build_groups(all_cards, cfg),
            "section_order": {
                spec["key"]: [card["appid"] for card in sorted(
                    _section_members(spec["key"], all_cards, cfg),
                    key=_section_sort(spec["key"], cfg))]
                for spec in HOME_SECTIONS
            },
        }
        (output_dir / "all.js").write_text(
            "window.ALL_DATA = " + json.dumps(all_payload, ensure_ascii=False) + ";\n",
            encoding="utf-8")
        paths["all_js"] = str(output_dir / "all.js")

    latest = {
        "generated_at": payload["generated_at"],
        "sweep": payload["sweep"],
        "overview": stats,
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
        views=views,
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