# -*- coding: utf-8 -*-
"""检查生成出来的报表内容对不对（不发任何网络请求，只读 output/）。

用途：改了渲染逻辑之后不想重跑整条流水线，用它快速核对：
分组标题与卡片标签是否一致、中文名有没有用上、跨区比价算得对不对、
「筛选条件」块有没有把阈值写全。

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
            f" [{(r.get('diff_text') or '-')}]"
            for r in i["compare"]
        )
        lines.append(f"    《{i.get('title_zh') or i['title']}》 国区 {i['price_text']} → {rows}")

    lines.append("")
    lines.append("--- 筛选条件块 ---")
    for line in payload.get("conditions") or []:
        lines.append("  - " + line)

    lines.append("")
    lines.append("--- 判定 ---")
    lines.append("  分组与卡片标签一致：" + ("是 ✓" if ok else "否 ✗"))
    lines.append("  中文名覆盖率：" + (f"{len(zh)}/{len(items)}" if items else "无条目"))
    lines.append("  跨区比价覆盖率：" + (f"{len(cmp_items)}/{len(items)}" if items else "无条目"))
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"结果已写入 {OUT}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
