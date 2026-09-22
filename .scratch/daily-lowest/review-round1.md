# 第一轮实现审查（对照 `docs/DEVELOPMENT.md` §12）

Status: **已按 §8 的顺序修完（第 1~7 项）**，修复结果见文末 §10；**§12 第 7 步已完成**（见 `docs/DEVELOPMENT.md §12.2`）。

审查时间：2026-09-21 22:45 起
审查范围：§12 第 2~6 步已完成的代码 + `data/logs/` 里的验收证据
新增实测：本轮另跑了 148 次 ITAD 请求（两个只读探针），结论见 §3
修复时间：2026-09-21 23:00 起 → 2026-09-22 完成第 7 步（中文名 / 跨区比价 / 汇率）


---

## 0. 一句话结论

第 2~6 步**主体是对的**（32 个单测全过、`--baseline`/断点续传/限流降速都有真实日志证据），
但有 **2 个 P0 必须在上线前修**（报表耦合详情、`.gitignore` 与状态提交冲突），
另外**发现了一个能省 83% 请求量的官方 filter 用法**（§3）。

| 级别 | 条目 | 影响 |
|---|---|---|
| P0 | 报表与详情未解耦 | 违反 §3.3，§11「大促模拟」验收不过 |
| P0 | `.gitignore` 把 `data/` 全忽略 | 第 9 步上线后状态永远提交不上去 |
| P1 | 状态库 5.09 MB/天提交回仓库 | 仓库持续膨胀，需要定策略 |
| P1 | 无 appid 条目每轮重复请求 | 量级放大后白白多花请求 |
| P1 | 概览「当日新增」显示的是过滤后的数 | 页面会**少报**真实新增量（105 → 显示 20） |
| P1 | 测试配置里写死了真实 API key | 违反 §2.1 的 key 纪律 |
| P1 | 每轮 162 次翻页（可压到 27） | 每轮成本，已实测出解法 |
| P2 | 「本周」视图实现与 §4.6 不符 等 6 项 | 现在不影响，上线前要处理 |

---

## 1. 先确认：已经做对的部分

不用返工，列出来是为了让后面 P0 的对比有参照。

- **单测 32 个全过**（实测 `python -m unittest discover -s tests -v` → `OK`）
- `--baseline` 只写状态、不取详情、不产报表：`baseline.log` 证据完整
- 断点续传真的成立：`accept_resume1.log` 在 `[4/6]` 后被打断，
  `accept_resume2.log` 只补了 67 个详情（不是 106 个），缓存命中生效
- 限流降速有效：`cfg_ratelimit.json` 把窗口调到 `5 / 60s` + `max_deals=2000`，
  `accept_ratelimit.log` 跑完 18 次请求 **0 次 429**
- 缺 key 的报错清晰、退出码 2、不产空报表：`accept_nokey.log`
- `flag` 直接套用的判定与 §4.1 一致（含 `flag is None` 但 `price<=storeLow` 的交叉校验）
- 状态库结构从第一天就按完整版写（`seen_deal` / `game_meta` / `run_log`），符合 §1.1

---

## 2. P0 问题

### P0-1 报表与详情没有解耦（§3.3 明确要求「报表永远不等详情」）

**现象**：`data/logs/out_resume/index.html` 在被打断的那一轮里不存在。
`accept_resume1.log` 停在 `[4/6] 当日新增候选：106 条`，之后再无输出，也就没有页面。

**证据**（`run.py`）：

```
210  detail_fetched = fetch_details(...)      ← 慢的部分（一游戏一请求）
213  merged = merge_details(...)
214  kept, deduped = classify.dedupe_by_appid(merged)
240  cards = [report.build_card(...)]
241  paths = report.render(...)               ← 到这里才第一次写 index.html
```

渲染是流水线的**最后一步**，所以「详情抓完」是「页面存在」的前提。

**顺带一个更隐蔽的同类问题**：`state.save()` 只在两个地方被调用 ——
`fetch_details` 内部每 20 条（`run.py:139`）和函数结尾（`run.py:263`）。
`run.py:207-208` 那个把 5243 条史低写进 `seen_deal` 的循环**之后没有任何落盘**。
所以如果详情阶段一个都没抓成（ITAD 抖动 / 网络断），
`fetched` 恒为 0 → 一次 `save()` 都不会发生 → **整轮 5243 条的攒库全部丢失**。

**修改方案**

把「合并 → 去重 → 分档 → 渲染」抽成一个可重复调用的函数，调用两次：

