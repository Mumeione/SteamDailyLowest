# -*- coding: utf-8 -*-
"""报表渲染（对应 docs/DEVELOPMENT.md §7）。

产出 ``index.html`` + ``data.js`` + ``latest.json`` + ``static/``（§6）。
第一版只有「当日新增」一个视图，其余视图位置预留但不可点（§1.1）。
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import classify

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

#: 预留的其余视图（第一版不上线，数据先攒库）
#: 批 F2：「已过期」已删除 —— 折扣过期后毫无价值（用户：过期折扣犹如砒霜），
#: 不配占一个分类按钮的位置。相关数据仍照常入库，只是不上页面。
VIEWS = [
    {"key": "new_today", "label": "当日新增", "enabled": True},
    {"key": "week", "label": "本周(14天)", "enabled": False},
    {"key": "active", "label": "折扣中", "enabled": False},
    {"key": "upcoming", "label": "即将过期", "enabled": False},
]


def group_specs(cfg: dict) -> list[dict]:
    """由配置生成分组标题与「入组条件」（§3.5）。

    标题刻意用**中性描述**而不是「优质」—— 70% 好评率是 Steam 的「多半好评」档，
    叫「优质」会让人误判。条件文案直接来自配置，改阈值时页面自动跟着变。
    """
    pct = int(round(float(cfg.get("min_positive_ratio", 0.7)) * 100))
    min_count = int(cfg.get("min_review_count", 100))
    notable = int(cfg.get("notable_review_count", 10000))
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

    **整元才省小数**：12700 分 → `¥127`、1200 分 → `¥12`；非整元保留两位
    （1270 分 → `¥12.70`）。原先统一 `rstrip("0")` 会把 12.70 砍成 12.7，
    不符合金额两位小数的惯例（2026-09-25 检查报告问题 3）。
    """
    if amount_int is None:
        return "—"
    symbol = CURRENCY_SYMBOLS.get(currency or "", "")
    if amount_int % 100 == 0:
        text = f"{amount_int // 100:,}"
    else:
        text = f"{amount_int / 100:,.2f}"
    return f"{symbol}{text}"


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


def build_card(entry: dict, now: datetime, labels: dict | None = None) -> dict:
    """把状态库条目（已合并详情）拼成卡片数据。"""
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
    # 用户要看的是「还剩几个日历天」这种粗粒度信息，精确时刻放在详情里（批 E spec E2）
    days_left = None
    if expiry_dt is not None:
        days_left = max(0, (expiry_dt.astimezone(now.tzinfo).date() - now.date()).days)
    # §3.6 史低天数（「距上次史低」那一行）：new = 这次就是新纪录（没有具体日期）；
    # tie = 上一次 Steam 达到该价的时间（storelow/v2 批量取，存 game_meta.last_low_at）。
    # 主文本只放天数保证单行（日期太长会把整行挤成两排），具体日期由
    # 前端悬停/点按显示（last_low_date）。取不到就不渲染这一行（JS 对空值自动跳过），不猜。
    last_low_date = None
    last_low_text = None
    if low_class == classify.STEAM_LOW_NEW:
        # 批 F5：文本由「本次刷新历史记录」改为「本次新史低」（用户定案：只改文本、标签仍为「距上次史低」）
        last_low_text = "本次新史低"
    elif low_class == classify.STEAM_LOW_TIE:
        low_at = classify.parse_time(entry.get("last_low_at"), now.tzinfo)
        if low_at is not None:
            # 批 E 第二轮：主文本就是「N 天」（标签侧已改为「距上次史低」，
            # 再写「N 天前」语义重复）；具体日期由前端悬停/点按显示
            last_low_text = f"{max(0, (now.date() - low_at.date()).days)} 天"
            last_low_date = low_at.strftime("%Y-%m-%d")
    return {
        "game_id": entry.get("game_id"),
        "title": entry.get("title"),
        # Steam 偶尔会把本地化标题存成带尾随空格（例："时之刃 "），渲染前统一清掉
        "title_zh": (entry.get("title_zh") or "").strip() or None,
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
        "last_low_text": last_low_text,
        # 有日期才渲染悬停/点按交互（new=新纪录没有具体日期）
        "last_low_date": last_low_date,
        # 批 E spec E3：原来这三条删掉了 —— 进报表的前提就是正处史低、storeLow 又已含
        # 本次折扣，所以「Steam 史低」在数学上恒等于现价（实测 50/51）；
        # 「全周期 / 近一年最低」则是 ITAD 全商店口径，与「本报告只看 Steam」冲突。
        "compare": compare_rows(entry),
        "start_text": start_dt.astimezone(now.tzinfo).strftime("%Y-%m-%d %H:%M") if start_dt else None,
        "expiry_text": expiry_dt.astimezone(now.tzinfo).strftime("%Y-%m-%d %H:%M") if expiry_dt else None,
        "reviews": reviews,
        "reviews_text": (
            f"{reviews['score']}% · {reviews['count']:,} 条" if reviews else None
        ),
        "tier": tier,
        "tier_label": label_map.get(tier, tier),
        "steam_url": f"https://store.steampowered.com/app/{appid}/" if appid else None,
        "xiaoheihe_url": f"https://www.xiaoheihe.cn/games/detail/{appid}" if appid else None,
        # R2：itad_url 已删（302 直跳 Steam，信息冗余），卡片与 payload 均不再输出
    }



