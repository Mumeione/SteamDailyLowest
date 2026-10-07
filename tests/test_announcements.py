# -*- coding: utf-8 -*-
"""顶部消息区内容源的单测（S9-3，refs.md §4 A-3 / B2）。

钉住四件事（都是「错了不会被发现、但用户会看到」的）：

1. 窗口判定**含首尾**、按北京时间，且在前端判不了（本模块是唯一判定处）；
2. 「季节特卖」优先于普通主题节 —— 同时进行时只显示一条；
3. **坏数据一律当作「没有活动」**：文件缺失 / 坏 JSON / 字段写错都不能抛异常
   （顶部条是锦上添花，不能连累整份报表渲染）；
4. 四季配色与「还有 X 天」的凌晨宽容口径。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import announcements, classify  # noqa: E402

#: 窗口判定一律按北京时间（服务端渲染时用的就是它）
ZONE = classify.zone("Asia/Shanghai")


def _now(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZONE)


def _write(data) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="sdl-ann-"))
    path = tmp / "announcements.json"
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class SeasonTest(unittest.TestCase):
    def test_four_seasons_by_month(self):
        self.assertEqual(announcements.season_of(3), "spring")
        self.assertEqual(announcements.season_of(5), "spring")
        self.assertEqual(announcements.season_of(6), "summer")
        self.assertEqual(announcements.season_of(8), "summer")
        self.assertEqual(announcements.season_of(9), "autumn")
        self.assertEqual(announcements.season_of(11), "autumn")
        self.assertEqual(announcements.season_of(12), "winter")
        self.assertEqual(announcements.season_of(1), "winter")
        self.assertEqual(announcements.season_of(2), "winter")

    def test_explicit_season_overrides_the_month(self):
        """跨季的活动（冬季特卖 12 月开始、次年 1 月结束）以 start 月份推，
        但写死 ``season`` 时以文件为准。"""
        now = _now("2026-10-06 20:00")
        path = _write({"festivals": [{
            "name": "测试节", "start": "2026-10-01 01:00", "end": "2026-10-08 01:00",
            "season": "winter"}]})
        item = announcements.active_festival(now, path)
        self.assertEqual(item["season"], "winter")
        self.assertEqual(item["season_label"], "冬季")


class ParseTimeTest(unittest.TestCase):
    def test_date_only_start_is_midnight_and_end_is_end_of_day(self):
        """写日期 = 含整天：start 取 00:00、end 取 23:59
        （否则 end 写 "2026-10-08" 会变成 08 日 00:00 就结束，整天都看不到）。"""
        start = announcements.parse_time("2026-10-06", ZONE)
        end = announcements.parse_time("2026-10-06", ZONE, end=True)
        self.assertEqual((start.hour, start.minute), (0, 0))
        self.assertEqual((end.hour, end.minute), (23, 59))

    def test_accepts_space_and_T_separator(self):
        a = announcements.parse_time("2026-10-06 01:00", ZONE)
        b = announcements.parse_time("2026-10-06T01:00", ZONE)
        self.assertEqual(a, b)

    def test_garbage_is_none(self):
        for bad in ("", "明天", None, 20261006, "2026-13-45"):
            self.assertIsNone(announcements.parse_time(bad, ZONE), bad)


class LoadTest(unittest.TestCase):
    def test_missing_file_is_empty(self):
        out = announcements.load(Path(tempfile.mkdtemp()) / "nope.json")
        self.assertEqual(out, {"festivals": [], "notices": []})

    def test_corrupt_json_is_empty(self):
        self.assertEqual(announcements.load(_write("{ 这不是 JSON")),
                         {"festivals": [], "notices": []})

    def test_top_level_must_be_object(self):
        self.assertEqual(announcements.load(_write("[1, 2, 3]")),
                         {"festivals": [], "notices": []})

    def test_non_dict_entries_are_dropped_not_fatal(self):
        path = _write({"festivals": ["字符串", 42, None, {"name": "有效节"}],
                       "notices": {"不是列表": True}})
        data = announcements.load(path)
        self.assertEqual(data["festivals"], [{"name": "有效节"}])
        self.assertEqual(data["notices"], [])


class ActiveWindowTest(unittest.TestCase):
    """窗口判定：start <= now <= end，含首尾；只写日期时 end 含整天。"""

    def setUp(self):
        self.path = _write({"festivals": [{
            "name": "秋季特卖", "kind": "season",
            "start": "2026-10-01 01:00", "end": "2026-10-08 01:00"}]})

    def test_before_inside_after(self):
        self.assertIsNotNone(announcements.active_festival(_now("2026-10-01 01:00"), self.path))
        self.assertIsNotNone(announcements.active_festival(_now("2026-10-05 12:00"), self.path))
        self.assertIsNone(announcements.active_festival(_now("2026-09-30 23:59"), self.path))
        self.assertIsNone(announcements.active_festival(_now("2026-10-08 01:01"), self.path))

    def test_date_only_end_covers_the_whole_day(self):
        path = _write({"festivals": [{
            "name": "只有日子", "start": "2026-10-01", "end": "2026-10-08"}]})
        self.assertIsNotNone(announcements.active_festival(_now("2026-10-08 22:00"), path))

    def test_broken_entries_are_ignored(self):
        path = _write({"festivals": [
            {"name": "缺时间", "start": "2026-10-01 01:00"},
            {"name": "时间写反", "start": "2026-10-08 01:00", "end": "2026-10-01 01:00"},
            {"name": "   ", "start": "2026-10-01", "end": "2026-10-08"},
            {"name": "坏的开始时间", "start": "十月一号", "end": "2026-10-08"},
            {"name": "唯一有效的", "start": "2026-10-01", "end": "2026-10-08"},
        ]})
        item = announcements.active_festival(_now("2026-10-05 12:00"), path)
        self.assertEqual(item["text"], "唯一有效的")


class PickTest(unittest.TestCase):
    def test_season_sale_wins_over_theme_fest(self):
        """同时进行时只显示一条，且「季节特卖」优先（refs.md §4 A-4 的季节特卖是重点）。"""
        path = _write({"festivals": [
            {"name": "某主题游戏节", "kind": "event",
             "start": "2026-10-05", "end": "2026-10-20"},
            {"name": "秋季特卖", "kind": "season",
             "start": "2026-10-01", "end": "2026-10-08"},
        ]})
        item = announcements.active_festival(_now("2026-10-06 20:00"), path)
        self.assertEqual(item["text"], "秋季特卖")
        self.assertEqual(item["kind"], "season")

    def test_earlier_end_breaks_the_tie(self):
        path = _write({"festivals": [
            {"name": "B 节", "kind": "event", "start": "2026-10-01", "end": "2026-10-20"},
            {"name": "A 节", "kind": "event", "start": "2026-10-01", "end": "2026-10-09"},
        ]})
        self.assertEqual(announcements.active_festival(_now("2026-10-06 20:00"), path)["text"],
                         "A 节")

    def test_newest_notice_wins(self):
        path = _write({"notices": [
            {"text": "老通知", "start": "2026-10-01", "end": "2026-10-31"},
            {"text": "新通知", "start": "2026-10-06", "end": "2026-10-31"},
        ]})
        self.assertEqual(announcements.active_notice(_now("2026-10-06 20:00"), path)["text"],
                         "新通知")

    def test_notice_keeps_url_and_text_field_name(self):
        """通知用 ``text`` 字段（节日用 ``name``）—— 两者都归一到 ``text``。"""
        path = _write({"notices": [{"text": "改版说明", "url": "https://example.com/a",
                                    "start": "2026-10-01", "end": "2026-10-31"}]})
        item = announcements.active_notice(_now("2026-10-06 20:00"), path)
        self.assertEqual(item["text"], "改版说明")
        self.assertEqual(item["url"], "https://example.com/a")

    def test_no_active_returns_none_for_both(self):
        path = _write({"festivals": [], "notices": []})
        self.assertEqual(announcements.current(_now("2026-10-06 20:00"), path),
                         {"festival": None, "notice": None})


class DaysLeftTest(unittest.TestCase):
    def test_calendar_days(self):
        path = _write({"festivals": [{"name": "节", "start": "2026-10-01",
                                      "end": "2026-10-08 12:00"}]})
        item = announcements.active_festival(_now("2026-10-06 20:00"), path)
        self.assertEqual(item["days_left"], 2)

    def test_early_morning_end_counts_as_previous_day(self):
        """10-08 01:00 收摊 ≈ 今天（10-07）结束 —— 与卡片的凌晨宽容同一口径。"""
        path = _write({"festivals": [{"name": "秋促", "start": "2026-10-01 01:00",
                                      "end": "2026-10-08 01:00"}]})
        self.assertEqual(
            announcements.active_festival(_now("2026-10-06 20:00"), path)["days_left"], 1)
        self.assertEqual(
            announcements.active_festival(_now("2026-10-07 20:00"), path)["days_left"], 0)


class DisplayTextTest(unittest.TestCase):
    """展示文案在**服务端**拼好（起止区间 + 还有几天）—— 别一半在这里、一半在模板里，
    否则改一次口径要翻两个文件（review-s9-05 提到的文案分裂）。"""

    def setUp(self):
        self.path = _write({"festivals": [{
            "name": "Steam 秋季特卖", "kind": "season",
            "start": "2026-10-01 01:00", "end": "2026-10-08 01:00"}]})

    def test_range_and_until_text(self):
        item = announcements.active_festival(_now("2026-10-06 20:00"), self.path)
        self.assertEqual(item["range_text"], "10-01 01:00 – 10-08 01:00")
        self.assertEqual(item["until_text"], "还有 1 天")

    def test_until_text_says_today_on_the_last_day(self):
        item = announcements.active_festival(_now("2026-10-07 20:00"), self.path)
        self.assertEqual(item["until_text"], "今天结束")


class RepoContentFileTest(unittest.TestCase):
    """仓库里那份内容文件必须是可读的 —— 它坏了线上只是不显示顶部条，
    但「季节特卖条整块消失」这种静默故障没人会注意到，所以在这里钉一下。"""

    def test_repo_file_parses(self):
        data = announcements.load()
        self.assertTrue(data["festivals"], "content/announcements.json 里的季节特卖列表是空的")
        self.assertIsInstance(data["notices"], list)

    def test_every_repo_entry_is_usable(self):
        now = _now("2026-10-06 20:00")
        data = announcements.load()
        for entry in data["festivals"] + data["notices"]:
            with self.subTest(entry=entry.get("name") or entry.get("text")):
                self.assertIsNotNone(announcements._normalize(entry, now))

    def test_four_season_sales_present(self):
        """四大季节特卖是重点（.scratch/.../festivals.md §三）；缺一个就是漏维护。"""
        text = " ".join(e.get("name", "") for e in announcements.load()["festivals"])
        for word in ("春季特卖", "夏季特卖", "秋季特卖", "冬季特卖"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
