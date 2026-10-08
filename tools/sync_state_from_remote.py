# -*- coding: utf-8 -*-
"""把**远端 data 分支**的四份状态文件拉回本机，替换本地副本（2026-10-08）。

背景：生产只跑在 GitHub Actions（每轮把 state/dynamic/cache/expiring 四份 JSON
回写到 ``origin/data`` 分支）；**本机不保存数据、不做更新数据的事** —— 本地数据
只用来验证报表 UI 与代码正确性，要新数据直接取远端（有新字段时本地测试抓取、
验证过才推送远端）。本机 cache 落后远端时，本地预览会比线上少数据 —— 典型症状：
比价/原价缓存陈旧，新史低的详情区缺外区比价。跑本工具即可把本机对齐到远端最新
（远端只读，**绝不 push**，不干扰远端 data 分支）。

用法（仓库根目录）::

    python tools/sync_state_from_remote.py            # 拉取并替换
    python tools/sync_state_from_remote.py --dry-run  # 只看远端概况，不落盘

行为：
  1. ``git fetch origin data``（只 fetch，不动工作区）；
  2. 对四份文件逐一 ``git show origin/data:data/<名>.json`` 读出内容并**先验证
     JSON 可解析**才落盘 —— 远端半截文件不会毁掉本地状态库；
  3. **不做备份**（2026-10-08 用户定案：本地不是数据源，旧数据没有保留价值，
     反正下一条命令就能从远端拿回最新），原子替换（写临时文件后 os.replace）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 远端 data 分支上的四份状态文件（都在 ``data/`` 下）。
#: fx_cache.json 不在远端 —— 汇率本机现取即可，不同步。
STATE_FILES = ("state.json", "dynamic.json", "cache.json", "expiring.json")
REMOTE_BRANCH = "origin/data"
REMOTE_DIR = "data"


def run_git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout


def fetch_remote() -> None:
    print("[sync] git fetch origin data ……")
    run_git("fetch", "origin", "data")
    head = run_git("rev-parse", "--short", REMOTE_BRANCH).strip()
    print(f"[sync] 远端 {REMOTE_BRANCH} = {head}")


def remote_names() -> set[str]:
    tree = run_git("ls-tree", "--name-only", f"{REMOTE_BRANCH}:{REMOTE_DIR}")
    return {line.strip() for line in tree.splitlines() if line.strip()}


def replace_file(local: Path, raw: str) -> None:
    """原子替换（写临时文件后 os.replace），中途断电不会留下半个 JSON。
    ⚠️ 不做备份 —— 本地不是数据源，旧数据没有保留价值（远端随时能给回最新）。"""
    tmp = local.with_suffix(local.suffix + ".tmp")
    tmp.write_text(raw, encoding="utf-8")
    os.replace(tmp, local)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="从远端 data 分支同步四份状态文件到本机")
    ap.add_argument("--dry-run", action="store_true", help="只显示远端概况，不替换本地")
    args = ap.parse_args()

    fetch_remote()
    names = remote_names()

    # 本地目录：跟 config.json 的 state_path 走（默认 data/）
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    state_path = ROOT / (cfg.get("state_path") or "data/state.json")
    local_dir = state_path.parent
    expiring_rel = (cfg.get("expiring_snapshot_path")
                    or "data/expiring.json")
    expiring_path = ROOT / expiring_rel

    changed = 0
    for name in STATE_FILES:
        if name not in names:
            print(f"[skip] 远端没有 {REMOTE_DIR}/{name}（跳过）")
            continue
        raw = run_git("show", f"{REMOTE_BRANCH}:{REMOTE_DIR}/{name}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[warn] 远端 {name} 不是合法 JSON（{exc}），跳过 —— 本地文件未动")
            continue
        local = expiring_path if name == "expiring.json" else local_dir / name
        n = len(data.get("seen_deal", data.get("entries", data.get("compare_cache", data)))) \
            if isinstance(data, dict) else "?"
        if args.dry_run:
            print(f"[dry] {local.relative_to(ROOT)}：远端可用，约 {n} 条；本地"
                  f"{'存在' if local.exists() else '不存在'}")
            continue
        existed = local.exists()               # 必须在替换前判 —— 替换后恒为 True
        replace_file(local, raw)
        changed += 1
        where = "" if existed else "（本地原没有，新建）"
        print(f"[ok]  {local.relative_to(ROOT)} ← 远端（约 {n} 条）{where}")

    if args.dry_run:
        print("[dry] 未替换任何文件。去掉 --dry-run 执行真实同步。")
    else:
        print(f"[done] 替换 {changed}/{len(STATE_FILES)} 份；远端只读未动。"
              "现在可以重跑 tools/render_report.py 预览了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
