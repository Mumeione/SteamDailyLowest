# -*- coding: utf-8 -*-
"""状态库（对应 docs/DEVELOPMENT.md §5；重构 spec §3.4）。

**重构 S4 起切两份**（2026-10-04）：

- ``state.json`` —— 不可重建：``seen_deal`` + ``game_meta`` + ``run_log``
- ``cache.json`` —— 可重建（丢了重拉）：``compare_cache`` + ``low_time_cache``

**重构 S6 起切三份**（2026-10-05，spec 决策 12/15）：

- ``dynamic.json`` —— 动态重要数据：``reviews`` / ``stats`` / ``fetched_at`` /
  ``detail_failed_at`` / ``detail_attempts``（按 game_id 键控）
- ``game_meta``（state.json 内）只留**不变层**：``appid`` / ``title_zh`` /
  ``title_zh_at`` / ``publishers`` / ``developers`` / ``release_date`` / ``unlisted``

分层依据：「不变」= 不随时间变化或变化可忽略（appid/中文标题/厂商/发行日）；
「动态重要」= 会变且有消费方（好评率、ITAD stats）；动态条目跟随 seen_deal
留存清理（游戏离开 seen_deal 即删，重现时靠不变层 appid 直批 GetItems，成本低）。
``Dynamic`` 的容错策略与 ``Cache`` 一致（文件缺失/损坏按空起步，可重抓自愈）；
``State.meta()`` 返回不变层 + 动态层的**合并视图**，读方无感；
旧版 game_meta 里混存的动态键在 :meth:`State.load` 时自动收编（代码自迁移）。
本模块负责状态库落盘（§6 职责边界；快照导出见 ``snapshot.py``，同为原子写）。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import classify

STATE_VERSION = 1
CACHE_VERSION = 1
DYN_VERSION = 1

# cache.json 收的两块缓存键（旧版 state.json 里的遗留键同名）
_CACHE_KEYS = ("compare_cache", "low_time_cache")

# 旧版 game_meta 里混存的动态键（S6 拆分前格式）→ 收编进 dynamic.json
_DYN_KEYS = ("reviews", "stats", "fetched_at", "detail_failed_at", "detail_attempts")


def _is_cc_suffix(suffix: str) -> bool:
    """compare_cache 新键（S7）后缀须为两位大写地区码（UA/IN/…）；
    旧方案 B 键的后缀是 ISO 时间戳或空串，据此在 load 自迁移时识别丢弃。"""
    return len(suffix) == 2 and suffix.isalpha() and suffix.isupper()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """紧凑 JSON + 原子写（先写临时文件再替换），State 与 Cache 共用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


class Cache:
    """可重建缓存（cache.json）：compare_cache + low_time_cache。

    **可重建**语义决定了它和 State 容错策略不同：文件缺失或损坏按空缓存
    起步（丢了重拉），不抛异常炸管线。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {
            "version": CACHE_VERSION,
            "updated_at": None,
            "compare_cache": {},
            "low_time_cache": {},
        }

    def load(self) -> "Cache":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}  # 可重建：坏了不炸管线，按空缓存起步（丢了重拉）
        if not isinstance(raw, dict):
            raw = {}
        for key in _CACHE_KEYS:
            if not isinstance(raw.get(key), dict):
                raw[key] = {}
        raw.setdefault("version", CACHE_VERSION)
        raw.setdefault("updated_at", None)
        self.data = raw
        return self

    def save(self, now: datetime | None = None) -> None:
        if now is not None:
            self.data["updated_at"] = now.isoformat(timespec="seconds")
        _atomic_write_json(self.path, self.data)


class Dynamic:
    """动态重要数据（dynamic.json）：reviews / stats / fetched_at / 失败簿记。

    ``entries`` 按 game_id 键控。**跟随 seen_deal 留存清理**（游戏离开
    seen_deal 即删，见 :meth:`State.cleanup_expired`）。容错策略：
    文件**缺失**按空起步（可重抓自愈）；文件**损坏抛错**与 state.json 同等
    对待（review-s6 P1-3）—— 拆分后 dynamic 是 reviews 的唯一副本，
    静默丢档会让全部已列游戏看起来「没抓过」而整轮重抓，必须响亮失败。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {
            "version": DYN_VERSION,
            "updated_at": None,
            "entries": {},
        }

    @property
    def entries(self) -> dict:
        return self.data["entries"]

    def load(self) -> "Dynamic":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"动态数据文件不是合法 JSON：{self.path}（{exc}）"
                "—— 拒绝按空库静默起步（reviews 唯一副本，丢了会整轮重抓）"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"动态数据文件顶层必须是对象：{self.path}")
        if not isinstance(raw.get("entries"), dict):
            raw["entries"] = {}
        raw.setdefault("version", DYN_VERSION)
        raw.setdefault("updated_at", None)
        self.data = raw
        return self

    def save(self, now: datetime | None = None) -> None:
        if now is not None:
            self.data["updated_at"] = now.isoformat(timespec="seconds")
        _atomic_write_json(self.path, self.data)


