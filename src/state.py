# -*- coding: utf-8 -*-
"""状态库（对应 docs/DEVELOPMENT.md §5）。

单个滚动 JSON，结构从一开始就按完整版写（第一版不做简化，避免以后迁移）。
本模块是**唯一碰磁盘的模块**（§6 职责边界）。
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
        """删除 expiry 已超过保留期的条目（§5 留存清理）。"""
        deadline = now - timedelta(days=retention_days)
        drop: list[str] = []
        for key, entry in self.seen_deal.items():
            expiry = classify.parse_time(entry.get("expiry"), self.tz)
            if expiry is not None and expiry < deadline:
                drop.append(key)
        for key in drop:
            del self.seen_deal[key]
        return len(drop)

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

    def set_meta(self, game_id: str, appid, reviews, now: datetime) -> None:
        entry = self.game_meta.get(game_id) or {}
        if appid:
            entry["appid"] = appid
        entry["reviews"] = reviews
        entry["fetched_at"] = now.isoformat(timespec="seconds")
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
    # 上次史低时间（§3.6）：与 appid/reviews 同存 game_meta，但**无 TTL**——
    # 每轮日常运行都会对「当日新增」整批重取（storelow/v2 批量、200 个/次），
    # 保证游戏从新史低转平史低后，显示的「上次史低」仍是真正的记录时间
    # ------------------------------------------------------------------
    def set_last_low_at(self, game_id: str, ts: str) -> None:
        if not ts or not game_id:
            return
        entry = self.game_meta.get(game_id) or {}
        entry["last_low_at"] = ts
        self.game_meta[game_id] = entry

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