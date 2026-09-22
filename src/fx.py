# -*- coding: utf-8 -*-
"""汇率（对应 docs/DEVELOPMENT.md §2.3）。

UA→UAH、IN→INR、CN→CNY 三个币种不统一，算「相对国区的差价百分比」必须先换汇。
**每天只取一次并缓存**；页面上必须标注汇率数值与取数日期（§7.4）。

汇率源用 `open.er-api.com`（免费、无需 key）。实测两个候选：

============  ==========  ==========================================
源             是否有 UAH  说明
============  ==========  ==========================================
open.er-api   ✅ 有       返回 `time_last_update_utc`，可直接当取数日期
frankfurter   ❌ 没有     基于 ECB 的币种表，**不含 UAH**，本项目不能用
============  ==========  ==========================================

只负责网络与解析，不做判定（§6 职责边界）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

RATES_URL = "https://open.er-api.com/v6/latest/{base}"
BASE_CURRENCY = "CNY"
USER_AGENT = "SteamDailyLowest/1.0 (+https://github.com/)"
TIMEOUT_SECONDS = 10


class FxError(RuntimeError):
    """汇率拿不到（连缓存也没有）时抛出。"""


def _http_json(url: str, timeout: float = TIMEOUT_SECONDS) -> dict:
    # 本机有 http_proxy 环境变量会导致直连失败，统一绕开（与 itad/steam 客户端一致）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_rates(base: str = BASE_CURRENCY, timeout: float = TIMEOUT_SECONDS) -> dict:
    """从汇率源取一次最新汇率。

    返回 ``{"base": "CNY", "date": "YYYY-MM-DD", "rates": {"UAH": 6.67, ...}}``，
    其中 ``rates[X]`` 的含义是 **1 个 base 能换多少 X**。
    """
    payload = _http_json(RATES_URL.format(base=base), timeout)
    if payload.get("result") not in (None, "success"):
        raise FxError(f"汇率源返回失败：{payload.get('error-type') or payload}")
    rates = payload.get("rates")
    if not isinstance(rates, dict) or base not in rates:
        raise FxError("汇率源返回的结构不认识（缺少 rates）")
    stamp = payload.get("time_last_update_utc") or ""
    date_text = stamp[:16] if stamp else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        date_iso = datetime.strptime(stamp[:16], "%a, %d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "base": payload.get("base_code") or base,
        "date": date_iso,
        "date_text": date_text,
        "rates": {k: float(v) for k, v in rates.items() if isinstance(v, (int, float))},
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def load_or_fetch(cache_path: str | Path, *, today: str, log: Callable[[str], None] = lambda m: None,
                  timeout: float = TIMEOUT_SECONDS) -> dict:
    """当天已有缓存就直接用，否则取一次并写回缓存。

    汇率源挂了也不会让整轮失败（返回缓存里的旧值），但会把事件写进日志。
    """
    path = Path(cache_path)
    cached: dict | None = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log(f"[warn] 汇率缓存读不了，将重新获取：{exc}")
            cached = None

    if cached and cached.get("date") == today and cached.get("base") == BASE_CURRENCY:
        log(f"[fx] 命中当天缓存：{today}（USD={cached['rates'].get('USD')}）")
        return cached

    try:
        fresh = fetch_rates(BASE_CURRENCY, timeout)
    except (urllib.error.URLError, OSError, ValueError, FxError) as exc:
        if cached:
            log(f"[warn] 汇率源取失败，沿用缓存（{cached.get('date')}）：{exc}")
            return cached
        raise FxError(f"汇率源取失败且没有可用缓存：{exc}") from exc

    fresh["date"] = today  # 以「本轮的今天」为准，避免时区把日期算差
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fresh, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"[fx] 已取汇率并缓存：{today} UAH={fresh['rates'].get('UAH')} INR={fresh['rates'].get('INR')}")
    return fresh


def to_base_minor(amount_minor: int | None, currency: str | None, rates: dict) -> int | None:
    """把某个币种的最小单位金额换算成 base（CNY）的最小单位（分）。

    ``rates[X]`` = 1 CNY 换多少 X，所以 ``X → CNY`` 是**除以**它。
    例：UA 45₴ = ``4500`` 戈比，``rates["UAH"] = 6.674`` → ``4500 / 6.674 ≈ 674`` 分 = ¥6.74。
    ``rates`` 缺少该币种或就是 base 时按原值/None 处理，绝不猜。
    """
    if amount_minor is None or not currency:
        return None
    if not isinstance(rates, dict):
        return None
    if currency == (rates.get("base") or BASE_CURRENCY):
        return int(amount_minor)
    rate = rates.get("rates", {}).get(currency)
    if not rate:
        return None
    return int(round(amount_minor / float(rate)))