```python
def render_pass(state, candidates, cfg, now, stats, tag) -> dict:
    """合并缓存 → 去重 → 分档 → 出报表。可以被调用多次。"""
    merged = merge_details(state, candidates, cfg)
    kept, deduped = classify.dedupe_by_appid(merged)
    tier_counts, shown = classify_tiers(kept, cfg)
    cards = [report.build_card(e, now) for e in shown]
    paths = report.render(cfg, cards, stats, now)
    return {"paths": paths, "tier": tier_counts, "shown": len(shown), "deduped": deduped}
```

`run_daily` 改成：

```python
for entry in hist_low:
    state.record_seen(entry, now)
state.save(now)                       # ① 先落盘「今日所见」，不等详情
                                      #    这样即便详情全失败，攒库也是安全的

first = render_pass(..., stats(first=True), tag="首版")
log(f"[5/6] 首版报表已生成（详情未抓，页面立刻可看）：{first['paths']['index']}")

detail_fetched = fetch_details(client, state, candidates, cfg, now)   # ② 慢的部分
state.save(now)

final = render_pass(..., stats(final=True), tag="完整版")             # ③ 覆盖
```

要点：
- 两次渲染**用同一个 `now`**，`generated_at` 与 `?v=` 保持一致，
  避免「页面生成时间不是本次运行时间」这种自相矛盾。
- 首版渲染前先 `state.save()`，把 P0-1 那个隐蔽问题一并解决。
- 首版里的 `stats` 要把 `detail_fetched` 写成 0、`pending` 数如实给，
  页面上就是「详情待补 N 条」——这正是 §3.3 想要的效果。
- `[N/6]` 的阶段文案要跟着改（现在是 6 步线性，改完第一版渲染插在取详情之前）。

**怎么验证**：把 `detail_daily_budget` 之类的机制造出「详情抓不完」，
在第 3 条详情时强杀进程，然后检查 `output/index.html` 已存在、
且页面上有「详情待补」分组 —— 这条过了，§11 的「大促模拟」就过了。

### P0-2 `.gitignore` 会把状态文件一起忽略掉（§8 的机制会静默失效）

**现象**：`.gitignore:2` 写的是 `data/`，而 §8 要求「用 `GITHUB_TOKEN` 把状态 JSON 提交回仓库」。

**后果**：第 9 步写 workflow 时，`git add data/state.json` 会被忽略规则吞掉，
**不会报错**，只是没提交。表现是：每次 Actions 都从空状态开始 →
① 详情缓存全失效（每轮重抓）、② `game_meta` 的 TTL 形同虚设、
③ §8 提到的「每日提交顺带规避公开仓库 60 天无活动禁用定时任务」也一起失效。
这是个上线后会非常难查的坑（因为本地跑完全正常）。

**修改方案**：改 `.gitignore`

```gitignore
data/*
!data/state.json
output/
```

必须写成 `data/*` 再取反。写成 `data/` 时 git 根本不会进入该目录，
后面的 `!` 取反是无效的（这是 gitignore 的已知行为）。

改动很小，但**必须在第 9 步之前做**。

---

## 3. 【本轮新实测】每轮 162 次翻页可以压到 27 次

这是你清单里第 5 条「可选优化」——我把它探完了，结论比预期好。

我下载了 ITAD 的 OpenAPI 规范（`docs.isthereanydeal.com/openapi.yaml`，186 KB）
并跑了两个只读探针（`data/logs/_probe_filter.py`、`_probe_filter2.py`，
合计 **148 次 ITAD 请求**，其中 `_probe_filter2_out.txt` 是结论）。

### 3.1 官方文档没写、但实测可用的服务端 filter

`/deals/v2` 的 `filter` 参数直接接受 JSON（形如 `filter={"cut":{"min":50,"max":null}}`），
可用字段（**全部实测通过**）：

| 字段 | 语义 | 实测 |
|---|---|---|
| `type` | `[1]`=本体 / `[2]`=DLC / `[3]`=包 / `[7]`=软件 | 单页 200/200 全是 `type=game` |
| `flag` | 史低标记，**层级语义** | 见 3.2 |
| `cut` / `price` / `regular` | `{"min":…,"max":…}` 区间 | 可用 |
| `steamCount` | Steam 评价数区间 | `{"min":100}` → 20 页 / 3864 条 |
| `steamPerc` | Steam 好评率 | ⚠️ 见 3.4 |
| `added` | 「ITAD 在最近 N 小时**发现**的折扣」 | `added=24` + `type=[1]` + `flag=N` → **1 页 / 69 条** |
| `expiry` / `releaseDays` / `releaseDate` | 有效期 / 发行时间 | 未测 |
| `shops` / `drm` / `platform` / `mature` / `tags` | 同类 | 未测 |

