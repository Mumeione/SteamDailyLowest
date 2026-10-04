# -*- coding: utf-8 -*-
"""重构 S4 一次性迁移：把 state.json 里的 compare_cache / low_time_cache 拆到 cache.json。

拆分口径（spec §3.4）：state.json 只留不可重建数据（seen_deal / game_meta / run_log），
两块可重建缓存独立落 cache.json（丢了重拉）。

**幂等**：重复执行无副作用 ——
- state.json 已无缓存键且 cache.json 存在 → no-op（退出码 0）
- state.json 已无缓存键但 cache.json 缺失 → 补建空 cache.json（补全拆分形态，
  state.json 内容不变；再跑即 no-op）
- state.json 有缓存键而 cache.json 已存在 → 按 key 取并集、cache.json 优先
  （cache.json 是切分后的权威来源，与 ``State.load`` 的自迁移口径一致）

用法：
    python tools/migrate_split_state.py [--state PATH] [--cache PATH]
默认：state 取配置 ``state_path``；cache 无独立配置键，固定与 state 同目录同名
（``state.json → cache.json``，与 ``State.__init__`` 单一派生规则一致）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.state import CACHE_VERSION, _CACHE_KEYS, _atomic_write_json  # noqa: E402


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def migrate(state_path: Path, cache_path: Path) -> int:
    if not state_path.exists():
        print(f"[FAIL] 找不到状态库：{state_path}")
        return 1

    raw = load_json(state_path)
    legacy: dict[str, dict] = {}
    for key in _CACHE_KEYS:
        val = raw.pop(key, None)
        if val is not None:
            if not isinstance(val, dict):
                print(f"[FAIL] state.json 里 {key} 不是对象，不敢动：{type(val).__name__}")
                return 1
            legacy[key] = val

    cache_exists = cache_path.exists()
    if not legacy and cache_exists:
        print(f"[OK] 已是拆分后格式，无需迁移（no-op）：state={state_path.name}, "
              f"cache={cache_path.name}")
        return 0
    if legacy and cache_exists:
        cache_raw = load_json(cache_path)
        if not isinstance(cache_raw, dict):
            print(f"[FAIL] cache.json 顶层不是对象：{cache_path}")
            return 1
    else:
        # 两种情况到此：①state 带遗留键但 cache.json 缺失；②已拆分但 cache.json
        # 缺失（补建空 cache.json 补全拆分形态，见模块 docstring 幂等口径第二条）
        cache_raw = {
            "version": CACHE_VERSION,
            "updated_at": raw.get("updated_at"),
            "compare_cache": {},
            "low_time_cache": {},
        }

    for key in _CACHE_KEYS:
        target = cache_raw.setdefault(key, {})
        if not isinstance(target, dict):
            target = cache_raw[key] = {}
        moved = 0
        for k, v in legacy.get(key, {}).items():
            if k not in target:
                target[k] = v
                moved += 1
        cache_raw.setdefault("version", CACHE_VERSION)
        cache_raw.setdefault("updated_at", raw.get("updated_at"))
        print(f"  {key}：合并 {moved} 条（cache.json 优先），现 {len(target)} 条")

    for key in _CACHE_KEYS:
        raw.pop(key, None)  # 上面只 pop 了一次，非 dict 的遗留键也确保剥掉

    _atomic_write_json(cache_path, cache_raw)
    _atomic_write_json(state_path, raw)
    print(f"[OK] 迁移完成：state={state_path}（{state_path.stat().st_size} 字节），"
          f"cache={cache_path}（{cache_path.stat().st_size} 字节）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", default=None, help="state.json 路径（默认取配置 state_path）")
    parser.add_argument("--cache", default=None, help="cache.json 路径（默认与 state 同目录同名）")
    args = parser.parse_args()

    cfg = config.load_config()
    state_path = Path(args.state) if args.state else config.resolve_path(cfg, "state_path")
    # cache 无独立配置键：与 state 同目录同名（与 State.__init__ 同一派生规则）
    cache_path = Path(args.cache) if args.cache else state_path.with_name("cache.json")
    print(f"迁移：{state_path} → 拆出 {cache_path}")
    return migrate(state_path, cache_path)


if __name__ == "__main__":
    sys.exit(main())
