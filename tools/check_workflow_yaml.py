#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""严格校验 workflow YAML：**重复键直接报错**。

起因：2026-10-02 的 `b38c1cc` 在同一个 job 下写了两个 `needs:` 键，
GitHub 判定 workflow 非法并触发 startup_failure。PyYAML 默认会静默取最后一个，
所以必须用自定义 loader 把重复键变成硬错误。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


class StrictLoader(yaml.SafeLoader):
    """把重复的 mapping 键变成错误，而不是静默覆盖。"""


def _strict_mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None,
                f"重复键 {key!r}（第 {key_node.start_mark.line + 1} 行；"
                f"上一次出现在第 {seen[key] + 1} 行）",
                key_node.start_mark,
            )
        seen[key] = key_node.start_mark.line
        yield key, loader.construct_object(value_node, deep=deep)


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: dict(_strict_mapping(loader, node)),
)


def check(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        print(f"❌ {path}\n   {exc}")
        return 1

    # YAML 1.1 会把裸 `on` 解析成布尔 True，两种都认
    trigger = data.get("on", data.get(True))
    jobs = data.get("jobs") or {}
    print(f"✅ {path}")
    print(f"   triggers = {list(trigger) if isinstance(trigger, dict) else trigger}")
    for name, job in jobs.items():
        needs = job.get("needs")
        print(f"   job {name!r}: needs={needs!r} "
              f"timeout_minutes={job.get('timeout-minutes')!r} "
              f"steps={len(job.get('steps') or [])}")
    if "workflow_dispatch" in (trigger or {}):
        inputs = (trigger["workflow_dispatch"] or {}).get("inputs") or {}
        for k, v in inputs.items():
            print(f"   input {k!r}: type={v.get('type')} default={v.get('default')!r}")
    return 0


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]] or sorted(
        Path(".github/workflows").glob("*.yml"))
    rc = 0
    for p in paths:
        rc |= check(p)
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
