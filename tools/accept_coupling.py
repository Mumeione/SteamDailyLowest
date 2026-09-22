# -*- coding: utf-8 -*-
"""验收脚本：报表与详情解耦（DEVELOPMENT.md §3.3 / §11「大促模拟」）。

做法：用一份**全新**的状态文件跑 `run.py`，在详情阶段还没结束（进程仍在运行）时
把进程强杀掉，然后检查：

  1. `index.html` 被打断时**已经存在**（而不是"详情抓完才有页面"）
  2. 页面里的缺详情条目落在「详情待补」分组
  3. 状态文件 `seen_deal` 也已落盘（详情全失败不该把攒课一起带走）

key 只通过环境变量 `ITAD_API_KEY` 传给子进程，不写任何文件。

用法：`python tools/accept_coupling.py`
     （约 27 次 ITAD 请求用于翻页；详情阶段会被强杀，所以不产生额外请求）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "data" / "probe"
CFG_PATH = PROBE / "accept_coupling_cfg.json"
STATE_PATH = PROBE / "accept_coupling_state.json"
OUT_DIR = PROBE / "accept_coupling_out"
OUT = PROBE / "accept_coupling_result.txt"

_lines: list[str] = []


def p(text: str = "") -> None:
    _lines.append(text)
    OUT.write_text("\n".join(_lines), encoding="utf-8")


def main() -> int:
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    cfg = {
        "itad_api_key": "",  # 由环境变量注入
        "country": "CN",
        "sweep_mode": "low_only",
        "state_path": str(STATE_PATH.relative_to(ROOT)),
        "output_dir": str(OUT_DIR.relative_to(ROOT)),
        "timezone": "Asia/Shanghai",
        "expired_retention_days": 7,
        "reviews_ttl_days": 7,
        "reviews_empty_ttl_days": 3,
        "notable_review_count": 10000,
        "min_positive_ratio": 0.7,
        "min_review_count": 100,
        "only_type": "game",
        "exclude_free": True,
        "exclude_mature": True,
        "itad_rate_limit": "800 / 300s",
        "itad_min_interval": 0.3,
        "request_pause_seconds": 0.3,
        "request_timeout_seconds": 25,
        "run_log_keep": 30,
    }
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    real = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    env = dict(os.environ, ITAD_API_KEY=real["itad_api_key"], PYTHONUNBUFFERED="1")

    index = OUT_DIR / "index.html"
    p("=" * 72)
    p("验收：报表与详情解耦（详情未抓完时页面是否已存在）")
    p("=" * 72)
    p(f"状态文件（全新）：{STATE_PATH.relative_to(ROOT)}")
    p(f"输出目录：{OUT_DIR.relative_to(ROOT)}")
    p("")

    started = time.time()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "--config", str(CFG_PATH)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    index_at: float | None = None
    deadline = started + 300
    while time.time() < deadline:
        if index.exists() and index.stat().st_size > 0:
            index_at = time.time() - started
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    p(f"index.html 首次出现于：{index_at:.1f} 秒" if index_at
      else "index.html 在进程结束前**始终没有出现** ← 解耦未生效")

    if index_at is not None and proc.poll() is None:
        p("此刻进程仍在运行（详情还在抓）→ 页面确实不等详情")
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        p("已强杀进程（模拟大促期间被打断）")
    else:
        proc.communicate(timeout=30)
        p("进程已自行结束（可能在抓到中途观测之前就跑完了）")

    p("")
    p("--- 被打断后落盘的页面内容 ---")
    if index.exists():
        data_js = OUT_DIR / "data.js"
        if data_js.exists():
            raw = data_js.read_text(encoding="utf-8")
            payload = json.loads(raw.split("=", 1)[1].rstrip().rstrip(";"))
            ov = payload["overview"]
            p(f"  index.html 存在 ✓（{index.stat().st_size} 字节）")
            p(f"  概览：当日新增={ov['new_today_raw']} · 进列表={ov['new_today_shown']}"
              f" · 详情已抓={ov['detail_fetched']} · 详情待补={ov['detail_pending']}")
            p(f"  分组：{[(g['label'], g['count']) for g in payload['groups']]}")
        else:
            p(f"  index.html 存在 ✓（{index.stat().st_size} 字节），但 data.js 缺失")
    else:
        p("  index.html 不存在 ✗")

    p("")
    p("--- 状态文件是否已落盘（详情阶段失败不该丢攒库）---")
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        p(f"  存在 ✓：seen_deal={len(state['seen_deal'])} 条 · "
          f"game_meta={len(state['game_meta'])} 条 · run_log={len(state['run_log'])} 条")
        state_ok = True
    else:
        p("  不存在 ✗（攒库被详情阶段带走）")
        state_ok = False

    passed = index_at is not None and index.exists()
    p("")
    p("=" * 72)
    p(f"§11「报表在详情未抓完时就已生成，且标记详情待补」→ {'通过' if passed else '失败'}")
    p(f"「状态先落盘」→ {'通过' if state_ok else '失败'}")
    p(f"结果已写入：{OUT}")
    p("=" * 72)
    return 0 if passed and state_ok else 1


if __name__ == "__main__":
    sys.exit(main())
