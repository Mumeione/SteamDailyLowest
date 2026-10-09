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
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from . import classify

STATE_VERSION = 1
CACHE_VERSION = 1
DYN_VERSION = 1

# cache.json 收的三块缓存键（旧版 state.json 里的遗留键同名）。
# heybox_miss：中文名回填的落空标记（2026-10-09）—— 键为 game_id、
# 值为时刻，**不参与** cleanup_expired（永久键，同 compare_cache 的 S7 先例），
# TTL 由读取方按「落空后 N 天内不重查」判定。
_CACHE_KEYS = ("compare_cache", "low_time_cache", "heybox_miss")

# 旧版 game_meta 里混存的动态键（S6 拆分前格式）→ 收编进 dynamic.json
_DYN_KEYS = ("reviews", "stats", "fetched_at", "detail_failed_at", "detail_attempts")


def _is_cc_suffix(suffix: str) -> bool:
    """compare_cache 新键（S7）后缀须为两位大写地区码（UA/IN/…）；
    旧方案 B 键的后缀是 ISO 时间戳或空串，据此在 load 自迁移时识别丢弃。"""
    return len(suffix) == 2 and suffix.isalpha() and suffix.isupper()


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：先写同目录临时文件（+ fsync）再 ``os.replace`` 换上去。

    中途断电 / 被 kill 都不会留下「半个文件」—— 状态库与报表快照都靠它
    （2026-10-09 卡片 05：从前 ``snapshot`` 与 ``tools/sync_state_from_remote``
    各自手抄了一份同款实现，改一处漏两处）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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


def atomic_write_json(path: Path, payload: dict) -> None:
    """紧凑 JSON（``separators=(",", ":")``）+ :func:`atomic_write_text`。"""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


