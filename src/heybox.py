# -*- coding: utf-8 -*-
"""小黑盒游戏资料客户端（``api.xiaoheihe.cn``，2026-10-09 新增）。

定位：**社区中文名**的补源 —— 官方中文名（Steam ``l=schinese``）缺位时，
给 ``tools/backfill_cn_names.py`` 逐条取小黑盒的编辑/社区中文名。
只服务回填脚本；**日常管线（run.py / enrich）不发任何小黑盒请求**
（用户 2026-10-09 定案：daily 不等这个慢源，中文名只走回填）。

两条实测得出的关键结论（2026-10-09 本机真机探测）：

1. ``GET /game/web/get_game_detail/?_time=<unix秒>&appid=<appid>``
   **免 hkey、免登录**即可拿到 ``result.name``（实测 appid 289070 → ``"文明6"``）；
   一旦带上 ``os_type=web&version=...`` 参数组合**会**强制要求 ``hkey``
   （签名算法不公开且常变）→ 本客户端**只用**免 hkey 形态，绝不加那两个参数。
2. ``name`` 对**没有中文译名**的游戏返回英文/日文原名 → 调用方必须过
   :func:`is_cn_name` 质量闸（含 CJK 汉字且不含假名）才算命中，
   否则就是把英文原名当「中文名」写进库。

⚠️ 非公开接口：无 SLA、无配额文档 → 宁慢勿封（回填默认 2s/req），
风控信号（403/429）由底座升级为 :class:`HeyboxBlocked` 中止本轮。
海外 GitHub runner 上的可达性/风控强度尚未验证（首轮 Actions 探针 ``--limit 5``）。

架构约定（架构检查卡片 01）：继承 :class:`BaseHttpClient` **必须挂**
``error_cls`` / ``blocked_cls`` —— 否则调用方的 ``except HeyboxError`` 静默失效。
"""

from __future__ import annotations

import re
import time
from typing import Callable

from . import classify
from .httpclient import BaseHttpClient, Blocked, HttpError

BASE = "https://api.xiaoheihe.cn"
#: 免 hkey 的详情端点（相对 :data:`BASE`）
PATH = "/game/web/get_game_detail/"

#: 浏览器形 UA（真机探测用它通过；底座默认 UA 未在小黑盒验证过）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) SteamDailyLowest-backfill/1.0"
)

#: CJK 汉字判定用唯一出处 :data:`classify.CJK_RE`（与 ``report.clean_title_zh``
#: 同源）；假名排除是本客户端特有的质量闸，留在本地。
#: 日文假名（ひらがな \\u3040-309f、カタカナ \\u30a0-30ff、カタカナ補助 \\u31f0-31ff、
#: 半角カタカナ \\uff66-\\uff9f）—— 真中文名永远不含假名。
_KANA_RE_CHARS = "\\u3040-\\u30ff\\u31f0-\\u31ff\\uff66-\\uff9f"

_CJK_RE = classify.CJK_RE
_KANA_RE = re.compile("[" + _KANA_RE_CHARS + "]")


class HeyboxError(HttpError):
    """小黑盒请求失败（不可重试或重试耗尽）。"""


class HeyboxBlocked(Blocked, HeyboxError):
    """连续 403 —— 滥用封禁，中止本轮并告警。"""


def has_cjk(text: str | None) -> bool:
    """文本是否含至少一个 CJK 汉字（茧 / 文明6 / 隻狼 都算）。"""
    return bool(_CJK_RE.search(text or ""))


def is_cn_name(name: str | None) -> bool:
    """返回名是否为**有效中文名**（回填入库的唯一质量闸，2026-10-09 定案）。

    - **含 ≥1 个 CJK 汉字**：单字中文名合法（用户举例《茧》COCOON），
      不能沿用 ``report.clean_title_zh`` 剥英文后 ``>= 2`` 的口径 —— 那是
      「剥完还剩多少」的另一回事。
    - **不含假名**：小黑盒对无译名的日文游戏返回原名（如 ``魔女の旅``），
      含汉字但也有假名 —— 写进 ``title_zh`` 等于把日文原名冒充中文名，拒。
      纯汉字日文原名（如 ``東方Project``）会过闸 —— 无害：它本来就是
      原名，展示等同英文回落。
    - 繁体**原样接受**（2026-10-09 定案）：不翻译、不转换；heybox 编辑名
      以简体为主，繁体只出现在仅有官方繁中的游戏。
    """
    if not name or not name.strip():
        return False
    return bool(_CJK_RE.search(name)) and not _KANA_RE.search(name)


def parse_name(payload) -> str | None:
    """把 ``get_game_detail`` 响应收敛成 ``(name)``；取不到 → ``None``（当落空）。

    响应形态（2026-10-09 实测）::

        {"status": "ok", "msg": "", "version": "1.0",
         "result": {"name": "文明6", "publishers": [...], ...}}

    ``status != "ok"`` / 缺 ``result`` / ``name`` 为空 → ``None``。
    ⚠️ 这是「没拿到」而不是「请求失败」—— 参数错不该走到这里（URL 只有
    ``_time``/``appid`` 两个参数，没有会变的筛选条件）。
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    name = result.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


class HeyboxClient(BaseHttpClient):
    """小黑盒 HTTP 客户端：限流 + 五种响应处理全部继承底座。

    **风控零容忍**（``abort_on_risk_control = True``）：403/429 第一次出现就抛
    :class:`HeyboxBlocked`，不退避不等待 —— 小黑盒无官方配额文档，等待重试
    只会白烧回填的墙钟预算、还会加深风控印象；调用方（回填脚本）捕获
    ``Blocked`` 后立即落盘退出。403×2 → Blocked 的底座默认策略对其他客户端
    （ITAD/Steam）保持不变。
    """

    BASE = BASE
    error_cls = HeyboxError
    blocked_cls = HeyboxBlocked
    abort_on_risk_control = True

    def __init__(self, limiter, timeout: float = 25, pause: float = 0.0,
                 session=None, max_attempts: int = 4,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda msg: None,
                 user_agent: str = DEFAULT_USER_AGENT):
        super().__init__(
            limiter=limiter, timeout=timeout, pause=pause, session=session,
            max_attempts=max_attempts, sleep=sleep, log=log, user_agent=user_agent,
        )

    def _endpoint(self, path: str) -> str:
        return "heybox"

    # ------------------------------------------------------------------
    # 业务端点
    # ------------------------------------------------------------------
    def fetch_name(self, appid: int) -> str | None:
        """按 appid 取小黑盒中文名；取不到（无译名/无此游戏）返回 ``None``。

        ``_time`` 用当前 Unix 秒（接口要求；实测校验不严，但别赌）。
        底座语义对齐：403×2 → :class:`HeyboxBlocked`；429/软限流重试降速；
        ``max_attempts`` 耗尽 → :class:`HeyboxError` 上抛，由调用方决定
        「跳过计数」还是「连续失败中止」。
        """
        payload = self.request("GET", PATH, params={
            "_time": int(time.time()),
            "appid": str(int(appid)),
        })
        return parse_name(payload)


__all__ = [
    "BASE",
    "DEFAULT_USER_AGENT",
    "PATH",
    "HeyboxBlocked",
    "HeyboxClient",
    "HeyboxError",
    "has_cjk",
    "is_cn_name",
    "parse_name",
]
