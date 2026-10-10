# -*- coding: utf-8 -*-
"""run_daily 退出码分级（S8 ⑨）的单元测试（review-s8 P1-4）。

不发任何网络请求：ITAD / Steam / 快照全部打桩，只验证「详情/覆盖阶段失败
不吞掉首版报表」的退出码语义：

- 退出码 4/5 的判别轴是「首版报表是否已产出兜底」，不是异常类型
  （review-s8 P1-1 裁决）：详情阶段任何失败（含 HttpError）都记 5；
  Blocked 显式放行交 main 记 3。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run import run_daily  # noqa: E402
from src import classify  # noqa: E402
from src.httpclient import Blocked, HttpError  # noqa: E402
from src.state import State  # noqa: E402

TZ = classify.zone("Asia/Shanghai")
DETAIL_STATS = {"fetched": 1, "fallback_fetched": 0, "fallback_skipped": 0}


def raw_item(game_id: str) -> dict:
    """一条能通过 normalize_item + funnel 的最小 ITAD item（本体 / 付费 / 新史低）。"""
    start = datetime.now(TZ).isoformat(timespec="seconds")
    return {
        "id": game_id, "slug": game_id, "title": "Some Game",
        "type": "game", "mature": False,
        "assets": {"boxart": f"https://x/{game_id}.jpg"},
        "deal": {
            "shop": {"id": 61, "name": "Steam"},
            "price": {"amountInt": 1000, "amount": 10.0, "currency": "CNY"},
            "regular": {"amountInt": 10000, "amount": 100.0},
            "cut": 90, "flag": "N",
            "timestamp": start,
            "expiry": (datetime.now(TZ) + timedelta(days=30)).isoformat(timespec="seconds"),
            "storeLow": {"amountInt": 1000, "amount": 10.0},
            "historyLow": {"amountInt": 1000, "amount": 10.0},
            "historyLow_1y": {"amountInt": 1000, "amount": 10.0},
        },
    }


class FakeItadClient:
    def __init__(self, items):
        self._items = items
        self.calls = 0
        self.events: list[dict] = []
        self.rate_limit_events = 0
        self.limiter = SimpleNamespace(stats=lambda: {})

    def fetch_deals(self, country, shops=61, limit=200, sort="-cut",
                    max_deals=None, sweep="low_only", progress=None):
        return self._items


class FakeSteamClient:
    def __init__(self):
        self.calls = 0
        self.events: list[dict] = []
        self.rate_limit_events = 0
        self.limiter = SimpleNamespace(stats=lambda: {})

    def info(self, appid, cc="CN"):
        return None

    def prices(self, appids, cc):
        return {}


class RunDailyExitCodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = State(self.root / "state.json", tz=TZ).load()
        self.cfg = {
            "timezone": "Asia/Shanghai",
            "country": "CN",
            "state_path": str(self.root / "state.json"),
            "output_dir": str(self.root / "output"),
            "expiring_snapshot_path": str(self.root / "expiring.json"),
            "fx_cache_path": str(self.root / "fx.json"),
            "expired_retention_days": 7,
            "sweep_mode": "low_only",
            "min_cut": 0, "max_price": None,
            "only_type": "game", "exclude_mature": True, "exclude_free": True,
            "min_positive_ratio": 0.7, "min_review_count": 100,
            "notable_review_count": 10000, "absolute_min_positive_ratio": None,
            "compare_countries": ["UA", "IN"],
            "list_batch": 30, "list_auto_max": 300,
            "mobile_breakpoint_px": 768, "stale_banner_hours": 36,
            "upcoming_expiry_hours": 48,
            "new_game_days": 30, "new_game_refresh_days": 1,
            "discount_refresh_days": 3, "expiry_refresh_days": 1,
            "detail_retry_cooldown_days": 3,
            "run_log_keep": 30,
        }
        # $GITHUB_STEP_SUMMARY 在测试环境不该存在（write_step_summary 静默跳过）
        self._summary_patcher = patch.dict("os.environ", {}, clear=False)
        self._summary_patcher.start()
        import os
        os.environ.pop("GITHUB_STEP_SUMMARY", None)

    def tearDown(self):
        self._summary_patcher.stop()
        self.tmp.cleanup()

    def _state_on_disk(self) -> State:
        """run_daily 内部自建 State 实例，断言须从磁盘重载（同一路径的另一对象）。"""
        return State(self.root / "state.json", tz=TZ).load()

    def _patches(self, *, fetch_details=None, fetch_last_low_times=0,
                 write_snapshot=0):
        """打桩 run_daily 的全部网络触点，返回上下文管理器组。"""
        import run
        itad = FakeItadClient([raw_item("uuid-1")])
        steam = FakeSteamClient()
        browse = FakeSteamClient()
        return [
            patch.object(run, "build_client", return_value=itad),
            patch.object(run, "build_steam_client", return_value=steam),
            patch.object(run, "build_steam_browse_client", return_value=browse),
            patch.object(run.enrich, "load_fx", return_value=None),
            patch.object(run, "fetch_details", fetch_details),
            patch.object(run, "fetch_last_low_times", return_value=fetch_last_low_times),
            patch.object(run.snapshot, "write_snapshot", return_value=write_snapshot),
        ]

    def test_detail_stage_http_error_returns_5_with_v1_report(self):
        """详情阶段 HttpError 也记 5（判别轴=页面已兜底，review-s8 P1-1）；
        首版报表已渲染到 output/，状态库已落盘。"""
        def boom(*args, **kwargs):
            raise HttpError("详情请求挂了")
        ctxs = self._patches(fetch_details=boom)
        with __import__("contextlib").ExitStack() as stack:
            for c in ctxs:
                stack.enter_context(c)
            code = run_daily(self.cfg)
        self.assertEqual(code, 5)
        self.assertTrue((self.root / "output" / "index.html").exists())
        # 攒库先行（record_seen + save），详情失败不带走（run_daily 内部已 save）
        self.assertGreaterEqual(len(self._state_on_disk().seen_deal), 1)

    def test_detail_stage_unexpected_error_returns_5(self):
        def boom(*args, **kwargs):
            raise RuntimeError("渲染炸了")
        ctxs = self._patches(fetch_details=boom)
        with __import__("contextlib").ExitStack() as stack:
            for c in ctxs:
                stack.enter_context(c)
            code = run_daily(self.cfg)
        self.assertEqual(code, 5)

    def test_detail_stage_blocked_propagates(self):
        """详情阶段撞滥用封禁不降级为 5：交 main 记退出码 3。"""
        def boom(*args, **kwargs):
            raise Blocked("滥用封禁")
        ctxs = self._patches(fetch_details=boom)
        with __import__("contextlib").ExitStack() as stack:
            for c in ctxs:
                stack.enter_context(c)
            with self.assertRaises(Blocked):
                run_daily(self.cfg)

    def test_success_path_returns_0(self):
        """全链路成功：退出码 0，run_log 记 mode=daily，报表与快照产出。"""
        ctxs = self._patches(fetch_details=lambda *a, **k: dict(DETAIL_STATS))
        with __import__("contextlib").ExitStack() as stack:
            for c in ctxs:
                stack.enter_context(c)
            code = run_daily(self.cfg)
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "output" / "index.html").exists())
        disk = self._state_on_disk()
        self.assertEqual(disk.last_run().get("mode"), "daily")
        self.assertEqual(disk.last_run().get("detail_fetched"), 1)


class RunDeadlineTest(unittest.TestCase):
    """P0-6：`http_budget_seconds` → 本轮总墙钟截止时刻；替身不打桩也安全。"""

    def test_disabled_when_zero_or_missing(self):
        from run import run_deadline
        self.assertIsNone(run_deadline({}))
        self.assertIsNone(run_deadline({"http_budget_seconds": 0}))

    def test_returns_future_deadline(self):
        import time

        from run import run_deadline
        deadline = run_deadline({"http_budget_seconds": 60})
        self.assertGreater(deadline, time.monotonic())
        self.assertLessEqual(deadline, time.monotonic() + 61)

    def test_arm_budget_skips_test_doubles(self):
        from run import arm_budget

        class Fake:
            pass

        fake = Fake()
        arm_budget(fake, 123.0)                  # 替身没有 set_deadline，不应抛
        self.assertFalse(hasattr(fake, "_deadline"))


if __name__ == "__main__":
    unittest.main()
