# -*- coding: utf-8 -*-
"""CLI 入口（对应 docs/DEVELOPMENT.md §12）。

    python run.py              # 日常：抓列表 → 筛 → 出首版报表 → 取当日新增详情 → 覆盖报表
    python run.py --baseline   # 只把当前全部折扣写入状态，不取详情、不出报表
    python run.py --audit      # 体检：无 filter 全量抓取，统计完整分布，不取详情、不出报表
    python run.py --prefetch   # 预抓：只补详情缓存（独立预算），不出报表（.scratch/prefetch/spec.md）
    python run.py --probe      # 抽查：重拉 3 个游戏的 Steam 实时数据与缓存对照（§12 第 8 步）

两个必须遵守的结构性约定（§3.3）：

1. **报表永远不等详情。** 先在缓存上渲染一版页面（缺的标「详情待补」），
   再去抓详情，抓完覆盖同一份文件。任何时刻页面都在，被打断也留下一版可看的报表。
2. **状态先落盘。** 写完 `seen_deal` 立刻 `save()`，再进详情阶段 ——
   否则详情全部失败时那一轮的攒库会一起丢。

第一版只上线「当日新增」一个视图（§1.1）：其余视图的数据照常写进状态库先攒着。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src import classify, enrich, report, snapshot
from src.config import ConfigError, api_key, load_config, parse_rate_limit, resolve_path
from src.httpclient import Blocked, HttpError
from src.itad import (
    SWEEP_FULL,
    SWEEP_LOW_ONLY,
    SWEEP_MODES,
    ItadClient,
    ItadError,
)
from src.ratelimit import RateLimiter
from src.state import State
from src.steam import SteamClient
from src.steam_browse import SteamBrowseClient

ROOT = Path(__file__).resolve().parent
DETAIL_SAVE_EVERY = 20

# ---- 详情抓取的批次口径（detail_targets 附在目标 dict 的 ``_scope`` 上）----
# 消费方：fetch_details（「当日新增不冷却」+ rejudge 跳折扣活跃闸门，见 L506 附近）、
# run_log 计数。review-s6 P2：别在调用点写裸字符串，口径改动只改这里。
SCOPE_NEW = "new"          # 当日新增：跳过冷却与折扣活跃闸门
SCOPE_STALE = "stale"      # 折扣活跃，但动态数据过了四档 TTL
SCOPE_REJUDGE = "rejudge"  # unlisted 翻案重判：start 变了才进来，跳折扣活跃闸门
SCOPE_TITLE = "title"      # 只补中文名（S9 clean_title_zh），不动评价数据


def log(message: str) -> None:
    print(message, flush=True)


def progress_log(pages: int, total: int) -> None:
    """翻页进度：只在第 1 页与每 10 页打一行（全量口径有 162 页）。"""
    if pages == 1 or pages % 10 == 0:
        log(f"      翻页 {pages}：累计 {total} 条")


class StageClock:
    """阶段墙钟（S8 可观测性）：累计各阶段耗时，汇成一行日志 + step summary。"""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._marks: dict[str, float] = {}
        self._last = self._t0

    def mark(self, name: str) -> None:
        """当前阶段计一段（从上次 mark 到现在的耗时归到 name）。"""
        now = time.monotonic()
        self._marks[name] = self._marks.get(name, 0.0) + now - self._last
        self._last = now

    def total(self) -> float:
        return time.monotonic() - self._t0

    def line(self) -> str:
        parts = [f"{name} {sec:.0f}s" for name, sec in self._marks.items() if sec >= 1]
        parts.append(f"总计 {self.total():.0f}s")
        return " · ".join(parts)


def write_step_summary(title: str, rows: list[tuple[str, object]]) -> None:
    """往 ``$GITHUB_STEP_SUMMARY`` 追加一张关键指标表（非 Actions 环境静默跳过）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n## {title}\n\n| 指标 | 值 |\n|---|---|\n")
            for key, val in rows:
                fh.write(f"| {key} | {val} |\n")
    except OSError as exc:
        log(f"[warn] 运行摘要写入失败：{exc}")


def build_client(cfg: dict) -> ItadClient:
    calls, window = parse_rate_limit(cfg["itad_rate_limit"])
    limiter = RateLimiter(
        name="itad",
        max_calls=calls,
        window_seconds=window,
        min_interval=float(cfg["itad_min_interval"]),
    )
    return ItadClient(
        api_key=api_key(cfg),
        limiter=limiter,
        timeout=float(cfg["request_timeout_seconds"]),
        pause=float(cfg["request_pause_seconds"]),
        log=log,
    )


def build_steam_client(cfg: dict) -> SteamClient:
    """Steam store 全站按**同一个预算**合并计数（§2.2 / §3.3）。"""
    calls, window = parse_rate_limit(cfg["steam_rate_limit"], default=(150, 300))
    limiter = RateLimiter(
        name="steam",
        max_calls=calls,
        window_seconds=window,
        min_interval=float(cfg["steam_min_interval"]),
    )
    return SteamClient(
        limiter=limiter,
        timeout=float(cfg.get("steam_timeout_seconds", 15)),
        pause=float(cfg["request_pause_seconds"]),
        log=log,
        lang=cfg.get("steam_lang", "schinese"),
        batch_size=int(cfg.get("steam_batch_size", 20)),
    )


def discount_active(entry: dict, now: datetime) -> bool:
    """条目是否折扣活跃（spec 决策 16：``start ≤ now ≤ expiry``）。

    边界解析不了的按**活跃**处理 —— 宁多刷不漏刷；非折扣期一律不刷新
    （用户 2026-10-05 裁决：不进列表就没有消费方）。
    """
    expiry = classify.parse_time(entry.get("expiry"), now.tzinfo)
    if expiry is not None and expiry < now:
        return False
    start = classify.parse_time(entry.get("start"), now.tzinfo)
    if start is not None and start > now:
        return False
    return True


def refresh_ttl_days(entry: dict, state: State, cfg: dict, now: datetime) -> int:
    """折扣感知的刷新 TTL（spec 决策 16，四档）：

    新游（release_date ≤ ``new_game_days``）1 天 → 到期窗口（expiry −
    ``upcoming_expiry_hours`` 起）1 天 → 折扣期普通条目 ``discount_refresh_days``
    （3 天）。调用方保证条目折扣活跃（非折扣期根本不进派生）。
    game_id 从 ``entry`` 派生（review-s6 P2 Data Clumps：它总是结伴出现，
    不该单独占一个参数）。
    """
    ng_days = int(cfg.get("new_game_days", 30) or 0)
    meta = state.meta(entry.get("game_id")) or {}
    rd = meta.get("release_date")
    if ng_days > 0 and rd:
        try:
            released = datetime.fromtimestamp(int(rd), tz=now.tzinfo)
            if abs((now - released).total_seconds()) <= ng_days * 86400:
                return int(cfg.get("new_game_refresh_days", 1))
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    expiry = classify.parse_time(entry.get("expiry"), now.tzinfo)
    if expiry is not None:
        window = timedelta(hours=int(cfg.get("upcoming_expiry_hours", 48)))
        if now >= expiry - window:
            return int(cfg.get("expiry_refresh_days", 1))
    return int(cfg.get("discount_refresh_days", 3))


def unlisted_frozen(entry: dict, mark: dict | None) -> bool:
    """unlisted 冻结判定（决策 17）：标记存在且仍是**同一折扣期**（start 一致）。

    :func:`entry_needs_detail` 与 :func:`detail_targets` 的 backlog 循环共用
    这一份 —— review-s6 P2：原来两处各写一遍，改口径容易漏一处。
    """
    return mark is not None and (entry.get("start") or "") == (mark.get("start") or "")


def entry_needs_detail(entry: dict, state: State, cfg: dict, now: datetime,
                       *, is_new: bool = False) -> bool:
    """单条目级「要不要发详情请求」（S6 折扣感知口径，detail_targets 的原子判定）。

    - unlisted 冻结（同一折扣期内，``start`` 与标记一致）→ False；
    - 非当日新增且**非折扣活跃** → False（非折扣期不刷新）；
    - 冷却期内失败过 → False（**当日新增不冷却**，报表核心每轮重试）；
    - 其余按动态数据年龄 vs :func:`refresh_ttl_days` 判定（没抓过 → True）。
    """
    game_id = entry.get("game_id")
    if not game_id:
        return False
    mark = state.unlisted(game_id)
    if unlisted_frozen(entry, mark):
        return False   # 同一折扣期内冻结（决策 17）
    if not is_new:
        if not discount_active(entry, now):
            return False
        if state.detail_recently_failed(
                game_id, now, int(cfg.get("detail_retry_cooldown_days", 3))):
            return False
    dyn = state.dyn(game_id)
    if not dyn or dyn.get("fetched_at") is None:
        return True
    fetched = classify.parse_time(dyn.get("fetched_at"), now.tzinfo)
    if fetched is None:
        return True
    ttl_days = refresh_ttl_days(entry, state, cfg, now)
    return (now - fetched) >= timedelta(days=ttl_days)


