# -*- coding: utf-8 -*-
"""离线验证：ITAD 的 flag(N/H/S) 与「Steam 口径」的新/平分类到底差多少。

不新增请求 —— 只用 state.json 里已有的 game_meta.last_low_at（storelow/v2 已跑）
与 seen_deal 的 discount 开始时间/本次史低价，离线交叉制表。

⚠️ flag 从 **state 的 seen_deal** 取，不从 payload 卡片取 ——
批 E spec E6 已删掉卡片的 `flag` / `flag_label`（页面改用它算出的 `low_class`）。
本脚本要的是**原始 ITAD flag** 当对照列，所以必须回到 state 拿。

输出写到 data/steam_flag_probe.txt 再读（`classify.STEAM_LOW_WINDOW_HOURS` 的
「窗口取 1h~72h 结果一致」结论就出自这份输出；改路径时同步 classify.py 与
tests/test_classify.py 里的引用）。
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
state = json.loads((ROOT / "data" / "state.json").read_text(encoding="utf-8"))
raw = (ROOT / "output" / "data.js").read_text(encoding="utf-8")
payload = json.loads(raw[raw.index("=") + 1:].rstrip().rstrip(";"))


def parse(v):
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


out = []


def p(s=""):
    out.append(str(s))


meta = state.get("game_meta") or {}
seen = state.get("seen_deal") or {}

# game_id -> 该 ref 对应的 seen_deal 条目（取最新的一个）
by_gid = {}
for key, entry in seen.items():
    gid = entry.get("game_id")
    if not gid:
        continue
    start = parse(entry.get("start"))
    cur = by_gid.get(gid)
    if cur is None or (start and cur[0] and start > cur[0]):
        by_gid[gid] = (start, entry)
p("state: game_meta %d 条 / seen_deal %d 条" % (len(meta), len(seen)))
p()

items = [i for g in (payload.get("groups") or []) for i in g["items"]]
p("样本（报表当日新增）: %d 条" % len(items))
p()

missing_start = 0
missing_low = 0
rows = []
for it in items:
    gid = it.get("game_id")
    start, entry = by_gid.get(gid, (None, {}))
    low_at = parse((meta.get(gid) or {}).get("last_low_at"))
    if start is None:
        missing_start += 1
    if low_at is None:
        missing_low += 1
    rows.append({
        "gid": gid,
        "title": it.get("title"),
        "flag": entry.get("flag"),   # 原始 ITAD flag：卡片字段已删，只有 state 里有
        "start": start,
        "low_at": low_at,
        "price": it.get("price_int"),
        "store_low": entry.get("store_low_int"),
    })

p("缺 start: %d / 缺 last_low_at: %d" % (missing_start, missing_low))
p()

WINDOWS = [("±1h", 3600), ("±6h", 6 * 3600), ("±24h", 24 * 3600), ("±72h", 72 * 3600)]

for label, secs in WINDOWS:
    tally = Counter()
    for r in rows:
        steam = "unknown"
        if r["start"] and r["low_at"]:
            delta = abs((r["low_at"] - r["start"]).total_seconds())
            steam = "new" if delta <= secs else "tie"
        elif r["low_at"] is None and r["start"] is None:
            steam = "unknown"
        tally[(r["flag"], steam)] += 1
    p("=== 判定窗口 %s ===" % label)
    # 表头：flag 行 × steam 列
    p("  flag\\steam   new  tie  unknown")
    for flag in ("N", "H", "S"):
        p("  %-11s %4d %6d %8d" % (
            flag,
            tally[(flag, "new")], tally[(flag, "tie")], tally[(flag, "unknown")]))
    total_new = sum(v for (f, s), v in tally.items() if s == "new")
    total_tie = sum(v for (f, s), v in tally.items() if s == "tie")
    p("  合计：新史低 %d / 平史低 %d （ITAD 口径 N=%d）" % (
        total_new, total_tie, sum(v for (f, s), v in tally.items() if f == "N")))
    p()

p("=== 逐样本明细（±24h 窗口） ===")
p("%-4s %-6s %-22s %-22s %-9s %s" % ("flag", "判定", "deal.start", "last_low_at", "间隔", "title"))
for r in sorted(rows, key=lambda x: (str(x["flag"]), str(x["start"]))):
    if r["start"] and r["low_at"]:
        delta = abs((r["low_at"] - r["start"]).total_seconds())
        verdict = "new" if delta <= 86400 else "tie"
        span = "%.1fh" % (delta / 3600) if delta < 172800 else "%.0fd" % (delta / 86400)
    else:
        verdict = "unknown"
        span = "-"
    p("%-4s %-6s %-22s %-22s %-9s %s" % (
        r["flag"], verdict,
        r["start"].strftime("%Y-%m-%d %H:%M") if r["start"] else "-",
        r["low_at"].strftime("%Y-%m-%d %H:%M") if r["low_at"] else "-",
        span,
        (r["title"] or "")[:34]))

(ROOT / "data" / "steam_flag_probe.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
