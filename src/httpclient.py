# -*- coding: utf-8 -*-
"""共用的 HTTP 客户端底座（对应 docs/DEVELOPMENT.md §3.3 / §10）。

ITAD 与 Steam 两个宿主都要同一套「限流 + 五种响应分开处理」的策略。
早前 `itad.py` 里自己实现了一份，加 `steam.py` 时如果再抄一份，
两处策略必然漂移 —— 所以抽到这里，两个客户端只保留各自端点的解析。

只负责网络，不做判定（§6 职责边界）。**绝不轮换 IP 规避限流。**

===============  ==========================================================
响应/异常         处理
===============  ==========================================================
429               尊重 `Retry-After`，否则从 10 秒起退避；同时自动降速
403               **滥用封禁，比 429 严重**：等 5 分钟；连续 2 次则中止本轮
200 + body=null   **软限流**（不报错，悄悄限你）→ 按限流处理，绝不当"没数据"
200 + success:false  **不是限流**：目标不存在 / 该区不售 → 返回 None 让调用方跳过
5xx               服务端故障，**与限流无关** → 指数退避重试，不计入限流统计
网络异常          重试 `max_attempts` 次
===============  ==========================================================
"""

from __future__ import annotations

import base64
import os
import platform
import random
import ssl
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

#: 从 Windows 证书库导出的 CA bundle 放这里（gitignored，环境相关）
CA_CACHE = Path(__file__).resolve().parent.parent / "data" / "ca_bundle.pem"


def system_ca_bundle(cache_path: Path = CA_CACHE) -> str | None:
    """把 Windows 证书库里的根证书导成一个 PEM，供 `requests` 当 CA 用。

    **为什么需要**：`requests` 默认只信任 `certifi` 那份证书列表，
    而本机出网经过了 TLS 拦截（本地代理工具），它的根证书**只装在 Windows 证书库里**，
    certifi 里没有 → 报 `SSLError: CERTIFICATE_VERIFY_FAILED`。
    同一个 URL 用 `urllib` 却能通，因为 `ssl.create_default_context()`
    会加载 Windows 证书库。排查脚本见 `tools/diag_ssl2.py`（它打印了对端证书的签发者，
    一眼就能看出是 SteamTools 在中间人）。

    ⚠️ 这不是"关掉校验"：仍然做完整校验，只是把**可信来源**换成系统证书库
    ——这也是浏览器和 curl 用的那套。拿不到就返回 None，调用方回落到 requests 默认行为。
    """
    if platform.system() != "Windows":
        return None
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return str(cache_path)
    try:
        certs = list(ssl.enum_certificates("ROOT")) + list(ssl.enum_certificates("CA"))
    except (AttributeError, OSError, ssl.SSLError):
        return None
    blocks: list[str] = []
    seen: set[bytes] = set()
    for der, encoding, _ in certs:
        # ⚠️ 文档里这个字段的值是 'x509_asn'（不是 'x509'）——
        # 写错一个字符就会一个证书都导不出来、然后静默退回 certifi。
        if encoding not in ("x509_asn", "x509") or der in seen:
            continue
        seen.add(der)
        body = base64.b64encode(der).decode("ascii")
        blocks.append("-----BEGIN CERTIFICATE-----\n"
                      + "\n".join(textwrap.wrap(body, 64))
                      + "\n-----END CERTIFICATE-----\n")
    if not blocks:
        return None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("".join(blocks), encoding="ascii")
    except OSError:
        return None
    return str(cache_path)


def _default_verify() -> bool | str:
    """优先用环境指定的 CA，其次用系统证书库，最后交给 requests 默认（certifi）。"""
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        value = os.environ.get(name)
        if value and Path(value).exists():
            return value
    bundled = system_ca_bundle()
    return bundled if bundled else True


class HttpError(RuntimeError):
    """请求失败（不可重试或重试耗尽）。"""


class Blocked(HttpError):
    """连续 403 —— 滥用封禁，中止本轮并告警。"""