def _is_new_today(entry: dict, now: datetime) -> bool:
    """兜底的当日新增判定（给 count_backlog / prefetch 用，无 candidates 入参时）：
    折扣开始日是今天，或首次见到是今天（pick_new_today 双口径的条目级复刻）。"""
    start = classify.parse_time(entry.get("start"), now.tzinfo)
    if start is not None and start.astimezone(now.tzinfo).date() == now.date():
        return True
    first = classify.parse_time(entry.get("first_seen_at"), now.tzinfo)
    return first is not None and first.astimezone(now.tzinfo).date() == now.date()


def detail_targets(candidates: list[dict], state: State,
                   cfg: dict, now: datetime) -> tuple[list[dict], dict]:
    """决定这一轮要给哪些游戏抓详情（S6 修订：**折扣感知派生**，spec §3.2 决策 16/17）。

        目标 = 当日新增
             ∪ {折扣活跃 ∧ 数据年龄 > TTL(e)}          # 四档 TTL，非折扣期不刷新
             ∪ {unlisted ∧ e.start ≠ 标记.start}       # 下次折扣重判翻案

    - **不建队列文件**：目标每轮从状态库现算派生；折扣结束 7 天后
      `seen_deal` 条目被留存清理，动态条目随之删除，集合自动收缩（§5）。
    - **unlisted 冻结**（决策 17）：未入列表条目同一折扣期内不重抓、不建动态条目；
      ``start`` 变了（下次折扣）才重抓一次重判。
    - 近期失败的条目按 ``detail_retry_cooldown_days`` 冷却排除
      （失败标记见 :meth:`State.set_detail_failed`）。
      **当日新增不冷却** —— 报表核心，每轮必须重试。
    - 排序：新史低（``low_kind == "N"``）优先 → 折扣力度降序（分层字典序的
      详情管线版，spec §3.3 决策 8）；同 game_id 多版本折扣取最近出现的为代表。
    - 返回的目标 dict 是 seen_deal 条目的**浅拷贝**并附 ``_scope``
      （取值 = 模块头部的 SCOPE_* 常量），fetch_details 用它区分「当日新增不冷却」。
    """
    cooldown = int(cfg.get("detail_retry_cooldown_days", 3))

    new_targets = [{**e, "_scope": SCOPE_NEW} for e in candidates
                   if e.get("game_id")
                   and entry_needs_detail(e, state, cfg, now, is_new=True)]
    new_ids = {e.get("game_id") for e in candidates}

    latest: dict[str, dict] = {}
    for entry in state.seen_deal.values():
        gid = entry.get("game_id")
        if not gid or gid in new_ids:
            continue
        prev = latest.get(gid)
        if prev is None or (entry.get("last_seen_at") or "") > (prev.get("last_seen_at") or ""):
            latest[gid] = entry

    backlog: list[dict] = []
    rejudge = 0
    for gid, entry in latest.items():
        mark = state.unlisted(gid)
        if mark is not None:
            if unlisted_frozen(entry, mark):
                continue   # 同一折扣期内冻结
            if state.detail_recently_failed(gid, now, cooldown):
                continue
            backlog.append({**entry, "_scope": SCOPE_REJUDGE})   # 下次折扣 → 重判翻案
            rejudge += 1
            continue
        if entry_needs_detail(entry, state, cfg, now):
            backlog.append({**entry, "_scope": SCOPE_STALE})
    backlog.sort(key=lambda e: (0 if e.get("low_kind") == "N" else 1,
                                -(int(e.get("cut") or 0)), e.get("expiry") or ""))
    return new_targets + backlog, {
        "new_today": len(new_targets),
        "backlog": len(backlog) - rejudge,
        "rejudge": rejudge,
        "total": len(new_targets) + len(backlog),
    }


def latest_entries(state: State) -> list[dict]:
    """每个 game_id 取最近出现的一条 seen_deal（多版本折扣去重）。

    供无本轮扫描的场合使用（S8 起 prefetch 不再扫折扣列表，条目池直接来自
    状态库）；daily 仍传本轮 hist_low，与视图口径保持一致。
    """
    latest: dict[str, dict] = {}
    for entry in state.seen_deal.values():
        gid = entry.get("game_id")
        if not gid:
            continue
        prev = latest.get(gid)
        if prev is None or (entry.get("last_seen_at") or "") > (prev.get("last_seen_at") or ""):
            latest[gid] = entry
    return list(latest.values())


def count_backlog(entries: list[dict], state: State, cfg: dict, now: datetime) -> int:
    """目录里还有多少条没详情（给页面显示增量补齐的进度）。

    S6 口径：只数**会真正派生为目标**的条目（折扣活跃 / 当日新增 / 重判），
    非折扣期与 unlisted 冻结条目不再计入 —— 33,891 条 game_meta 存量里
    大部分是非折扣期条目，按旧口径数会虚高且永远清不掉。
    条目池 ``entries`` 由调用方给：daily 传本轮 hist_low；prefetch 传
    :func:`latest_entries`（S8 起 prefetch 无本轮列表）。
    """
    return sum(
        1 for e in entries
        if e.get("game_id") and entry_needs_detail(
            e, state, cfg, now, is_new=_is_new_today(e, now))
    )


def apply_listing(state: State, targets: list[dict], cfg: dict, now: datetime) -> dict:
    """详情抓完后的分档落库（spec 决策 17）：达标转正常，不达标打 unlisted 并丢动态数据。

    对本轮目标里有动态数据的条目重算 tier：

    - 进列表（quality/notable/pending）→ 摘除 unlisted 标记（翻案）；
    - 不进列表（cold/other）→ 打 ``unlisted{at, start}`` 并删除动态条目
      —— 评价数 <100 或差评低热的游戏永不展示，为它们存评价数据纯属浪费；
      下次折扣（``start`` 变化）由 :func:`detail_targets` 重抓重判。

    返回 ``{"unlisted": 标记数, "relisted": 翻案数}``（日志与 run_log 用）。
    """
    marked = relisted = 0
    for entry in targets:
        gid = entry.get("game_id")
        if not gid:
            continue
        dyn = state.dyn(gid)
        if not dyn or dyn.get("fetched_at") is None:
            continue   # 本轮没抓到（预算截断 / 失败），维持原状
        if classify.is_shown(classify.tier_of(dyn.get("reviews"), cfg)):
            if state.clear_unlisted(gid):
                relisted += 1
        else:
            state.set_unlisted(gid, now, entry.get("start"))
            state.drop_dyn(gid)
            marked += 1
    return {"unlisted": marked, "relisted": relisted}


def resolve_sweep(cfg: dict, audit: bool) -> str:
    """体检模式强制全量口径；其余按配置（默认只取史低，27 页）。"""
    if audit:
        return SWEEP_FULL
    sweep = cfg.get("sweep_mode") or SWEEP_LOW_ONLY
    if sweep not in SWEEP_MODES:
        raise ConfigError(f"sweep_mode 只能是 {SWEEP_MODES}，当前 {sweep!r}")
    return sweep


def funnel(entries: list[dict], cfg: dict) -> dict:
    """筛选链（§3.2）：只留本体游戏 → 排除免费 → 判史低。

    ⚠️ 当 `sweep_mode=low_only` 时，`type` 与 `flag` 已经由服务端过滤过了，
    所以 `type_not_game` / `not_historical_low` / `flag_price_mismatch`
    三个计数必然为 0 —— 那不是「今天没有异常」，而是**这两个口径看不到**。
    要看真实分布请跑 `--audit`。
    """
    counts = {
        "type_not_game": 0,
        "mature": 0,
        "free": 0,
        "price_missing": 0,
        "below_min_cut": 0,
        "above_max_price": 0,
        "store_low_missing": 0,
        "not_historical_low": 0,
        "flag_price_mismatch": 0,
    }
    hist_low: list[dict] = []
    only_type = cfg.get("only_type")
    min_cut = cfg.get("min_cut") or 0
    max_price = cfg.get("max_price")

    for entry in entries:
        if only_type and entry.get("type") != only_type:
            counts["type_not_game"] += 1
            continue
        if cfg.get("exclude_mature") and entry.get("mature"):
            counts["mature"] += 1
            continue
        price = entry.get("price_int")
        if price is None:
            counts["price_missing"] += 1
            continue
        if cfg.get("exclude_free") and price <= 0:
            counts["free"] += 1
            continue
        if (entry.get("cut") or 0) < min_cut:
            counts["below_min_cut"] += 1
            continue
        if max_price is not None and price > max_price:
            counts["above_max_price"] += 1
            continue

        kind = classify.low_kind(entry)  # 直接用 deal.flag（§4.1）
        if kind == "unknown":
            counts["store_low_missing"] += 1
            continue
        if kind is None:
            counts["not_historical_low"] += 1
            if classify.flag_price_mismatch(entry):
                counts["flag_price_mismatch"] += 1
            continue
        entry["low_kind"] = kind
        hist_low.append(entry)

    return {"counts": counts, "hist_low": hist_low}


