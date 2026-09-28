# -*- coding: utf-8 -*-
"""「即将过期」快照导出（给外部数据管道消费，``.scratch/expiring-snapshot/spec.md``）。

与 ``output/latest.json`` 的区别：那个是报表的伴生产物、随 Pages 发布；
这个是**跨仓库的数据契约**（``data/expiring.json``），落 data 分支、与 state.json 同级。

口径边界：导出的是窗口内**已进列表**（``is_shown``）的条目（已合并详情、已按 appid
去重、已过口碑分档）——口碑门槛就是本仓库的进列表口径，口径单点收敛在这里，
消费方不再自建近似门槛（版本演化见 .scratch/expiring-snapshot/changelog.md）。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import classify

SNAPSHOT_VERSION = 2

#: 导出字段（写出的键顺序即此顺序，便于人工 diff）。
#: ``compare`` 来自 enrich 阶段、原样透传：输入已是进列表条目，理论上都该有比价，
#: 个别为 null（某些区无售或拉取失败）—— 消费方降级省略比价行即可，不必过滤条目。
KEEP = (
    "game_id",
    "title",
    "title_zh",
    "appid",
    "flag",
    "price_int",
    "regular_int",
    "cut",
    "currency",
    "start",
    "expiry",
    "reviews",
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
    """把（已 merge_details + 已按 appid 去重 + 已过 is_shown 分档）的条目组装成快照。"""
    items = []
    for entry in entries:
        item = {key: entry.get(key) for key in KEEP}
        item["key"] = classify.deal_key(
            entry.get("game_id"), entry.get("price_int"), entry.get("expiry")
        )
        items.append(item)
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": int(cfg.get("upcoming_expiry_hours", 48)),
        "fx": build_fx(fx),
        "count": len(items),
        "items": items,
    }


def write_snapshot(path: str | Path, entries: list[dict], now: datetime, cfg: dict,
                   fx: dict | None = None) -> int:
    """原子写出快照，返回条目数。"""
    path = Path(path)
    payload = json.dumps(
        build_snapshot(entries, now, cfg, fx), ensure_ascii=False, separators=(",", ":")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return len(entries)
