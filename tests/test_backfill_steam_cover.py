# -*- coding: utf-8 -*-
"""封面补缺回填（``tools/backfill_steam_cover.py``，2026-10-10）单测。

不发任何网络请求：``client.fetch`` 注入假客户端。覆盖：

- :func:`select_targets`：只取「**ITAD 无 boxart** + 能进列表（``is_shown_meta``）
  + 还没有 ``cover`` + 有 appid」；**appid 层去重**（同 appid 多个 gid 并组）；
- :func:`backfill`：命中写 ``game_meta.cover``（受控写口、**同写全组**）/
  无资产计 ``no_cover`` 不写 / ``--limit`` 限量 / 墙钟预算到点收尾 /
  连续失败中止 / ``Blocked`` 中止不炸 / ``dry_run`` 不写盘。

``CFG`` 从 ``config.DEFAULTS`` 派生（别手抄三值——DEFAULTS 调整会静默漂移）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.config import DEFAULTS  # noqa: E402
from src.httpclient import Blocked, HttpError  # noqa: E402
from src.state import State  # noqa: E402
from tools.backfill_steam_cover import (  # noqa: E402
    ABORT_BLOCKED,
    ABORT_CONSECUTIVE,
    ABORT_TIME_UP,
    backfill,
    select_targets,
)

TZ = classify.zone("Asia/Shanghai")
#: is_shown_meta 消费的三个档位键 —— 从单表派生，别手抄
CFG = {k: DEFAULTS[k] for k in ("min_positive_ratio", "min_review_count",
                                "notable_review_count")}
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
COVER = "steam/apps/7/library_600x900.jpg"


class FakeClient:
    """假 GetItems 客户端：按 appid 返回带 ``cover`` 的 meta；``fail`` 指定第 n 次调用抛错。"""

    def __init__(self, covers: dict[int, str | None], *, fail: dict[int, Exception] | None = None):
        self._covers = covers
        self._fail = fail or {}
        self.calls = 0
        self.rate_limit_events = 0
        self.events: list[dict] = []
        self.seen: list[list[int]] = []

    def fetch(self, appids):
        appids = list(appids)
        self.seen.append(appids)
        exc = self._fail.get(self.calls)
        self.calls += 1
        if exc:
            raise exc
        return {a: SimpleNamespace(cover=self._covers.get(a)) for a in appids}


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def _game(self, gid, *, appid=7, boxart=None, cover=None, count=500):
        """造一条游戏：seen_deal（可带 boxart）+ 动态层评价（决定能不能进列表）+ game_meta。"""
        entry = {"game_id": gid, "title": gid}
        if boxart:
            entry["boxart"] = boxart
        self.state.data["seen_deal"][f"{gid}|100|e"] = entry
        self.state.dynamic.set_entry(gid, {"reviews": {"score": 90, "count": count}})
        meta: dict = {"fetched_at": NOW.isoformat(timespec="seconds")}
        if appid:
            meta["appid"] = appid
        if cover:
            meta["cover"] = cover
        self.state.data["game_meta"][gid] = meta


class SelectTargetsTest(BaseCase):
    def test_only_gap_entries_with_appid_and_not_yet_filled(self):
        self._game("with-boxart", appid=1, boxart="https://x/b.jpg")
        self._game("already", appid=2, cover="steam/apps/2/library_600x900.jpg")
        self._game("no-appid", appid=None)
        self._game("cold", appid=3, count=1)          # 评价数太少 → 不进列表
        self._game("gap", appid=4)
        self.assertEqual(select_targets(self.state, CFG), [(4, ["gap"])])

    def test_multi_gid_grouped_by_appid(self):
        self._game("a", appid=7)
        self._game("b", appid=7)
        self._game("c", appid=8)
        self.assertEqual(select_targets(self.state, CFG), [(7, ["a", "b"]), (8, ["c"])])


class BackfillTest(BaseCase):
    def test_writes_cover_to_all_gids_of_an_appid(self):
        self._game("a", appid=7)
        self._game("b", appid=7)
        stats = backfill(self.state, FakeClient({7: COVER}), [(7, ["a", "b"])], NOW,
                         clock=lambda: 0.0)
        self.assertEqual(stats["filled"], 2)
        self.assertEqual(self.state.game_meta["a"]["cover"], COVER)
        self.assertEqual(self.state.game_meta["b"]["cover"], COVER)

    def test_missing_asset_counted_but_not_written(self):
        self._game("a", appid=7)
        stats = backfill(self.state, FakeClient({7: None}), [(7, ["a"])], NOW,
                         clock=lambda: 0.0)
        self.assertEqual((stats["filled"], stats["no_cover"]), (0, 1))
        self.assertNotIn("cover", self.state.game_meta["a"])

    def test_limit_stops_before_next_batch(self):
        self._game("a", appid=10)
        self._game("b", appid=11)
        client = FakeClient({10: COVER, 11: COVER})
        stats = backfill(self.state, client, [(10, ["a"]), (11, ["b"])], NOW,
                         batch_size=1, limit=1, clock=lambda: 0.0)
        self.assertEqual(stats["filled"], 1)
        self.assertEqual(len(client.seen), 1)         # 第二批没发

    def test_limit_is_per_appid_not_per_batch(self):
        """``--limit`` 精确到**条**：额度小于一批时只取那么多（不是跑完整批）。"""
        targets = []
        for i in range(5):
            self._game(f"g{i}", appid=200 + i)
            targets.append((200 + i, [f"g{i}"]))
        client = FakeClient({200 + i: COVER for i in range(5)})
        stats = backfill(self.state, client, targets, NOW,
                         batch_size=5, limit=3, clock=lambda: 0.0)
        self.assertEqual(client.seen, [[200, 201, 202]])   # 只发 3 个 appid
        self.assertEqual(stats["requested"], 3)

    def test_time_budget_stops(self):
        self._game("a", appid=10)
        now = [0.0]

        def clock():                      # 每次读表前进 100s → 首轮就超预算
            now[0] += 100.0
            return now[0]

        client = FakeClient({10: COVER})
        stats = backfill(self.state, client, [(10, ["a"])], NOW,
                         time_budget=10.0, clock=clock)
        self.assertEqual(stats["aborted"], ABORT_TIME_UP)
        self.assertEqual(client.seen, [])             # 预算到点：一个请求都没发

    def test_consecutive_failures_abort(self):
        targets = []
        for i in range(4):
            self._game(f"g{i}", appid=100 + i)
            targets.append((100 + i, [f"g{i}"]))
        fail = {0: HttpError("x"), 1: HttpError("x"), 2: HttpError("x")}
        stats = backfill(self.state, FakeClient({}, fail=fail), targets, NOW,
                         batch_size=1, clock=lambda: 0.0)
        self.assertEqual(stats["aborted"], ABORT_CONSECUTIVE)
        self.assertEqual(stats["errors"], 3)

    def test_blocked_aborts_without_raising(self):
        self._game("a", appid=10)
        stats = backfill(self.state, FakeClient({}, fail={0: Blocked("封禁")}),
                         [(10, ["a"])], NOW, clock=lambda: 0.0)
        self.assertEqual(stats["aborted"], ABORT_BLOCKED)
        self.assertEqual(stats["filled"], 0)

    def test_dry_run_writes_nothing(self):
        self._game("a", appid=10)
        stats = backfill(self.state, FakeClient({10: COVER}), [(10, ["a"])], NOW,
                         dry_run=True, clock=lambda: 0.0)
        self.assertEqual(stats["filled"], 1)          # 照常统计
        self.assertNotIn("cover", self.state.game_meta["a"])


if __name__ == "__main__":
    unittest.main()