### 3.2 关键发现：`flag` 是**层级**语义，不是等值匹配

实测（国区、`shops=61`、`limit=200`、逐页翻完）：

| 查询 | 页数 | 条数 | 对照状态库 |
|---|---|---|---|
| `{"type":[1], "flag":"N"}` | 7 页 | **1,223** | 状态库里 `flag=N` 有 **1,224** 条 ✅ |
| `{"type":[1], "flag":"H"}` | 23 页 | **4,530** | = N(1224) + H(3310) = **4,534** ✅ |
| `{"type":[1], "flag":"S"}` | 27 页 | **5,243** | = 全部史低 **5,246** ✅ |

也就是说 `flag` 是「**至少**达到这个档」：
`N ⊂ H ⊂ S`，`flag="S"` 返回**全部史低**。
（这也解释了为什么 `S` 那一栏最多 —— 它是最宽松的档。）

**所以 `filter={"type":[1],"flag":"S"}` 一次查询 = 「本体游戏 + 全部史低」，
27 页 / 5243 条，替掉现在的 162 页 / 32364 条。**

**交叉验证（说明服务端过滤是无损的）**：
`{"type":[1]}` 单查得到 **48 页 / 9531 条**，
而实际全量抓取后客户端筛出的本体游戏是 **9535 条** —— 差 4 条（两次调用之间的数据抖动）。
**结论：服务端 `type` 过滤与客户端过滤结果一致，可以放心交给服务端。**

### 3.3 成本对比

| 方案 | deals/v2 页数 | 说明 |
|---|---|---|
| 现状 | **162** | 无 filter，抓 32364 条后在客户端筛 |
| 加 `type=[1]` | 48 | 只用服务端 type 过滤，其余照旧 |
| 加 `type=[1]` + `flag=S` | **27** | **推荐默认**，等于「本体 + 史低」，客户端判定逻辑不变 |
| 再加 `steamCount>=100` | 20 | 会**丢掉「冷门」这一档的计数**，不建议 |

配合「详情只取当日新增」，一轮总请求从 **267 次降到约 132 次**（再算上 P1-4 的修复会更低）。

**注意要保住 `run_log` 的统计口径**：加了 `flag=S` 之后，客户端再也看不到非史低的折扣，
`type_not_game`（22829）、`not_historical_low`（4286）、`flag_price_mismatch`（§4.1 的交叉校验）
这三个计数就拿不到了。建议：
- 默认跑「27 页」模式，`run_log` 里记为 `mode=daily, sweep=filtered`；
- 另加一个 `--audit`（无 filter 全量），**按需/每周一次**，专门产出完整的分布与交叉校验，
  `run_log` 记为 `mode=audit`。这样诊断能力不丢，日常成本降下来。

### 3.4 两个「静默出错」的坑（必须写进文档，否则以后会踩）

1. **`steamPerc` 的文档值域是错的。** OpenAPI 里写 `minimum: 0, maximum: 100`，
   但实测**必须传 0~1 的小数**：
   - `{"steamPerc":{"min":70}}` → **0 条**（不是报错，是静默返回空）
   - `{"steamPerc":{"min":0.7}}` → 正常返回
   传错不会报错、只会「今天一个游戏都没有」，属于最阴的一类 bug。

2. **`flag` 传数组会被静默忽略。**
   `{"flag":["N","H","S"]}` 实测返回的结果与**完全不传 filter 一样**（不报错、不过滤）。
   所以要拿全部史低，只能用单值 `"S"`（见 3.2 的层级语义），不能传数组。

### 3.5 顺带否掉一条：「按 timestamp 排序」不可行

`sort` 只接受网站折扣列表那几个值。实测
`-timestamp` / `timestamp` / `added` / `-added` / `release` / `-release`
**全部 400 `Unknown 'sort' value`**。

所以「用排序把 162 页压下来」这条路是死的；
但 §3.2 的 `filter` 路子更彻底（不是排序，是直接筛），已经够用。
分页稳定性问题照旧存在（`sort=-cut` + `offset` 翻页期间数据变动可能重/漏），
不过状态库按键幂等，只影响 `run_log` 的计数，不影响正确性。