def flag_distribution(entries: list[dict]) -> dict:
    """`flag` 分布（体检用）：能看出 ITAD 的口径有没有变。"""
    dist: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("flag"))
        dist[key] = dist.get(key, 0) + 1
    return dist


def pick_new_today(hist_low: list[dict], tz, today, has_seen) -> tuple[list[dict], dict]:
    """当日新增（§4.2）：主口径 timestamp，仅在缺失时才用「首次见到」兜底。"""
    picked: list[dict] = []
    by_reason = {"timestamp": 0, "first_seen": 0, "not_new": 0}
    for entry in hist_low:
        if classify.timestamp_is_today(entry, today, tz):
            picked.append(entry)
            by_reason["timestamp"] += 1
            continue
        if classify.timestamp_missing(entry, tz) and not has_seen(entry):
            entry["new_reason"] = "first_seen"
            picked.append(entry)
            by_reason["first_seen"] += 1
            continue
        by_reason["not_new"] += 1
    return picked, by_reason


def build_steam_browse_client(cfg: dict) -> SteamBrowseClient:
    """GetItems（api.steampowered.com）独立限流通道（重构 spec §3.1）。

    与 store 域是否共用限流桶判定不了 → 新开一条窗口，最小间隔小得多
    （实测 0.14~0.36s/次）；429/403 自动降速由底座策略保留。
    """
    calls, window = parse_rate_limit(cfg.get("steam_browse_rate_limit") or "150 / 300s",
                                     default=(150, 300))
    limiter = RateLimiter(
        name="steam_browse",
        max_calls=calls,
        window_seconds=window,
        min_interval=float(cfg.get("steam_browse_min_interval", 0.5)),
    )
    return SteamBrowseClient(
        limiter=limiter,
        timeout=float(cfg.get("request_timeout_seconds", 25)),
        pause=float(cfg.get("request_pause_seconds", 0.3)),
        log=log,
    )


def _write_browse_meta(state: State, game_id: str, meta, now: datetime) -> None:
    """把一条 GetItems 的 :class:`~src.steam_browse.GameMeta` 写进 game_meta。

    ⚠️ 厂商字段**只填空白条目、id 置 None（name-only）**：GetItems 的
    ``creator_clan_account_id`` 与 ITAD 厂商 id **不同源**且实测 63% 条目缺失
    （2026-10-04 对照探针），进不得倒排索引；已有 ITAD 口径厂商的存量一律不覆盖
    （``stats`` 传 None 同理 —— GetItems 没有 rank/waitlisted，保留旧值）。
    中文名是同源的 Steam 本地化标题，直接刷新（永久缓存）。
    """
    existing = state.meta(game_id) or {}
    publishers = developers = None
    if not existing.get("publishers") and meta.publishers:
        publishers = [{"id": None, "name": p.get("name")} for p in meta.publishers]
    if not existing.get("developers") and meta.developers:
        developers = [{"id": None, "name": d.get("name")} for d in meta.developers]
    state.set_meta(game_id, meta.appid, meta.reviews, now,
                   publishers=publishers, developers=developers, stats=None)
    state.set_release_date(game_id, meta.release_date)
    if meta.name:
        state.set_title_zh(game_id, meta.name.strip(), now)


def fetch_details(client: ItadClient, state: State, targets: list[dict], cfg: dict,
                  now: datetime, browse: SteamBrowseClient | None = None) -> dict:
    """批量详情管线（重构 S2）：lookup 批映射 → GetItems 批量 → info/v2 降级。

    目标由 :func:`detail_targets` 派生（当日新增 ∪ 欠账，无条数上限），这里只管抓：

    1. 缺 appid 的 uuid → ``lookup/shop/61/id/v1`` 批量映射（免鉴权、不计额度），
       命中的先写 :meth:`State.set_appid`（appid 永久缓存，断点续传）；
    2. 有 appid 的 → ``GetItems`` 批量（250/批）：好评率 + 厂商名 + 中文名一次到手，
       每批落盘一次；**连续 3 批失败即熔断**（GetItems 大概率整体失效，
       别把墙钟耗在逐批重试上）；
    3. 前两步拿不到的 → ``info/v2`` 逐条降级（完整 ITAD 口径），每轮上限
       ``detail_fallback_budget``（默认 1000）—— 降级路径 1 请求/条，必须设界；
       GetItems 正常时该路径平时为空。降级仍失败/为空的才写失败标记。

    返回统计 dict（fetched / fallback_fetched / fallback_skipped），
    ``fetched`` = 本轮新写入详情的条数（两跳 + 降级合计）。
    Blocked（连续 403 滥用封禁）不降级、不上抛被吞 —— 照常中止本轮。
    """
    fallback_budget = max(0, int(cfg.get("detail_fallback_budget", 1000) or 0))
    # 内层再过滤一次：同轮重复目标 / 断点续传后已补齐的跳过。
    # 「当日新增不冷却」由 detail_targets 附在目标上的 _scope 传递。
    # rejudge 同样跳过折扣活跃闸门（review-s6 P0-1）：派生处已按
    # 「start ≠ 标记.start」放行且自查冷却，若这里再走 discount_active，
    # 新折扣 start 在未来（跨时区时间戳）时会被静默剔除，
    # run_log 的 detail_rejudge 计数与实际请求数不符。
    todo = [e for e in targets if e.get("game_id") and entry_needs_detail(
        e, state, cfg, now,
        is_new=e.get("_scope") in (SCOPE_NEW, SCOPE_REJUDGE))]
    stats = {"fetched": 0, "fallback_fetched": 0, "fallback_skipped": 0}
    if not todo:
        return stats

    # ---- 1) uuid → appid 批量映射（免费） ----
    need_lookup = [e["game_id"] for e in todo if not state.has_appid(e["game_id"])]
    if need_lookup:
        try:
            mapped = client.fetch_appid_batch(need_lookup)
        except Blocked:
            raise   # 滥用封禁：中止本轮（main 的 Blocked 处理），绝不降级硬扛
        except ItadError as exc:
            log(f"[warn] uuid→appid 批量映射失败（{len(need_lookup)} 条），"
                f"本批全部转入 info/v2 降级：{exc}")
            mapped = {}
        for gid, appid in mapped.items():
            state.set_appid(gid, appid)
        log(f"      uuid→appid 映射：{len(mapped)}/{len(need_lookup)} 命中"
            f"（lookup {len(need_lookup)} 条）")

    # ---- 2) GetItems 批量（连续失败熔断） ----
    with_appid = [e for e in todo if state.has_appid(e["game_id"])]
    getitems_broken: list[dict] = []
    disabled = browse is None
    consecutive_failures = 0
    batch_size = browse.batch_size if browse else 1
    for start in range(0, len(with_appid), batch_size):
        chunk = with_appid[start:start + batch_size]
        if disabled:
            getitems_broken.extend(chunk)
            continue
        appids = [int(state.meta(e["game_id"])["appid"]) for e in chunk]
        try:
            metas = browse.fetch(appids)
        except Blocked:
            raise   # 滥用封禁：中止本轮（main 的 Blocked 处理），绝不降级硬扛
        except HttpError as exc:
            log(f"[warn] GetItems 批量失败（{len(chunk)} 条），转入降级：{exc}")
            getitems_broken.extend(chunk)
            consecutive_failures += 1
            if consecutive_failures >= 3 and not disabled:
                disabled = True
                log("[warn] GetItems 连续 3 批失败，本轮剩余批次不再重试，全部降级")
            continue
        consecutive_failures = 0
        for entry in chunk:
            appid = int(state.meta(entry["game_id"])["appid"])
            meta = metas.get(appid)
            if meta is None:
                getitems_broken.append(entry)   # 无效 appid / 响应缺条
                continue
            _write_browse_meta(state, entry["game_id"], meta, now)
            stats["fetched"] += 1
        state.save(now)   # 断点续传：每批落盘

    # ---- 3) info/v2 逐条降级（每轮上限 detail_fallback_budget） ----
    lookup_missed = [e for e in todo if not state.has_appid(e["game_id"])]
    fallback_pool: list[dict] = []
    seen_gids: set[str] = set()
    for entry in lookup_missed + getitems_broken:
        gid = entry.get("game_id")
        if gid and gid not in seen_gids:
            seen_gids.add(gid)
            fallback_pool.append(entry)
    if len(fallback_pool) > fallback_budget:
        stats["fallback_skipped"] = len(fallback_pool) - fallback_budget
        log(f"[warn] info/v2 降级候选 {len(fallback_pool)} 条超出本轮预算 "
            f"{fallback_budget}，其余留给下轮（派生欠账会自动重派）")
    for entry in fallback_pool[:fallback_budget]:
        gid = entry["game_id"]
        try:
            info = client.fetch_info(gid)
        except Blocked:
            raise   # 滥用封禁：中止本轮（main 的 Blocked 处理），绝不降级硬扛
        except ItadError as exc:
            log(f"[warn] 详情抓取失败，标记「详情待补」：{entry.get('title')}（{exc}）")
            state.set_detail_failed(gid, now)
            continue
        if not info:
            log(f"[warn] 详情为空，标记「详情待补」：{entry.get('title')}")
            state.set_detail_failed(gid, now)
            continue
        state.set_meta(gid, info.get("appid"), info.get("reviews"), now,
                       publishers=info.get("publishers"),
                       developers=info.get("developers"),
                       stats=info.get("stats"))
        stats["fetched"] += 1
        stats["fallback_fetched"] += 1
        if stats["fallback_fetched"] % DETAIL_SAVE_EVERY == 0:
            state.save(now)   # 断点续传：中途失败下次只补缺的
    state.save(now)
    return stats


