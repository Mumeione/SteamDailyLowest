# -*- coding: utf-8 -*-
"""顶部消息区的内容源：季节特卖/活动条 + 站点通知（refs.md §4 A-3 / B2）。

内容在 ``content/announcements.json``（仓库内手工维护，格式见该文件 ``_readme``）；
本模块只做两件事：**读**、**按北京时间的当前时刻筛出「正在进行」的**。

三条设计约定（改之前先看完）：

1. **窗口判定在服务端做**，用渲染时刻 ``now``（北京时间）。静态页里的 ``new Date()``
   是**访客本机时区**，跨时区会把日子算错 —— 前端只负责画，不再判日期。
2. **没活动就整块不显示**：这里筛不出东西就返回 ``None``，模板那一行根本不渲染，
   所以「空着的位置」不需要前端再判一次。
3. **文件缺失 / 坏 JSON / 字段写错一律当作「没有活动」** —— 顶部条是锦上添花，
   绝不能因为它让整份报表渲染失败（同 ``report.py`` 里「坏数据不炸管线」的一贯口径）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import classify

ROOT = Path(__file__).resolve().parent.parent
#: 内容文件（配置键 ``announcements_path`` 可覆盖）
DEFAULT_PATH = ROOT / "content" / "announcements.json"

SEASONS = ("spring", "summer", "autumn", "winter")
SEASON_LABELS = {"spring": "春季", "summer": "夏季", "autumn": "秋季", "winter": "冬季"}

# ⚠️ 站点通知与「数据陈旧告警」共用消息区第二行，**陈旧告警优先**（用户 2026-10-07
# 拍板：数据可不可信比公告更要紧）—— 这个优先级在 app.js 里落地，本模块不管。


def season_of(month: int) -> str:
    """按月份推季节主题（refs.md B2「四季的季节颜色不同」）。

    ``season`` 字段可显式覆盖 —— 季节特卖**跨季**（如冬季特卖 12-17 起、
    结束在次年 1 月）时按开始月份算才符合观感，所以默认看 ``start``。
    """
    if 3 <= month <= 5:
        return "spring"
    if 6 <= month <= 8:
        return "summer"
    if 9 <= month <= 11:
        return "autumn"
    return "winter"


def parse_time(value, tz, *, end: bool = False) -> datetime | None:
    """``"YYYY-MM-DD"`` / ``"YYYY-MM-DD HH:MM"`` → 带时区的 datetime。

    解析本身复用 ``classify.parse_time``（ITAD 那套 ISO 解析，别再写第二份）；
    这里只补一条内容文件特有的语义：**只写日期时** ``end`` 取当天 23:59
    （写日期 = 含整天 —— 否则 ``end`` 写 "2026-10-08" 会变成 08 日 00:00 就结束，
    整天都看不到活动）。
    """
    parsed = classify.parse_time(value, tz)
    if parsed is None:
        return None
    if end and isinstance(value, str) and ":" not in value:
        parsed = parsed.replace(hour=23, minute=59)
    return parsed


def load(path: str | Path | None = None) -> dict:
    """读内容文件；任何异常都退化成空内容（见模块 docstring 第 3 条）。"""
    target = Path(path) if path else DEFAULT_PATH
    if not target.is_absolute():
        target = ROOT / target
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"festivals": [], "notices": []}
    if not isinstance(raw, dict):
        return {"festivals": [], "notices": []}
    out: dict = {}
    for key in ("festivals", "notices"):
        value = raw.get(key)
        out[key] = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    return out


def _normalize(entry: dict, now: datetime) -> dict | None:
    """单条 → 展示用的结构；时间缺失/写反/没名字的一律丢弃（返回 ``None``）。"""
    start = parse_time(entry.get("start"), now.tzinfo)
    end = parse_time(entry.get("end"), now.tzinfo, end=True)
    if start is None or end is None or end < start:
        return None
    text = str(entry.get("name") or entry.get("text") or "").strip()
    if not text:
        return None
    if now.tzinfo is None:
        # ``classify.parse_time`` 在没有时区可用时会补 UTC —— 而 naive 的 now
        # 只出现在测试里，留着 aware 会和它比出 TypeError。统一成 naive 再比。
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    season = entry.get("season") if entry.get("season") in SEASONS else season_of(start.month)
    # ⚠️ 与卡片「剩 X 天」共用同一份口径（classify.days_until：日历天 + 凌晨宽容）
    days_left = classify.days_until(end, now)
    return {
        "text": text,
        # 「季节特卖」优先于普通主题节（同时进行时只显示一条）
        "kind": "season" if entry.get("kind") == "season" else "event",
        "season": season,
        "season_label": SEASON_LABELS[season],
        "url": str(entry.get("url") or "").strip() or None,
        "start": start,
        "end": end,
        # 起止区间与「还有几天」的**文案都在服务端拼好** —— 别一半在这、一半在模板，
        # 那样改一次口径要翻两个文件（review-s9-05 提到的文案分裂）。
        # ⚠️ 原先还有 `note`（例「一年四大特卖之一」）—— 用户 2026-10-07 明确不要，
        # 只要「季节 + 活动名 + 进行中 + 起止 + 还有几天」这一句，故字段一并删掉。
        "range_text": f"{start.strftime('%m-%d %H:%M')} – {end.strftime('%m-%d %H:%M')}",
        "end_text": end.strftime("%m-%d %H:%M"),
        "days_left": days_left,
        "until_text": "今天结束" if days_left <= 0 else f"还有 {days_left} 天",
    }


def _active(entries: list[dict], now: datetime) -> list[dict]:
    out = []
    for entry in entries:
        item = _normalize(entry, now)
        if item is not None and item["start"] <= now <= item["end"]:
            out.append(item)
    return out


def _pick(entries: list[dict], now: datetime, key, *, reverse: bool = False) -> dict | None:
    """正在进行里的第一条：``key`` 决定谁排前面（季节特卖优先 / 最新通知优先）。"""
    items = _active(entries, now)
    if not items:
        return None
    items.sort(key=key, reverse=reverse)
    return items[0]


#: 活动：**季节特卖优先**于主题游戏节，其次最早结束的（同时进行时只显示一条）
_FESTIVAL_KEY = lambda e: (0 if e["kind"] == "season" else 1, e["end"], e["text"])
#: 通知：**最新发布**的先看到（用 reverse 排序，datetime 取不了负）
_NOTICE_KEY = lambda e: (e["start"], e["text"])


def active_festival(now: datetime, path: str | Path | None = None) -> dict | None:
    """当前正在进行的活动（季节特卖优先，其次最早结束的）；没有则 ``None``。"""
    return _pick(load(path)["festivals"], now, _FESTIVAL_KEY)


def active_notice(now: datetime, path: str | Path | None = None) -> dict | None:
    """当前有效的站点通知（最新发布的那条）；没有则 ``None``。"""
    return _pick(load(path)["notices"], now, _NOTICE_KEY, reverse=True)


def current(now: datetime, path: str | Path | None = None) -> dict:
    """顶部消息区要用的两条内容（refs.md §4 A-3：最多两行，没内容不显示）。

    内容文件**只读一次** —— 原先两个函数各调一次 ``load()``，每轮渲染白读一遍。
    """
    data = load(path)
    return {
        "festival": _pick(data["festivals"], now, _FESTIVAL_KEY),
        "notice": _pick(data["notices"], now, _NOTICE_KEY, reverse=True),
    }
