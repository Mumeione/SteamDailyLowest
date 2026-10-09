#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令跑齐所有契约锁（2026-10-09，配合 checks.yml 改成只手动触发）。

**为什么需要它**：checks.yml 改成只手动触发之后，「本地必须跑齐」成了**唯一的**
安全网 —— daily.yml 不看它，而发布跑的是 main 上最新的代码。可原来跑齐要敲五条
命令、还要记得设 `NODE_PATH`，漏掉任何一条，这张网就破一个洞。这里把四道锁串成
一条，并在最后给一张通过/失败表：

  ① workflow YAML 严格校验（重复键直接报错）
  ② pytest（判定 / 渲染 / 状态库 / 传输底座）
  ③ ``tools/render_report.py --at latest``（后面的检查都要读 output/，零网络）
  ④ ``tools/check_payload.py``（报表产物的跨端字面量双向比对）
  ⑤ ``tests/jsdom/smoke_s9.js``（前端 DOM 行为冒烟）

``checks.yml`` 里跑的就是本文件 —— **本地和 CI 是同一套**，不会出现「本地绿、
CI 红」这种最浪费时间的情况。

⚠️ **默认一条都不跳过**：缺 node / 缺 jsdom 一律**算失败并给出修复命令**，而不是
静默少跑一道 —— 静默跳过正是这张网最该避免的事。真要临时跳过请显式写
``--skip-smoke``。

用法::

    python tools/check_all.py                  # 跑齐（本机 35~55 秒，冷缓存更慢）
    python tools/check_all.py --skip-smoke     # 临时不跑前端冒烟
    python tools/check_all.py --only pytest    # 只跑某一项（可重复传）
    python tools/check_all.py -v               # 透传子进程输出（排查用）

本机的 jsdom 不在仓库里（装在 agent 的 node workspace），所以要给它指路：

    export SDL_NODE_MODULES=~/.../node_modules       # bash
    set SDL_NODE_MODULES=D:\\...\\node_modules         # Windows cmd

没指路时本工具**直接失败并打印上面的修复命令**，不会静默少跑一道 —— 前端冒烟
是四道锁里最值钱的那道（S9 期间抓到过 N 次真实回归）。CI 里装在仓库根，免配置。

退出码：0 = 全绿；1 = 有失败项；2 = 用法或环境问题（缺解释器 / 缺 node 等）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Step:
    """一道锁：名字、说明、命令行、以及它依赖的上一道（用于失败时避免连锁刷屏）。"""

    def __init__(self, slug: str, title: str, argv: list[str],
                 *, needs: str | None = None, timeout: int = 600,
                 env: dict | None = None):
        self.slug = slug
        self.title = title
        self.argv = argv
        self.needs = needs
        self.timeout = timeout
        self.env = env


def which_node() -> str | None:
    """找 node：``--node`` / ``SDL_NODE`` / PATH 依次尝试。"""
    explicit = os.environ.get("SDL_NODE")
    if explicit and Path(explicit).exists():
        return explicit
    return shutil.which("node")