---

## 4. P1 问题

### P1-1 状态库 5.09 MB / 天，提交回仓库的膨胀（你清单第 2 条）

实测数据（`data/logs/_review_size_out.txt`）：

| 存法 | 体积 | 相对现状 |
|---|---|---|
| 现状（`indent=2` 全字段） | **5.09 MB** | — |
| ① 只改紧凑分隔符 | 4.15 MB | 省 19% |
| ② 紧凑 + gzip | 597 KB | 省 89% |
| ③ 精简到 16 个字段（紧凑） | 2.87 MB | 省 44% |
| ④ 精简 16 字段 + gzip | **374 KB** | 省 93% |
| ⑤ 再去掉封面 URL（紧凑） | 2.36 MB | 省 54% |

体积构成：`seen_deal` 占 4.83 MB（单条 0.85 KB），`game_meta` 只占 16.8 KB。
最占地的字段依次是 `banner` 483 KB、`boxart` 467 KB、`itad_url` 348 KB、
`game_id` 195 KB、四个时间戳各 138 KB、`title` 113 KB、`slug` 111 KB。

**哪些是白占地方的**：
- `banner` 与 `boxart` 大量重复（`normalize_item` 里 banner 本来就回落 boxart）
- `slug` 可由 `itad_url`/`game_id` 推、`shop_id` 恒 61、`type` 恒 `game`、`mature` 恒 `False`、`currency` 恒 `CNY`
- `itad_url` 只在卡片上做一个「ITAD」链接，§7.2 要求的链接是 Steam + 小黑盒，
  这条属于可砍项

**修改方案（建议按顺序做前两步）**：
1. `state.save()` 改 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` → 4.15 MB
2. 精简 `seen_deal` 的落库字段到 16 个（上面的 `SLIM_KEEP` 清单）→ 2.87 MB
3. 关于「仓库会不会被撑爆」，说清楚机制再决策：
   git 对 blob 会做 zlib 压缩 + **跨版本 delta**，而 state.json 每次变化很小，
   实际 packfile 增长通常**远小于**文件体积。但这不能当兜底 ——
   建议先按 1+2 做，跑 30 天后用 `git count-objects -vH` 看 `.git` 的真实增长，
   再决定要不要上第 4 步。
4. 如果确实涨太快，最终的解法是**把状态挪到独立的 `state` 分支、每轮 force push**
   （历史只保留最近一份），主分支只放代码。
   ⚠️ 但这样主分支就没有每日提交了，会**触发 §8 那个「公开仓库 60 天无活动禁用定时任务」的坑**
   —— 需要另找一个活动源（例如保留一条极小的 heartbeat 提交）。
   也就是说这两件事是联动的，别只改一半。

> 另外 6.07 MB 现在堆在 `data/logs/`（`state_resume.json` 5.33 MB + `state_rl.json` 580 KB），
> 见 §6 的清理建议。

### P1-2 无 appid 的条目每轮都会被重新请求（你清单第 3 条）

**证据**（`run.py:127`）：

```python
if state.has_appid(game_id) and state.reviews_valid(game_id, now, ttl):
    continue
```

`has_appid` 为假时**短路**，`reviews_valid` 里的 TTL 判断根本没机会执行。
而 `reviews_valid`（`state.py:119-128`）本身已经处理了「抓过但没数据」的情况
（走 1 天 TTL）—— 所以 `has_appid and` 这个前缀是**多余且有害的**。

实测影响：当前 `game_meta` 105 条里有 **1 条** appid 缺失，每轮白花 1 次；
量级放大到几千条时，就是每轮几百次白请求。

**修改方案**：
```python
# run.py:127 去掉前缀
if state.meta_valid(game_id, now, ttl, empty_ttl):
    continue