class State:
    def __init__(self, path: str | Path, tz=None, cache_path: str | Path | None = None,
                 dynamic_path: str | Path | None = None):
        self.path = Path(path)
        # cache.json / dynamic.json 固定与 state.json 同目录同名
        # （data/state.json → data/cache.json + data/dynamic.json），
        # 无独立配置键 —— 派生规则单一来源；特殊场景用参数显式指定
        self.cache = Cache(cache_path if cache_path else self.path.with_name("cache.json"))
        self.dynamic = Dynamic(dynamic_path if dynamic_path else self.path.with_name("dynamic.json"))
        self.tz = tz
        self.data: dict = {
            "version": STATE_VERSION,
            "updated_at": None,
            "seen_deal": {},
            "game_meta": {},
            "run_log": [],
        }

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def load(self) -> "State":
        if not self.path.exists():
            self.cache.load()
            self.dynamic.load()
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"状态文件不是合法 JSON：{self.path}（{exc}）") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"状态文件顶层必须是对象：{self.path}")
        for key in ("seen_deal", "game_meta"):
            if not isinstance(raw.get(key), dict):
                raw[key] = {}
        if not isinstance(raw.get("run_log"), list):
            raw["run_log"] = []
        raw.setdefault("version", STATE_VERSION)
        raw.setdefault("updated_at", None)
        # ---- 重构 S4 代码自迁移：旧版 state.json 里带 compare_cache /
        # low_time_cache（切分前的格式）→ 收编进 Cache 后从 state 里剥掉。
        # cache.json 里已有的键不覆盖（cache.json 是切分后的权威来源），
        # 缺的键补上 —— 既幂等又无损。
        self.cache.load()
        legacy_found = False
        for key in _CACHE_KEYS:
            legacy = raw.pop(key, None)
            if isinstance(legacy, dict) and legacy:
                legacy_found = True
                target = self.cache.data[key]
                for k, v in legacy.items():
                    target.setdefault(k, v)
        if legacy_found:
            self.cache.save()
        # ---- 重构 S7 代码自迁移：旧版 compare_cache（方案 B）键 ``<appid>|<expiry>``
        # 存的是**折扣现价**（price_overview.final），与现模型（``<appid>|<cc>``
        # 存区域**原价** initial）不是同一个量 —— 收编会把折扣价当原价、污染
        # 重定价校准基线，直接丢弃（用户 2026-10-05 裁决清空重建；新缓存由每轮
        # 真查响应里的 initial 零成本自建，无额外请求）。幂等：合法键（后缀为
        # 两位大写地区码）不受影响。
        stale = [k for k in self.cache.data["compare_cache"]
                 if not _is_cc_suffix(k.rsplit("|", 1)[-1])]
        if stale:
            for k in stale:
                del self.cache.data["compare_cache"][k]
            self.cache.save()
        self.data = raw
        # ---- 重构 S6 代码自迁移：旧版 game_meta 里混存的动态键（reviews /
        # stats / fetched_at / 失败簿记）→ 收编进 dynamic.json 后从 game_meta
        # 剥掉。dynamic.json 已有的键不覆盖（它是拆分后的权威来源）。幂等。
        self.dynamic.load()
        dyn_found = False
        for gid, entry in self.game_meta.items():
            if not isinstance(entry, dict):
                continue
            legacy_dyn = {k: entry.pop(k) for k in _DYN_KEYS if k in entry}
            if legacy_dyn:
                dyn_found = True
                target = self.dynamic.entries.setdefault(gid, {})
                for k, v in legacy_dyn.items():
                    target.setdefault(k, v)
        if dyn_found:
            self.dynamic.save()
        return self

    def save(self, now: datetime | None = None) -> None:
        """原子写入（先写临时文件再替换），避免中途失败写坏状态。

        两件事同时在这里做：

        1. **按 :data:`classify.SEEN_KEEP` 裁剪所有条目**（幂等）。这样即使状态文件是
           早期版本写下的（带 `banner` / `slug` / `shop_id` 等常量字段），
           下一次保存就会自动收敛成精简格式，不需要单独写迁移脚本。
        2. **用紧凑 JSON**（不缩进、无多余空格）：实测 5.09 MB → 4.15 MB（省 19%）。
           状态文件是给程序读的，格式化缩进没有收益，只增加 Actions 的 IO 与传输量。

        重构 S4 起 cache.json、S6 起 dynamic.json 随同一次保存落盘
        （state 先、dynamic 次、cache 后）。三次独立的原子替换**做不到跨文件原子**：
        中途崩溃可能留下新旧组合。三侧都可安全收敛，不会丢数据：
        cache 缺键下一轮自然重拉；dynamic 缺条按折扣活跃度重新派生重抓；
        state 侧 seen_deal 有幂等键，重跑重记即可。
        """
        if now is not None:
            self.data["updated_at"] = now.isoformat(timespec="seconds")
        for key, entry in list(self.seen_deal.items()):
            self.seen_deal[key] = classify.slim_deal(entry)
        _atomic_write_json(self.path, self.data)
        self.dynamic.save(now)
        self.cache.save(now)

    # ------------------------------------------------------------------
    # seen_deal：幂等 + 兜底口径 + 其余视图的攒库
    # ------------------------------------------------------------------
    @property
    def seen_deal(self) -> dict:
        return self.data["seen_deal"]

    def has_seen(self, game_id: str, price_int, expiry) -> bool:
        return classify.deal_key(game_id, price_int, expiry) in self.seen_deal

    def record_seen(self, deal: dict, now: datetime) -> tuple[str, bool]:
        """写入一条折扣；返回 ``(幂等键, 是否首次见到)``。

        落库前按 :data:`classify.SEEN_KEEP` 裁剪字段（§5），避免把常量字段
        与重复的封面 URL 每天提交回仓库。
        """
        key = classify.deal_key(deal.get("game_id"), deal.get("price_int"), deal.get("expiry"))
        entry = self.seen_deal.get(key)
        stamp = now.isoformat(timespec="seconds")
        if entry is None:
            entry = classify.slim_deal(deal)
            entry["first_seen_at"] = stamp
            entry["last_seen_at"] = stamp
            self.seen_deal[key] = entry
            return key, True
        entry["last_seen_at"] = stamp
        return key, False

    def cleanup_expired(self, now: datetime, retention_days: int) -> int:
        """删除 expiry 已超过保留期的条目（§5 留存清理）。

        「折扣期暂存」的缓存（上次史低时间，键含 expiry）随 seen_deal 同一
        保留期一起清理，不单独设 TTL。**S7 起比价缓存不参与清理**：它已换
        永久键 ``<appid>|<cc>``（存区域原价），键不含 expiry —— 若仍走 expiry
        清理逻辑，地区码后缀解析不成时间会被当成「绑定不了折扣期」误删
        （S7 头号坑，handoff 明确警告过）。
        **S6 起 dynamic.json 同步收缩**：被清条目的 game_id 若已不在任何
        seen_deal 条目里，其动态数据（reviews/stats/失败簿记）一并删除 ——
        动态数据的消费方（欠账派生、报表合并）都源自 seen_deal。
        """
        deadline = now - timedelta(days=retention_days)
        drop: list[str] = []
        for key, entry in self.seen_deal.items():
            expiry = classify.parse_time(entry.get("expiry"), self.tz)
            if expiry is not None and expiry < deadline:
                drop.append(key)
        dropped_gids = {self.seen_deal[key].get("game_id") for key in drop}
        for key in drop:
            del self.seen_deal[key]
        self._cleanup_expiry_keyed(self.low_time_cache, deadline)
        live_gids = {e.get("game_id") for e in self.seen_deal.values()}
        for gid in dropped_gids:
            if gid and gid not in live_gids:
                self.dynamic.entries.pop(gid, None)
        return len(drop)

    @staticmethod
    def _cleanup_expiry_keyed(cache: dict, deadline: datetime) -> int:
        """清掉缓存键里 expiry 早于 deadline 的条目（键格式 ``<id>|<expiry>``）。

        expiry 缺失或解析不了的键无法与折扣期绑定，一并清掉 —— 暂存可重建，
        留着只会缓慢积累。
        """
        stale: list[str] = []
        for key in cache:
            expiry = classify.parse_time(key.rsplit("|", 1)[1], deadline.tzinfo) \
                if "|" in key else None
            if expiry is None or expiry < deadline:
                stale.append(key)
        for key in stale:
            del cache[key]
        return len(stale)

    # ------------------------------------------------------------------
    # game_meta（不变层） + dynamic.json（动态层） + 合并视图
    # ------------------------------------------------------------------
    @property
    def game_meta(self) -> dict:
        return self.data["game_meta"]

    def meta(self, game_id: str) -> dict | None:
        """合并视图：不变层（game_meta）+ 动态层（dynamic.json）。

        动态键（reviews/stats/fetched_at/失败簿记）以动态层为准；
        读方（报表合并 / 比价 / 探针）无感 —— 拿到的仍是「一个 meta dict」。
        """
        base = self.game_meta.get(game_id)
        dyn = self.dynamic.entries.get(game_id)
        if dyn:
            return {**(base or {}), **dyn}
        return base

    def dyn(self, game_id: str) -> dict | None:
        """原始动态条目（不合并），供派生欠账判定数据年龄。"""
        return self.dynamic.entries.get(game_id)

    def drop_dyn(self, game_id: str) -> None:
        """删除整个动态条目（unlisted 收敛 / 留存清理用）。"""
        self.dynamic.entries.pop(game_id, None)

    def detail_fetched_recently(self, game_id: str, now: datetime, ttl_days: int) -> bool:
        """动态数据是否在 ttl 天内抓过（给一次性工具做幂等跳过用；
        管线内的新鲜度判定走 run.py 的折扣感知逻辑，不用这个）。"""
        dyn = self.dyn(game_id)
        if not dyn or dyn.get("fetched_at") is None:
            return False
        fetched = classify.parse_time(dyn.get("fetched_at"), self.tz)
        return fetched is not None and now - fetched < timedelta(days=ttl_days)

    def has_appid(self, game_id: str) -> bool:
        entry = self.game_meta.get(game_id)
        return bool(entry and entry.get("appid"))

    def set_appid(self, game_id: str, appid) -> None:
        """只写 appid（不变层），**不碰 reviews 与 fetched_at**（批量映射专用，重构 S2）。

        appid 是永久缓存（见本节注释），批量 lookup 拿到的映射没有好评率可写；
        若走 :meth:`set_meta` 会把 reviews 清成 None、fetched_at 刷成今天，
        既丢已有好评率又干扰刷新节奏。
        """
        if not appid or not game_id:
            return
        entry = self.game_meta.get(game_id) or {}
        entry["appid"] = int(appid)
        self.game_meta[game_id] = entry

    # ---- release_date（不变层）：GetItems 顺带返回，新游判定用（spec 决策 16）----
    def set_release_date(self, game_id: str, ts) -> None:
        """发行时间戳（秒）。**首见即定、不回退** —— 发行日不会变，避免每轮覆写。"""
        if not game_id or ts is None:
            return
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return
        entry = self.game_meta.get(game_id) or {}
        if entry.get("release_date") is None:
            entry["release_date"] = ts
            self.game_meta[game_id] = entry

    # ---- unlisted 标记（不变层）：未入列表条目（spec 决策 17）----
    def unlisted(self, game_id: str) -> dict | None:
        return (self.game_meta.get(game_id) or {}).get("unlisted")

    def set_unlisted(self, game_id: str, now: datetime, start: str | None) -> None:
        """打未入列表标记：``{at, start}``。

        ``start`` = 判定时的折扣开始日 —— 派生欠账时对比条目当前的 ``start``，
        相同 = 同一折扣期内冻结（不建动态条目、不重抓）；不同 = 下次折扣，重抓重判。
        """
        if not game_id:
            return
        entry = self.game_meta.get(game_id) or {}
        entry["unlisted"] = {
            "at": now.isoformat(timespec="seconds"),
            "start": start,
        }
        self.game_meta[game_id] = entry

    def clear_unlisted(self, game_id: str) -> bool:
        """摘除标记（翻案转正常记录）；返回是否真的摘了。"""
        entry = self.game_meta.get(game_id)
        if entry and "unlisted" in entry:
            del entry["unlisted"]
            return True
        return False

    def set_detail_failed(self, game_id: str, now: datetime) -> None:
        """标记本轮详情抓取失败（重构 S2 派生式欠账的失败标记）。

        ``detail_failed_at`` + ``detail_attempts``（累加）写**动态层** ——
        派生欠账时按 ``detail_retry_cooldown_days`` 冷却排除近期失败项，
        避免每轮重试注定拿不到的坏条目。
        """
        if not game_id:
            return
        entry = self.dynamic.entries.setdefault(game_id, {})
        entry["detail_failed_at"] = now.isoformat(timespec="seconds")
        entry["detail_attempts"] = int(entry.get("detail_attempts") or 0) + 1
        self.dynamic.entries[game_id] = entry

    def detail_recently_failed(self, game_id: str, now: datetime, cooldown_days: int) -> bool:
        """详情是否在冷却期内失败过（派生欠账时排除，避免每轮重试坏条目）。"""
        entry = self.dyn(game_id) or {}
        failed = classify.parse_time(entry.get("detail_failed_at"), self.tz)
        return failed is not None and now - failed < timedelta(days=cooldown_days)

    def set_meta(self, game_id: str, appid, reviews, now: datetime,
                 publishers: list[dict] | None = None,
                 developers: list[dict] | None = None,
                 stats: dict | None = None) -> None:
        """写入详情缓存（S6 起按层分写）。

        2026-09-30 起多收 ``publishers`` / ``developers`` / ``stats``（快照 v3 需要）。
        三条约束（改这里前先想清楚）：

        - **只更新传进来的键**，``None`` 表示「本次没取到」→ 保留旧值，**不要清空**；
          （空列表 ``[]`` 是「取到了，确实没有」→ 正常写入。）
        - **不碰 ``fetched_at`` 的语义** —— 它只决定 ``reviews`` 的刷新节奏，
          新字段不是时间字段（和 ``title_zh`` / ``title_zh_at`` 同一套路）。
        - 条目允许**多代字段共存**：旧条目缺新字段是正常状态，
          新鲜度判定不能因此判「无效」而重取。

        分层落点：``appid`` / ``publishers`` / ``developers`` → game_meta（不变层）；
        ``reviews`` / ``fetched_at`` / ``stats`` → dynamic.json（动态层，决策 15）。
        """
        if appid:
            entry = self.game_meta.get(game_id) or {}
            entry["appid"] = appid
            self.game_meta[game_id] = entry
        dyn = self.dynamic.entries.setdefault(game_id, {})
        dyn["reviews"] = reviews
        dyn["fetched_at"] = now.isoformat(timespec="seconds")
        if stats is not None:
            dyn["stats"] = stats
        self.dynamic.entries[game_id] = dyn
        # 新字段的写入规则与回填完全一致 —— 复用 set_meta_extras，别再抄一份
        # if-is-not-None（两处逻辑漂移过一次：code-review 2026-09-30）
        self.set_meta_extras(game_id, publishers=publishers, developers=developers)

    def set_meta_extras(self, game_id: str, *, publishers=None, developers=None,
                        stats=None) -> None:
        """只补附加字段，**不碰 reviews 与 fetched_at**。

        专供 ``tools/backfill_game_meta.py`` 一次性回填用 —— 回填时并不重新拿好评率，
        若走 :meth:`set_meta` 会把 ``fetched_at`` 刷成今天、白白推迟 reviews 的刷新。
        传 ``None`` 的键一律跳过（保留旧值）。
        分层落点：publishers/developers → 不变层；stats → 动态层（决策 15）。
        """
        if publishers is None and developers is None and stats is None:
            return
        if publishers is not None or developers is not None:
            entry = self.game_meta.get(game_id) or {}
            if publishers is not None:
                entry["publishers"] = publishers
            if developers is not None:
                entry["developers"] = developers
            self.game_meta[game_id] = entry
        if stats is not None:
            dyn = self.dynamic.entries.setdefault(game_id, {})
            dyn["stats"] = stats
            self.dynamic.entries[game_id] = dyn

    # ------------------------------------------------------------------
    # 中文名：永久缓存（Steam 的本地化标题不会变，没必要每次重拉）
    # ------------------------------------------------------------------
    def title_zh(self, game_id: str) -> str | None:
        entry = self.game_meta.get(game_id)
        return (entry or {}).get("title_zh")

    def set_title_zh(self, game_id: str, title: str | None, now: datetime | None = None) -> None:
        """单独写中文名。

        ⚠️ **不能顺手改 `fetched_at`** —— 那个字段决定 `reviews` 的 TTL，
        而中文名与详情是两条独立的缓存。
        """
        if not title:
            return
        entry = self.game_meta.get(game_id) or {}
        entry["title_zh"] = title
        entry["title_zh_at"] = (now or datetime.now()).isoformat(timespec="seconds")
        self.game_meta[game_id] = entry

    # ------------------------------------------------------------------
    # 上次史低时间（§3.6）：**折扣期暂存**（2026-09-27 起不再放 game_meta）。
    # 键 ``<game_id>|<expiry>`` —— 与比价缓存同一套「折扣期暂存」模式：
    # 每轮日常运行对「当日新增 + 即将过期」整批重取覆盖，条目过期后随
    # :meth:`cleanup_expired` 一起清掉，不永久积累。
    # 旧 state.json 里 game_meta.last_low_at 的存量字段留存不迁移（读不到
    # 就等下一轮 storelow 重取补上，一天内自愈）。
    # ------------------------------------------------------------------
    @property
    def low_time_cache(self) -> dict:
        return self.cache.data["low_time_cache"]

    @staticmethod
    def low_time_key(game_id: str, expiry: str | None) -> str:
        return f"{game_id}|{expiry or ''}"

    def last_low_at(self, game_id: str, expiry: str | None) -> str | None:
        return self.low_time_cache.get(self.low_time_key(game_id, expiry))

    def set_last_low_at(self, game_id: str, expiry: str | None, ts: str) -> None:
        if not ts or not game_id:
            return
        self.low_time_cache[self.low_time_key(game_id, expiry)] = ts

    # ------------------------------------------------------------------
    # 跨区比价（重构 S7 换模型）：**区域原价永久缓存**，键 ``<appid>|<cc>``。
    # 存 price_overview.initial（该区常规原价）—— 原价不随折扣期失效，永久
    # 积累；现价（final）每轮真查、不进缓存。
    # 真查即校准：每轮真查响应里的 initial 与缓存比对，不一致 = Valve 区域
    # 重定价，回写自愈并由 enrich 层记日志（验收要求「捕获并记日志」）。
    # 旧方案 B（键 ``<appid>|<expiry>`` 存折扣现价 final）语义废弃：final 与
    # initial 不是同一个量，旧格式键在 :meth:`load` 自迁移丢弃（收编会污染
    # 校准基线，用户 2026-10-05 裁决清空重建）。
    # ⚠️ 本缓存**不在** :meth:`cleanup_expired` 的清理范围里 —— 永久键不含
    # expiry，被 expiry 清理扫到会因后缀解析不了而误删（S7 头号坑）。
    # ------------------------------------------------------------------
    @property
    def compare_cache(self) -> dict:
        return self.cache.data["compare_cache"]

    @staticmethod
    def compare_key(appid: int, cc: str) -> str:
        return f"{appid}|{cc}"

    def compare_original(self, appid: int, cc: str) -> int | None:
        """缓存里的区域原价（price_overview.initial），没有则 None。"""
        entry = self.compare_cache.get(self.compare_key(appid, cc))
        return entry.get("initial") if entry else None

    def set_compare_original(self, appid: int, cc: str,
                             initial: int | str | None, currency: str | None,
                             now: datetime) -> int | None:
        """记录区域原价（真查即校准）。

        返回**旧值** = 发生区域重定价（Valve 调价，调用方据此记日志）；
        首见写入或值未变返回 None。原价缺失（None/非法）不落缓存 ——
        宁可留空不猜。"""
        if not appid or not cc or initial is None:
            return None
        try:
            initial = int(initial)
        except (TypeError, ValueError):
            return None
        key = self.compare_key(appid, cc)
        entry = self.compare_cache.get(key)
        if entry and entry.get("initial") == initial:
            return None
        self.compare_cache[key] = {
            "initial": initial,
            "currency": currency,
            "fetched_at": now.isoformat(timespec="seconds"),
        }
        return entry.get("initial") if entry else None

    # ------------------------------------------------------------------
    # run_log
    # ------------------------------------------------------------------
    @property
    def run_log(self) -> list:
        return self.data["run_log"]

    def add_run_log(self, entry: dict, keep: int = 30) -> None:
        self.run_log.append(entry)
        if keep > 0 and len(self.run_log) > keep:
            del self.run_log[: len(self.run_log) - keep]

    def last_run(self) -> dict | None:
        return self.run_log[-1] if self.run_log else None