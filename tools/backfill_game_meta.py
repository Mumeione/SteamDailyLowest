# -*- coding: utf-8 -*-
"""一次性回填：给 ``game_meta`` 补 ``publishers`` / ``developers`` / ``stats``。

## 为什么需要它

``fetch_details()`` 一直在逐游戏调 ITAD ``GET /games/info/v2``，但历史上只取了
``appid`` 与 ``reviews`` —— 同一个响应里的 ``publishers`` / ``developers`` /
``stats{rank, waitlisted, collected}`` 被丢掉了（实测填充率 100%，52 条分层样本）。
快照 v3 需要它们，而**已经缓存过详情的旧条目不会再发这个请求**，所以要单独回填一次。

## 用法

```
python tools/backfill_game_meta.py --dry-run      # 只统计要补多少条
python tools/backfill_game_meta.py                # 真跑（可随时 Ctrl-C，下次自动续跑）
python tools/backfill_game_meta.py --limit 200    # 只跑前 200 条（试探）
```

## 行为

- 只处理 **有 ``appid`` 且 ``publishers`` 缺键或为 null** 的条目；已有的跳过（幂等、可续跑）。
  ⚠️ 判缺不能用 ``not entry.get("publishers")`` —— 空列表 ``[]`` 是「取到了，确实没有」，
  会被当成缺失而永远重抓（code-review 2026-09-30 抓到）。
- 走 :func:`run.build_client`（**复用项目自己的 ITAD 限流**：滑动窗口
  ``itad_rate_limit`` + ``itad_min_interval``），不新造限流逻辑。
- 只写 ``set_meta_extras()`` —— **不碰 ``reviews`` 与 ``fetched_at``**：
  回填没有重新拿好评率，不能把 reviews 的 TTL 刷新掉。
- 每 ``--save-every``（默认 25）条落一次盘，并打印当前限流窗口用量（计划书 §改动3 要求
  「打印当前窗口已用 N/800 便于自检」）；中途失败下次自动跳过已完成的。

## 耗时（实测，别按 `min_interval` 算）

⚠️ **瓶颈是请求往返（实测 ~1.4s/条），不是 ``min_interval``** ——
按 0.3s 的配置值算会严重低估。5047 条量级顺序跑约 **2 小时**。

> 2026-09-30 的首次回填为了压到「一小时左右」，用的是**双线程 + 共享滑动窗口**的
> 临时脚本（见 ``.scratch/expiring-snapshot/v3-plan.md`` §5），不是本工具。
> 本工具保留为**可复跑、与项目口径一致**的正式版本 —— 顺序跑慢但简单、可审。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import build_client, log  # noqa: E402
from src import config  # noqa: E402
from src.classify import zone  # noqa: E402
from src.state import State  # noqa: E402


def targets(state: State) -> list[str]:
    """要回填的 game_id：有 appid，且 ``publishers`` 缺键或为 null。

    ⚠️ 不能用 ``not entry.get("publishers")``：空列表 ``[]`` 是「取到了，确实没有」，
    会被当成缺失而永远重抓。
    """
    return [
        gid for gid, entry in state.game_meta.items()
        if entry.get("appid") and entry.get("publishers") is None
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 game_meta 的厂商 / stats（一次性）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全部）")
    parser.add_argument("--save-every", type=int, default=25, help="每 N 条落一次盘")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不发请求")
    args = parser.parse_args(argv)

    cfg = config.load_config()
    tz = zone(cfg.get("timezone", "Asia/Shanghai"))
    now = datetime.now(tz)
    state_path = config.resolve_path(cfg, "state_path")
    state = State(state_path, tz=tz).load()

    todo = targets(state)
    if args.limit:
        todo = todo[:args.limit]
    log(f"状态库 {state_path}")
    log(f"game_meta 共 {len(state.game_meta)} 条；待回填 {len(todo)} 条"
        f"（有 appid 且缺 publishers）")
    if args.dry_run:
        log("--dry-run：不发请求")
        return 0
    if not todo:
        log("没有需要回填的条目，退出")
        return 0
    log("⚠️ 顺序跑，实测约 1.4s/条 —— 5047 条量级约 2 小时；随时 Ctrl-C，下次自动续跑")

    client = build_client(cfg)
    ok = fail = 0
    for n, game_id in enumerate(todo, 1):
        try:
            info = client.fetch_info(game_id)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不阻断整批
            info = None
            log(f"  [warn] {game_id} 失败：{type(exc).__name__}: {exc}")
        if info:
            state.set_meta_extras(
                game_id,
                publishers=info.get("publishers"),
                developers=info.get("developers"),
                stats=info.get("stats"),
            )
            ok += 1
        else:
            fail += 1
        if n % args.save_every == 0:
            state.save()
            log(f"  {n}/{len(todo)}｜成功 {ok} 失败 {fail}"
                f"｜窗口 {client.limiter.window_used()}/{client.limiter.max_calls}"
                f"｜已落盘 {state_path.name}")
    state.save()
    log(f"完成：成功 {ok}，失败 {fail}；状态库已写入 {state_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
