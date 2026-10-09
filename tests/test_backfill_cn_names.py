# -*- coding: utf-8 -*-
"""中文名回填（``tools/backfill_cn_names.py``，2026-10-09）单测。

不发任何网络请求：``fetch_name`` 注入假函数。覆盖：

- :func:`select_targets`：只取能进列表（``classify.is_shown_meta``）+ 缺中文名
  （无 CJK）+ 负缓存外；**appid 层去重**（同游戏多条 deal / uuid 多对一两层）；
  **评价数降序**（热门优先，零命中守卫依赖这个次序）；``miss_ttl_days`` 生效；
- :func:`backfill`：命中写 ``title_zh``（受控写口、**同写 appid 全部 gid**）/
  单字《茧》过闸 / 假名拒收 / 落空记负缓存 / ``--limit`` 限量（按请求数）/
  墙钟预算到点收尾 / 连续失败中止 / Blocked 中止不炸 / 零命中守卫 /
  ``dry_run`` 不写任何状态。

``CFG`` 从 ``config.DEFAULTS`` 派生（别手抄三值——DEFAULTS 调整会静默漂移）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import classify  # noqa: E402
from src.config import DEFAULTS  # noqa: E402
from src.httpclient import HttpError  # noqa: E402
from src.heybox import HeyboxBlocked  # noqa: E402
from src.state import State  # noqa: E402
from tools.backfill_cn_names import (  # noqa: E402
    ABORT_CONSECUTIVE,
    ABORT_TIME_UP,
    ABORT_ZERO_HIT,
    DEFAULT_MISS_TTL_DAYS,
    backfill,
    select_targets,
)

TZ = classify.zone("Asia/Shanghai")
#: is_shown_meta 消费的三个档位键 —— 从单表派生，别手抄
CFG = {k: DEFAULTS[k] for k in ("min_positive_ratio", "min_review_count",
                                "notable_review_count")}
NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


class BaseStateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json", tz=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def _game(self, gid, count, appid=289070, title_zh=None):
        """造一条「能进列表（quality）」的游戏；appid/title_zh 可选。"""
        self.state.data["seen_deal"][gid] = {"game_id": gid}
        self.state.dynamic.set_entry(gid, {"reviews": {"score": 90, "count": count}})
        meta: dict = {}
        if appid:
            meta["appid"] = appid
        if title_zh is not None:
            meta["title_zh"] = title_zh
        if meta:
            self.state.data["game_meta"][gid] = meta


class SelectTargetsTest(BaseStateCase):
    def test_only_shown_tiers_missing_cn_sorted_by_reviews_desc(self):
        self._game("cold", 50, appid=1000)     # 评价太少 → 排除
        self._game("q", 500, appid=1001)
        self._game("n", 20000, appid=1002)     # 热门优先（零命中守卫依赖这个次序）
        self._game("zh", 900, appid=1003, title_zh="已有中文名")   # 有 CJK → 只补缺，跳过
        self._game("zh1", 800, appid=1004, title_zh="茧")          # 单字中文名也算「有」
        self.assertEqual(select_targets(self.state, CFG, NOW),
                         [(1002, ["n"]), (1001, ["q"])])

    def test_english_title_zh_is_a_target(self):
        # Steam 回退英文名 → title_zh 存了英文 → 正是回填对象
        self._game("g", 500, title_zh="Elden Ring")
        self.assertEqual(select_targets(self.state, CFG, NOW), [(289070, ["g"])])

    def test_same_game_multiple_deals_deduped(self):
        # seen_deal 的幂等键是单条折扣：同一游戏多条 deal 只能入选一次（DayZ 实测两条）
        self._game("g", 500)
        self.state.data["seen_deal"]["g-deal-2"] = {"game_id": "g"}
        self.state.data["seen_deal"]["g-deal-3"] = {"game_id": "g"}
        self.assertEqual(select_targets(self.state, CFG, NOW), [(289070, ["g"])])

    def test_same_appid_multiple_uuids_grouped(self):
        # ITAD uuid → appid 多对一：两个 game_id 指向同一 appid → 并成一组，
        # 一次请求命中后同写全组（DayZ 实测）
        self._game("uuid-b", 500, appid=221100)
        self._game("uuid-a", 500, appid=221100)
        self.assertEqual(select_targets(self.state, CFG, NOW),
                         [(221100, ["uuid-a", "uuid-b"])])

    def test_missing_appid_excluded_from_targets(self):
        self._game("g", 500, appid=None)
        self._game("ok", 400, appid=1001)
        self.assertEqual(select_targets(self.state, CFG, NOW), [(1001, ["ok"])])

    def test_negative_cache_within_ttl_excluded(self):
        self._game("g", 500)
        self.state.heybox_miss("g", NOW - timedelta(days=1))
        self.assertEqual(select_targets(self.state, CFG, NOW), [])

    def test_negative_cache_expired_included_again(self):
        self._game("g", 500)
        stale = NOW - timedelta(days=DEFAULT_MISS_TTL_DAYS + 1)
        self.state.heybox_miss("g", stale)
        self.assertEqual(select_targets(self.state, CFG, NOW), [(289070, ["g"])])

    def test_miss_ttl_days_parameter_takes_effect(self):
        # 定案参数必须穿透到行为：TTL 调小 → 明天落空的明天就能重查
        self._game("g", 500)
        self.state.heybox_miss("g", NOW - timedelta(days=2))
        self.assertEqual(select_targets(self.state, CFG, NOW, miss_ttl_days=1),
                         [(289070, ["g"])])


class BackfillTest(BaseStateCase):
    def test_fills_title_zh_via_controlled_write(self):
        self._game("g", 500)
        stats = backfill(self.state, lambda appid: "文明6",
                         [(289070, ["g"])], NOW)
        self.assertEqual((stats["filled"], stats["missed"], stats["requested"]), (1, 0, 1))
        self.assertIsNone(stats["aborted"])
        self.assertEqual(self.state.game_meta["g"]["title_zh"], "文明6")
        self.assertIn("title_zh_at", self.state.game_meta["g"])

    def test_hit_writes_every_uuid_of_same_appid(self):
        # uuid 多对一：一次请求必须喂饱同 appid 的全部 gid
        self._game("uuid-b", 500, appid=221100)
        self._game("uuid-a", 500, appid=221100)
        stats = backfill(self.state, lambda appid: "DayZ僵尸末日",
                         [(221100, ["uuid-a", "uuid-b"])], NOW)
        self.assertEqual(stats["filled"], 2)
        self.assertEqual(stats["requested"], 1)         # 只发一次请求
        self.assertEqual(self.state.game_meta["uuid-a"]["title_zh"], "DayZ僵尸末日")
        self.assertEqual(self.state.game_meta["uuid-b"]["title_zh"], "DayZ僵尸末日")

    def test_single_char_cocoon_is_accepted(self):
        self._game("g", 500)
        stats = backfill(self.state, lambda appid: "茧", [(289070, ["g"])], NOW)
        self.assertEqual(stats["filled"], 1)
        self.assertEqual(self.state.game_meta["g"]["title_zh"], "茧")

    def test_kana_name_rejected_and_recorded_as_miss(self):
        self._game("g", 500)
        stats = backfill(self.state, lambda appid: "魔女の旅",
                         [(289070, ["g"])], NOW)
        self.assertEqual((stats["filled"], stats["missed"]), (0, 1))
        self.assertNotIn("title_zh", self.state.game_meta["g"])     # 不覆盖
        self.assertIsNotNone(self.state.heybox_missed_at("g"))      # 负缓存入账

    def test_miss_records_negative_cache_for_every_uuid(self):
        self._game("uuid-b", 500, appid=221100)
        self._game("uuid-a", 500, appid=221100)
        stats = backfill(self.state, lambda appid: None,
                         [(221100, ["uuid-a", "uuid-b"])], NOW)
        self.assertEqual(stats["missed"], 2)
        self.assertIsNotNone(self.state.heybox_missed_at("uuid-a"))
        self.assertIsNotNone(self.state.heybox_missed_at("uuid-b"))

    def test_none_returns_count_as_miss(self):
        self._game("g", 500)
        stats = backfill(self.state, lambda appid: None, [(289070, ["g"])], NOW)
        self.assertEqual(stats["missed"], 1)

    def test_limit_caps_requests(self):
        for i in range(5):
            self._game(f"g{i}", 500 - i, appid=1000 + i)
        seen: list[int] = []

        def fetch(appid):
            seen.append(appid)
            return "文明6"

        targets = select_targets(self.state, CFG, NOW)
        backfill(self.state, fetch, targets, NOW, limit=3)
        self.assertEqual(len(seen), 3)                  # limit 按「请求数」计

    def test_time_budget_stops_gracefully(self):
        for i in range(5):
            self._game(f"g{i}", 500 - i, appid=1000 + i)
        tick = {"t": 0.0}

        def clock():
            tick["t"] += 100.0          # 每次看表跳 100s
            return tick["t"]

        targets = select_targets(self.state, CFG, NOW)
        stats = backfill(self.state, lambda appid: "文明6", targets, NOW,
                         time_budget=250.0, clock=clock)
        self.assertEqual(stats["aborted"], ABORT_TIME_UP)
        self.assertLess(stats["requested"], 5)          # 没跑完就收了

    def test_three_consecutive_failures_abort(self):
        self._game("g", 500)
        self._game("g2", 400, appid=1001)
        self._game("g3", 300, appid=1002)

        def fetch(appid):
            raise HttpError("boom")

        targets = select_targets(self.state, CFG, NOW)
        stats = backfill(self.state, fetch, targets, NOW)
        self.assertEqual(stats["aborted"], ABORT_CONSECUTIVE)
        self.assertEqual(stats["errors"], 3)            # 连续第 3 次才停

    def test_single_failure_then_success_continues(self):
        self._game("g", 500)
        self._game("g2", 400, appid=1001)
        flips = {"n": 0}

        def fetch(appid):
            flips["n"] += 1
            if flips["n"] == 1:
                raise HttpError("transient")
            return "文明6"

        targets = select_targets(self.state, CFG, NOW)
        stats = backfill(self.state, fetch, targets, NOW)
        self.assertIsNone(stats["aborted"])
        self.assertEqual(stats["filled"], 1)            # 偶发失败不弃轮

    def test_blocked_aborts_without_raising(self):
        self._game("g", 500)

        def fetch(appid):
            raise HeyboxBlocked("403 封禁（风控零容忍，立即中止）")

        stats = backfill(self.state, fetch, [(289070, ["g"])], NOW)
        self.assertEqual(stats["aborted"], "blocked")   # 部分成果保留，由 main 落盘

    def test_zero_hit_guard_aborts_after_ten_requests(self):
        for i in range(12):
            self._game(f"g{i}", 500 - i, appid=1000 + i)
        targets = select_targets(self.state, CFG, NOW)
        stats = backfill(self.state, lambda appid: None, targets, NOW)
        self.assertEqual(stats["aborted"], ABORT_ZERO_HIT)
        self.assertEqual(stats["requested"], 10)        # 查满守卫阈值即停

    def test_dry_run_requests_but_writes_nothing(self):
        self._game("g", 500)
        stats = backfill(self.state, lambda appid: "文明6",
                         [(289070, ["g"])], NOW, dry_run=True)
        self.assertEqual(stats["filled"], 1)
        self.assertNotIn("title_zh", self.state.game_meta["g"])
        self.assertIsNone(self.state.heybox_missed_at("g"))


if __name__ == "__main__":
    unittest.main()
