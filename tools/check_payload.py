# -*- coding: utf-8 -*-
"""检查生成出来的报表内容对不对（不发任何网络请求，只读 output/）。

用途：改了渲染逻辑之后不想重跑整条流水线，用它快速核对：
分组标题与卡片标签是否一致、中文名有没有用上、跨区比价算得对不对、
判定口径字段有没有彻底从 payload 里清掉（批 F 后口径只在 README）。

⚠️ 2026-10-08：data.js 首屏瘦身、不再写 ``groups`` / ``view_groups``，
卡片改从 ``output/all/`` 的分片读取（「全部折扣」那套 = 全量池）——
所以卡片级覆盖统计（中文名 / 比价 / 分类 / 剩 X 天）是**全部池**口径，不再只数当日新增；
「概览色点」仍与 data.js 的当日新增口径对齐（见下）。

⚠️ 2026-10-08（第二轮）：单文件 ``all.js`` 已换成按板块顺序切的
``all/<key>_<n>.js``（分类页 20 秒），分组结构随之下线 —— 分片里只有卡片。
「分组标题 vs 卡片标签」那条校验因此失去对象（两边同源、恒真），改为看 tier 分布。

用法：`python tools/check_payload.py` → 结果落 data/probe/report_check.txt
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import classify, report  # noqa: E402

OUT = ROOT / "data" / "probe" / "report_check.txt"
SHOW = 8
#: 跨端协议校验用：分片第 0 片带的预聚合表（dim 是四个维度的**唯一出处**）
LATEST = ROOT / "output" / "latest.json"


def main() -> int:
    data_js = ROOT / "output" / "data.js"
    if not data_js.exists():
        print(f"没找到 {data_js} —— 先跑一次 run.py")
        return 1
    payload = json.loads(data_js.read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";"))

    all_dir = ROOT / "output" / "all"
    if not all_dir.exists():
        print(f"没找到 {all_dir} —— 卡片现在只由分片承载"
              f"（data.js 已停写 groups），先跑一次 run.py")
        return 1
    # 「全部折扣」那套分片 = 全量池（tier 分组 + 折扣降序），覆盖统计用它。
    # 分片是 (window.ALL_S = window.ALL_S || {})["<slot>"] = {...}; 形态，非纯 JSON。
    items: list[dict] = []
    shard0: dict = {}
    for path in sorted(all_dir.glob("__all___*.js")):
        shard = json.loads(
            path.read_text(encoding="utf-8").split("] = ", 1)[1].rstrip().rstrip(";"))
        items.extend(shard["items"])
        if path.name == "__all___0.js":
            shard0 = shard
    agg = shard0.get("agg") or {}
    dims = agg.get("dim") or {}
    agg_counts = agg.get("counts") or {}

    # 概览 / 色点只在 latest.json 里（2026-10-09 卡片 08：data.js 不再下发死键）
    latest = {}
    if LATEST.exists():
        try:
            latest = json.loads(LATEST.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            latest = {}
    stats_obj = latest.get("overview") or {}

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("报表内容自检（output/data.js + output/all/ + output/latest.json）")
    lines.append("=" * 72)
    lines.append("overview（latest.json）: " + json.dumps(stats_obj, ensure_ascii=False))
    lines.append("low_points（latest.json）: "
                 + json.dumps(latest.get("low_points"), ensure_ascii=False))
    # data.js 不该再带前端不读的键（卡片 08：死键已移出）
    dead_keys = [k for k in ("overview", "fx", "steam", "sweep", "low_points",
                             "filter_defaults", "generated_at_text") if k in payload]
    lines.append("data.js 无死键："
                 + ("是 ✓" if not dead_keys else f"否 ✗ 残留 {dead_keys}"))

    lines.append("")
    lines.append("--- 全量池（来自 all/__all___*.js 分片）---")
    tier_counts = Counter(i.get("tier") for i in items)
    for tier, n in tier_counts.most_common():
        lines.append(f"  {tier}: {n} 条（{classify.TIER_LABELS.get(tier, tier)}）")
    lines.append(f"  合计卡片 {len(items)} 条")
    ok = True

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
    # 「class 与标签不对应」校验已随 all.js 瘦身（2026-10-08 问题1）失去对象：
    # all.js 卡片不再下发 low_label（前端只用色条表达史低类型，无文字标签可比对）。
    # 若在这里拿 classify.steam_low_label 现派生来比对，两边同源、恒真，等于没校验 ——
    # 所以只保留上面 still 有效取值校验（bad_class）；真要防标签漂移，
    # 看 smoke_s9.js 与 tests/ 里对 classify 常量的断言。
    lines.append(f"  取值非法：{len(bad_class)} 条"
                 + ("" if not bad_class else f" → {sorted(set(bad_class))}"))
    lines.append("  class 与标签不对应：校验已下线（all.js 不再下发 low_label，前端只用色条）")

    lines.append("")
    lines.append("--- low_class 字面量 vs classify 常量（步骤 5.1）---")
    # low_class 的三个取值在 Python（classify 常量）与 JS（app.js 手写的字面量）各存一份，
    # 两边没有机制保证一致 —— Python 改名后前端会**静默失配**：页面不报错，
    # 只是标签与色条全错。这条校验把「静默错」变成「跑一次就红」。
    valid = {classify.STEAM_LOW_NEW, classify.STEAM_LOW_TIE, classify.STEAM_LOW_UNKNOWN}
    js_path = ROOT / "output" / "static" / "app.js"
    js = js_path.read_text(encoding="utf-8") if js_path.exists() else ""
    # 方向一（主）：每个 Python 常量都要以**完整引号字面量**出现在 app.js。
    # 必须用完整字面量而非子串 —— 否则 "new" 会被 "tag-low-new" 这类 CSS 类名假命中。
    not_in_js = [c for c in sorted(valid)
                 if f'"{c}"' not in js and f"'{c}'" not in js]
    # 方向二（辅）：与 low_class 比较、或取其兜底默认值的字面量，必须都在常量集内。
    # 只覆盖 `low_class === "x"` 与 `low_class || "x"` 两种写法；前端若换写法会漏，
    # 属已知局限（届时把新写法补进这里）。
    # ⚠️ `low_?[Cc]lass` 才是「low_class 或 lowClass」—— 写成 `low[Cc]lass` 会漏掉带下划线的
    # 那一种（`low_class || "unknown"` 就是这么被漏掉的，实测过）。
    used = set(re.findall(r'low_?[Cc]lass\s*[!=]==?\s*"([^"]*)"', js))
    used |= set(re.findall(r'low_?[Cc]lass\s*\|\|\s*"([^"]*)"', js))
    strange = sorted(used - valid)
    lines.append(f"  classify 常量: {sorted(valid)}")
    lines.append(f"  app.js 里比较/兜底用的: {sorted(used)}")
    lines.append("  常量都在 app.js 里出现："
                 + ("是 ✓" if not not_in_js else f"否 ✗ 缺 {not_in_js}"))
    lines.append("  没有 Python 不认识的取值："
                 + ("是 ✓" if not strange else f"否 ✗ 多出 {strange}"))

    lines.append("")
    lines.append("--- 跨端协议字面量（卡片 03：把「改名单 → 前端静默错」变成红）---")
    # 这三条协议原先**没有任何锁**：Python 侧改个名字，前端不报错、只是行为悄悄变，
    # 要等人工发现。做法与上面 low_class 那条一样 —— 双向字面量比对 + 数据级对拍。
    view_keys = set(classify.VIEW_KEYS)
    js_views = set(re.findall(r"views\.(?:indexOf|includes)\(\s*[\"']([^\"']*)[\"']", js))
    view_strange = sorted(js_views - view_keys)
    lines.append(f"  classify.VIEW_KEYS: {sorted(view_keys)}")
    lines.append(f"  app.js 里 views 判定用的: {sorted(js_views)}")
    lines.append("  views 判定用到了 Python 不认识的成员："
                 + ("否 ✓" if not view_strange else f"是 ✗ {view_strange}"))
    lines.append("  views 判定确实被抓到（写法没换）："
                 + ("是 ✓" if js_views else "否 ✗（前端改了读法，本条校验已失效）"))
    # 数据级：分片卡片里出现的 views 成员必须都在 VIEW_KEYS 里
    bad_views = sorted({v for i in items for v in (i.get("views") or [])
                        if v not in view_keys})
    lines.append("  卡片 views 取值都在 VIEW_KEYS 内："
                 + ("是 ✓" if not bad_views else f"否 ✗ {bad_views}"))

    # ---- 日期协议（"dN" / "0" / "all"）：服务端拼、前端 parseDateSpec 解 ----
    date_vals = dims.get("date") or []
    shapes = set()
    for v in date_vals:
        shapes.add("all" if v == "all" else ("days" if v.startswith("d") else "exact"))
    handled = {
        "all": bool(re.search(r'value\s*===\s*["\']all["\']', js)),
        "days": bool(re.search(r'charAt\(0\)\s*===\s*["\']d["\']', js)),
        "exact": bool(re.search(r"parseInt\(value,\s*10\)", js)),
    }
    date_missing = sorted(s for s in shapes if not handled.get(s))
    lines.append(f"  agg 的 date 档位: {date_vals}（形状 {sorted(shapes)}）")
    lines.append(f"  app.js parseDateSpec 认得: {sorted(k for k, ok in handled.items() if ok)}")
    lines.append("  Python 产出的每种形状前端都能解析："
                 + ("是 ✓" if not date_missing else f"否 ✗ 缺 {date_missing}"))
    lines.append("  日期档位里一定有「全部」："
                 + ("是 ✓" if "all" in date_vals else "否 ✗"))

    # ---- 预聚合计数表的键序：Python AGG_KEY_ORDER ↔ JS aggCount 的数组顺序 ----
    py_order = list(report.AGG_KEY_ORDER)
    # ⚠️ app.js 里 ``var parts`` 不止一处（行卡的评价行也用同名变量）——
    # 只认那个「由 f.<维度> 拼出来」的数组，否则会抓到无关的那个、静默跳过校验。
    js_arr = next((m.group(1) for m in re.finditer(r"var parts = \[([^\]]*)\]", js)
                   if re.search(r"\bf\.\w+", m.group(1))), None)
    js_order = re.findall(r"f\.(\w+)", js_arr) if js_arr else []
    lines.append(f"  Python AGG_KEY_ORDER: {py_order}")
    lines.append(f"  app.js aggCount 拼键顺序: {js_order or '(没抓到)'}")
    lines.append("  键序两边一致："
                 + ("是 ✓" if js_order == py_order else "否 ✗（改键序 = 改协议）"))
    # 数据级对拍：每个 agg 键按 AGG_KEY_ORDER 拆开，逐段必须落在对应维度的取值集合里
    dim_lists = {k: (dims.get(k) or []) for k in py_order}
    bad_agg_keys = []
    for k in list(agg_counts)[:5000]:
        parts = k.split("|")
        if len(parts) != len(py_order) or any(
                parts[i] not in dim_lists[name] for i, name in enumerate(py_order)):
            bad_agg_keys.append(k)
    lines.append(f"  agg 键都能按该顺序拆对（抽样 {min(len(agg_counts), 5000)} 条）："
                 + ("是 ✓" if not bad_agg_keys else f"否 ✗ {bad_agg_keys[:3]}"))

    lines.append("")
    lines.append("--- 概览色点 vs 当日新增进列表（必须自洽）---")
    points = latest.get("low_points") or {}
    lines.append("  low_points: " + json.dumps(points, ensure_ascii=False))
    points_sum = sum(int(v) for v in points.values())
    # low_points 的池子是「当日新增」，不是 all/ 分片的「全部」池 ——
    # 当日新增进列表条数由 overview.new_today_shown 给出（= 传进 render 的 items 条数）
    new_today_shown = stats_obj.get("new_today_shown")
    lines.append(f"  色点合计 {points_sum} / 当日新增进列表 {new_today_shown}"
                 f" -> {'一致' if points_sum == new_today_shown else '★不一致★'}")

    lines.append("")
    lines.append("--- 「剩 X 天」---")
    noDays = [i for i in items if i.get("days_left") is None]
    lines.append(f"  无 days_left {len(noDays)}/{len(items)}（expiry 缺失才有，属异常）")
    days = sorted({i["days_left"] for i in items if i.get("days_left") is not None})
    lines.append(f"  取值分布: {days}")

    lines.append("")
    lines.append("--- 各 tier 的「折扣开始」多数派（批 E spec E5 → 批 G）---")
    for tier in [t for t, _ in tier_counts.most_common()]:
        bucket = [i for i in items if i.get("tier") == tier]
        counts = Counter(i.get("start_text") for i in bucket if i.get("start_text"))
        # 口径：取频次最高，并列时取较晚的那个（比较的是定宽字符串，字典序即时序）
        majority = max(counts, key=lambda text: (counts[text], text)) if counts else None
        lines.append(f"  {tier}: 组内多数派={majority} · 分布={dict(counts)}")

    lines.append("")
    lines.append("--- 判定 ---")
    lines.append("  tier 取值都认识："
                 + ("是 ✓" if set(tier_counts) <= set(classify.TIER_LABELS) else "否 ✗"))
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
    # ⚠️ `bad_pair` 那条（分组标签 vs 卡片标签）已随分片的分组结构下线一并移除 ——
    #    它此前就已是未定义变量（会 NameError），正好借这次改造清掉。
    ok = (ok and not left and not bad_class
          and points_sum == new_today_shown and not leaks
          and not not_in_js and not strange
          and set(tier_counts) <= set(classify.TIER_LABELS)
          # 跨端协议（卡片 03）
          and bool(js_views) and not view_strange and not bad_views
          and not date_missing and "all" in date_vals
          and js_order == py_order and not bad_agg_keys
          # data.js 不带死键（卡片 08）
          and not dead_keys)
    text = "\n".join(lines)
    # ⚠️ 写之前**必须自己建目录**：本机 data/probe/ 早就有（跑过探针），CI 里却只有
    # `mkdir -p data`（state-restore 只建 data/）—— 少了这句，本机永远绿、CI 一跑就
    # FileNotFoundError（2026-10-09 实际踩到：checks.yml 第一次上 CI 就红在这）。
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"结果已写入 {OUT}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