def fetch_last_low_times(client: ItadClient, state: State, candidates: list[dict],
                         cfg: dict, now: datetime) -> int:
    """批量补「上次史低时间」（§3.6，``storelow/v2``）：当日新增 + 即将过期。

    ⚠️ 每轮**整批重取**、不做缓存命中跳过 —— 这个字段必须反映
    ITAD 当前记录的店内史低时间：游戏今天以新史低入榜（timestamp≈今天），
    明天转成平史低时，「上次史低」显示的才是正确的记录时间；若永久缓存
    第一次的值，之后再次新低时会拿到过期时间。

    即将过期条目大多**从未当过当日新增**（几天前就开始的折扣），
    meta 里没有 last_low_at，报表的「距上次史低」行会整行缺失 ——
    2026-09-27 起并入每轮重取（ITAD 批量接口 200 个/批，+几批/天，成本低）。
    请求量：200 个/批，日常约 1~10 次/天。失败不阻断（下次运行自动补）。
    """
    if not cfg.get("fetch_last_low_time", True) or not candidates:
        return 0
    ids = sorted({e.get("game_id") for e in candidates if e.get("game_id")})
    if not ids:
        return 0
    # 同一 game_id 可能有多版本折扣（不同 expiry），各折扣期分别落暂存
    expiries_by_gid: dict[str, list[str | None]] = {}
    for e in candidates:
        gid = e.get("game_id")
        if not gid:
            continue
        bucket = expiries_by_gid.setdefault(gid, [])
        if e.get("expiry") not in bucket:
            bucket.append(e.get("expiry"))
    try:
        lows_map = client.fetch_storelow(cfg["country"], ids)
    except ItadError as exc:
        log(f"[warn] 上次史低时间抓取失败，本轮跳过（下次运行重取）：{exc}")
        return 0
    for gid, ts in lows_map.items():
        for expiry in expiries_by_gid.get(gid, []):
            state.set_last_low_at(gid, expiry, ts)
    return len(lows_map)


def merge_details(state: State, entries: list[dict], cfg: dict) -> list[dict]:
    """把详情缓存合并进条目并分档（§3.5 / §7.2）。

    「详情待补」只用于**这一轮没抓到详情**的条目（抓取失败、被中断）；
    抓到但 ITAD 没有 Steam 好评率数据 → 归入「冷门 / 无数据」，不展示。

    中文名也在这里合并 —— 它是 `game_meta` 里的一等缓存字段，
    **不能只在 enrich 阶段才读**：首版渲染不跑 enrich（那时大多还没有 appid），
    漏掉这一步就会让已经有中文名的游戏在首版退化成英文名。
    「上次史低时间」同样在这里合并，但它走的是折扣期暂存
    ``low_time_cache``（§3.6，键 ``game_id|expiry``），不属于 game_meta。
    """
    merged: list[dict] = []
    for entry in entries:
        meta = state.meta(entry.get("game_id"))
        item = dict(entry)
        item["title_zh"] = meta.get("title_zh") if meta else None
        item["last_low_at"] = state.last_low_at(entry.get("game_id"), entry.get("expiry"))
        # 厂商 / stats（快照 v3，2026-09-30）：同样来自 meta 合并视图的一等缓存字段。
        # 旧条目尚无这些键 → 一律给 None / []，消费方（快照）不得因此判「异常」。
        item["publishers"] = (meta or {}).get("publishers") or []
        item["developers"] = (meta or {}).get("developers") or []
        item["stats"] = (meta or {}).get("stats")
        # S6 决策 17：unlisted 条目的动态数据已被丢弃 —— 必须归「冷门 / 无数据」
        # 而不是「详情待补」，否则会以 PENDING 档重新进列表（PENDING 是展示档）。
        if meta and meta.get("unlisted"):
            item["appid"] = meta.get("appid")
            item["reviews"] = None
            item["tier"] = classify.TIER_COLD
        elif not meta or not meta.get("fetched_at"):
            item["appid"] = None
            item["reviews"] = None
            item["tier"] = classify.TIER_PENDING
        else:
            item["appid"] = meta.get("appid")
            item["reviews"] = meta.get("reviews")
            item["tier"] = classify.tier_of(item["reviews"], cfg)
        merged.append(item)
    return merged


