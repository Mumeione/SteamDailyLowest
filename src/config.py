# -*- coding: utf-8 -*-
"""配置读取（对应 docs/DEVELOPMENT.md §9）。

key 优先取环境变量 ``ITAD_API_KEY``（GitHub Actions 用），否则取 ``config.json``。
"""

from __future__ import annotations

import copy
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
    # 好评率的「高分档」线（卡片上好评率分档上色用：≥ 它是高分色，≥ min_positive_ratio 是普通色）
    "good_positive_ratio": 0.9,
    "min_review_count": 100,
    "only_type": "game",
    "exclude_mature": True,
    "exclude_free": True,
    "expired_retention_days": 7,
    "upcoming_expiry_hours": 48,
    "week_window_days": 14,
    # 顶部消息区的「数据陈旧」两档阈值（refs.md B2：>26h 黄 = Actions 延迟 /
    # >36h 红 = 今天压根没更新）。`stale_banner_hours` 是红档，键名沿用不改
    # （relocate 会让 Actions 里的旧配置静默失配）。
    "stale_banner_hours": 36,
    "stale_warn_hours": 26,
    # 首页顶部「节日/活动条 + 站点通知」的内容文件（仓库内手工维护，见
    # src/announcements.py；文件坏了只是不显示顶部条，不影响出报表）
    "announcements_path": "content/announcements.json",
    "timezone": "Asia/Shanghai",
    "state_path": "data/state.json",
    "expiring_snapshot_path": "data/expiring.json",
    "output_dir": "output",
    "request_pause_seconds": 0.3,
    "request_timeout_seconds": 25,
    # 一轮运行的**总墙钟预算**（秒；0 = 不限）。取「job timeout − 5 分钟」：到点主动
    # 中止，走正常失败路径（退出码 5 + 首版报表兜底 + 下一轮自愈），而不是被 job 的
    # timeout-minutes 硬杀（那会漏发布报表、漏回写状态，且走 cancelled 不告警）。
    # 5100 = 85 分钟，卡在 daily 的 90 分钟前；大促末尾实测峰值约 19 分钟（限流主导，
    # 见 run_log 的 steam_wait），余量充足。
    "http_budget_seconds": 5100,
    "steam_timeout_seconds": 15,
    "notable_review_count": 10000,
    "absolute_min_positive_ratio": None,
    "steam_lang": "schinese",
    "itad_rate_limit": "800 / 300s",
    "itad_min_interval": 0.3,
    "steam_rate_limit": "150 / 300s",
    "steam_min_interval": 2.0,
    # api.steampowered.com（GetItems）独立限流通道：与 store 域是否共桶判定不了，
    # 新开一条（重构 spec §3.1）；实测 0.14~0.36s/次 → 最小间隔可比 store 域小得多
    "steam_browse_rate_limit": "150 / 300s",
    "steam_browse_min_interval": 0.5,
    "fetch_last_low_time": True,
    # ---- S6 评价刷新（折扣感知四档 TTL，spec §3.2 / 决策 16）----
    # 原 reviews_ttl_days / reviews_empty_ttl_days 已废弃：派生欠账不再用全局 TTL，
    # 改为「新游 1 天 / 到期窗口 1 天 / 折扣期普通 3 天 / 非折扣期不刷新」
    "new_game_days": 30,
    "new_game_refresh_days": 1,
    "discount_refresh_days": 3,
    "expiry_refresh_days": 1,
    # 详情抓取失败的冷却天数（原复用 reviews_empty_ttl_days，语义拆独立）
    "detail_retry_cooldown_days": 3,
    "sweep_mode": "low_only",
    # GetItems 失效时 info/v2 逐条降级的每轮上限（1 请求/条，必须设界；
    # GetItems 正常时该路径为空）。额度按 key 计 1000 请求/5min 滑窗，实测无压力
    "detail_fallback_budget": 1000,
    "prefetch_daily_budget": 300,
    "probe_timeout_seconds": 6,
    "fx_cache_path": "data/fx_cache.json",
    "steam_batch_size": 20,
    "page_size_mobile": 10,
    "page_size_desktop": 20,
    # S9-卡片（用户 2026-10-07：「现在的档位太多了，只要手机、平板、pc 三个」）：
    # mobile = 手机竖屏上限（≤ 它是手机档）；tablet = 平板上限（≤ 它是平板档）
    # 手机只有竖屏 / 平板横竖屏都要好用 / PC 只有横屏。CSS 的 @media 必须与这两个值一致。
    # 手机上限 600：常见手机竖屏最大约 430px，600 足够；**768（iPad 竖屏）算平板**
    # —— 用户 2026-10-07：「平板缩小一点可以放两列」，而 768 卡在原来那条线上会掉进手机档。
    "mobile_breakpoint_px": 600,
    "tablet_breakpoint_px": 1100,
    "max_deals": None,
    "run_log_keep": 30,
    # ---- 首页 / 大卡 / 列表的阈值（2026-10-09 收编）----
    # ⚠️ 这几个键原先**只活在 report.py 的 DEFAULT_* 常量与读点字面量里**，
    # 不在本表：`home_new_low_days` / `big_cut_percent` 等在 config.example.json 里
    # 有、用户的 config.json 里没有 ⇒ 实际生效的是 report.py 那份第二张表。
    # 结果是「改一个 48 要动 5 处」，而且两张表可以悄悄不一致。
    # 现在这里是**唯一来源**：report.py 的同名常量只是 `DEFAULTS[...]` 的别名。
    "home_new_low_days": 7,
    "big_cut_percent": 80,
    "home_picks": 15,
    "home_section_preview": 10,
    "home_section_preview_min": 5,
    "list_batch": 30,
    "list_auto_max": 300,
    "bad_positive_ratio": 0.4,
    # 精选/大卡打分的「前置门槛」（§10.2）：有评价数 · 好评率 ≥ min_rate · 评价数 ≥ min_count
    "recommend_min_rate": 70,
    "recommend_min_count": 100,
    # 两张权重表**不在配置里定义默认值**：默认档是 report.py 的 PICKS_WEIGHTS /
    # RECOMMEND_WEIGHTS 代码常量，配置只做「按名覆盖缺项」（见 report._resolve_weights）。
    # 这里显式登记为 None，是为了让「谁会被读」这件事在单表里可见（示例配置里就有这两个键）。
    "recommend_weights": None,
    "picks_weights": None,
    "site_repo_url": "https://github.com/Mumeione/SteamDailyLowest",
    "site_actions_url": "https://github.com/Mumeione/SteamDailyLowest/actions",
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

    #: ⚠️ **深拷贝**（code-audit-2026-10-09 #11）：`dict(DEFAULTS)` 是浅拷贝，
    #: `compare_countries`（list）等嵌套值与模块级 DEFAULTS **共享引用** ——
    #: 调用方一句 `cfg["compare_countries"].append(...)` 就会污染默认表、跨测试串味。
    cfg = copy.deepcopy(DEFAULTS)
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