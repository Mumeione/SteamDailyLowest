# -*- coding: utf-8 -*-
"""报表渲染（对应 docs/DEVELOPMENT.md §7）。

产出 ``index.html`` + ``data.js`` + ``latest.json`` + ``static/``（§6）。
第一版只有「当日新增」一个视图，其余视图位置预留但不可点（§1.1）。
"""

from __future__ import annotations

import json
import shutil
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
VIEWS = [
    {"key": "new_today", "label": "当日新增", "enabled": True},
    {"key": "week", "label": "本周(14天)", "enabled": False},
    {"key": "active", "label": "折扣中", "enabled": False},
    {"key": "upcoming", "label": "即将过期", "enabled": False},
    {"key": "expired", "label": "已过期", "enabled": False},
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
            "key": classify.TIER_QUALITY,
            "label": "好评达标",
            "criteria": f"好评率 ≥ {pct}% 且 评价数 ≥ {min_count}",
            "collapsed": GROUP_COLLAPSED[classify.TIER_QUALITY],
        },
        {
            "key": classify.TIER_NOTABLE,
            "label": "高热度 · 口碑不一",
            "criteria": f"评价数 ≥ {notable:,}，不看好评率",
            "collapsed": GROUP_COLLAPSED[classify.TIER_NOTABLE],
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


def conditions(cfg: dict, stats: dict, fx: dict | None = None) -> list[str]:
    """页面上要注明的筛选条件（用户明确要求：把条件写出来）。

    纯文本、不带 markdown 标记 —— 这些字符串会直接渲染进 HTML 与 `data.js`。
    """
    pct = int(round(float(cfg.get("min_positive_ratio", 0.7)) * 100))
    min_count = int(cfg.get("min_review_count", 100))
    notable = int(cfg.get("notable_review_count", 10000))
    lines = [
        "只收本体游戏（ITAD type=game）；DLC、合集包、原声带一律不收",
        "只收史低：ITAD 官方 flag 为 N（新史低）/ H（平史低）/ S（仅 Steam 店史低）",
        f"「好评达标」= 好评率 ≥ {pct}% 且 评价数 ≥ {min_count}",
        f"「高热度 · 口碑不一」= 评价数 ≥ {notable:,}，不看好评率（单独分组、默认收起）",
        f"评价数 < {min_count} 的冷门游戏不展示（样本太小，没有参考价值）",
    ]
    absolute = cfg.get("absolute_min_positive_ratio")
    if absolute is not None:
        lines.append(f"绝对下限：好评率低于 {int(round(float(absolute) * 100))}% 一律不展示")
    if stats.get("new_today_raw") is not None:
        lines.append(
            f"当日新增按折扣开始时间（ITAD timestamp）判定："
            f"本轮 {stats.get('new_today_raw')} 条，进列表 {stats.get('new_today_shown')} 条"
        )
    sweep = stats.get("sweep")
    if sweep == "low_only":
        lines.append("本轮抓取口径 low_only：服务端已按「本体游戏 + 史低」过滤")
    elif sweep:
        lines.append("本轮抓取口径 full：无服务端过滤的全量抓取（体检用）")
    backlog = stats.get("detail_backlog")
    if backlog is not None:
        lines.append(
            f"详情（好评率）是增量补齐的：本轮抓了 {stats.get('detail_fetched')} 条，"
            f"目录里还有 {backlog} 条待补（下次运行继续）"
        )
    if fx and fx.get("date"):
        rates = fx.get("rates") or {}
        parts = "、".join(f"1 {fx.get('base', 'CNY')} = {rates[k]:.4g} {k}"
                        for k in sorted(rates) if k in ("UAH", "INR", "USD"))
        lines.append(f"汇率取数日期 {fx['date']}（{parts}）；区域价格仅供比价参考，"
                     f"以 Steam 实际结算为准")
    return lines


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
    """把「分」格式化成展示金额。"""
    if amount_int is None:
        return "—"
    symbol = CURRENCY_SYMBOLS.get(currency or "", "")
    text = f"{amount_int / 100:,.2f}".rstrip("0").rstrip(".")
    return f"{symbol}{text}"


def compare_rows(entry: dict) -> list[dict]:
    """把 `entry["compare"]` 的原始数值格式化成卡片要显示的行（§7.2）。

    原始数据由 `src.enrich.enrich_steam` 填：`final` 是**原币种最小单位**，
    `cny_minor` 是换算成人民币分，`diff_pct` 是相对国区的差价百分比。
    """
    rows = []
    for item in entry.get("compare") or []:
        price_text = format_amount(item.get("final"), item.get("currency"))
        cny = item.get("cny_minor")
        diff = item.get("diff_pct")
        if diff is None:
            diff_text = None
        elif diff < 0:
            diff_text = f"比国区便宜 {abs(diff)}%"
        elif diff > 0:
            diff_text = f"比国区贵 {diff}%"
        else:
            diff_text = "与国区同价"
        rows.append({
            "label": item.get("label") or item.get("cc"),
            "price_text": price_text,
            "cny_text": f"≈ {format_amount(cny, 'CNY')}" if cny is not None else None,
            "diff_text": diff_text,
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
    return {
        "game_id": entry.get("game_id"),
        "title": entry.get("title"),
        # Steam 偶尔会把本地化标题存成带尾随空格（例："时之刃 "），渲染前统一清掉
        "title_zh": (entry.get("title_zh") or "").strip() or None,
        "appid": appid,
        "boxart": entry.get("boxart"),
        "banner": entry.get("banner") or entry.get("boxart"),
        "price_text": format_amount(entry.get("price_int"), currency),
        "regular_text": format_amount(entry.get("regular_int"), currency),
        "cut": entry.get("cut"),
        "flag": entry.get("flag"),
        "flag_label": classify.low_label(entry.get("flag")),
        "store_low_text": format_amount(entry.get("store_low_int"), currency),
        "history_low_text": format_amount(entry.get("history_low_int"), currency),
        "history_low_1y_text": format_amount(entry.get("history_low_1y_int"), currency),
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
        "itad_url": entry.get("itad_url"),
    }



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

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_text": now.strftime("%Y-%m-%d %H:%M"),
        "stale_banner_hours": int(cfg.get("stale_banner_hours", 36)),
        "sweep": stats.get("sweep"),
        "overview": stats,
        "views": views,
        "groups": groups,
        "conditions": conditions(cfg, stats, fx),
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
                "flag": item["flag"],
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
        generated_at_text=payload["generated_at_text"],
        stale_banner_hours=payload["stale_banner_hours"],
        notice=payload["notice"],
        conditions=payload["conditions"],
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")

    return {
        "index": str(output_dir / "index.html"),
        "data_js": str(output_dir / "data.js"),
        "latest_json": str(output_dir / "latest.json"),
        "item_count": len(items),
    }