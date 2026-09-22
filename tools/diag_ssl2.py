# -*- coding: utf-8 -*-
"""诊断：系统证书库导出 + 真实对端证书是谁签的。"""
from __future__ import annotations

import socket
import ssl
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "probe" / "_ssl_diag2.txt"
_lines: list[str] = []


def p(t: str = "") -> None:
    _lines.append(t)
    OUT.write_text("\n".join(_lines), encoding="utf-8")


def _hook(t, v, tb):
    p("异常：")
    p("".join(traceback.format_exception(t, v, tb)))


sys.excepthook = _hook

p("=== 1. ssl.enum_certificates 原始返回值样例 ===")
try:
    roots = list(ssl.enum_certificates("ROOT"))
    cas = list(ssl.enum_certificates("CA"))
    p(f"  ROOT 证书数={len(roots)} · CA 证书数={len(cas)}")
    if roots:
        der, enc, trust = roots[0]
        p(f"  第一条：len(der)={len(der)} encoding={enc!r} trust={trust!r}")
    from collections import Counter
    p(f"  encoding 取值分布 ROOT={Counter(e for _, e, _ in roots)}")
except Exception as exc:  # noqa: BLE001
    p(f"  失败：{exc}")

p("")
p("=== 2. 导出函数结果 ===")
from src.httpclient import CA_CACHE, system_ca_bundle  # noqa: E402

if CA_CACHE.exists():
    CA_CACHE.unlink()
path = system_ca_bundle()
p(f"  system_ca_bundle() -> {path!r}")
if path and Path(path).exists():
    text = Path(path).read_text(encoding="ascii")
    p(f"  文件大小={Path(path).stat().st_size} 字节 · 证书块数={text.count('BEGIN CERTIFICATE')}")

p("")
p("=== 3. 对端证书是谁签的（看有没有被拦截）===")
for host in ("store.steampowered.com", "api.isthereanydeal.com", "open.er-api.com"):
    try:
        ctx = ssl.create_default_context()   # 走 Windows 证书库
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        issuer = dict(x[0] for x in cert.get("issuer", []))
        subject = dict(x[0] for x in cert.get("subject", []))
        p(f"  {host}")
        p(f"      对端 subject={subject.get('commonName')!r}")
        p(f"      签发者 issuer={issuer.get('commonName')!r} / {issuer.get('organizationName')!r}")
    except Exception as exc:  # noqa: BLE001
        p(f"  {host} 失败：{type(exc).__name__}: {str(exc)[:120]}")

p("")
p("读法：issuer 如果是本地代理工具的名字（不是 DigiCert/Let's Encrypt 之类），")
p("      就说明 TLS 被本机代理拦截，必须信任它的根证书。")
