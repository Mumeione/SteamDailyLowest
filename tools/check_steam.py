# -*- coding: utf-8 -*-
"""检查 Steam 各端点是否已恢复（用项目自己的客户端 + 限流器，绝不裸打）。"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config, parse_rate_limit  # noqa: E402
from src.httpclient import HttpError  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402
from src.steam import SteamClient  # noqa: E402

OUT = ROOT / "data" / "probe" / "_steam_health.txt"
_lines: list[str] = []


def p(t: str = "") -> None:
    _lines.append(t)
    OUT.write_text("\n".join(_lines), encoding="utf-8")


def _hook(t, v, tb):
    p("异常：")
    p("".join(traceback.format_exception(t, v, tb)))


sys.excepthook = _hook

cfg = load_config()
calls, window = parse_rate_limit(cfg["steam_rate_limit"], default=(150, 300))
client = SteamClient(
    limiter=RateLimiter("steam", calls, window, min_interval=float(cfg["steam_min_interval"])),
    timeout=8,
    pause=0.0,
    log=lambda m: None,
)

p("Steam 端点健康检查（限流器 2 秒间隔）")
p("")

p("1) 单个 appdetails（中文名 + 国区价）")
try:
    info = client.info(1091500, cc="CN")
    p(f"   -> {info}")
except HttpError as exc:
    p(f"   -> 失败：{exc}")

p("2) 批量 appdetails（跨区价格，3 个 appid）")
try:
    prices = client.prices([1091500, 447040, 1658920], "UA")
    p(f"   -> {prices}")
except HttpError as exc:
    p(f"   -> 失败：{exc}")

p("3) appreviews（兜底好评率）")
try:
    reviews = client.reviews(1091500)
    p(f"   -> {reviews}")
except HttpError as exc:
    p(f"   -> 失败：{exc}")

p("")
p(f"合计 {client.calls} 次请求 · 429={client.rate_limit_events} · 网络错误={client.network_errors}")
