# -*- coding: utf-8 -*-
"""重构 S4 迁移脚本 ``tools/migrate_split_state.py`` 的单元测试。

验收口径（spec §6）：state.json 与 cache.json 的迁移可重复执行且幂等。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from migrate_split_state import migrate  # noqa: E402


def legacy_state() -> dict:
    return {
        "version": 1,
        "updated_at": "2026-10-04T16:00:01+08:00",
        "seen_deal": {"g|865|e": {"game_id": "g", "price_int": 865}},
        "game_meta": {"g": {"appid": 111, "fetched_at": "t"}},
        "compare_cache": {"111|e": {"rows": [{"cc": "UA", "final": 1}], "fetched_at": "t"}},
        "low_time_cache": {"g|e": "2026-06-01T12:00:00+02:00"},
        "run_log": [{"mode": "daily"}],
    }


def split_state() -> dict:
    raw = legacy_state()
    del raw["compare_cache"], raw["low_time_cache"]
    return raw


class MigrateSplitStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.cache_path = Path(self.tmp.name) / "cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrates_and_preserves_state_fields(self):
        self.state_path.write_text(
            json.dumps(legacy_state(), ensure_ascii=False), encoding="utf-8")
        self.assertEqual(migrate(self.state_path, self.cache_path), 0)

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        # state：不可重建数据原样保留，缓存键剥掉
        for key in ("version", "updated_at", "seen_deal", "game_meta", "run_log"):
            self.assertEqual(state[key], legacy_state()[key])
        self.assertNotIn("compare_cache", state)
        self.assertNotIn("low_time_cache", state)
        # cache：两块缓存完整搬过去，版本/updated_at 带上
        self.assertEqual(cache["compare_cache"], legacy_state()["compare_cache"])
        self.assertEqual(cache["low_time_cache"], legacy_state()["low_time_cache"])
        self.assertEqual(cache["updated_at"], legacy_state()["updated_at"])

    def test_idempotent_repeat_run_is_noop(self):
        """重复执行无副作用：第二次跑文件逐字节不变（验收 spec §6）。"""
        self.state_path.write_text(
            json.dumps(legacy_state(), ensure_ascii=False), encoding="utf-8")
        self.assertEqual(migrate(self.state_path, self.cache_path), 0)
        state_bytes = self.state_path.read_bytes()
        cache_bytes = self.cache_path.read_bytes()
        self.assertEqual(migrate(self.state_path, self.cache_path), 0)
        self.assertEqual(self.state_path.read_bytes(), state_bytes)
        self.assertEqual(self.cache_path.read_bytes(), cache_bytes)

    def test_merge_precedence_cache_json_wins(self):
        """cache.json 已存在时：已有键不被覆盖，缺的键从遗留键收编（并集）。"""
        cache = {"version": 1, "updated_at": "new",
                 "compare_cache": {"111|e": {"rows": [{"cc": "UA", "final": 2}], "fetched_at": "new"}},
                 "low_time_cache": {}}
        self.cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        self.state_path.write_text(
            json.dumps(legacy_state(), ensure_ascii=False), encoding="utf-8")
        self.assertEqual(migrate(self.state_path, self.cache_path), 0)

        cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        # 111|e 两边都有 → cache.json 优先；g|e 只有遗留键有 → 收编
        self.assertEqual(cache["compare_cache"]["111|e"]["rows"][0]["final"], 2)
        self.assertEqual(cache["low_time_cache"]["g|e"], "2026-06-01T12:00:00+02:00")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("compare_cache", state)

    def test_noop_when_already_split(self):
        """已是拆分后格式（state 无缓存键 + cache.json 存在）→ no-op。"""
        self.state_path.write_text(
            json.dumps(split_state(), ensure_ascii=False), encoding="utf-8")
        self.cache_path.write_text(
            json.dumps({"version": 1, "updated_at": None,
                        "compare_cache": {}, "low_time_cache": {}},
                       ensure_ascii=False), encoding="utf-8")
        state_bytes = self.state_path.read_bytes()
        cache_bytes = self.cache_path.read_bytes()
        self.assertEqual(migrate(self.state_path, self.cache_path), 0)
        self.assertEqual(self.state_path.read_bytes(), state_bytes)
        self.assertEqual(self.cache_path.read_bytes(), cache_bytes)

    def test_split_state_without_cache_json_builds_empty_cache(self):
        """边界：已拆分 state + cache.json 缺失 → 补建空 cache.json，再跑 no-op。"""
        self.state_path.write_text(
            json.dumps(split_state(), ensure_ascii=False), encoding="utf-8")
        self.assertEqual(migrate(self.state_path, self.cache_path), 0)

        # state 内容不变（不可重建字段原样），cache 补建为空
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8")), split_state())
        cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cache["compare_cache"], {})
        self.assertEqual(cache["low_time_cache"], {})
        self.assertEqual(cache["updated_at"], split_state()["updated_at"])

        # 补建后再跑收敛为 no-op（文件不再变化）
        self.assertEqual(migrate(self.state_path, self.cache_path), 0)
        self.assertEqual(
            json.loads(self.cache_path.read_text(encoding="utf-8")), cache)

    def test_missing_state_fails_cleanly(self):
        self.assertEqual(migrate(self.state_path, self.cache_path), 1)


if __name__ == "__main__":
    unittest.main()
