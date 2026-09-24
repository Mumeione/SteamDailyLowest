# -*- coding: utf-8 -*-
"""检查生成出来的报表内容对不对（不发任何网络请求，只读 output/）。

用途：改了渲染逻辑之后不想重跑整条流水线，用它快速核对：
分组标题与卡片标签是否一致、中文名有没有用上、跨区比价算得对不对、
判定口径字段有没有彻底从 payload 里清掉（批 F 后口径只在 README）。

用法：`python tools/check_payload.py` → 结果落 data/probe/report_check.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "probe" / "report_check.txt"
SHOW = 8


def main() -> int:
    data_js = ROOT / "output" / "data.js"
    if not data_js.exists():
        print(f"没找到 {data_js} —— 先跑一次 run.py")
        return 1
    payload = json.loads(data_js.read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";"))

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("报表内容自检（output/data.js）")
    lines.append("=" * 72)
    lines.append("overview: " + json.dumps(payload.get("overview"), ensure_ascii=False))
    lines.append("fx      : " + json.dumps(payload.get("fx"), ensure_ascii=False))
    lines.append("steam   : " + json.dumps(payload.get("steam"), ensure_ascii=False))

    groups = payload.get("groups") or []
    items = [i for g in groups for i in g["items"]]
    lines.append("")
    lines.append("--- 分组（标题 + 入组条件）---")
    for g in groups:
        lines.append(f"  {g['label']} | {g['criteria']} | {g['count']} 条 | 默认收起={g['collapsed']}")
    lines.append(f"  合计进列表 {len(items)} 条")

    lines.append("")
    lines.append("--- 分组标签 vs 卡片标签（必须一致）---")
    group_labels = {g["key"]: g["label"] for g in groups}
    card_labels = {i["tier"]: i["tier_label"] for i in items}
    ok = True
    for key, label in group_labels.items():
        same = card_labels.get(key) == label
        ok = ok and same
        lines.append(f"  {key}: 分组「{label}」 vs 卡片「{card_labels.get(key)}」"
                     f" -> {'一致' if same else '★不一致★'}")

    lines.append("")
    lines.append("--- 中文名 ---")
    zh = [i for i in items if i.get("title_zh")]
    lines.append(f"  有中文名 {len(zh)}/{len(items)}"
                 "（Steam 上没有中文标题的会回调英文名，属正常）")
    for i in zh[:SHOW]:
        lines.append(f"    {i['title']}  ->  {i['title_zh']}")

    lines.append("")
    lines.append("--- 跨区比价 ---")
    cmp_items = [i for i in items if i.get("compare")]
    lines.append(f"  有比价 {len(cmp_items)}/{len(items)}")
    for i in cmp_items[:SHOW]:
        rows = "；".join(
            f"{r['label']} {r['price_text']} {r.get('cny_text') or '-'}"
            f" [{('%+d%%' % r['diff_pct']) if r.get('diff_pct') is not None else '-'}]"
            for r in i["compare"]
        )
        lines.append(f"    《{i.get('title_zh') or i['title']}》 国区 {i['price_text']} → {rows}")

    lines.append("")
    lines.append("--- 筛选条件 ---")
    # 批 F（2026-09-24）：页面上的「筛选条件」折叠框已删，判定口径全部搬到
    # README「筛选条件」一节；payload 里不再有 conditions / criteria_digest 字段
    leaks = [f for f in ("conditions", "criteria_digest") if f in payload]
    lines.append("  payload 已无 conditions / criteria_digest："
                 + ("是 ✓" if not leaks else f"否 ✗ 残留 {leaks}"))

    lines.append("")
    lines.append("--- 史低分类（批 E spec E1：Steam 口径的新 / 平）---")
    for c in ("new", "tie", "unknown"):
        lines.append(f"  {c}: {sum(1 for i in items if i.get('low_class') == c)}")
    bad_class = [i.get("low_class") for i in items
                 if i.get("low_class") not in ("new", "tie", "unknown")]
    expect_label = {"new": "新史低", "tie": "平史低", "unknown": "史低待确认"}
    bad_pair = [(i.get("low_class"), i.get("low_label")) for i in items
                if expect_label.get(i.get("low_class")) != i.get("low_label")]
    lines.append(f"  取值非法：{len(bad_class)} 条"
                 + ("" if not bad_class else f" → {sorted(set(bad_class))}"))
    lines.append(f"  class 与标签不对应：{len(bad_pair)} 条"
                 + ("" if not bad_pair else f" → {sorted(set(bad_pair))}"))

    lines.append("")
    lines.append("--- 概览色点 vs 分组明细（必须自洽）---")
    points = payload.get("low_points") or {}
    lines.append("  low_points: " + json.dumps(points, ensure_ascii=False))
    points_sum = sum(int(v) for v in points.values())
    lines.append(f"  色点合计 {points_sum} / 进列表 {len(items)}"
                 f" -> {'一致' if points_sum == len(items) else '★不一致★'}")

    lines.append("")
    lines.append("--- 「剩 X 天」---")
    noDays = [i for i in items if i.get("days_left") is None]
    lines.append(f"  无 days_left {len(noDays)}/{len(items)}（expiry 缺失才有，属异常）")
    days = sorted({i["days_left"] for i in items if i.get("days_left") is not None})
    lines.append(f"  取值分布: {days}")

    lines.append("")
    lines.append("--- 分组「折扣开始」上提（批 E spec E5）---")
    for g in groups:
        uniform = len({i.get("start_text") for i in g["items"]}) == 1
        lines.append(f"  {g['label']}: 全组一致={uniform} · group.start_text="
                     f"{g.get('start_text')}")

    lines.append("")
    lines.append("--- 判定 ---")
    lines.append("  分组与卡片标签一致：" + ("是 ✓" if ok else "否 ✗"))
    # R2 验收：payload 与卡片中无 itad_url
    no_itad = all("itad_url" not in i for i in items)
    ok = ok and no_itad
    lines.append("  payload 无 itad_url：" + ("是 ✓" if no_itad else "否 ✗（R2 未生效）"))
    lines.append("  中文名覆盖率：" + (f"{len(zh)}/{len(items)}" if items else "无条目"))
    lines.append("  跨区比价覆盖率：" + (f"{len(cmp_items)}/{len(items)}" if items else "无条目"))
    # 批 E spec E6：删掉的字段不能还留在 payload 里
    removed = ("flag", "flag_label", "store_low_text",
               "history_low_text", "history_low_1y_text")
    left = [f for f in removed if any(f in i for i in items)]
    lines.append("  已删字段无残留：" + ("是 ✓" if not left else f"否 ✗ {left}"))
    ok = (ok and not left and not bad_class and not bad_pair
          and points_sum == len(items) and not leaks)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"结果已写入 {OUT}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
