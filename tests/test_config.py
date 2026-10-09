# -*- coding: utf-8 -*-
"""配置读取的直测 + **默认值单表的机械校验**（架构检查卡片 06 / 09）。

`src/config.py` 此前零测试，而它承载的是全仓库的默认值契约。这里锁三件事：

1. 畸形输入的报错行为（非法 JSON / 顶层非对象 / key 缺失）、环境变量覆盖、
   ``parse_rate_limit`` 的宽松解析、``resolve_path`` 的仓库根解析；
2. :data:`DEFAULTS` 里**不允许出现「代码里带着默认值去 .get()、但表里没有」的键**
   —— 那正是「三张默认值表并存」的来源（改一个 48 要动 5 处）；
3. 前端/工具真正依赖的那几个键必须在表里（防止有人从表里删掉又靠读点兜底）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    DEFAULTS,
    ROOT,
    ConfigError,
    api_key,
    load_config,
    parse_rate_limit,
    resolve_path,
)

#: 扫描范围：运行管线（src/ + run.py）与工具（tools/，回填脚本也读配置）。
#: 只认**配置形接收者**（``cfg`` / ``self.cfg`` / ``(cfg or {})``）—— 产物/统计
#: dict 上的 ``.get(key, default)`` 与配置无关，不该被这条校验牵连。
SCAN_DIRS = ("src", "tools")
CONFIG_GET = re.compile(
    r'(?:^|[^\w.])(?:cfg|self\.cfg)\.get\(\s*["\']([a-z_0-9]+)["\']\s*,'
    r'|\(cfg or \{\}\)\s*\.get\(\s*["\']([a-z_0-9]+)["\']\s*,')


class DefaultsSingleTableTest(unittest.TestCase):
    """`DEFAULTS` 是默认值的**唯一一张表**，读点兜底不许再写第二份字面量。"""

    def test_no_config_key_read_with_default_outside_defaults(self):
        paths = [ROOT / "run.py"]
        for base in SCAN_DIRS:
            paths += [p for p in (ROOT / base).rglob("*.py") if "__pycache__" not in str(p)]
        found: dict[str, str] = {}
        for path in paths:
            for groups in CONFIG_GET.findall(path.read_text(encoding="utf-8")):
                found.setdefault(next(g for g in groups if g), str(path.relative_to(ROOT)))
        stray = {k: v for k, v in found.items() if k not in DEFAULTS}
        self.assertEqual(
            stray, {},
            f"这些键带默认值被 .get() 读取，却不在 config.DEFAULTS 里（默认值要单表）：{stray}")

    def test_frontend_and_tool_contract_keys_present(self):
        """前端 / 工具真正依赖的键（payload 下发、工具读配置）必须留在表里。"""
        for key in ("home_new_low_days", "big_cut_percent", "notable_review_count",
                    "upcoming_expiry_hours", "list_batch", "list_auto_max",
                    "home_section_preview", "home_section_preview_min", "home_picks",
                    "min_positive_ratio", "good_positive_ratio", "bad_positive_ratio",
                    "mobile_breakpoint_px", "tablet_breakpoint_px", "timezone",
                    "state_path", "output_dir", "compare_countries"):
            self.assertIn(key, DEFAULTS, f"config.DEFAULTS 缺少 {key}")

    def test_report_constants_are_aliases_of_defaults(self):
        """report.py 的同名常量只是单表的别名，不是第二张表。"""
        from src import report
        self.assertEqual(report.DEFAULT_HOME_DAYS, DEFAULTS["home_new_low_days"])
        self.assertEqual(report.DEFAULT_BIG_CUT, DEFAULTS["big_cut_percent"])
        self.assertEqual(report.DEFAULT_NOTABLE, DEFAULTS["notable_review_count"])
        self.assertEqual(report.DEFAULT_UPCOMING_HOURS, DEFAULTS["upcoming_expiry_hours"])
        self.assertEqual(report.DEFAULT_HOME_SECTION_PREVIEW, DEFAULTS["home_section_preview"])
        self.assertEqual(report.DEFAULT_HOME_SECTION_FLOOR, DEFAULTS["home_section_preview_min"])
        self.assertEqual(report.HOME_PICKS_DEFAULT, DEFAULTS["home_picks"])

    def test_example_config_has_no_key_missing_from_defaults(self):
        """config.example.json 的键必须都在单表里（否则示例教人写「表外的键」）。"""
        example = ROOT / "config.example.json"
        if not example.exists():
            self.skipTest("没有 config.example.json")
        keys = set(json.loads(example.read_text(encoding="utf-8")))
        missing = sorted(keys - set(DEFAULTS))
        self.assertEqual(missing, [], f"示例配置里有 DEFAULTS 不认识的键：{missing}")


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text: str, name="config.json") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_uses_defaults_only(self):
        cfg = load_config(self.dir / "nope.json")
        self.assertEqual(cfg["country"], DEFAULTS["country"])
        self.assertEqual(cfg["timezone"], DEFAULTS["timezone"])

    def test_user_config_overrides_defaults(self):
        cfg = load_config(self.write('{"country": "US", "home_new_low_days": 3}'))
        self.assertEqual(cfg["country"], "US")
        self.assertEqual(cfg["home_new_low_days"], 3)
        self.assertEqual(cfg["notable_review_count"], DEFAULTS["notable_review_count"])

    def test_malformed_json_raises_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write("{ not json"))
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_non_object_top_level_raises(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write("[1, 2, 3]"))
        self.assertIn("必须是对象", str(ctx.exception))

    def test_config_path_recorded(self):
        path = self.write("{}")
        self.assertEqual(load_config(path)["_config_path"], path)

    def test_env_var_overrides_file(self):
        path = self.write('{"itad_api_key": "from-file"}')
        old = os.environ.get("ITAD_API_KEY")
        os.environ["ITAD_API_KEY"] = "from-env"
        try:
            self.assertEqual(load_config(path)["itad_api_key"], "from-env")
        finally:
            if old is None:
                os.environ.pop("ITAD_API_KEY", None)
            else:
                os.environ["ITAD_API_KEY"] = old


class ApiKeyTest(unittest.TestCase):
    def test_missing_key_raises(self):
        with self.assertRaises(ConfigError):
            api_key({})

    def test_blank_key_raises(self):
        with self.assertRaises(ConfigError):
            api_key({"itad_api_key": "   "})

    def test_key_is_stripped(self):
        self.assertEqual(api_key({"itad_api_key": " abc "}), "abc")


class ParseRateLimitTest(unittest.TestCase):
    def test_text_form(self):
        self.assertEqual(parse_rate_limit("800 / 300s"), (800, 300))

    def test_loose_spacing_and_missing_unit(self):
        self.assertEqual(parse_rate_limit(" 150/300 "), (150, 300))

    def test_sequence_form(self):
        self.assertEqual(parse_rate_limit([10, 20]), (10, 20))

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(parse_rate_limit("nonsense"), (800, 300))
        self.assertEqual(parse_rate_limit(None), (800, 300))
        self.assertEqual(parse_rate_limit("1/2"), (1, 2))


class ResolvePathTest(unittest.TestCase):
    def test_relative_resolves_against_repo_root(self):
        self.assertEqual(resolve_path({"state_path": "data/state.json"}, "state_path"),
                         ROOT / "data" / "state.json")

    def test_absolute_is_kept(self):
        # 跨平台取一个真绝对路径当夹具：`C:/abs/...` 在 Windows 是绝对路径、
        # 在 Linux 上 is_absolute() 为 False 会被当相对路径拼进 ROOT（CI 实测翻车）
        target = Path(tempfile.gettempdir()) / "abs" / "x.json"
        self.assertEqual(resolve_path({"p": str(target)}, "p"), target)

    def test_unset_raises(self):
        for value in (None, ""):
            with self.assertRaises(ConfigError):
                resolve_path({"p": value}, "p")


if __name__ == "__main__":
    unittest.main()
