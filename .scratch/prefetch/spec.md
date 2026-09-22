# 预抓取任务（prefetch）spec

> Status: R1/R3 已实现（2026-09-22 批 B，见 Comments）；R2 的 workflow job 已落地
> 决策时间：2026-09-22，需求来自用户：「建立 UUID 与 appID 的对应数据库，每日增量更新，
> 也可以每日单开一个任务就跑这个，把符合条件的游戏信息全部收入数据库中，防止大促时详情拿不到。
> 后台慢慢跑，第二天合并。」

## 背景

现状已有**半套**：`state.json` 的 `game_meta` 就是「uuid → appid / 好评率 / 中文名」的库
（appid 与中文名**永久缓存**，好评率 TTL 7 天），详情靠 `detail_daily_budget=300` 在主任务里
增量补齐。缺口是：大促日当日新增几百条，预算不够，详情滞后要数轮才追平。

## 方案：不需要显式「合并」

回答用户问题（能后台慢慢跑、第二天合并吗）：**能，且不用写合并逻辑**——

- `game_meta` 与 `seen_deal` 在**同一个滚动 state.json** 里，appid / 中文名永久缓存；
- 预抓任务只是**另一个「只补缓存、不生成报表」的运行方**，跑完把 state.json 写回；
- 次日主任务（或第 9 步的 Pages 构建）自然读到补全的数据；
- 唯一约束：**两个进程不能同时写 state.json** → 调度上错开时间即可（主任务 03:00，预抓 15:00）。

## 需求

### R1 `run.py --prefetch`

- 加载 config + state → 复用与日常**同一套筛选**（funnel：`min_cut` / `min_positive_ratio` /
  `min_review_count` / `only_type` / `exclude_*` 等）得出「符合条件全集」；
- 对缺数据的条目补齐，顺序 = **最近出现在史低的优先**（复用 `detail_targets` / backlog 口径）：
  1. 缺 appid / 好评率（ITAD `info/v2`，走现有 `fetch_details` 路径）；
  2. 缺中文名（Steam 逐游戏，**必须走 `src/steam.py`**，2 秒间隔，绝不裸请求）；
- 预算 = 新配置 `prefetch_daily_budget`（默认 300），与 `detail_daily_budget` **相互独立**；
- 产物：**只写 `state.json` + run_log**，不生成 `output/` 任何文件；
- 幂等：连续跑两次，第二次应几乎零请求（缓存命中）。

### R2 调度（与第 9 步一起落地）

- GitHub Actions：同一 workflow 加第二个定时入口（建议 `15:00` Asia/Shanghai），
  或主任务 publish 完成后的独立 job —— 两者取其一，保证与主任务**不并发**写 state；
- runner 无持久磁盘 → 照 §8 用 `GITHUB_TOKEN` 把 state.json 提交回仓库；
- 本机可随时手动 `python run.py --prefetch`。

### R3 预算与时长量级（校验可行性）

- 300 条 ≈ ITAD info 300 次（0.3s 间隔 ≈ 2 分钟）+ Steam 中文名 300 次（2s 间隔 ≈ 10 分钟）
  → 单轮 ≈ 15 分钟内，远低于 runner 6 小时上限；
- 跑不完的次日继续（backlog 优先级保证热门先补）。

## 验收

- [ ] `--prefetch` 真跑一轮：`game_meta` 增量、`state.json` 写回、`output/` 无新产物。
- [ ] 与主任务兼容：跑完 `--prefetch` 再跑日常，后者对已补条目零重复请求。
- [ ] 单测：预算裁剪、最近优先排序、无报表输出、幂等。

## Comments

### 实现记录（2026-09-22，批 B）

- **R1 `run.py --prefetch`**：`run_prefetch()` 复用日常的 `collect + funnel` 同套筛选；
  新增 `prefetch_targets()` —— 「缺数据」= `meta_valid` 不通过（缺 appid/好评率）
  **或** 已有详情但缺中文名（两条独立缓存）；顺序 = **`start`（deal.timestamp）降序**
  （最近出现在史低的优先；不用 `seen_deal.last_seen_at`，因为 record_seen 会把本轮
  全部条目刷成同一时间戳，排序失效）；预算 = `prefetch_daily_budget`（默认 300，
  已进 §9 / config.example.json），`0` = 关闭。
  详情走现有 `fetch_details`（ITAD info/v2，缓存命中零请求）；中文名走
  `SteamClient.info`（逐游戏，全站合并限流 + 2 秒间隔）。产物只有 state.json + run_log
  （mode="prefetch"），**不写 output/ 任何文件**、不拉汇率。
- **R2 调度**：daily.yml 加 cron `0 7 * * *`（15:00 CST）+ `prefetch` job
  （恢复 state → `python run.py --prefetch` → 回写 data 分支）；
  与主任务共用 `state-write` 并发组串行，不会并发写 state。
  每月 2 日 0 7 UTC 预抓与 orphan 清理同刻命中：清理只清历史、保留当前 state 内容，无冲突。
- **幂等**：第二轮 meta_valid 命中 + title_zh 永久缓存 → 详情与 Steam 请求均为零
  （单测 `test_idempotent_second_run` 钉死）。
- **测试**：`tests/test_prefetch.py` 8 个（假客户端零网络）：预算截断 / 预算 0 关闭 /
  最近优先排序 / 缺 start 排最后 / 幂等 / 无报表输出 / 中文名补齐 / 只缺中文名不碰 ITAD。
  全量 103 个单测通过；`check_payload.py` 判定不受影响。
- 注意：Steam 侧真跑（含 15:00 的 job）前留意 store host 静默超时惩罚
  （交接文档 §3）——本地今天已触发，真跑一轮验收（spec 验收第 1、2 条）建议放 Steam 恢复后。
