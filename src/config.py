# -*- coding: utf-8 -*-
"""配置读取（对应 docs/DEVELOPMENT.md §9）。

key 优先取环境变量 ``ITAD_API_KEY``（GitHub Actions 用），否则取 ``config.json``。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"

DEFAULTS: dict = {
    "itad_api_key": "",
    "country": "CN",
    "compare_countries": ["UA", "IN"],
    "min_cut": 0,
    "max_price": None,
    "min_positive_ratio": 0.7,
    "min_review_count": 100,
    "only_type": "game",
    "exclude_mature": True,
    "exclude_free": True,
    "expired_retention_days": 7,
    "upcoming_expiry_hours": 48,
    "week_window_days": 14,
    "stale_banner_hours": 36,
    "timezone": "Asia/Shanghai",
    "state_path": "data/state.json",
    "output_dir": "output",
    "request_pause_seconds": 0.3,
    "request_timeout_seconds": 25,
    "steam_timeout_seconds": 15,
    "notable_review_count": 10000,
    "absolute_min_positive_ratio": None,
    "steam_lang": "schinese",
    "itad_rate_limit": "800 / 300s",
    "itad_min_interval": 0.3,
    "steam_rate_limit": "150 / 300s",
    "steam_min_interval": 2.0,
    "max_concurrency": 1,
    "fetch_last_low_time": True,
    "use_steam_side_fetch": False,
    "reviews_ttl_days": 7,
    "reviews_empty_ttl_days": 3,
    "sweep_mode": "low_only",
    "detail_scope": "catalog",
    "detail_daily_budget": 300,
    "prefetch_daily_budget": 300,
    "probe_timeout_seconds": 6,
    "fx_cache_path": "data/fx_cache.json",
    "steam_batch_size": 20,
    "page_size_mobile": 10,
    "page_size_desktop": 20,
    "mobile_breakpoint_px": 768,
    "max_deals": None,
    "run_log_keep": 30,
}

# 环境变量覆盖表：env 名 -> 配置键
ENV_OVERRIDES = {
    "ITAD_API_KEY": "itad_api_key",
}

_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*s?\s*$")


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def load_config(path: str | Path | None = None) -> dict:
    """读取 config.json 并与默认值合并；文件不存在时只用默认值。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    user_cfg: dict = {}
    if cfg_path.exists():
        try:
            user_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{cfg_path}（{exc}）") from exc
        if not isinstance(user_cfg, dict):
            raise ConfigError(f"配置文件顶层必须是对象：{cfg_path}")

    cfg = dict(DEFAULTS)
    cfg.update(user_cfg)

    for env_name, key in ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name)
        if env_value:
            cfg[key] = env_value

    cfg["_config_path"] = cfg_path
    return cfg


def api_key(cfg: dict) -> str:
    """返回 ITAD API key；缺失时抛 ConfigError（不允许静默跑空报表）。"""
    key = (cfg.get("itad_api_key") or "").strip()
    if not key:
        raise ConfigError(
            "缺少 ITAD API key：请在 config.json 填写 itad_api_key，"
            "或设置环境变量 ITAD_API_KEY"
        )
    return key


def parse_rate_limit(text: str, default: tuple[int, int] = (800, 300)) -> tuple[int, int]:
    """解析 ``"800 / 300s"`` → ``(800, 300)``。"""
    if isinstance(text, (list, tuple)) and len(text) == 2:
        return int(text[0]), int(text[1])
    m = _RATE_RE.match(str(text or ""))
    if not m:
        return default
    return int(m.group(1)), int(m.group(2))


def resolve_path(cfg: dict, key: str) -> Path:
    """把配置里的相对路径解析成相对仓库根目录的绝对路径。"""
    raw = cfg.get(key)
    if not raw:
        raise ConfigError(f"配置项 {key} 未设置")
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p