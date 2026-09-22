# -*- coding: utf-8 -*-
"""run.py --probe 抽查模式的单元测试（不发网络，注入假客户端）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.state import State
from run import run_probe


class FakeSteamClient:
    """按 appid 返回罐头数据的假 Steam 客户端（info + reviews 各记 1 次调用）。"""

    def __init__(self, info_map=None, reviews_map=None):
        self.info_map = info_map or {}
        self.reviews_map = reviews_map or {}
        self.calls = 0
        self.asked_info = []
        self.asked_reviews = []

    def info(self, appid, cc="CN"):
        self.calls += 1
        self.asked_info.append((appid, cc))
        return self.info_map.get(int(appid))

    def reviews(self, appid):
        self.calls += 1
        self.asked_reviews.append(int(appid))
        return self.reviews_map.get(int(appid))


def make_state(tmp: Path, n_good: int = 5, n_pending: int = 1,
               no_title_idx: int | None = None) -> State:
    """n_good 个「有缓存详情」的候选 + n_pending 个没有 appid 的（应被排除）。

    `no_title_idx` 指定的候选不写中文名缓存（模拟 Steam 无中文标题、回落英文名）。
    """
    state = State(tmp / "state.json", tz=None)
    now = datetime(2026, 9, 22, 3, 0, 0)
    for i in range(n_good):
        gid = f"uuid-{i:02d}"
        state.seen_deal[f"{gid}|1000|2026-09-30"] = {
            "game_id": gid,
            "title": f"Game {i}",
            "price_int": 1000 + i,
            "expiry": "2026-09-30T19:00:00+02:00",
            "last_seen_at": f"2026-09-2{i % 10}T03:00:00+08:00",
        }
        state.set_meta(gid, 100 + i, {"score": 80, "count": 1000}, now)
        if i != no_title_idx:
            state.set_title_zh(gid, f"游戏{i}", now)
    for i in range(n_pending):
        gid = f"pending-{i}"
        state.seen_deal[f"{gid}|500|2026-09-30"] = {
            "game_id": gid,
            "title": f"Pending {i}",
            "price_int": 500,
            "expiry": "2026-09-30T19:00:00+02:00",
            "last_seen_at": "2026-09-22T03:00:00+08:00",
        }
    return state


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out_dir = self.tmp / "out"
        self.cfg = {"timezone": "Asia/Shanghai", "country": "CN", "output_dir": str(self.out_dir)}
        self.state = make_state(self.tmp)

    def fake_steam(self, name_shift=0, price_shift=0, score_shift=0):
        info_map, reviews_map = {}, {}
        for i in range(5):
            info_map[100 + i] = {
                "name": f"游戏{i}" if name_shift == 0 else f"游戏{i}X",
                "final": 1000 + i + price_shift,
                "discount_percent": 50,
            }
            reviews_map[100 + i] = {"score": 80 + score_shift, "count": 1001}
        return FakeSteamClient(info_map, reviews_map)

    def report_lines(self):
        return (self.out_dir / "probe_report.txt").read_text(encoding="utf-8").splitlines()

    def test_all_match(self):
        """全部一致：抽样 3 个、报告落盘、结论 0 处。"""
        rc = run_probe(self.cfg, state=self.state, steam=self.fake_steam(), log=lambda m: None)
        self.assertEqual(rc, 0)
        lines = self.report_lines()
        self.assertTrue(any("0 处需人工确认" in line for line in lines))
        # 抽样数 = min(3, 候选数)
        self.assertTrue(any("抽样 3 个" in line for line in lines))

    def test_requests_shape(self):
        """每个抽查对象恰好 2 次 Steam 请求（info + reviews），且走了国区 cc。"""
        steam = self.fake_steam()
        run_probe(self.cfg, state=self.state, steam=steam, log=lambda m: None)
        self.assertEqual(steam.calls, 6)
        self.assertTrue(all(cc == "CN" for _, cc in steam.asked_info))

    def test_mismatch_counted(self):
        """名称 / 价格 / 好评率三路不一致各计 1 处。"""
        steam = self.fake_steam(name_shift=1, price_shift=100, score_shift=5)
        run_probe(self.cfg, state=self.state, steam=steam, log=lambda m: None)
        lines = self.report_lines()
        self.assertTrue(any("9 处需人工确认" in line for line in lines))
        self.assertTrue(any("不一致" in line for line in lines))

    def test_no_cached_title_is_ok(self):
        """缓存无中文名（Steam 回落英文名）不计不一致。"""
        state = make_state(self.tmp, no_title_idx=4)  # 4 是抽样尾位
        steam = self.fake_steam()
        run_probe(self.cfg, state=state, steam=steam, log=lambda m: None)
        lines = self.report_lines()
        self.assertTrue(any("回落英文名" in line for line in lines))
        self.assertTrue(any("0 处需人工确认" in line for line in lines))

    def test_state_not_written(self):
        """探针只读：跑完不产生 state.json 文件、内存数据不变。"""
        before = json.dumps(self.state.data, ensure_ascii=False, sort_keys=True)
        run_probe(self.cfg, state=self.state, steam=self.fake_steam(), log=lambda m: None)
        after = json.dumps(self.state.data, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)
        self.assertFalse((self.tmp / "state.json").exists())

    def test_pending_excluded(self):
        """没有 appid 缓存的条目不参与抽查。"""
        steam = self.fake_steam()
        run_probe(self.cfg, state=self.state, steam=steam, log=lambda m: None)
        asked = set(steam.asked_info)
        self.assertNotIn(500, asked)  # pending 没进 meta，无所谓；关键是候选只有 5 个 uuid
        # pending 的 game_id 不应出现在报告里
        report = "\n".join(self.report_lines())
        self.assertNotIn("Pending", report)

    def test_no_candidates(self):
        """一个候选都没有：直接返回 0，不写报告。"""
        empty = State(self.tmp / "empty.json", tz=None)
        rc = run_probe(self.cfg, state=empty, steam=self.fake_steam(), log=lambda m: None)
        self.assertEqual(rc, 0)
        self.assertFalse((self.out_dir / "probe_report.txt").exists())


if __name__ == "__main__":
    unittest.main()