def build_groups(items: list[dict], cfg: dict) -> list[dict]:
    groups = []
    for spec in group_specs(cfg):
        group_items = [item for item in items if item["tier"] == spec["key"]]
        if not group_items:
            continue
        group_items.sort(key=lambda i: (-(i["cut"] or 0), i["title"] or ""))
        # 批 E spec E5：全组「折扣开始」相同时上提到分组头，卡片里不再每行重复；
        # 2026-09-25 起改为**按多数派**上提（卡片一律不渲染该行）—— 实测 quality 组
        # 121 张里 113 张同是 01:20，另 8 张真的不同；若坚持「唯一才上提」，整组会退回
        # null，前端给每张卡插一行「折扣开始」，详情区从两栏变三栏。
        # 用户口径：开始时刻只看个大概（知道是当天的新折扣），精确与否不重要，结束时刻才重要。
        starts = [item["start_text"] for item in group_items if item.get("start_text")]
        # 并列时取较晚的那个，保证组头不会显示得比实际更早
        # （比较的是 strftime("%Y-%m-%d %H:%M") 定宽字符串，字典序即时序）
        counts = Counter(starts)
        group_start = max(counts, key=lambda text: (counts[text], text)) if counts else None
        groups.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "criteria": spec["criteria"],
                "collapsed": spec["collapsed"],
                "count": len(group_items),
                "start_text": group_start,
                "items": group_items,
            }
        )
    return groups


def render(cfg: dict, items: list[dict], stats: dict, now: datetime,
           *, fx: dict | None = None, steam: dict | None = None) -> dict:
    """写出一整套静态文件，返回产出路径。"""
    output_dir = Path(cfg["output_dir"])
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "static").mkdir(parents=True, exist_ok=True)

    for name in ("app.css", "app.js"):
        shutil.copyfile(STATIC_DIR / name, output_dir / "static" / name)

    version = str(int(now.timestamp()))
    groups = build_groups(items, cfg)
    views = []
    for view in VIEWS:
        item = dict(view)
        item["count"] = len(items) if view["enabled"] else None
        views.append(item)

    # 批 E spec E4：概览的「史低构成」色点 —— 直接数 cards，
    # 保证色点之和 == 进列表条数（不可能出现对不上的情况）
    low_points = {classify.STEAM_LOW_NEW: 0, classify.STEAM_LOW_TIE: 0,
                  classify.STEAM_LOW_UNKNOWN: 0}
    for item in items:
        key = item.get("low_class") or classify.STEAM_LOW_UNKNOWN
        low_points[key] = low_points.get(key, 0) + 1

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_text": now.strftime("%Y-%m-%d %H:%M"),
        "stale_banner_hours": int(cfg.get("stale_banner_hours", 36)),
        "sweep": stats.get("sweep"),
        "overview": stats,
        "low_points": low_points,
        "views": views,
        "groups": groups,
        "fx": fx_display(cfg, fx),
        "steam": steam or {},
        #: 断点必须与 app.css 的 @media 一致，否则「布局按手机、每页按桌面」会错位
        "page_size": {
            "mobile": int(cfg.get("page_size_mobile", 10)),
            "desktop": int(cfg.get("page_size_desktop", 20)),
            "breakpoint": int(cfg.get("mobile_breakpoint_px", 768)),
        },
        "notice": "第一版仅上线「当日新增」视图，其余视图的数据先攒库（见 DEVELOPMENT.md §1.1）",
    }

    data_js = "window.REPORT_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    (output_dir / "data.js").write_text(data_js, encoding="utf-8")

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
            for group in groups
            for item in group["items"]
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
    template = env.get_template("index.html.j2")
    html = template.render(
        payload=payload,
        assets_version=version,
        views=views,
        overview=stats,
        fx=payload["fx"],
        low_points=low_points,
        generated_at_text=payload["generated_at_text"],
        stale_banner_hours=payload["stale_banner_hours"],
        notice=payload["notice"],
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")

    return {
        "index": str(output_dir / "index.html"),
        "data_js": str(output_dir / "data.js"),
        "latest_json": str(output_dir / "latest.json"),
        "item_count": len(items),
    }