class BaseHttpClient:
    #: 子类覆盖成各自 API 的根地址
    BASE = ""

    def __init__(
        self,
        limiter,
        timeout: float = 25,
        pause: float = 0.0,
        session: requests.Session | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda msg: None,
        user_agent: str = "SteamDailyLowest/1.0 (+https://github.com/)",
    ):
        self.limiter = limiter
        self.timeout = timeout
        self.pause = pause
        self.max_attempts = max(2, int(max_attempts))
        self._sleep = sleep
        self._log = log
        self.session = session or self._new_session(user_agent)

        self.calls = 0
        self.rate_limit_events = 0
        self.server_errors = 0
        self.network_errors = 0
        self.soft_null_events = 0
        self.events: list[dict] = []
        self._consecutive_403 = 0

    def _new_session(self, user_agent: str) -> requests.Session:
        s = requests.Session()
        # 本机有 http_proxy 环境变量会导致直连失败，统一不走环境代理（与探针一致）
        s.trust_env = False
        # 但**证书来源**要跟着环境走：系统证书库（见 system_ca_bundle 的说明）
        s.verify = _default_verify()
        s.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        return s

    # ------------------------------------------------------------------
    # 子类可覆盖的钩子
    # ------------------------------------------------------------------
    def _prepare(self, path: str, params: dict | None) -> tuple[str, dict]:
        """返回 ``(完整 URL, 最终 query 参数)``。子类在这里加 api key 等。"""
        return self.BASE + path, dict(params or {})

    def _endpoint(self, path: str) -> str:
        """给错误信息用的可读端点名。"""
        return path

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------
    def _record(self, kind: str, path: str, **extra: Any) -> None:
        event = {"kind": kind, "path": path, "at": _now_iso()}
        event.update(extra)
        self.events.append(event)
        self._log(f"[{self._endpoint(path)}] 异常事件 {kind} {path} {extra}")

    def _backoff_wait(self, seconds: float) -> None:
        self.limiter.wait(seconds)

    def request(self, method: str, path: str, params: dict | None = None, json_body: Any = None) -> Any:
        """发一次请求并返回解析后的 JSON；重试耗尽则抛 :class:`HttpError`。"""
        url, query = self._prepare(path, params)

        attempt = 0
        backoff = 10.0
        while True:
            attempt += 1
            self.limiter.acquire()
            if self.pause:
                self._sleep(self.pause)
            try:
                resp = self.session.request(
                    method, url, params=query, json=json_body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                self.network_errors += 1
                self._record("network", path, error=str(exc), attempt=attempt)
                if attempt >= self.max_attempts:
                    raise HttpError(f"网络异常，重试 {attempt} 次仍失败：{exc}") from exc
                self._backoff_wait(min(2 ** attempt + random.uniform(0, 1), 60))
                continue

            self.calls += 1
            status = resp.status_code

            if status == 200:
                text = resp.text.strip()
                if text == "null":
                    # 软限流：不报错，悄悄限你 —— 必须当限流处理
                    self.soft_null_events += 1
                    self.rate_limit_events += 1
                    self._record("soft_null", path, attempt=attempt)
                    if attempt >= self.max_attempts:
                        raise HttpError(f"连续软限流（body=null），放弃：{path}")
                    self.limiter.slow_down()
                    self._backoff_wait(backoff)
                    backoff = min(backoff * 2, 300)
                    continue
                try:
                    payload = resp.json()
                except ValueError as exc:
                    self._record("bad_json", path, attempt=attempt, body=text[:200])
                    if attempt >= self.max_attempts:
                        raise HttpError(f"响应不是 JSON：{path}") from exc
                    self._backoff_wait(backoff)
                    backoff = min(backoff * 2, 300)
                    continue
                self._consecutive_403 = 0
                if isinstance(payload, dict) and payload.get("success") is False:
                    # 不是限流：目标不存在 / 该区不售
                    self._record("success_false", path, reason=payload.get("reason_phrase"))
                    return None
                return payload

            if status == 429:
                self.rate_limit_events += 1
                self.limiter.slow_down()
                retry_after = _retry_after_seconds(resp)
                wait = retry_after if retry_after is not None else backoff
                self._record("429", path, attempt=attempt, wait=round(wait, 1))
                if attempt >= self.max_attempts:
                    raise HttpError(f"429 限流，重试 {attempt} 次仍失败：{path}")
                self._backoff_wait(wait)
                backoff = min(backoff * 2, 300)
                continue

            if status == 403:
                self._consecutive_403 += 1
                self.rate_limit_events += 1
                self._record("403", path, attempt=attempt, consecutive=self._consecutive_403)
                if self._consecutive_403 >= 2:
                    raise Blocked(f"连续收到 403（滥用封禁），中止本轮并告警：{path}")
                if attempt >= self.max_attempts:
                    raise HttpError(f"403 封禁：{path}")
                self.limiter.slow_down()
                self._backoff_wait(300)  # 社区做法：等 5 分钟
                continue

            if 500 <= status < 600:
                # 服务端故障，与限流无关：重试但不计入限流统计
                self.server_errors += 1
                self._record("5xx", path, status=status, attempt=attempt)
                if attempt >= self.max_attempts:
                    raise HttpError(f"HTTP {status}，重试 {attempt} 次仍失败：{path}")
                self._backoff_wait(min(2 ** attempt + random.uniform(0, 1), 60))
                continue

            raise HttpError(f"HTTP {status}：{path}")

    def stats(self) -> dict:
        return {
            "requests": self.calls,
            "rate_limit_events": self.rate_limit_events,
            "soft_null_events": self.soft_null_events,
            "server_errors": self.server_errors,
            "network_errors": self.network_errors,
            "limiter": self.limiter.stats(),
        }


def _retry_after_seconds(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