def render_pass(state: State, candidates: list[dict], cfg: dict, now: datetime,
                stats_of: Callable[[dict], dict], *, announce_merges: bool,
                enrich_hook: Callable[[list[dict], list[dict], dict], dict] | None = None,
                fx: dict | None = None,
                upcoming: list[dict] | None = None,
                all_entries: list[dict] | None = None) -> dict:
    """合并缓存 → 多版本去重 → 分档 →（补 Steam 展示数据）→ 出报表。**可以调用多次**。

    第一次调用发生在详情还没抓的时候（缺的标「详情待补」，页面立刻可看），
    第二次在详情抓完之后（覆盖同一份文件）。

    ``stats_of`` 接收本轮的统计事实、返回要写进页面的 ``stats`` ——
    这样「概览」里的分档条数与实际渲染出来的分组一定是同一份数据。

    ``enrich_hook`` 只在**最后一版**传（首版里的条目大多还没有 appid，
    补不出东西，白白发 Steam 请求）；接收 ``(当日新增, 即将过期, info)``
    两个集合，跨区比价在两者间合并 appid 去重后现价真查（S7 换模型：
    原价 `appid|cc` 永久缓存 + 真查即校准）。

    ``upcoming``：「即将过期」视图的候选条目（调用方按
    :func:`classify.in_view` 筛好）；``None`` = 本轮不产出该视图。

    ``all_entries``（重构 S5）：今日筛选链通过的**全量**史低（hist_low），
    用于「全部」视图的 all.js 与「本周 / 折扣中」的按钮 count；
    ``None`` = 不产出（三个懒加载视图按钮自动禁用）。
    """
    def build_view(entries: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        merged = merge_details(state, entries, cfg)
        kept, deduped = classify.dedupe_by_appid(merged)
        shown = [entry for entry in kept if classify.is_shown(entry["tier"])]
        return kept, deduped, shown

    kept, deduped, shown = build_view(candidates)
    if announce_merges:
        for record in deduped:
            losers = "、".join(f"《{d['title']}》{d['price_int']}" for d in record["dropped"])
            log(f"      多版本合并：appid={record['appid']} 保留《{record['kept']}》"
                f"（{record['kept_price_int']}），合并掉 {losers}")

    # upcoming 的 None（不产出该视图）与 []（产出但为空）是两种不同语义，不能混
    has_upcoming = upcoming is not None
    # upcoming_shown_items：已合并详情、已按 appid 去重、且已过口碑分档（is_shown）——
    # 即「即将过期」视图里实际进列表的那批，供 data 分支的 expiring.json 导出。
    # ⚠️ 与 info["upcoming_shown"]（数字）并存：后者被 run_log / render_report 使用，
    # 不要把数字字段改成列表，否则 298 条数组会被塞进 state.json（changelog v3）。
    upcoming_shown_items: list[dict] = []
    upcoming_shown: list[dict] = []
    if has_upcoming:
        _, _, upcoming_shown = build_view(upcoming)
        upcoming_shown_items = upcoming_shown

    tier_counts = {tier: 0 for tier in classify.TIER_LABELS}
    for entry in kept:
        tier_counts[entry["tier"]] += 1

    info = {
        "tier": tier_counts,
        "shown": len(shown),
        "deduped": deduped,
        "kept": len(kept),
        "upcoming_shown": len(upcoming_shown),
        "upcoming_shown_items": upcoming_shown_items,
    }
    if enrich_hook:
        info["steam"] = enrich_hook(shown, upcoming_shown, info)
    stats = stats_of(info)
    labels = report.tier_labels(cfg)

    # ---- 重构 S5：「全部」视图数据 = 今日筛选链全量（含当日新增），逐条打视图标志 ----
    # 懒加载视图（板块完整列表）的数据源：all.js 用它。
    # ⚠️ 2026-10-08 起不再统计视图按钮 count（那组 payload 已删）。
    # 比价数据只覆盖 enrich 过的当日新增 + 即将过期（全量补比价 = 上千次 Steam 请求，
    # 不做）—— 其余条目详情区由前端回落单栏。
    all_cards: list[dict] | None = None
    if all_entries is not None:
        _, _, all_shown = build_view(all_entries)
        all_cards = []
        for entry in all_shown:
            card = report.build_card(entry, now, labels, cfg)
            card["views"] = [
                key for key in classify.VIEW_KEYS
                if classify.in_view(key, entry, now, cfg)
            ]
            # S9：首页四板块归属（前端按它筛板块的完整列表；同一游戏可跨板块）
            card["sections"] = report.section_keys(card, cfg)
            all_cards.append(card)
        info["all_shown"] = len(all_shown)

    cards = [report.build_card(entry, now, labels, cfg) for entry in shown]
    info["paths"] = report.render(
        cfg, cards, stats, now, fx=fx, steam=info.get("steam"),
        all_cards=all_cards,
        run_log=state.run_log,   # S9：「关于网站」页的每日更新日志
    )
    return info


def build_stats(*, sweep: str, deals_fetched: int, hist_low_total: int, candidates: int,
                info: dict, detail_fetched: int, detail_targets_n: int,
                detail_backlog: int, now: datetime) -> dict:
    """页面「概览」与 `latest.json` 的口径（§7.1）。

    ⚠️ `new_today_raw` 才是**真实当日新增量**；`new_today_shown` 是过了好评分档
    之后实际进列表的条数。两者可能差很多（实测 105 vs 20），页面必须显示前者。
    """
    steam = info.get("steam") or {}
    return {
        "sweep": sweep,
        "deals_fetched": deals_fetched,
        "hist_low_total": hist_low_total,
        "new_today_raw": candidates,
        "new_today_shown": info["shown"],
        "detail_fetched": detail_fetched,
        "detail_targets": detail_targets_n,
        "detail_backlog": detail_backlog,
        "detail_pending": info["tier"].get(classify.TIER_PENDING, 0),
        "deduped_versions": len(info["deduped"]),
        "title_fetched": steam.get("title_fetched", 0),
        "title_cached": steam.get("title_cached", 0),
        "compare_batches": steam.get("compare_batches", 0),
        "price_mismatch": steam.get("price_mismatch") or [],
        "last_run_at": now.isoformat(timespec="seconds"),
    }


def collect(cfg: dict, client: ItadClient, sweep: str):
    """抓折扣列表并归一化。"""
    items = client.fetch_deals(
        country=cfg["country"],
        limit=200,
        max_deals=cfg.get("max_deals"),
        sweep=sweep,
        progress=progress_log,
    )
    if not items:
        raise ItadError("折扣列表为空 —— 中止，不生成空报表")
    return items, [classify.normalize_item(item) for item in items]


def run_daily(cfg: dict, audit: bool = False) -> int:
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    today = now.date()
    sweep = resolve_sweep(cfg, audit)

    state = State(resolve_path(cfg, "state_path"), tz=tz).load()
    client = build_client(cfg)
    clock = StageClock()

    mode_label = "体检 --audit" if audit else "日常运行"
    total_steps = 3 if audit else 7
    log("=" * 70)
    log(f"SteamDailyLowest {mode_label} {now.isoformat(timespec='seconds')}"
        f"（时区 {cfg['timezone']}，抓取口径 {sweep}）")
    log("=" * 70)

    dropped = state.cleanup_expired(now, int(cfg["expired_retention_days"]))
    log(f"[1/{total_steps}] 留存清理：删除过期超过 {cfg['expired_retention_days']} 天的条目 {dropped} 条")

    if audit:
        items, normalized = collect(cfg, client, sweep)
        deals_fetched = len(items)
        clock.mark("列表抓取")
        log(f"[2/{total_steps}] 抓取折扣列表：{deals_fetched} 条（口径 {sweep}）")
        result = funnel(normalized, cfg)
        counts = result["counts"]
        hist_low = result["hist_low"]
        log(f"[3/{total_steps}] 筛选链：本体游戏 {deals_fetched - counts['type_not_game']} · "
            f"排除免费 {counts['free']} · 史低 {len(hist_low)}")
        log("      " + " · ".join(f"{k}={v}" for k, v in counts.items() if v))
        log(f"      flag 分布：{flag_distribution(normalized)}")
        for entry in hist_low:
            state.record_seen(entry, now)
        state.add_run_log(
            {
                "run_at": now.isoformat(timespec="seconds"),
                "mode": "audit",
                "sweep": sweep,
                "deals_fetched": deals_fetched,
                "itad_requests": client.calls,
                "steam_requests": 0,
                "filtered": counts,
                "flag_distribution": flag_distribution(normalized),
                "hist_low_total": len(hist_low),
                "errors": [],
            },
            keep=int(cfg.get("run_log_keep", 30)),
        )
        state.save(now)
        log(f"      体检完成：未取详情、未产出报表。ITAD 请求 {client.calls} 次，"
            f"状态库已写入 {resolve_path(cfg, 'state_path')}")
        log(f"      ⏱ {clock.line()}")
        return 0

    items, normalized = collect(cfg, client, sweep)
    deals_fetched = len(items)
    clock.mark("列表抓取")
    log(f"[2/{total_steps}] 抓取折扣列表：{deals_fetched} 条（口径 {sweep}）")

    result = funnel(normalized, cfg)
    counts = result["counts"]
    hist_low = result["hist_low"]
    log(f"[3/{total_steps}] 筛选链：本体游戏 {deals_fetched - counts['type_not_game']} · "
        f"排除免费 {counts['free']} · 史低 {len(hist_low)}")
    shown_counts = " · ".join(f"{k}={v}" for k, v in counts.items() if v)
    if sweep == SWEEP_LOW_ONLY:
        shown_counts += "   （low_only 口径下 type/flag 计数必然为 0，要看分布请跑 --audit）"
    log(f"      {shown_counts}")

    candidates, by_reason = pick_new_today(
        hist_low, tz, today,
        lambda e: state.has_seen(e.get("game_id"), e.get("price_int"), e.get("expiry")),
    )
    log(f"[4/{total_steps}] 当日新增候选：{len(candidates)} 条"
        f"（timestamp 命中 {by_reason['timestamp']}，首次见到兜底 {by_reason['first_seen']}）")

    # 「即将过期」视图候选：还在折扣期内、48h 内到期的史低（§4.6）。
    # 折扣没结束就必然还在本轮 deals 列表里，所以直接从 hist_low 筛，不必翻 seen_deal。
    upcoming_entries = [
        e for e in hist_low if classify.in_view("upcoming", e, now, cfg)
    ]
    log(f"      即将过期候选（{cfg.get('upcoming_expiry_hours', 48)}h 内到期）：{len(upcoming_entries)} 条")

    # 状态库按完整结构写：所有史低都攒库（其余视图以后再开，§1.1）
    for entry in hist_low:
        state.record_seen(entry, now)
    state.save(now)  # ← 关键：先落盘，别让详情阶段的失败把这一轮的攒库一起带走
    log(f"      状态库已落盘（{len(hist_low)} 条已见记录）：随后即便详情全失败也不会丢")

    # ---- 详情抓取目标：当日新增 ∪ 折扣活跃欠账 ∪ unlisted 重判（无条数上限，spec §3.2）----
    targets, target_info = detail_targets(candidates, state, cfg, now)
    log(f"      详情目标 {target_info['total']} 个：当日新增 {target_info['new_today']}"
        f" + 折扣期欠账 {target_info['backlog']} + unlisted 重判 {target_info['rejudge']}"
        f"（派生式，不设预算）")

    fx_rates = enrich.load_fx(cfg, today.isoformat(), log)
    steam_client = build_steam_client(cfg)
    browse_client = build_steam_browse_client(cfg)

    def stats_of(detail_fetched: int, backlog: int):
        def build(info: dict) -> dict:
            return build_stats(
                sweep=sweep, deals_fetched=deals_fetched, hist_low_total=len(hist_low),
                candidates=len(candidates), info=info, detail_fetched=detail_fetched,
                detail_targets_n=len(targets), detail_backlog=backlog, now=now,
            )
        return build

    # ---- 首版报表：不等详情（§3.3）----
    first = render_pass(state, candidates, cfg, now, stats_of(0, count_backlog(hist_low, state, cfg, now)),
                        announce_merges=False, fx=fx_rates, upcoming=upcoming_entries,
                        all_entries=hist_low)
    clock.mark("筛选攒库与首版渲染")
    log(f"[5/{total_steps}] 首版报表已生成（缺详情的标「详情待补」，页面立刻可看）："
        f"{first['paths']['index']}；全部视图数据 {first.get('all_shown', 0)} 条")

    # ---- 慢的部分放最后（S8 ⑨：详情/覆盖阶段失败不吞 —— 首版报表已在 output/，
    #      workflow 用 always() 兜底发布；退出码 5 供告警分级，欠账由下一轮派生自愈）----
    try:
        detail_stats = fetch_details(client, state, targets, cfg, now, browse=browse_client)
        detail_fetched = detail_stats["fetched"]
        state.save(now)
        # ---- 分档落库（S6 决策 17）：达标转正常，不达标打 unlisted 并丢动态数据 ----
        listing = apply_listing(state, targets, cfg, now)
        if listing["unlisted"] or listing["relisted"]:
            log(f"      分档落库：新标记未入列表 {listing['unlisted']} 个"
                f" · 翻案转正常 {listing['relisted']} 个")
        state.save(now)
        backlog = count_backlog(hist_low, state, cfg, now)
        log(f"[6/{total_steps}] 取详情：新抓 {detail_fetched} 个（目标 {len(targets)} 个；"
            f"GetItems 批量 {detail_stats['fetched'] - detail_stats['fallback_fetched']}"
            f" + info/v2 降级 {detail_stats['fallback_fetched']}"
            f"{' ，预算截断 ' + str(detail_stats['fallback_skipped']) + ' 条' if detail_stats['fallback_skipped'] else ''}）；"
            f"目录还差 {backlog} 条")
        clock.mark("详情")

        # ---- 上次史低时间（§3.6）：批量接口，当日新增 + 即将过期每轮重取 ----
        low_time_fetched = fetch_last_low_times(client, state, candidates + upcoming_entries, cfg, now)
        if low_time_fetched:
            state.save(now)
        total_low_ids = len({e.get("game_id") for e in candidates + upcoming_entries if e.get("game_id")})
        log(f"      上次史低时间：批量补 {low_time_fetched}/{total_low_ids} 条"
            f"（当日新增 {len(candidates)} + 即将过期 {len(upcoming_entries)}，storelow/v2）")

        def do_enrich(shown: list[dict], upcoming_shown: list[dict], info: dict) -> dict:
            facts = enrich.enrich_steam(steam_client, state, shown, cfg, fx_rates, now, log,
                                        upcoming=upcoming_shown)
            log(f"      中文名：新取 {facts['title_fetched']} 个 · 命中缓存 {facts['title_cached']} 个"
                f"；跨区比价批量 {facts['compare_batches']} 次"
                f"（现价真查 {facts['compare_fetched']} 个 · 重定价校准 {facts['compare_repriced']} 次）"
                + (f"；Steam 请求合计 {steam_client.calls} 次" if steam_client.calls else ""))
            if facts["price_mismatch"]:
                for item in facts["price_mismatch"]:
                    log(f"      [warn] 国区价对不上：{item['title']} "
                        f"ITAD={item['itad_price_int']} Steam={item['steam_price_int']}")
            return facts

        final = render_pass(state, candidates, cfg, now, stats_of(detail_fetched, backlog),
                            announce_merges=True, enrich_hook=do_enrich, fx=fx_rates,
                            upcoming=upcoming_entries, all_entries=hist_low)
        log(f"[7/{total_steps}] 完整版报表已覆盖：进列表 {final['shown']} 条（分档 {final['tier']}）"
            f"；即将过期进列表 {final['upcoming_shown']} 条；全部视图数据 {final.get('all_shown', 0)} 条")

        errors = [e for e in client.events if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")]
        state.add_run_log(
            {
                "run_at": now.isoformat(timespec="seconds"),
                "mode": "daily",
                "sweep": sweep,
                "deals_fetched": deals_fetched,
                "itad_requests": client.calls,
                "steam_requests": steam_client.calls,
                "filtered": counts,
                "new_by_reason": by_reason,
                "new_today_raw": len(candidates),
                "new_today_shown": final["shown"],
                "upcoming_raw": len(upcoming_entries),
                "upcoming_shown": final["upcoming_shown"],
                "detail_pipeline": "derived-batch",
                "detail_targets": len(targets),
                "detail_new_today": target_info["new_today"],
                "detail_derived_backlog": target_info["backlog"],
                "detail_rejudge": target_info["rejudge"],
                "detail_unlisted_marked": listing["unlisted"],
                "detail_relisted": listing["relisted"],
                "detail_fetched": detail_fetched,
                "detail_fallback_fetched": detail_stats["fallback_fetched"],
                "detail_fallback_skipped": detail_stats["fallback_skipped"],
                "detail_backlog": backlog,
                "last_low_fetched": low_time_fetched,
                "detail_pending": final["tier"].get(classify.TIER_PENDING, 0),
                "title_zh_fetched": (final.get("steam") or {}).get("title_fetched", 0),
                "title_zh_cached": (final.get("steam") or {}).get("title_cached", 0),
                "compare_repriced": (final.get("steam") or {}).get("compare_repriced", 0),
                "compare_fetched": (final.get("steam") or {}).get("compare_fetched", 0),
                "price_mismatch": (final.get("steam") or {}).get("price_mismatch") or [],
                "deduped_versions": len(final["deduped"]),
                "tier": final["tier"],
                "fx": {"date": (fx_rates or {}).get("date"), "base": (fx_rates or {}).get("base")},
                "limiter": client.limiter.stats(),
                "steam_limiter": steam_client.limiter.stats(),
                "steam_browse_limiter": browse_client.limiter.stats(),
                "errors": errors + [e for e in steam_client.events
                                    if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")]
                + [e for e in browse_client.events
                   if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")],
            },
            keep=int(cfg.get("run_log_keep", 30)),
        )
        state.save(now)
        log(f"      ITAD 请求 {client.calls} 次（限流事件 {client.rate_limit_events}）· "
            f"Steam 请求 {steam_client.calls} 次（限流事件 {steam_client.rate_limit_events}）· "
            f"状态库已写入 {resolve_path(cfg, 'state_path')}")
        clock.mark("完整渲染")

        # ---- 即将过期快照：给外部数据管道消费，落 data 分支 ----
        # 放在 state.save 之后：快照是可重建的衍生品，其 IO 失败不能连累本轮攒库落盘（§12.1）
        snapshot_path = resolve_path(cfg, "expiring_snapshot_path")
        snapshot_count = snapshot.write_snapshot(
            snapshot_path, final["upcoming_shown_items"], now, cfg, fx=fx_rates,
        )
        log(f"      即将过期快照已写出：{snapshot_count} 条 → {snapshot_path}")
        clock.mark("快照")
    except Blocked:
        raise   # 详情阶段撞滥用封禁按严重级处理：交 main 记退出码 3，不按「详情失败」降级（S8 ⑨）
    except Exception:
        # ⚠️ 语义口径（review-s8 P1-1 裁决）：详情/覆盖阶段的**任何**失败——包括
        # HttpError——都记 5。4 与 5 的本质区分是「首版报表是否已产出兜底」：
        # 4 = 报表产出前的请求失败（页面没更新）；5 = 页面已兜底、欠账下轮自愈。
        # 若把详情期 HttpError 记 4，退出码 4 就同时涵盖两种相反的页面状态，判别力更差。
        log("[错误] 详情/覆盖阶段失败 —— 首版报表仍在 output/ 兜底，Pages 照常发布；"
            "欠账由下一轮派生自愈（退出码 5）：")
        log(traceback.format_exc())
        write_step_summary("日常运行（详情/覆盖阶段失败）", [
            ("结果", "详情/覆盖阶段失败；首版报表兜底发布，欠账下轮自愈"),
            ("本轮攒库", f"{len(hist_low)} 条史低已落盘"),
        ])
        return 5

    log(f"      ⏱ {clock.line()}")
    write_step_summary("日常运行摘要", [
        ("抓取口径", sweep),
        ("折扣列表", f"{deals_fetched} 条"),
        ("当日新增（进列表）", final["shown"]),
        ("即将过期（进列表）", final["upcoming_shown"]),
        ("详情目标 / 新抓", f"{len(targets)} / {detail_fetched}"),
        ("目录欠账", backlog),
        ("ITAD 请求", client.calls),
        ("Steam 请求", steam_client.calls + browse_client.calls),
        ("阶段耗时", clock.line()),
    ])
    return 0


def run_baseline(cfg: dict) -> int:
    """只写状态：不发详情请求、不产出报表（§4.2）。"""
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    sweep = resolve_sweep(cfg, audit=False)
    state = State(resolve_path(cfg, "state_path"), tz=tz).load()
    client = build_client(cfg)

    log("=" * 70)
    log(f"SteamDailyLowest --baseline {now.isoformat(timespec='seconds')}"
        f"（时区 {cfg['timezone']}，抓取口径 {sweep}）")
    log("=" * 70)

    dropped = state.cleanup_expired(now, int(cfg["expired_retention_days"]))
    items, normalized = collect(cfg, client, sweep)
    result = funnel(normalized, cfg)
    hist_low = result["hist_low"]
    new_count = 0
    for entry in hist_low:
        _, first_time = state.record_seen(entry, now)
        if first_time:
            new_count += 1

    log(f"抓取 {len(items)} 条，史低 {len(hist_low)} 条，新写入状态 {new_count} 条，留存清理 {dropped} 条")
    log(f"未取详情、未产出报表。ITAD 请求 {client.calls} 次。")

    state.add_run_log(
        {
            "run_at": now.isoformat(timespec="seconds"),
            "mode": "baseline",
            "sweep": sweep,
            "deals_fetched": len(items),
            "itad_requests": client.calls,
            "steam_requests": 0,
            "filtered": result["counts"],
            "hist_low_total": len(hist_low),
            "seen_written": new_count,
            "errors": [],
        },
        keep=int(cfg.get("run_log_keep", 30)),
    )
    state.save(now)
    log(f"状态库已写入：{resolve_path(cfg, 'state_path')}")
    return 0


def _start_key(entry: dict) -> datetime:
    """预抓排序键：折扣开始时间的**绝对时刻**。

    ITAD 的 ``deal.timestamp`` 带各自时区偏移（实测 +02:00 / +08:00 混杂），
    直接对原始字符串做字典序会跨偏移错序，必须解析成 aware datetime 再比；
    解析不了的排最后。
    """
    parsed = classify.parse_time(entry.get("start"))
    if parsed is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed


def prefetch_targets(state: State, cfg: dict, now: datetime,
                     pool: list[dict] | None = None) -> tuple[list[dict], dict]:
    """批 B 预抓目标（S8 去 list 化）：**纯状态库派生**，不再扫描折扣列表。

    与日常 :func:`detail_targets` 同一公式（当日新增 ∪ 折扣活跃欠账 ∪ unlisted
    重判），差别在三点：

    1. 当日新增的来源从「本轮扫描的 candidates」换成「seen_deal 里今天
       首见/开折的条目」—— prefetch 跑在主跑之后，当天条目已由主跑攒库；
    2. 顺序 = 最近出现在史低的优先（``start`` 绝对时刻降序，prefetch spec R1）；
    3. 独立预算 ``prefetch_daily_budget`` 截断（``0`` = 关闭预抓）。

    预抓特有的一类目标是**只缺中文名**的条目（详情达标但 GetItems 没给名，
    两条独立缓存），走 Steam 逐游戏补 —— detail_targets 不含这类。
    03:14 主跑之后新上架的折扣不在 state 里，由次日主跑覆盖（CONTEXT.md「预抓」）。
    ``pool`` 可传 :func:`latest_entries` 结果复用（run_prefetch 里 count_backlog
    也要用同一份，省一次全量遍历，review-s8 P2）。
    """
    budget = int(cfg.get("prefetch_daily_budget", 300) or 0)

    latest = pool if pool is not None else latest_entries(state)
    candidates = [e for e in latest if _is_new_today(e, now)]
    targets, info = detail_targets(candidates, state, cfg, now)

    need = list(targets)
    have = {e.get("game_id") for e in need}
    for entry in latest:
        gid = entry.get("game_id")
        if not gid or gid in have or state.unlisted(gid):
            continue
        if state.has_appid(gid) and not state.title_zh(gid):
            need.append({**entry, "_scope": SCOPE_TITLE})
    need.sort(key=_start_key, reverse=True)
    chosen = need[:max(0, budget)]
    return chosen, {**info, "budget": budget, "chosen": len(chosen)}


def run_prefetch(cfg: dict, *, state: State | None = None,
                 client: ItadClient | None = None,
                 steam: SteamClient | None = None,
                 browse: SteamBrowseClient | None = None,
                 log: Callable[[str], None] = log) -> int:
    """批 B 预抓（.scratch/prefetch/spec.md R1）：只补缓存，**不出报表**。

    与日常同一套**欠账公式**（S8 去 list 化：不再 collect + funnel 扫折扣列表，
    目标纯 state 派生，见 :func:`prefetch_targets`）；按「最近出现在史低的优先」
    用独立预算 `prefetch_daily_budget` 补齐：

    1. 缺 appid / 好评率 → 复用日常的批量详情管线 :func:`fetch_details`
       （lookup → GetItems → info/v2 降级，缓存命中不发请求，断点续传）；
    2. 缺中文名 → GetItems 已在批量详情里顺带写入；仍缺的（GetItems 无名等）
       走 Steam 逐游戏补（必须走 :mod:`src.steam`，全站合并限流 + 最小间隔 2 秒）。

    产物只有 state.json + run_log，不写 `output/` 任何文件 ——
    次日常规运行（或 Pages 构建）自然读到补全的数据。幂等：连续跑第二次应几乎零请求。
    """
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    state = state or State(resolve_path(cfg, "state_path"), tz=tz).load()
    client = client or build_client(cfg)
    steam = steam or build_steam_client(cfg)
    browse = browse or build_steam_browse_client(cfg)
    clock = StageClock()

    log("=" * 70)
    log(f"SteamDailyLowest 预抓 --prefetch {now.isoformat(timespec='seconds')}"
        f"（时区 {cfg['timezone']}，纯 state 派生，不扫描折扣列表）")
    log("=" * 70)

    dropped = state.cleanup_expired(now, int(cfg["expired_retention_days"]))
    state.save(now)  # 先落盘：详情阶段的失败不带走清理结果（§3.3 同款思路）
    log(f"留存清理：删除过期超过 {cfg['expired_retention_days']} 天的条目 {dropped} 条")

    pool = latest_entries(state)   # seen_deal 在本函数内不再变化，取一次复用（review-s8 P2）
    targets, target_info = prefetch_targets(state, cfg, now, pool=pool)
    log(f"预抓目标 {len(targets)} 个：当日新增 {target_info['new_today']}"
        f" + 折扣期欠账 {target_info['backlog']} + unlisted 重判 {target_info['rejudge']}"
        f"（预算 {target_info['budget']}，顺序=最近出现在史低优先）")

    # 1) 批量详情管线：lookup → GetItems → info/v2 降级（缓存命中不发请求，断点续传）
    detail_stats = fetch_details(client, state, targets, cfg, now, browse=browse)
    fetched = detail_stats["fetched"]
    state.save(now)
    # 1.5) 分档落库（S6 决策 17）：预抓抓到的详情同样要过 unlisted 收敛
    listing = apply_listing(state, targets, cfg, now)
    state.save(now)

    # 2) Steam 中文名：正常已被 GetItems 批量写入（_write_browse_meta）；
    #    仍缺的（GetItems 失效走了 info/v2 降级等）逐游戏单请求补，永久缓存
    title_fetched = 0
    title_errors = 0
    done = 0
    for entry in targets:
        game_id = entry.get("game_id")
        if state.unlisted(game_id):
            continue   # 本轮刚被收敛为未入列表：不进列表的游戏补中文名没有消费方
        if state.has_appid(game_id) and not state.title_zh(game_id):
            appid = int(state.meta(game_id).get("appid"))
            try:
                info = steam.info(appid, cc=cfg.get("country", "CN"))
            except HttpError as exc:
                log(f"[warn] 中文名取失败，下次运行再补：{entry.get('title')}（{exc}）")
                title_errors += 1
            else:
                if info and info.get("name"):
                    state.set_title_zh(game_id, info["name"].strip(), now)
                    title_fetched += 1
        done += 1
        if done % DETAIL_SAVE_EVERY == 0:
            state.save(now)  # 断点续传：中途失败下次只补缺的
    state.save(now)

    backlog = count_backlog(pool, state, cfg, now)
    log(f"预抓完成：详情新抓 {fetched} 个（info/v2 降级 {detail_stats['fallback_fetched']}）"
        f"· 中文名新取 {title_fetched} 个"
        f"（失败 {title_errors} 个，Steam 请求 {steam.calls} 次）；"
        f"目录还差 {backlog} 条。未产出报表。")

    errors = [e for e in client.events if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")]
    errors += [e for e in steam.events if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")]
    errors += [e for e in browse.events if e.get("kind") in ("429", "403", "soft_null", "5xx", "network")]
    state.add_run_log(
        {
            "run_at": now.isoformat(timespec="seconds"),
            "mode": "prefetch",
            "derived_from_state": True,   # S8 起 targets 纯 state 派生，无本轮扫描
            "itad_requests": client.calls,
            "steam_requests": steam.calls,
            "steam_browse_requests": browse.calls,
            "prefetch_budget": target_info["budget"],
            "prefetch_targets": len(targets),
            "detail_new_today": target_info["new_today"],
            "detail_derived_backlog": target_info["backlog"],
            "detail_rejudge": target_info["rejudge"],
            "detail_fetched": fetched,
            "detail_fallback_fetched": detail_stats["fallback_fetched"],
            "detail_unlisted_marked": listing["unlisted"],
            "detail_relisted": listing["relisted"],
            "title_zh_fetched": title_fetched,
            "detail_backlog": backlog,
            "limiter": client.limiter.stats(),
            "steam_limiter": steam.limiter.stats(),
            "steam_browse_limiter": browse.limiter.stats(),
            "errors": errors,
        },
        keep=int(cfg.get("run_log_keep", 30)),
    )
    state.save(now)
    log(f"状态库已写入：{resolve_path(cfg, 'state_path')}")
    log(f"      ⏱ {clock.line()}")
    write_step_summary("预抓运行摘要（纯 state 派生，不出报表）", [
        ("预抓目标 / 新抓", f"{len(targets)} / {fetched}"),
        ("unlisted 标记 / 翻案", f"{listing['unlisted']} / {listing['relisted']}"),
        ("中文名新取", title_fetched),
        ("目录欠账", backlog),
        ("ITAD 请求", client.calls),
        ("Steam 请求", steam.calls + browse.calls),
        ("阶段耗时", clock.line()),
    ])
    return 0


def build_probe_steam_client(cfg: dict) -> SteamClient:
    """探针专用 Steam 客户端：超时用 `probe_timeout_seconds`（§9 的独立短超时）。"""
    calls, window = parse_rate_limit(cfg["steam_rate_limit"], default=(150, 300))
    limiter = RateLimiter(
        name="steam",
        max_calls=calls,
        window_seconds=window,
        min_interval=float(cfg["steam_min_interval"]),
    )
    return SteamClient(
        limiter=limiter,
        timeout=float(cfg.get("probe_timeout_seconds", 6)),
        pause=float(cfg["request_pause_seconds"]),
        log=log,
        lang=cfg.get("steam_lang", "schinese"),
    )


def run_probe(cfg: dict, *, state: State | None = None,
              steam: SteamClient | None = None,
              log: Callable[[str], None] = log) -> int:
    """§12 第 8 步：人工抽查 3 个游戏的「价格 / 中文名」。

    从状态库选 3 个**最近出现**且已有缓存详情的游戏，实时重拉 Steam 侧
    （`appdetails` 单 appid 给 name + 国区价），与 `game_meta` / `seen_deal`
    里缓存的数据对照。**只读 state，不写。**
    （原来的好评率对照随 `appreviews` 端点 2026-10-22 (PT) 停用一并移除，
    决策 4：好评价径现在只有 ITAD `info/v2` 与 `GetItems`。）

    报告落 `output/probe_report.txt`（UTF-8）——另一台 agent 的环境
    PowerShell 捕获 stdout 不可靠，一律以文件为准；stdout 同时打印一份。
    """
    tz = classify.zone(cfg["timezone"])
    now = datetime.now(tz)
    if state is None:
        state = State(resolve_path(cfg, "state_path"), tz=tz).load()
    if steam is None:
        steam = build_probe_steam_client(cfg)

    # ---- 抽样：按 game_id 取最近一次出现的折扣，要求缓存里有 appid + 好评率 ----
    latest: dict[str, dict] = {}
    for entry in state.seen_deal.values():
        gid = entry.get("game_id")
        if not gid:
            continue
        prev = latest.get(gid)
        if prev is None or (entry.get("last_seen_at") or "") > (prev.get("last_seen_at") or ""):
            latest[gid] = entry
    candidates = [
        e for e in latest.values()
        if state.has_appid(e["game_id"]) and (state.meta(e["game_id"]) or {}).get("reviews")
    ]
    candidates.sort(key=lambda e: e.get("last_seen_at") or "", reverse=True)
    if not candidates:
        log("[probe] 状态库里没有可抽查的对象（需要已有 appid + 好评率缓存）")
        return 0
    # 取首 / 中 / 尾各一，避免全抽到最近三条
    picks = {0, len(candidates) // 2, len(candidates) - 1}
    samples = [candidates[i] for i in sorted(picks)]
    log(f"[probe] 抽样 {len(samples)} 个：{[s.get('title') for s in samples]}")

    lines: list[str] = [
        f"probe 抽查报告 {now.isoformat(timespec='seconds')}",
        f"抽样 {len(samples)} 个 · Steam 客户端超时 {cfg.get('probe_timeout_seconds')}s",
        "=" * 70,
    ]
    mismatches = 0
    for entry in samples:
        gid = entry["game_id"]
        appid = state.meta(gid).get("appid")
        cached_meta = state.meta(gid) or {}
        cached_reviews = cached_meta.get("reviews") or {}
        cached_title = state.title_zh(gid)
        lines.append(f"游戏：{entry.get('title')}  appid={appid}  last_seen={entry.get('last_seen_at')}")
        try:
            info = steam.info(int(appid), cc=cfg.get("country", "CN"))
        except HttpError as exc:
            lines.append(f"  [请求失败] {exc}")
            mismatches += 1
            continue

        # 1) 中文名（Steam 没有中文标题时会回落英文名，缓存为 None 属正常，§2.5）
        live_name = (info or {}).get("name")
        if not cached_title:
            name_ok = True
            lines.append(f"  中文名  缓存=（无）  实时={live_name!r}  → 一致（Steam 无中文标题，回落英文名）")
        else:
            name_ok = bool(live_name) and cached_title.strip() == live_name.strip()
            lines.append(f"  中文名  缓存={cached_title!r}  实时={live_name!r}  → {'一致' if name_ok else '不一致'}")
        if not name_ok:
            mismatches += 1

        # 2) 价格（ITAD 落库价 vs Steam 国区实时价；折扣已过期的差异属正常，附 expiry 供人工判断）
        itad_price = entry.get("price_int")
        steam_final = (info or {}).get("final")
        price_ok = itad_price and steam_final is not None and abs(int(steam_final) - int(itad_price)) <= 1
        lines.append(
            f"  价格    ITAD落库={itad_price}  Steam国区={steam_final}  "
            f"折扣={((info or {}).get('discount_percent'))}%  expiry={entry.get('expiry')}"
            f"  → {'一致' if price_ok else '不一致（先看折扣是否已结束）'}"
        )
        if not price_ok:
            mismatches += 1
        # 缓存好评率参考（不实时对照：appreviews 已停用，GetItems 批量对照见 tools/probe_api_limits.py P7）
        cached_score = cached_reviews.get("score")
        if cached_score is not None:
            lines.append(f"  好评率  缓存={cached_score}%/{cached_reviews.get('count')}条"
                         f"  （不再实时对照）")
        lines.append("-" * 70)

    lines.append(f"结论：{len(samples)} 个抽查对象，{mismatches} 处需人工确认（Steam 实发 {steam.calls} 次请求）。")
    lines.append("说明：价格不一致 ≠ 数据错误 —— 折扣结束后 Steam 恢复原价而 ITAD 落库价是折扣价，属预期。")

    out_dir = resolve_path(cfg, "output_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "probe_report.txt"
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        log(line)
    log(f"报告已写入：{out_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Steam 史低日报（第一版：只上线「当日新增」）")
    parser.add_argument("--baseline", action="store_true",
                        help="只把当前全部折扣写入状态，不取详情、不出报表")
    parser.add_argument("--audit", action="store_true",
                        help="体检：无 filter 全量抓取（162 页），统计完整分布与 §4.1 交叉校验，"
                             "不取详情、不出报表")
    parser.add_argument("--prefetch", action="store_true",
                        help="预抓：纯 state 派生欠账（S8 起不扫描折扣列表），按 prefetch_daily_budget"
                             " 补详情缓存（只写 state、不出报表）")
    parser.add_argument("--probe", action="store_true",
                        help="抽查：重拉 3 个游戏的 Steam 实时数据与缓存对照（§12 第 8 步），"
                             "报告写 output/probe_report.txt，只读状态库")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.json）")
    args = parser.parse_args(argv)

    # 退出码（S8 ⑨，告警按此分级）：
    #   0 成功；1 未预期异常；2 配置错误；3 滥用封禁（Blocked）；4 请求失败（HttpError，
    #     **仅报表产出前**——collect/低时间等阶段，页面没更新）；
    #   5 详情/覆盖阶段失败（**含请求失败**——首版报表已产出，Pages 兜底发布，欠账自愈）。
    #   4/5 的判别轴是「页面是否兜底」，不是异常类型（review-s8 P1-1）

    try:
        cfg = load_config(args.config)
        if args.probe:
            return run_probe(cfg)
        if args.prefetch:
            return run_prefetch(cfg)
        if args.audit:
            return run_daily(cfg, audit=True)
        return run_baseline(cfg) if args.baseline else run_daily(cfg)
    except ConfigError as exc:
        log(f"[错误] {exc}")
        return 2
    except Blocked as exc:
        # ITAD / Steam 任一被滥用封禁都走这里（两者共用 httpclient 的策略）
        log(f"[中止] {exc}")
        log("      本轮未产出报表；请先排查是否触发滥用封禁（不要轮换 IP）。")
        return 3
    except HttpError as exc:
        log(f"[错误] 请求失败：{exc}")
        return 4
    except Exception:
        log("[错误] 未预期的异常：")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