```
并把「抓过但 ITAD 没有 Steam 评测数据」的 TTL 从写死的 1 天提取成配置项
（建议 `reviews_empty_ttl_days = 3`）—— 因为有些冷门游戏确实永远没有好评率，
1 天太频繁、7 天又太久。
另：`state.reviews_valid` 建议改名 `meta_valid` 或新增方法，名字现在和它的实际职责（整体缓存有效性）不符。

**验证**：造一条 `{"reviews": null, "fetched_at": <now>}` 的 meta，
连跑两次 `run_daily`，第二次 `detail_fetched` 必须是 0。

### P1-3 概览的「当日新增」显示的是**过滤后**的条数，会少报

**证据**：`output/latest.json`

```json
"new_today": 20,
"new_today_raw": 105,
"shown_total": 20,
```

页面顶部四格（`templates/index.html.j2:24-39`）显示：
`当日新增 20` / `史低总数 5246` / `抓取总数 32364` / `本页展示 20`。

问题有两个：
1. 标签写「当日新增」，数字却是**分档过滤之后**的 20，而真实当日新增是 **105**。
   你会以为今天只有 20 个新史低，实际有 105 个（85 个被「冷门/未达标」砍掉了）。
2. 四格里有两格数字完全一样（当日新增=本页展示=20），浪费一格。

**修改方案**：
- `report.py` 的 `stats` 用 `new_today_raw` 作为「当日新增」，
  另加 `new_today_shown`；
- 模板四格改成：`当日新增 105` · `进列表 20` · `史低总数 5246` · `抓取总数 32364`；
- `latest.json` 里那个叫 `new_today` 但其实等于「进列表」的字段改名为 `shown`，避免以后对接时被误读。

### P1-4 测试配置里写死了真实 API key（我发现的，与 §2.1 的纪律冲突）

```
data/logs/cfg_ratelimit.json:2   "itad_api_key": "（真实 key，已截略）"
data/logs/cfg_resume.json:2      "itad_api_key": "（真实 key，已截略）"
```

虽然 `data/` 被 gitignore、没有进仓库，但：
1. AGENTS.md 与 §2.1 都写了「**任何情况下不得写入入库文件**」，
   这两个文件正是「要不要清掉」的候选，一旦被挪出 `data/` 或误提交就出事；
2. 更实际的风险是这些文件会被复制来复制去（做验收、写文档、贴日志）。

**修改方案**：
- 立刻把这两处的 key 换成空串（测试用 `ITAD_API_KEY` 环境变量注入即可）；
- 以后写测试配置一律 `"itad_api_key": ""` + `set ITAD_API_KEY=…`；
- ⚠️ 这只 key 已经在本次会话里出现过（我为了跑探针读了 `config.json`）。
  如果它曾经出现在任何截图、聊天记录或 git 历史里，**建议直接去 ITAD 后台轮换一次**。

---

## 5. P2 问题（现在不阻塞，但上线前要处理）

### P2-1 「本周」视图的实现与 §4.6 定义不一致

`classify.py:240-246` 的 `in_week` 是**滚动 14 天**（`now - 14d ~ now + 1d`），
而 §4.6 定义是「**本周一 00:00 ~ 下周日 23:59:59**」（自然周对齐）。
两者在周中运行时会给出不同结果。

§11 有一条验收是「视图筛选结果与 §4.6 的定义逐条对得上」——
**现在实现和文档打架，上线时必然不过**。要么改实现（按自然周算），要么改文档。
单测 `test_in_week` 只验了「14 天内 True / 更早 False」，没覆盖周一 00:00、下周日 23:59:59 这两个边界。

顺带：`is_upcoming` 的 `hours` 参数默认写死 48，没有接 `cfg["upcoming_expiry_hours"]`。

### P2-2 前端分页断点与 CSS 断点不一致

- `templates/static/app.js:8`：`matchMedia("(max-width: 768px)")` → 每页 10 / 20 条
- `templates/static/app.css:125`：`@media (max-width: 560px)` → 移动端布局

在 **561~768 px** 宽度下，布局走桌面样式，但每页只给 10 条。
另外 `pageSize` 只在脚本加载时算一次，横竖屏切换不会重算。
建议：把断点提成一个常量（两处共用），并监听 `matchMedia` 的 change 事件后重渲染。

### P2-3 配置项有 5 个「配了但代码从没读过」

| 配置项 | 状态 |
|---|---|
| `detail_daily_budget` | §9 写了「每天额外补详情的条数上限」，**代码里从未读取** |
| `max_concurrency` | 未读取（实际靠串行实现，行为正确，但配置是装饰） |
| `use_steam_side_fetch` | 未读取（属第 7 步，可接受） |
| `fetch_last_low_time` | 未读取（§3.6 的 `storelow/v2` 还没实现） |
| `upcoming_expiry_hours` / `week_window_days` / `compare_countries` | 未读取（视图/比价还没上线） |

另外 `probe_timeout_seconds` 在 §9 里写了，但 **`config.py` 的 `DEFAULTS` 里根本没有这一项**
（`config.example.json` 也没有）。文档与代码的清单没对齐。

建议：把「已废弃/待实现」在 §9 里标出来，避免看配置的人以为它生效。

### P2-4 验收证据里混着不同版本的代码输出

`data/logs/daily.log`（21:38 那次）显示 `分档：{quality:20, notable:0, cold:44, other:3, pending:38}`，
而 `accept_resume2.log`（22:20）是 `{quality:20, cold:83, other:3, pending:0}`。

同样的数据、同样的输入，`pending` 从 38 变 0 —— 因为代码改了：
现在「抓到了但 ITAD 没有 Steam 好评率」归入 `cold`（`run.py:146-148` 的注释写清楚了），
旧版则归入 `pending`。**两版都对，但两个日志放在一起会让人以为出了 bug。**
建议 `data/logs/` 按日期归档，或在日志头写一行代码版本/commit。

### P2-5 日志噪音

`fetch_deals` 的 `progress` 每页打一行，162 页就是 162 行（见任何一份日志）。
建议每 10 页打一次，或只在页数变化时打。对应的 `[N/6]` 阶段编号在 P0-1 改完后也要重排。

### P2-6 小瑕疵

- `templates/index.html.j2:65` 页脚文案 `数据来源：IsThereAnyDeal/ Steam·` 有多余空格，缺斜杠两边的空格；
- §7.2 要求卡片的 `title_zh` 是**中文名**，但代码里没有任何地方写 `title_zh`
  （`report.py:74` 读它，却永远是 `None`）→ 第一版卡片现在显示的是英文名，
  这条要等第 7 步 `steam.py` 才达成，**不算 bug，但 §11「中文名与 Steam 一致」这条现在不成立**；
- §7.4 要求页脚标注「汇率及取数日期」，现在没有（同样卡第 7 步）。

---

## 6. 关于「要不要清掉测试残留」

现有 `data/logs/` 共 **6.07 MB**：

| 文件 | 体积 | 建议 |
|---|---|---|
| `state_resume.json` | 5.33 MB | 删（它是被中断那轮的快照，`data/state.json` 才是正式的） |
| `state_rl.json` | 580 KB | 删（限流验收用的临时状态） |
| `out_resume/` `out_rl/` | 共约 25 KB | 保留或删，看要不要留证据 |
| `itad_openapi.yaml` | 187 KB | **保留** —— 这是我本轮下载的官方规范，以后查参数很快 |
| `baseline.log` / `daily.log` / `accept_*.log` | 共约 28 KB | 保留（验收证据） |
| `cfg_ratelimit.json` / `cfg_resume.json` | 各 1 KB | **先改 key 再删/留** |
| `_probe_filter*.py` / `_probe_filter*_out.txt` | 约 12 KB | 建议**移进 `tools/`** 并纳入版本管理（它们是正式探针，符合项目里 `tools/probe_*` 的惯例） |

结论：**可以清，但先做 P1-4 的 key 替换**。
我不会在你确认前动任何文件。

---

## 7. 文档需要更新的数字（你清单第 4 条）

`docs/DEVELOPMENT.md` 里这几处的实测值已经过时，建议改成实测数：

| 位置 | 文档现值 | 实测值（2026-09-21 国区） |
|---|---|---|
| §2.1 实测规模 | 折扣条目「> 5000 条（翻到 25 页上限仍未到底）」 | **32,364 条 / 162 页（翻到底了）** |
| §2.1 / §3.2 | 史低候选 **1228** | **5,246** |
| §2.1 type 分布 | `game 2727 / dlc 1092 / package 1007 / None 174` | 前 32364 条：本体 **9,535**，非本体 **22,829**（70.5%） |
| §3.3 请求预算 | 日常「约 30 + 几十~两百」次 | 实测 **267 次/轮**（162 + 105），且**可压到约 132**（§3） |
| §3.3 | 「`--baseline` 约 30 次」 | **162 次** |
| §7 视图 | — | 「即将过期（<48h）」实测 **2,406 条** → 其余 4 个视图上线后是千级体量，分页要重新设计 |

补充一组本轮首次拿到的分布（写文档很有用）：
- `flag` 分布：**N=1,224 / H=3,310 / S=712**（合计 5,246）
- 距到期：24h 内 **2,065** · 1~7 天 **1,746** · 8~14 天 **1,422** · 14 天以上 8

还有一条**验收标准本身要改**：§11 的「同一天重复运行，`当日新增` 数量不变（幂等）」——
实测 21:38 那次 105 条、22:20 那次 106 条。这不是幂等出问题，
而是 `当日新增` 的主口径是 `timestamp`，**每次运行都重新从实时数据算**，
期间有新折扣上线数就会变。真正该验的是「状态库不产生重复条目（同一 `(game_id, price, expiry)` 只写一次）」。
建议把这条改写清楚，否则会一直被当成失败。

---

## 8. 建议的开工顺序

我的建议是**先做 P0 两件 + P1 的高杠杆件**，再进第 7 步：

1. **P0-1** 报表与详情解耦（含「record_seen 后先 save」）—— 约半天，解掉 §11 唯一没过的验收
2. **P0-2** `.gitignore` 改 `data/*` + `!data/state.json` —— 5 分钟，但不定它第 9 步必炸
3. **P1-4** 清掉两份配置里的 key（顺手把残留文件规整进 `tools/`）
4. **P1-3** 概览口径修正（避免页面少报，观感问题也好解决）
5. **P1-2** 无 appid 重抓修复（3 行）
6. **§3 的 27 页方案** —— 改动集中在 `itad.fetch_deals` 加 `filter` 参数 +
   `run.py` 传参 + 新增 `--audit`，收益最大，但要同步改 `run_log` 的统计口径
7. **P1-1** 状态库精简（先做紧凑 + 精简字段，观察 30 天再决定要不要挪分支）
8. 然后才是 §12 第 7 步（`steam.py` + `fx.py`）→ 第 8 步 `--probe` → 第 9 步 workflow

第 3 步之前请不要动 `data/logs/`，因为那两份配置里的 key 还在。

---

## 9. 本轮我实际做了什么（可复核）

- 读了 `docs/DEVELOPMENT.md`、`AGENTS.md`、`spec.md`、`run.py`、
  `src/` 全部 7 个模块、`templates/` 三个文件、`tests/` 三个文件、`.gitignore`、`config.example.json`
- 读了 `data/logs/` 下全部验收日志，交叉核对了 `output/latest.json` 与 `data/state.json`
- 跑通 `python -m unittest discover -s tests -v` → **32 passed**
- 下载官方 `openapi.yaml`（187 KB）核对 filter / sort 的规范定义
- 跑了 **148 次 ITAD 请求**的只读探针（2 个脚本，皆在 `data/logs/` 下）：
  验证 `type` / `flag` / `steamCount` / `steamPerc` / `added` 五个 filter 与 6 个 sort 值
- 探针期间 **429=0、软限流=0、5xx=0**
- **没有修改任何项目代码 / 配置 / 状态文件**（新增文件只有 `data/logs/` 下的探针与统计脚本，
  该目录已在 gitignore 内）

---

## 10. 修复结果（按 §8 的顺序执行）

### 10.1 改了什么

| # | 项 | 落点 |
|---|---|---|
| 1 | 报表与详情解耦 | `run.py`：新增 `render_pass()`，`run_daily` 里渲染两次（取详情前 / 后）；两次共用同一个 `now`；阶段编号从 6 步改 7 步（`--audit` 为 3 步） |
| 1 | 状态先落盘 | `run.py`：`record_seen` 循环之后立刻 `state.save(now)`，注释写明原因 |
| 2 | `.gitignore` | `data/` → `data/*` + `!data/state.json`，并写了「为什么必须这么写」的注释。**已在临时仓库用 `git add -A --dry-run` 实测**：旧写法会静默跳过 `state.json`，新写法正常暂存 ✓ |
| 3 | key 清理 | `data/logs/cfg_ratelimit.json` / `cfg_resume.json` 的 `itad_api_key` 置空 + 加 `_note`；探针脚本收进 `tools/probe_deals_filter.py`（合并两轮、可 `--quick`） |
| 4 | 概览口径 | `report.py` 的 `build_stats()`：新增 `new_today_raw` / `new_today_shown` / `detail_pending`；模板四格改为「当日新增 / 进列表 / 史低总数 / 抓取总数」；`latest.json` 的 `new_today` → `shown`；页脚补上抓取口径 |
| 5 | 无 appid 重抓 | `state.reviews_valid` → **`meta_valid(game_id, now, ttl_days, empty_ttl_days)`**；`fetch_details` 去掉 `has_appid and` 前缀；新增配置 `reviews_empty_ttl_days=3` |
| 6 | 27 页方案 | `itad.py`：新增 `sweep_filter()` / `SWEEP_LOW_ONLY` / `SWEEP_FULL` / `FLAG_ANY_LOW`，`fetch_deals` 接受 `sweep`；`run.py` 新增 `--audit` 与 `sweep_mode` 配置；`run_log` 每条都带 `sweep` 字段 |
| 7 | 状态库精简 | `classify.SEEN_KEEP`（17 字段）+ `classify.slim_deal()`；`state.save()` 紧凑 JSON **且幂等收敛旧数据**（存量 5MB 文件下次保存自动变精简） |
| 顺手 | 日志噪音 | `progress_log` 只在第 1 页与每 10 页打一行 |
| 顺手 | 分页断点 | JS 与 CSS 共用 `mobile_breakpoint_px=768`，并监听断点变化重画（修掉「561~768px 布局按桌面、每页按手机」） |
| 顺手 | 配置补齐 | `probe_timeout_seconds` 补进 `config.py` 的 DEFAULTS 与 `config.example.json`（原先只有文档里有） |
| 文档 | 数字回写 | `DEVELOPMENT.md`：新增「服务端 filter」小节与三个静默坑；实测规模全量重写；§3.2 加抓取口径表；§3.3 请求预算改实测；§9 补 6 个配置项；§11 幂等验收改写 + 新增两条解耦验收；新增 §12.1 修正记录 |

### 10.2 实测验证

| 验收项 | 结果 |
|---|---|
| 单测 | **44 passed**（新增 `test_itad_sweep.py`、`meta_valid` / `slim_deal` / 精简收敛的用例） |
| §11 大促模拟（强杀详情阶段） | **通过**。`index.html` 在 **64.6 秒**出现，此时进程仍在抓详情；强杀后页面仍在，105 条全在「详情待补」分组，概览 `当日新增=105 · 进列表=105 · 详情已抓=0 · 详情待补=105` |
| 状态先落盘 | **通过**。同一时刻 `seen_deal=5239 条`、`game_meta=0 条`（一条详情都没抓成，攒库没丢） |
| 每轮请求量 | **267 次 → 27 次**（`--audit` 仍是 162 次，按需跑） |
| 日常运行 | 27 页 / 5242 条 → 史低 5239 → 当日新增 105 → 进列表 20（quality 20 / cold 82 / other 3） |
| 同一天重复运行 | 两次都是「当日新增 105、详情新抓 0（全部命中缓存）」，状态库键幂等 |
| `--audit` | 通过（用 `max_deals=600` 限流验证代码路径）：`type_not_game=90 · free=3 · not_historical_low=250` + `flag 分布`，未取详情、未出报表 |
| 状态库体积 | **5.09 MB → 3.27 MB**（省 36%；保留 `itad_url`，比预估的 2.87 MB 多约 0.4 MB） |
| 概览口径 | `output/index.html` 实测显示 `105 / 20 / 5239 / 5242`（改前是 `20 / … / 20` 两格重复且少报） |

验收脚本可重复执行：`python tools/accept_coupling.py`；
原始输出 `data/probe/accept_coupling_result.txt`、`run_daily_r2.log`、`accept_audit.log`。

### 10.3 已知的遗留

1. **「本周」视图窗口仍与 §4.6 不一致**（`in_week` 是滚动 14 天）。
   该视图第一版不上线，所以没在这轮动 —— 但**开视图前必须二选一改齐**（改实现或改文档），
   已写进 `DEVELOPMENT.md §12.1`。
2. **`data/logs/` 的旧验收产物未删**（`state_resume.json` 5.33 MB、`state_rl.json` 580 KB、
   `out_rl/`、`out_resume/`）。本轮想删时被沙箱的回收站机制挡下
   （`safe-delete ... trash-failed`），需要你手动删或换成别的删除方式。
   > `cfg_*.json` 里的 key 已经清空，所以现在删它们不再有泄密风险。
3. **`detail_daily_budget` / `max_concurrency` / `fetch_last_low_time` / `use_steam_side_fetch`
   仍是「配了但代码没读」** —— 分别等第 7~8 步与 §3.6。
4. 那只 API key 仍在 `config.json` 里（这是它唯一该在的地方）。
   由于它在本次会话中被读取过，**仍建议去 ITAD 后台轮换一次**。
5. **仓库还没有 `git init`**（工作区里没有 `.git`）。所以：
   - 「仓库按天膨胀」目前还只是前瞻性问题（已把状态库从 5.09 MB 压到 3.27 MB）；
   - 第 9 步之前要先建仓库、加 remote，并确认 `.gitignore` 生效。