class Cache:
    """可重建缓存（cache.json）：compare_cache + low_time_cache + heybox_miss。

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
            "heybox_miss": {},
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
        atomic_write_json(self.path, self.data)


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
    def entries(self) -> Mapping:
        """动态条目（game_id → reviews / stats / fetched_at / 失败计数）—— **只读视图**。

        写入走 :meth:`set_entry` / :meth:`drop_entry`（State 的 set_meta /
        set_detail_failed / set_title_zh / drop_dyn 都经由它们）。语义与限制
        同 ``State.seen_deal``（只读、浅只读）。
        """
        return MappingProxyType(self.data["entries"])

    def set_entry(self, game_id: str, entry: dict) -> None:
        """**受控写口**：按原样写一条动态条目（与只读的 :attr:`entries` 配对）。

        生产侧不直接用（走 State 的 set_meta / set_detail_failed 等），
        但测试夹具与一次性脚本需要一个「塞一条现成的」入口 —— 那就是这里，
        而不是 ``state.dynamic.data["entries"][gid] = ...``。
        """
        self.data["entries"][game_id] = entry

    def ensure_entry(self, game_id: str) -> dict:
        """取一条**可变的**动态条目（不存在就建空的并挂上，返回库里那个 dict）。

        与只读 :attr:`entries` 配对：纯读用只读视图，**读-改-写**走这里 ——
        ``setdefault`` 返回的就是库里那个对象，改完不必（也没必要）再赋值回去。
        """
        return self.data["entries"].setdefault(game_id, {})

    def drop_entry(self, game_id: str) -> None:
        """删除一条动态条目（不存在的 game_id 静默跳过）。"""
        self.data["entries"].pop(game_id, None)

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
        atomic_write_json(self.path, self.data)


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
        # low_time_cache 的 game_id 索引（惰性建；写/清后置 None 失效），见 last_low_at
        self._low_time_idx: dict | None = None
        #: **原始载荷**（state.json 的顶层对象）—— 读写用，见下面的约定：
        #: ⚠️ 生产代码**不要直接改它**（也别改 ``self.dynamic.data`` / ``self.cache.data``）：
        #:   · ``data["seen_deal"]`` / ``data["game_meta"]`` 有配套语义（幂等键、首见时间、
        #:     留存清理、失败标记），必须走具名方法（record_seen / set_meta / set_appid /
        #:     set_title_zh / set_low_period / set_unlisted / set_compare_original …）；
        #:   · 对外只读视图是 :attr:`seen_deal` / :attr:`game_meta` /
        #:     :attr:`low_time_cache` / :attr:`compare_cache` / :attr:`Dynamic.entries`。
        #: 允许直接用的只有两类：**代码自迁移**（load 里重整旧格式）与**测试夹具**
        #: （要造一条不符合任何方法前置条件的原始行）。
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
                target = self.dynamic.ensure_entry(gid)
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
            self.data["seen_deal"][key] = classify.slim_deal(entry)
        atomic_write_json(self.path, self.data)
        self.dynamic.save(now)
        self.cache.save(now)

    # ------------------------------------------------------------------
    # seen_deal：幂等 + 兜底口径 + 其余视图的攒库
    # ------------------------------------------------------------------
    @property
    def seen_deal(self) -> Mapping:
        """已见折扣（幂等键 → 条目）—— **只读视图**（2026-10-09 卡片 04）。

        ⚠️ 返回的是 :class:`~types.MappingProxyType`，**赋值 / 删除会直接抛
        ``TypeError``** —— 写入必须走具名方法（:meth:`record_seen` 负责幂等键构造与
        首见时间、:meth:`cleanup_expired` 负责留存清理）。从前这里直接返回内部 dict
        本体，调用方一句 ``state.seen_deal[k] = v`` 就能绕过全部配套语义
        （``tools/backfill_low_period`` 直写 game_meta 那次就是这么长出并行口径的）。

        ⚠️ **浅只读**：条目内部仍是原 dict，``state.seen_deal[k]["x"] = 1`` 不会报错。
        要改内容请用对应方法，别往里塞字段。
        ⚠️ 夹具 / 一次性脚本若要往原始载荷里塞行，走 :attr:`data`（见那里的说明）。
        """
        return MappingProxyType(self.data["seen_deal"])

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
            self.data["seen_deal"][key] = entry
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
        dropped_gids = {self.data["seen_deal"][key].get("game_id") for key in drop}
        for key in drop:
            del self.data["seen_deal"][key]
        # ⚠️ 传的是**原始载荷里的那张表**（要删键）—— 别传只读视图 self.low_time_cache
        self._cleanup_expiry_keyed(self.cache.data["low_time_cache"], deadline)
        self._low_time_idx = None   # 缓存被清理，game_id 索引一并失效
        live_gids = {e.get("game_id") for e in self.seen_deal.values()}
        for gid in dropped_gids:
            if gid and gid not in live_gids:
                self.dynamic.drop_entry(gid)
        return len(drop)

    @staticmethod
    def _cleanup_expiry_keyed(cache: dict, deadline: datetime) -> int:
        """清掉缓存键里 expiry 早于 deadline 的条目（键格式 ``<id>|<expiry>``）。

        参数必须是**可变的原始表**（``self.cache.data["low_time_cache"]``）——
        传只读视图会 TypeError（卡片 04 的改造把两者分开了）。

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
    def game_meta(self) -> Mapping:
        """不变层缓存（game_id → appid / title_zh / 厂商 / release_date / unlisted / low_period）
        —— **只读视图**（2026-10-09 卡片 04）。

        写入走具名方法：:meth:`set_appid` / :meth:`set_title_zh` /
        :meth:`set_release_date` / :meth:`set_unlisted` /
        :meth:`set_low_period`（回填写口）/ :meth:`set_meta`。语义与限制同
        :attr:`seen_deal`（只读、浅只读）。
        """
        return MappingProxyType(self.data["game_meta"])

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
        """原始动态条目（不合并），供派生欠账判定数据年龄。

        ⚠️ 返回的是**库里的那个 dict**（可写）—— 只读的 :attr:`Dynamic.entries`
        是另一条路（映射视图）。要改内容用具名方法（set_detail_failed / set_meta…）。
        """
        return self.dynamic.entries.get(game_id)

    def drop_dyn(self, game_id: str) -> None:
        """删除整个动态条目（unlisted 收敛 / 留存清理用）。"""
        self.dynamic.drop_entry(game_id)

    # 注：原 `detail_fetched_recently(game_id, now, ttl_days)` 已删（2026-10-09 卡片 04）
    # —— 它只被自己那条单测调过，生产/工具零引用；新鲜度判定的唯一出处是
    # classify.entry_needs_detail / refresh_ttl_days（折扣感知四档，不是单一 TTL）。

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
        self.data["game_meta"][game_id] = entry

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
            self.data["game_meta"][game_id] = entry

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
        self.data["game_meta"][game_id] = entry

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
        entry = self.dynamic.ensure_entry(game_id)
        entry["detail_failed_at"] = now.isoformat(timespec="seconds")
        entry["detail_attempts"] = int(entry.get("detail_attempts") or 0) + 1

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
            self.data["game_meta"][game_id] = entry
        dyn = self.dynamic.ensure_entry(game_id)
        dyn["reviews"] = reviews
        dyn["fetched_at"] = now.isoformat(timespec="seconds")
        if stats is not None:
            dyn["stats"] = stats
        # 新字段的写入规则与回填完全一致 —— 复用 set_meta_extras，别再抄一份
        # if-is-not-None（两处逻辑漂移过一次：code-review 2026-09-30）
        self.set_meta_extras(game_id, publishers=publishers, developers=developers)

    def set_meta_extras(self, game_id: str, *, publishers=None, developers=None,
                        stats=None) -> None:
        """只补附加字段，**不碰 reviews 与 fetched_at**。

        原供 2026-10 的一次性回填用（``publishers`` / ``developers`` / ``stats``
        历史上没存，脚本已随仓库整理退场，git 历史可找回）—— 回填时并不重新拿
        好评率，若走 :meth:`set_meta` 会把 ``fetched_at`` 刷成今天、白白推迟
        reviews 的刷新。传 ``None`` 的键一律跳过（保留旧值）。
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
            self.data["game_meta"][game_id] = entry
        if stats is not None:
            self.dynamic.ensure_entry(game_id)["stats"] = stats

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
        self.data["game_meta"][game_id] = entry

    # ------------------------------------------------------------------
    # 上次史低时间（§3.6）：**折扣期暂存**（2026-09-27 起不再放 game_meta）。
    # 键 ``<game_id>|<expiry>`` —— 与比价缓存同一套「折扣期暂存」模式：
    # 每轮日常运行对「当日新增 + 即将过期」整批重取覆盖，条目过期后随
    # :meth:`cleanup_expired` 一起清掉，不永久积累。
    # 旧 state.json 里 game_meta.last_low_at 的存量字段留存不迁移（读不到
    # 就等下一轮 storelow 重取补上，一天内自愈）。
    # ------------------------------------------------------------------
    @property
    def low_time_cache(self) -> Mapping:
        """折扣期暂存（``game_id|expiry`` → 上次史低时间）—— **只读视图**（卡片 04）。

        写入走 :meth:`set_last_low_at`；跨换档的读取回退由 :meth:`last_low_at` 负责
        —— 直接翻这张表会绕过「精确键 miss 就回退到该游戏最新一条」的语义。
        """
        return MappingProxyType(self.cache.data["low_time_cache"])

    @staticmethod
    def low_time_key(game_id: str, expiry: str | None) -> str:
        return f"{game_id}|{expiry or ''}"

    def _low_time_by_game(self) -> dict:
        """``game_id -> (expiry, ts)`` 索引（惰性建，写/清后失效），expiry 取该游戏
        已有键里**最新**的折扣期。给 :meth:`last_low_at` 的换档回退用。

        背景（2026-10-09 换档日实测）：键 ``<gid>|<expiry>`` 把「上次史低时间」绑死在
        折扣期上，而折扣期会被 Steam **延长**（跨周四换档续到下周期）—— 延长后
        「当日新增 / 即将过期」两条抓取路径都覆盖不到这种老条目，精确查找必 miss，
        「距上次史低」整行消失（线上热门板块 tie 卡 10/10 丢行、大额折扣 9/10）。
        """
        if self._low_time_idx is None:
            idx: dict = {}
            for key, ts in self.low_time_cache.items():
                gid, _, exp = key.partition("|")
                parsed = classify.parse_time(exp, self.tz) if exp else None
                prev = idx.get(gid)
                # 留折扣期最新的那条；解析不了（None）只在没得比时兜底
                if prev is None or (parsed is not None
                                    and (prev[0] is None or parsed > prev[0])):
                    idx[gid] = (parsed, ts)
            self._low_time_idx = idx
        return self._low_time_idx

    def last_low_at(self, game_id: str, expiry: str | None) -> str | None:
        hit = self.low_time_cache.get(self.low_time_key(game_id, expiry))
        if hit is not None:
            return hit
        # 换档回退：折扣期变了导致精确键 miss ⇒ 退回该游戏缓存里折扣期最新的
        # 一条。「上次史低时间」描述的是**上一次**到这个价的时间，不随当前折扣期
        # 变，值仍然有效；等哪轮批量重取（当日新增 / 即将过期）覆盖到它时，
        # 新键写入、自然校正。
        # ⚠️ 回退是**无条件**的（不区分「折扣期被延长」与「换了新折扣」），这是
        # 有意的取舍 —— **宁旧勿缺**：真正的换价当天就落在「当日新增」候选里
        # （开始日/首见日是今天，不分 new/tie），会被 storelow 重取覆盖精确键；
        # 空窗期留着上一次的值（最多偏一天）比整行空更好。放宽到精确键优先于此
        # 回退，只在 `fetch_last_low_time=false` 或该批重取失败时才会短暂偏旧。
        fallback = self._low_time_by_game().get(game_id or "")
        return fallback[1] if fallback else None

    def set_last_low_at(self, game_id: str, expiry: str | None, ts: str) -> None:
        if not ts or not game_id:
            return
        self.cache.data["low_time_cache"][self.low_time_key(game_id, expiry)] = ts
        self._low_time_idx = None

    # ------------------------------------------------------------------
    # 史低期记忆（§3.6 扩展，2026-10-09）：每个游戏一对时间戳（game_meta 永久层）
    # —— ``low_period = {cur: 本次史低期开始, prev: 上一次史低期开始}``。
    # 动机：**新史低**的「距上次史低」没有数据源 —— storelow/v2 对新史低返回空
    # （这个价从没出现过），只能自己攒：新史低期首次入账时滚动更新
    # （prev=cur、cur=新值），渲染时新史低卡取 prev 算间隔。平史低不动
    # （继续走 ITAD storelow 的上次同价时刻，更精确）。
    # 每游戏只有两条时间戳、新值覆盖旧值 —— 不积累垃圾（等价于「只保存到当前
    # 折扣结束」），也不把数据绑在 expiry 上 —— 天然免疫折扣期延长键漂移
    # （low_time_cache 2026-10-09 踩过的坑）。
    # ------------------------------------------------------------------
    def record_low_period(self, game_id: str | None, start: str | None) -> None:
        """史低期首次入账时滚动更新记忆。

        同 deal 重复入账 / 折扣期延长（同 start）不更新；乱序写入只认
        **更晚**的开始时间。首次史低（cur 原本为空）→ prev 记 None，
        该卡维持「本次新史低」文案。
        """
        if not game_id or not start:
            return
        # 「时刻 t 是否开启一段史低期」的判定收在 classify.roll_low_period（同一份
        # 规则回填也吃）—— 这里只负责把新 cur 落到 game_meta，并滚动 prev。
        period = (self.game_meta.get(game_id) or {}).get("low_period") or {}
        cur = period.get("cur")
        new_cur = classify.roll_low_period(cur, start, self.tz)
        if new_cur is None:
            return
        self.set_low_period(game_id, new_cur, cur)

    def set_low_period(self, game_id: str, cur: str | None, prev: str | None) -> None:
        """**受控写口**：直接落一对 ``(cur, prev)``（回填专用）。

        ⚠️ 回填**必须走这里**，别自己写 ``state.game_meta[gid]["low_period"]`` ——
        那就是绕过 :meth:`record_low_period` 的乱序/重复保护，在状态库外面复刻
        一条「何时能写」的并行口径（架构检查卡片 04）。语义与增量路径一致：
        ``prev`` 传旧 cur，首次史低传 None。
        """
        if not game_id:
            return
        meta = self.game_meta.get(game_id) or {}
        meta["low_period"] = {"cur": cur, "prev": prev}
        self.data["game_meta"][game_id] = meta

    def prev_low_start(self, game_id: str | None) -> str | None:
        """该游戏上一次史低期的开始时间（无记忆返回 None —— 首次史低/冷启动）。"""
        if not game_id:
            return None
        period = (self.game_meta.get(game_id) or {}).get("low_period") or {}
        return period.get("prev") or None

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
    def compare_cache(self) -> Mapping:
        """区域原价永久缓存（``appid|cc`` → ``{initial, currency, fetched_at}``）
        —— **只读视图**（卡片 04）。

        读用 :meth:`compare_entry` / :meth:`compare_original`（前者一次给出原价与币种），
        写走 :meth:`set_compare_original`（它带「真查即校准 / 值未变不刷时间戳」语义）。
        """
        return MappingProxyType(self.cache.data["compare_cache"])

    @staticmethod
    def compare_key(appid: int, cc: str) -> str:
        return f"{appid}|{cc}"

    def compare_entry(self, appid: int, cc: str) -> dict | None:
        """缓存里该区域的整条记录（``{initial, currency, fetched_at}``），没有则 None。

        **给需要「原价 + 币种」一起用的调用方**（如 :func:`src.enrich.estimate_compare`）
        —— 从前它自己 ``state.compare_cache.get(state.compare_key(...))`` 直探内部字典，
        等于把缓存结构外泄成公开接口（卡片 04）。
        """
        return self.compare_cache.get(self.compare_key(appid, cc))

    def compare_original(self, appid: int, cc: str) -> int | None:
        """缓存里的区域原价（price_overview.initial），没有则 None。"""
        entry = self.compare_entry(appid, cc)
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
        self.cache.data["compare_cache"][key] = {
            "initial": initial,
            "currency": currency,
            "fetched_at": now.isoformat(timespec="seconds"),
        }
        return entry.get("initial") if entry else None

    # ------------------------------------------------------------------
    # heybox_miss：中文名回填的落空标记（2026-10-09）
    # ------------------------------------------------------------------
    # 小黑盒对无中文译名的游戏返回英文/日文原名（不算命中）——不记负缓存的话，
    # 每次手动触发回填都会把这些已知落空的条目重查一遍白烧请求。
    # 落在 **cache 层**（可重建，丢了重查一轮即可自愈）；键空间与
    # compare_cache 同款永久键，**绝不挂进 cleanup_expired**（S7 头号坑：
    # expiry 键清理按「后缀解析时间」走，解析不了会误删）。
    # ------------------------------------------------------------------
    def heybox_miss(self, game_id: str, now: datetime) -> None:
        """记录「该游戏在小黑盒没有有效中文名」及其时刻（TTL 判定在读取方）。"""
        if not game_id:
            return
        self.cache.data["heybox_miss"][game_id] = now.isoformat(timespec="seconds")

    def heybox_missed_at(self, game_id: str) -> datetime | None:
        """该游戏最近一次「小黑盒落空」的时刻；从未落空 → None。"""
        raw = self.cache.data["heybox_miss"].get(game_id)
        return classify.parse_time(raw, self.tz)

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