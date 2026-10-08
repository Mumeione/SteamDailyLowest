# -*- coding: utf-8 -*-
"""只重渲染报表的**测试工具**：全本地数据、零网络请求（不打 ITAD 也不打 Steam）。

用途：改了 report.py / templates / app.js|css 之后快速预览临时效果，
产出的只是测试报表，不是最终报表 —— 正式报表仍由 run.py 全流程产出。

数据来源（均为最近一次 run.py 落盘的本地内容）：
- 当日新增候选：state.seen_deal 里「折扣开始时间是今天」的条目
  （timestamp 缺失但今天首次见到的，沿用 §4.2 同一兜底口径）；
- 详情 / 中文名 / 好评率：state.game_meta 缓存，merge_details 照常合并；
- 跨区比价：本脚本不发请求，改从 `cache.json` 的**区域原价**推算
  （现价 = 外区原价 × 国区折扣比例，refs §3.3 实测 15/20 与真查一致、5/20 差 1~2 点）——
  ⚠️ 是**估算**，只为本地看版式；正式产物的比价由 enrich 每轮真查。
  （曾经从旧产物回收：那条路的行是**已格式化**的，没有 final → 价格会渲染成「—」，已删。）
- 汇率：data/fx_cache.json 的当天缓存，只读不取（没有就留空，页脚不显示）；
- 概览统计：state.run_log 最近一次 mode=daily 的记录，缺的用可推导值补。

用法：`python tools/render_report.py`（可选 `--config 路径`，同 run.py）
      `python tools/render_report.py --at 2026-10-06`：把「今天」固定成那一天
      （取当天 20:00），本地 data/ 不是当天时**直接用这个** —— 否则 0 候选、退出码 1。
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
from src import enrich  # noqa: E402
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


def pick_upcoming_from_state(state: State, now: datetime, cfg: dict) -> list[dict]:
    """从状态库重建「即将过期」候选（与 run.py 的 hist_low 口径一致：
    折扣没结束就必然还在当轮史低列表里；本工具不发请求，只能翻 seen_deal）。"""
    picked: list[dict] = []
    for entry in state.seen_deal.values():
        item = dict(entry)
        if classify.in_view("upcoming", item, now, cfg):
            picked.append(item)
    return picked


def load_fx_cache_only(cfg: dict, today: str, log=None) -> dict | None:
    """只读汇率缓存，不发请求。

    优先当天的；本地预览常常没有当天的（`fx_cache.json` 是上一次正式跑留下的），
    这种情况退而用缓存里**最新的那一份**并在日志里写明日期 —— 页脚本来就会显示
    汇率取数日期，所以不会让人误以为是今天的汇率。
    """
    cache_path = Path(cfg.get("fx_cache_path") or "data/fx_cache.json")
    if not cache_path.is_absolute():
        cache_path = ROOT / cache_path
    if not cache_path.exists():
        return None
    try:
        fx = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if fx.get("date") == today:
        return fx
    if log and fx.get("date"):
        log(f"[info] 汇率用的是缓存里最新的一份（{fx.get('date')}），不是今天的 ——"
            f" 本地预览要看得见比价就得有汇率，页脚会照样标出取数日期")
    return fx


def graft_compare_from_cache(state, cfg: dict, entries: list[dict], fx: dict | None,
                             log, appid_of) -> int:
    """给**本地预览**补上跨区比价行，数据来自 `cache.json` 的区域原价。

    ⚠️ 现价是**估算**：`外区现价 = 外区原价 × (国区现价 / 国区原价)`
    （refs §3.3 实测：15/20 与真查完全一致，5/20 差 1~2 个百分点，成因是各区价格
    四舍五入后反算）。2026-10-08 起生产对非真查板块（热门/大额折扣等历史条目）
    也用同一估算（``enrich.estimate_compare``）；真查只保证「当日新增 + 即将到期」
    两板块的真实性。本工具的差异只在于**本地不发任何网络请求** —— 连真查覆盖的
    条目也用缓存估算，让预览完全离线可看。

    ⚠️ 存在理由（2026-10-08 决策，回应 check-report 的「修时要一并考虑」）：
    曾经它在渲染前预注入 compare、**掩盖了** run.py 的回灌缺失（线上 compare 全空、
    本地却看得到）；该回归已由 run.render_pass 的 `compare_by_appid` 回灌修复。
    本函数仍需保留 —— 本地预览不做 Steam 真查（费额度、要 key），没有它本地就
    完全看不到比价行版式。

    ``appid_of``：条目 → appid 的解析函数。**必须传** —— `seen_deal` 里没有 appid，
    它是 `game_meta` 的字段、由 `merge_details` 在渲染时才合并进去（2026-10-07 踩过：
    直接用 `entry["appid"]` 结果一条都补不上）。
    """
    countries = [c for c in (cfg.get("compare_countries") or []) if c]
    added = 0
    for entry in entries:
        if entry.get("compare"):
            continue                      # 已有真查结果的（上一次产物的回收）不动
        # 估算公式收敛到 enrich.estimate_compare（2026-10-08）：生产 run.py 对
        # 非真查板块用同一个函数补估算行，两处口径永远一致，这里只管遍历与计数。
        est = enrich.estimate_compare(state, entry, countries, fx, appid=appid_of(entry))
        if est:
            entry["compare"] = est
            added += 1
    if added:
        log(f"[info] 比价行由 cache.json 的区域原价推算补上：{added} 条"
            f"（现价 = 外区原价 × 国区折扣比例）")
    return added


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








def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="全本地只重渲染测试报表（零网络请求）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    parser.add_argument(
        "--at", default=None, metavar="YYYY-MM-DD",
        help="把「今天」固定成某一天，用那天落盘的数据重建当日新增。"
             "本地 data/ 不是当天时必用（否则 0 候选、退出码 1）——"
             "报表是测试产物，不影响线上。",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log(f"[错误] {exc}")
        return 2

    tz = classify.zone(cfg["timezone"])
    if args.at:
        try:
            # 取当天 20:00（晚于主跑 03:14 CST，确保那天的条目都已入库）
            now = datetime.strptime(args.at, "%Y-%m-%d").replace(hour=20, tzinfo=tz)
        except ValueError:
            log(f"[错误] --at 需要 YYYY-MM-DD 格式，收到：{args.at}")
            return 2
    else:
        now = datetime.now(tz)
    today = now.date()
    log(f"参照时刻：{now.isoformat(timespec='minutes')}")

    state = State(resolve_path(cfg, "state_path"), tz=tz).load()
    candidates = pick_today_from_state(state, tz, today)
    upcoming = pick_upcoming_from_state(state, now, cfg)
    log(f"当日新增候选（从状态库重建）：{len(candidates)} 条；"
        f"即将过期候选：{len(upcoming)} 条")
    if not candidates:
        log("状态库里没有今天的条目 —— 先正常跑一次 run.py，再改报表才有东西可渲染。")
        return 1

    fx = load_fx_cache_only(cfg, today.isoformat(), log)
    if not fx:
        log("[warn] 汇率缓存缺失：本轮页脚不显示汇率、比价行只有原币种价（不换算 CNY）")

    last = last_daily_stats(state)
    hist_low_all = list(state.seen_deal.values())
    # 待补数以最近一次日常运行的口径为准（它只数那一轮的史低目录，
    # 而不是状态库里跨天攒下的全量 seen_deal）；没有记录时才用全量兜底
    if last.get("detail_backlog") is not None:
        backlog = int(last["detail_backlog"])
    else:
        backlog = count_backlog(hist_low_all, state, cfg, now)

    # 比价行：本地不发网络请求，只能从 cache.json 的**区域原价**推算
    # （**渲染前注入**：S9 之后卡片是从 sections/picks/all.js 出来的，渲染后再改 payload
    #   只能影响 groups，那几处现在基本是空的 —— 老实现就是这么失效的）
    output_dir = resolve_output_dir(cfg)
    all_entries = candidates + upcoming + hist_low_all
    # seen_deal 里没有 appid（它是 game_meta 的字段），只能经 state.meta() 解析
    def appid_of(entry: dict):
        appid = entry.get("appid")
        if appid:
            return appid
        meta = state.meta(entry.get("game_id")) or {}
        return meta.get("appid")

    estimated = graft_compare_from_cache(state, cfg, all_entries, fx, log, appid_of)

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
                       announce_merges=False, enrich_hook=None, fx=fx,
                       upcoming=upcoming, all_entries=hist_low_all)
    log(f"测试报表已渲染：进列表 {info['shown']} 条（分档 {info['tier']}）；"
        f"即将过期进列表 {info['upcoming_shown']} 条；"
        f"全部视图数据 {info.get('all_shown', 0)} 条；"
        f"比价行：按 cache.json 的区域原价推算 {estimated} 条"
        f"（本工具不发网络请求；正式产物由 enrich 每轮真查）")
    log(f"  index.html : {info['paths']['index']}")
    log(f"  data.js    : {info['paths']['data_js']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
