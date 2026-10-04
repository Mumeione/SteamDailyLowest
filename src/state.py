# -*- coding: utf-8 -*-
"""状态库（对应 docs/DEVELOPMENT.md §5）。

单个滚动 JSON，结构从一开始就按完整版写（第一版不做简化，避免以后迁移）。
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


class State:
    def __init__(self, path: str | Path, tz=None):
        self.path = Path(path)
        self.tz = tz
        self.data: dict = {
            "version": STATE_VERSION,
            "updated_at": None,
            "seen_deal": {},
            "game_meta": {},
            "compare_cache": {},
            "low_time_cache": {},
            "run_log": [],
        }

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def load(self) -> "State":
        if not self.path.exists():
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
        if not isinstance(raw.get("compare_cache"), dict):
            raw["compare_cache"] = {}
        if not isinstance(raw.get("low_time_cache"), dict):
            raw["low_time_cache"] = {}
        raw.setdefault("version", STATE_VERSION)
        raw.setdefault("updated_at", None)
        self.data = raw
        return self

    def save(self, now: datetime | None = None) -> None:
        """原子写入（先写临时文件再替换），避免中途失败写坏状态。

        两件事同时在这里做：

        1. **按 :data:`classify.SEEN_KEEP` 裁剪所有条目**（幂等）。这样即使状态文件是
           早期版本写下的（带 `banner` / `slug` / `shop_id` 等常量字段），
           下一次保存就会自动收敛成精简格式，不需要单独写迁移脚本。
        2. **用紧凑 JSON**（不缩进、无多余空格）：实测 5.09 MB → 4.15 MB（省 19%）。
           状态文件是给程序读的，格式化缩进没有收益，只增加 Actions 的 IO 与传输量。
        """
        if now is not None:
            self.data["updated_at"] = now.isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for key, entry in list(self.seen_deal.items()):
            self.seen_deal[key] = classify.slim_deal(entry)
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

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

        「折扣期暂存」的两块缓存（比价 / 上次史低时间，键都含 expiry）
        随 seen_deal 同一保留期一起清理，不单独设 TTL。
        """
        deadline = now - timedelta(days=retention_days)
        drop: list[str] = []
        for key, entry in self.seen_deal.items():
            expiry = classify.parse_time(entry.get("expiry"), self.tz)
            if expiry is not None and expiry < deadline:
                drop.append(key)
        for key in drop:
            del self.seen_deal[key]
        self._cleanup_expiry_keyed(self.compare_cache, deadline)
        self._cleanup_expiry_keyed(self.low_time_cache, deadline)
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
    # game_meta：详情缓存（appid 永久有效 / reviews TTL 7 天）
    # ------------------------------------------------------------------
    @property
    def game_meta(self) -> dict:
        return self.data["game_meta"]

    def meta(self, game_id: str) -> dict | None:
        return self.game_meta.get(game_id)

    def meta_valid(self, game_id: str, now: datetime, ttl_days: int, empty_ttl_days: int = 3) -> bool:
        """详情缓存是否还在有效期内（决定这一轮要不要再发 ``info/v2``）。

        分两种 TTL：

        - **拿到了 Steam 好评率** → ``ttl_days``（默认 7 天，好评率变化很慢）
        - **抓过但没拿到好评率**（ITAD 对这个游戏没有 Steam 评测数据，或连 appid 都没给）
          → ``empty_ttl_days``（默认 3 天）

        ⚠️ 旧实现是「``has_appid`` 且 ``reviews_valid``」两个条件相与，
        没有 appid 的条目会**每一轮都被重新请求**。判定必须落在「有没有抓过 + 抓过多久」
        上，而不是「有没有 appid」上 —— appid 本来就可能永远拿不到。
        """
        entry = self.game_meta.get(game_id)
        if not entry or entry.get("fetched_at") is None:
            return False
        fetched = classify.parse_time(entry.get("fetched_at"), self.tz)
        if fetched is None:
            return False
        days = ttl_days if entry.get("reviews") else empty_ttl_days
        return now - fetched < timedelta(days=days)

    def has_appid(self, game_id: str) -> bool:
        entry = self.game_meta.get(game_id)
        return bool(entry and entry.get("appid"))

    def set_appid(self, game_id: str, appid) -> None:
        """只写 appid，**不碰 reviews 与 fetched_at**（批量映射专用，重构 S2）。

        appid 是永久缓存（见本节注释），批量 lookup 拿到的映射没有好评率可写；
        若走 :meth:`set_meta` 会把 reviews 清成 None、fetched_at 刷成今天，
        既丢已有好评率又干扰 TTL。
        """
        if not appid or not game_id:
            return
        entry = self.game_meta.get(game_id) or {}
        entry["appid"] = int(appid)
        self.game_meta[game_id] = entry

    def set_detail_failed(self, game_id: str, now: datetime) -> None:
        """标记本轮详情抓取失败（重构 S2 派生式欠账的失败标记）。

        ``detail_failed_at`` + ``detail_attempts``（累加）—— 派生欠账时按
        冷却天数排除近期失败项，避免每轮重试注定拿不到的坏条目。
        """
        if not game_id:
            return
        entry = self.game_meta.get(game_id) or {}
        entry["detail_failed_at"] = now.isoformat(timespec="seconds")
        entry["detail_attempts"] = int(entry.get("detail_attempts") or 0) + 1
        self.game_meta[game_id] = entry

    def detail_recently_failed(self, game_id: str, now: datetime, cooldown_days: int) -> bool:
        """详情是否在冷却期内失败过（派生欠账时排除，避免每轮重试坏条目）。"""
        entry = self.game_meta.get(game_id) or {}
        failed = classify.parse_time(entry.get("detail_failed_at"), self.tz)
        return failed is not None and now - failed < timedelta(days=cooldown_days)

    def set_meta(self, game_id: str, appid, reviews, now: datetime,
                 publishers: list[dict] | None = None,
                 developers: list[dict] | None = None,
                 stats: dict | None = None) -> None:
        """写入详情缓存。

        2026-09-30 起多收 ``publishers`` / ``developers`` / ``stats``（快照 v3 需要）。
        三条约束（改这里前先想清楚）：

        - **只更新传进来的键**，``None`` 表示「本次没取到」→ 保留旧值，**不要清空**；
          （空列表 ``[]`` 是「取到了，确实没有」→ 正常写入。）
        - **不碰 ``fetched_at`` 的语义** —— 它只决定 ``reviews`` 的 TTL，
          新字段不是 TTL 字段（和 ``title_zh`` / ``title_zh_at`` 同一套路）。
        - ``game_meta`` 的条目允许**多代字段共存**：旧条目缺新字段是正常状态，
          ``meta_valid()`` 不能因此判定「无效」而重取。
        """
        entry = self.game_meta.get(game_id) or {}
        if appid:
            entry["appid"] = appid
        entry["reviews"] = reviews
        entry["fetched_at"] = now.isoformat(timespec="seconds")
        self.game_meta[game_id] = entry
        # 新字段的写入规则与回填完全一致 —— 复用 set_meta_extras，别再抄一份
        # if-is-not-None（两处逻辑漂移过一次：code-review 2026-09-30）
        self.set_meta_extras(game_id, publishers=publishers,
                             developers=developers, stats=stats)

    def set_meta_extras(self, game_id: str, *, publishers=None, developers=None,
                        stats=None) -> None:
        """只补 ``publishers`` / ``developers`` / ``stats``，**不碰 reviews 与 fetched_at**。

        专供 ``tools/backfill_game_meta.py`` 一次性回填用 —— 回填时并不重新拿好评率，
        若走 :meth:`set_meta` 会把 ``fetched_at`` 刷成今天、白白推迟 reviews 的 TTL 刷新。
        传 ``None`` 的键一律跳过（保留旧值）。
        """
        if publishers is None and developers is None and stats is None:
            return
        entry = self.game_meta.get(game_id) or {}
        if publishers is not None:
            entry["publishers"] = publishers
        if developers is not None:
            entry["developers"] = developers
        if stats is not None:
            entry["stats"] = stats
        self.game_meta[game_id] = entry

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
        return self.data["low_time_cache"]

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
    # 比价缓存（2026-09-27，方案 B）：键 ``<appid>|<expiry>`` —— 同一折扣期内
    # 不重拉外区价（48h 窗口里的条目每天重复拉是纯浪费）。
    # ⚠️ 只缓存**原币种**数据（cc/label/currency/final）；``cny_minor`` 与
    # ``diff_pct`` 依赖当天汇率，渲染时现算 —— 汇率债不进缓存。
    # 拉取失败（rows 为空）不落缓存，下轮自然重试（宁可留空不猜）。
    # 过期清理跟随 :meth:`cleanup_expired`（与 seen_deal 同一保留期）。
    # ------------------------------------------------------------------
    @property
    def compare_cache(self) -> dict:
        return self.data["compare_cache"]

    @staticmethod
    def compare_key(appid: int, expiry: str | None) -> str:
        return f"{appid}|{expiry or ''}"

    def compare_cached(self, appid: int, expiry: str | None) -> list[dict] | None:
        """命中返回缓存的原始比价行（不含换算字段），未命中返回 None。"""
        entry = self.compare_cache.get(self.compare_key(appid, expiry))
        return entry.get("rows") if entry else None

    def set_compare(self, appid: int, expiry: str | None, rows: list[dict],
                    now: datetime) -> None:
        if not appid or not rows:
            return
        self.compare_cache[self.compare_key(appid, expiry)] = {
            "rows": [
                {k: row[k] for k in ("cc", "label", "currency", "final") if k in row}
                for row in rows
            ],
            "fetched_at": now.isoformat(timespec="seconds"),
        }

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