def jsdom_env(node: str) -> tuple[dict | None, str | None]:
    """确认 node 能 `require('jsdom')`；返回 ``(环境变量, 错误说明)``。

    jsdom 是冒烟的前置（``tests/jsdom/smoke_s9.js`` 第一行就 require 它）。
    本机通常装在 agent 的 node workspace 里、靠 ``NODE_PATH`` 才解析得到；
    CI 是在仓库根 ``npm install`` 后从 ``node_modules`` 直接解析。
    两种都能认：先按**当前环境**试，失败再把 ``SDL_NODE_MODULES`` / 已有
    ``NODE_PATH`` 显式塞进去重试一次。
    """
    def resolves(env: dict) -> bool:
        try:
            proc = subprocess.run([node, "-e", "require.resolve('jsdom')"],
                                  cwd=str(ROOT), env=env, timeout=60,
                                  capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    base = dict(os.environ)
    if resolves(base):
        return None, None

    extra = os.environ.get("SDL_NODE_MODULES") or os.environ.get("NODE_PATH")
    if extra:
        retry = dict(base)
        retry["NODE_PATH"] = os.pathsep.join(
            [p for p in (extra, base.get("NODE_PATH")) if p])
        if resolves(retry):
            return retry, None

    hint = (
        "找不到 jsdom（node 无法 require）。二选一：\n"
        "      ① 在仓库根跑 `npm install --no-save jsdom`；\n"
        "      ② 指向已有的 node_modules：\n"
        "         set SDL_NODE_MODULES=<含 jsdom 的 node_modules 目录>   (Windows cmd)\n"
        "         export SDL_NODE_MODULES=<...>/node_modules            (bash)\n"
        "      临时不跑这道就加 --skip-smoke（但那张网会少一块）"
    )
    return None, hint


def build_steps(args) -> list[Step]:
    """组装要跑的道次（顺序即依赖顺序：渲染必须在报表契约检查之前）。"""
    py = sys.executable
    steps = [
        Step("workflow", "workflow YAML 严格校验",
             [py, "tools/check_workflow_yaml.py"], timeout=120),
        Step("pytest", "单元 / 集成测试",
             [py, "-m", "pytest", "-q"], timeout=900),
        Step("render", "本地重渲染报表（零网络）",
             [py, "tools/render_report.py", "--at", "latest"], timeout=600),
        Step("payload", "报表契约前后端比对",
             [py, "tools/check_payload.py"], needs="render", timeout=300),
    ]
    node = which_node()
    if node is None:
        steps.append(Step("smoke", "前端行为冒烟（缺 node）", [], needs="render"))
    else:
        env, _ = jsdom_env(node)
        steps.append(Step("smoke", "前端行为冒烟",
                          [node, "tests/jsdom/smoke_s9.js"],
                          needs="render", timeout=600, env=env))
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {s.slug for s in steps}
        if unknown:
            print(f"[错误] --only 不认识：{sorted(unknown)}"
                  f"（可选 {[s.slug for s in steps]}）")
            raise SystemExit(2)
        steps = [s for s in steps if s.slug in wanted]
    if args.skip_smoke:
        steps = [s for s in steps if s.slug != "smoke"]
    return steps


def preflight(steps: list[Step]) -> str | None:
    """跑之前先自查：脚本文件都在吗、node 找得到吗。问题一律**响亮失败**。"""
    for step in steps:
        for arg in step.argv[1:]:
            if arg.endswith(".py") or arg.endswith(".js"):
                if not (ROOT / arg).exists():
                    return f"第 {step.slug} 道锁要跑的 {arg} 不存在（改名了？）"
    if any(s.slug == "smoke" for s in steps):
        if which_node() is None:
            return ("找不到 node —— 前端冒烟跑不了（它是四道锁里最值钱的那道）。\n"
                    "      装个 node，或用 SDL_NODE=<node 可执行文件路径> 指定")
        _, err = jsdom_env(which_node())
        if err:
            return err
    return None


def run_step(step: Step, *, verbose: bool) -> tuple[bool, float, str]:
    """跑一道锁；返回 ``(是否通过, 耗时秒, 出错时的输出尾巴)``。"""
    env = dict(os.environ)
    if step.env:
        env.update(step.env)
    started = time.monotonic()
    try:
        proc = subprocess.run(step.argv, cwd=str(ROOT), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=step.timeout)
    except subprocess.TimeoutExpired:
        return False, time.monotonic() - started, f"超过 {step.timeout}s 未结束"
    except OSError as exc:
        return False, time.monotonic() - started, f"起不来：{exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if verbose and out.strip():
        print(out.rstrip())
    if proc.returncode != 0:
        tail = "\n".join(out.rstrip().splitlines()[-25:])
        return False, time.monotonic() - started, tail or f"退出码 {proc.returncode}"
    return True, time.monotonic() - started, ""


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="一条命令跑齐所有契约锁（本地与 checks.yml 同一套）")
    parser.add_argument("--only", action="append", metavar="名字",
                        help="只跑某一项（可重复）：workflow / pytest / render / payload / smoke")
    parser.add_argument("--skip-smoke", action="store_true",
                        help="临时不跑前端冒烟（会让安全网少一块，仅排查时用）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="透传每道锁的完整输出")
    args = parser.parse_args(argv)

    steps = build_steps(args)
    if not steps:
        print("[错误] 没有可跑的项（检查 --only / --skip-smoke 的组合）")
        return 2
    problem = preflight(steps)
    if problem:
        print("[错误] " + problem)
        return 2

    print(f"契约锁 {len(steps)} 道（仓库根 {ROOT}）")
    print("-" * 64)
    results: list[tuple[Step, bool, float, str]] = []
    failed: set[str] = set()
    for step in steps:
        if step.needs and step.needs in failed:
            print(f"  跳过  {step.title}（上游 {step.needs} 已失败）")
            results.append((step, False, 0.0, f"上游 {step.needs} 失败"))
            failed.add(step.slug)
            continue
        if not step.argv:
            print(f"  失败  {step.title}（环境缺件，见上面 [错误]）")
            results.append((step, False, 0.0, "环境缺件"))
            failed.add(step.slug)
            continue
        ok, seconds, detail = run_step(step, verbose=args.verbose)
        print(f"  {'通过' if ok else '失败'}  {step.title}  {seconds:.1f}s")
        if not ok:
            failed.add(step.slug)
            for line in detail.splitlines():
                print(f"        {line}")
        results.append((step, ok, seconds, detail))

    passed = sum(1 for _, ok, _, _ in results if ok)
    total = sum(s for _, _, s, _ in results)
    print("-" * 64)
    print(f"{passed}/{len(results)} 道通过 · 合计 {total:.1f}s")
    if failed:
        print("失败项：" + " · ".join(sorted(failed)))
        print("⚠️ 本地跑齐是现在唯一的安全网（checks.yml 已改成只手动触发）—— 别带着红的提交。")
        return 1
    print("全绿 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
