# -*- coding: utf-8 -*-
"""只重渲染报表的**测试工具**：全本地数据、零网络请求（不打 ITAD 也不打 Steam）。

用途：改了 report.py / templates / app.js|css 之后快速预览临时效果，
产出的只是测试报表，不是最终报表 —— 正式报表仍由 run.py 全流程产出。

数据来源（均为最近一次 run.py 落盘的本地内容）：
- 当日新增候选：state.seen_deal 里「折扣开始时间是今天」的条目
  （timestamp 缺失但今天首次见到的，沿用 §4.2 同一兜底口径）；
- 详情 / 中文名 / 好评率：state.game_meta 缓存，merge_details 照常合并；
- 跨区比价：比价不落缓存、本脚本又不发请求，所以从**上一次产出的
  output/data.js** 里按 appid 回收（正式 run.py 跑过一次后就一直有）；
- 汇率：data/fx_cache.json 的当天缓存，只读不取（没有就留空，页脚不显示）；
- 概览统计：state.run_log 最近一次 mode=daily 的记录，缺的用可推导值补。

用法：`python tools/render_report.py`（可选 `--config 路径`，同 run.py）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import (  # noqa: E402
    build_stats,
    count_backlog,
    log,
    render_pass,
)
from src import classify  # noqa: E402
from src.config import ConfigError, load_config, resolve_path  # noqa: E402
from src.state import State  # noqa: E402


def pick_today_from_state(state: State, tz, today) -> list[dict]:
    """从状态库重建「当日新增」候选（§4.2 主口径 + 首见兜底）。"""
    picked: list[dict] = []
    for entry in state.seen_deal.values():
        if classify.timestamp_is_today(entry, today, tz):
            picked.append(dict(entry))
            continue
        if classify.timestamp_missing(entry, tz):
            first = entry.get("first_seen_at") or ""
            if first[:10] == today.isoformat():
                item = dict(entry)
                item["new_reason"] = "first_seen"
                picked.append(item)
    return picked


def load_fx_cache_only(cfg: dict, today: str) -> dict | None:
    """只读当天的汇率缓存，不发请求。"""
    cache_path = Path(cfg.get("fx_cache_path") or "data/fx_cache.json")
    if not cache_path.is_absolute():
        cache_path = ROOT / cache_path
    if not cache_path.exists():
        return None
    try:
        fx = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return fx if fx.get("date") == today else None


def last_daily_stats(state: State) -> dict:
    """最近一次日常运行的统计记录（概览数字以它为准）。"""
    for entry in reversed(state.run_log):
        if entry.get("mode") == "daily":
            return entry
    return {}


def resolve_output_dir(cfg: dict) -> Path:
    out_dir = Path(cfg["output_dir"])
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    return out_dir


def load_previous_compare(output_dir: Path) -> dict[int, list]:
    """渲染**之前**从上一次产出的 data.js 里捞出 appid → 比价行（渲染会覆盖它）。"""
    data_js = output_dir / "data.js"
    if not data_js.exists():
        return {}
    try:
        text = data_js.read_text(encoding="utf-8")
        old = json.loads(text.split("=", 1)[1].rstrip().rstrip(";"))
    except (OSError, ValueError, IndexError):
        return {}
    old_map: dict[int, list] = {}
    for group in old.get("groups") or []:
        for item in group.get("items") or []:
            appid = item.get("appid")
            if appid and item.get("compare"):
                old_map[appid] = item["compare"]
    return old_map


def graft_compare(output_dir: Path, old_map: dict[int, list], log) -> int:
    """渲染后把回收的比价行按 appid 注回新 payload 并重写 data.js。"""
    if not old_map:
        log("[info] 上一次产物里没有比价数据（正式 run.py 跑过一次后才会有）")
        return 0
    data_js = output_dir / "data.js"
    payload = json.loads(data_js.read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";"))
    count = 0
    for group in payload.get("groups") or []:
        for item in group.get("items") or []:
            rows = old_map.get(item.get("appid"))
            if rows is not None:
                item["compare"] = rows
                count += 1
    data_js.write_text(
        "window.REPORT_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    return count


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="全本地只重渲染测试报表（零网络请求）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log(f"[错误] {exc}")
        return 2

    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    today = now.date()

    state = State(resolve_path(cfg, "state_path"), tz=tz).load()
    candidates = pick_today_from_state(state, tz, today)
    log(f"当日新增候选（从状态库重建）：{len(candidates)} 条")
    if not candidates:
        log("状态库里没有今天的条目 —— 先正常跑一次 run.py，再改报表才有东西可渲染。")
        return 1

    fx = load_fx_cache_only(cfg, today.isoformat())
    if not fx:
        log("[warn] 汇率缓存里没有今天的数据：本轮页脚不显示汇率、比价不换算 CNY")

    last = last_daily_stats(state)
    hist_low_all = list(state.seen_deal.values())
    # 待补数以最近一次日常运行的口径为准（它只数那一轮的史低目录，
    # 而不是状态库里跨天攒下的全量 seen_deal）；没有记录时才用全量兜底
    if last.get("detail_backlog") is not None:
        backlog = int(last["detail_backlog"])
    else:
        backlog = count_backlog(hist_low_all, state, cfg, now)

    # 渲染会覆盖 output/data.js，比价行要先捞出来（全本地回收，不发 Steam 请求）
    output_dir = resolve_output_dir(cfg)
    old_compare = load_previous_compare(output_dir)

    def stats_of(detail_fetched: int, backlog_now: int):
        def build(info: dict) -> dict:
            return build_stats(
                sweep=last.get("sweep") or "low_only",
                deals_fetched=int(last.get("deals_fetched") or 0),
                hist_low_total=len(hist_low_all),
                candidates=len(candidates),
                info=info,
                detail_fetched=int(last.get("detail_fetched") or detail_fetched),
                detail_targets_n=int(last.get("detail_targets") or len(candidates)),
                detail_backlog=backlog_now,
                now=now,
            )
        return build

    info = render_pass(state, candidates, cfg, now, stats_of(0, backlog),
                       announce_merges=False, enrich_hook=None, fx=fx)
    grafted = graft_compare(output_dir, old_compare, log)
    log(f"测试报表已渲染：进列表 {info['shown']} 条（分档 {info['tier']}）；"
        f"比价行回收 {grafted} 条（其余为空，属预期）")
    log(f"  index.html : {info['paths']['index']}")
    log(f"  data.js    : {info['paths']['data_js']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
