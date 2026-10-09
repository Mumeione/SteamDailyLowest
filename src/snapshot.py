# -*- coding: utf-8 -*-
"""「即将过期」快照导出（给外部数据管道消费，``.scratch/expiring-snapshot/spec.md``）。

与 ``output/latest.json`` 的区别：那个是报表的伴生产物、随 Pages 发布；
这个是**跨仓库的数据契约**（``data/expiring.json``），落 data 分支、与 state.json 同级。

口径边界：导出的是窗口内**已进列表**（``is_shown``）的条目（已合并详情、已按 appid
去重、已过口碑分档）——口碑门槛就是本仓库的进列表口径，口径单点收敛在这里，
消费方不再自建近似门槛（版本演化见 .scratch/expiring-snapshot/changelog.md）。

**当前契约 `SNAPSHOT_VERSION = 3`**（2026-09-30）：在 v2 之上加了 ``low_class`` /
``publishers`` / ``developers`` / ``stats`` 四个字段，供消费方做「Steam 口径新史低 +
分位排序」选稿。**消费方必须按 `version` 分支处理**，不要硬编码 `== 3`。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import classify
from .config import DEFAULTS
from .state import atomic_write_json

SNAPSHOT_VERSION = 3

#: 导出字段（写出的键顺序即此顺序，便于人工 diff）。
#: ``compare`` 来自 enrich 阶段、原样透传：输入已是进列表条目，理论上都该有比价，
#: 个别为 null（某些区无售或拉取失败）—— 消费方降级省略比价行即可，不必过滤条目。
#:
#: v3（2026-09-30）新增四个字段，理由见 `.scratch/expiring-snapshot/spec.md` §6
#: 与 `changelog.md` v4（原 v3-plan.md 已按脱敏口径删除，勿再引用）：
#: - ``low_class``：**Steam 口径**的史低分类（``new`` / ``tie`` / ``unknown``）。
#:   ⚠️ 与 ``flag`` 是两套口径，别混用：``flag`` 是 ITAD 的**全商店**标记
#:   （``N`` / ``H`` / ``S``），存在「Steam 店内首次到该价、但别家更早更便宜过」
#:   因而被标成 H/S 的条目 —— 对只买 Steam 的读者那其实是新史低。
#:   实测 2026-09-30：790 条里 ``flag=N`` 只有 63 条，而 ``low_class=="new"`` 有 190 条。
#: - ``publishers`` / ``developers``：``[{"id", "name"}]``，来自 ITAD ``info/v2``
#:   （与 reviews 同一个响应，**不新增请求**）。``id`` 是稳定标识，名字有变体。
#: - ``stats``：``{rank, waitlisted, collected}``，同源。**允许 null 或部分缺**
#:   —— 旧条目回填完成前就是 null。
KEEP = (
    "game_id",
    "title",
    "title_zh",
    "appid",
    "flag",
    "low_class",
    "price_int",
    "regular_int",
    "cut",
    "currency",
    "start",
    "expiry",
    "reviews",
    "publishers",
    "developers",
    "stats",
    "compare",
)


def build_fx(fx: dict | None) -> dict | None:
    """透出汇率表，方向保持「1 CNY 换多少 X」（base=CNY）。

    ⚠️ **不要取倒数** —— 消费方直接用这个方向展示（``¥1 ≈ ₴6.67``），
    金额换算早在 enrich 阶段就做完了（``compare[].cny_minor``），消费方不需要再算一次。
    汇率源本来就是这个方向，改写方向等于凭空多一次换算和一份出错机会。
    """
    if not isinstance(fx, dict):
        return None
    rates = fx.get("rates") or {}
    if not rates:
        return None
    return {
        "date": fx.get("date"),
        "base": fx.get("base") or "CNY",
        "rates": {k: float(v) for k, v in rates.items()},
    }


def build_snapshot(entries: list[dict], now: datetime, cfg: dict,
                   fx: dict | None = None) -> dict:
    """把（已 merge_details + 已按 appid 去重 + 已过 is_shown 分档）的条目组装成快照。

    ``low_class`` 在这里**现算**（不落库）：输入条目已由 ``pipeline.merge_details()`` 带上
    ``last_low_at``，直接套 :func:`classify.steam_low_class` 即可 ——
    与报表卡片（``report.build_card``）用的是同一个函数、同一份依据，不存在第二套口径。
    时区按计划书取 ``classify.zone(cfg["timezone"])``，``now`` 失去 tzinfo 时也能兜住。
    """
    tz = now.tzinfo or classify.zone(cfg.get("timezone") or DEFAULTS["timezone"])
    items = []
    for entry in entries:
        item = {key: entry.get(key) for key in KEEP}
        # 契约三值域（new/tie/unknown）：classify 返回 None 的只有「storeLow 在而
        # flag 缺失/非法」的异常形态 —— 按既定口径（§10 如实标记）收敛为 unknown，
        # 快照里不出现 null（code-review 2026-09-30 抓到的取值域漏洞）。
        item["low_class"] = classify.steam_low_class(entry, tz) or "unknown"
        # 两个厂商列表统一成 []（缺键 / null 都收敛），消费方不用判 null；
        # ``stats`` 保持原样（null = 还没回填到，是真信息，不能假装成 {}）
        item["publishers"] = item["publishers"] or []
        item["developers"] = item["developers"] or []
        item["key"] = classify.deal_key(
            entry.get("game_id"), entry.get("price_int"), entry.get("expiry")
        )
        items.append(item)
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": int(cfg.get("upcoming_expiry_hours", DEFAULTS["upcoming_expiry_hours"])),
        "fx": build_fx(fx),
        "count": len(items),
        "items": items,
    }


def write_snapshot(path: str | Path, entries: list[dict], now: datetime, cfg: dict,
                   fx: dict | None = None) -> int:
    """原子写出快照，返回条目数。

    原子写用 :func:`src.state.atomic_write_json`（同一份实现，卡片 05）——
    从前这里手抄了一遍写临时文件 + fsync + replace。
    """
    path = Path(path)
    atomic_write_json(path, build_snapshot(entries, now, cfg, fx))
    return len(entries)
