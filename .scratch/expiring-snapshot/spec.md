# 「即将过期」快照导出：`data/expiring.json` 数据契约

> **一句话**：本仓库每天已经算出了「48h 内到期的史低」，把这份结果**多写一个文件**出来，
> 供外部数据管道消费，使其不必拉取 4MB 的全量 `state.json`。
> 消费方的具体用途不在本仓库讨论范围。
>
> **版本历史**：见同目录 `changelog.md`（v1→v3 的口径演化与实测数据）。
> **现行实现**：`src/snapshot.py`（本文只描述契约与约定，不内嵌源码）。
> **配套**：消费方仓库有自己的契约副本；**字段增删必须双向同步**。

## 1. 为什么改

原先的方案是消费方匿名 raw 拉 `data/state.json`（约 4MB），自己筛窗口、拼去重键、
取 appid 和好评率。问题有两个：口径逻辑（48h 窗口、史低门）要在两个仓库各实现一份，
将来改口径得同步改两处；消费方还要为一个每天只用一次的任务下载 4MB。

改成主仓库直接产出精简快照后：**窗口口径单点收敛在主仓库**，消费方拿到的是现成的
窗口内条目，下载量降到几十 KB。

> **口径边界的约定（v3 修订）**：快照导出的是**已进列表**（`is_shown`）的条目——口碑门槛
> 就是本仓库的进列表口径，**单点收敛在本仓库**，消费方不再自建近似门槛。
> **同 appid 去重**仍在内（同一游戏的多版本折扣只留一条）—— 一个游戏只应出现一个条目。

## 2. 契约

**路径**：`data/expiring.json`，落 data 分支与 `state.json` 同级（runner 无持久磁盘）。
**配置键**：`expiring_snapshot_path`（`src/config.py` DEFAULTS）。
**写出时机**：`run_daily()` 完整版报表渲染之后（只有那一轮跑过 enrich 且 merge_details 已合并）。

```json
{
  "version": 3,
  "generated_at": "2026-09-30T06:56:38+08:00",
  "window_hours": 48,
  "fx": {"date": "2026-09-30", "base": "CNY", "rates": {"UAH": 6.67, "INR": 12.1}},
  "count": 790,
  "items": [
    {
      "game_id": "8a1c...",
      "title": "Persona 3 Reload",
      "title_zh": "女神异闻录３ Reload",
      "appid": 2161700,
      "flag": "H",
      "low_class": "new",
      "price_int": 8040,
      "regular_int": 26800,
      "cut": 70,
      "currency": "CNY",
      "start": "2026-09-17T19:20:47+02:00",
      "expiry": "2026-10-01T19:00:00+02:00",
      "reviews": {"score": 95, "count": 43670},
      "publishers": [{"id": 369, "name": "SEGA"}],
      "developers": [{"id": 366, "name": "ATLUS"}],
      "stats": {"rank": 385, "waitlisted": 16847, "collected": 6601},
      "compare": [],
      "key": "8a1c...|8040|2026-10-01T19:00:00+02:00"
    }
  ]
}
```

**字段语义与 null 约定**：

| 字段 | 说明 |
|---|---|
| `version` | 当前 `3`。消费方**必须按版本分支处理**，不得硬编码 `== 1` / `== 3`（v1 没有 `compare`/`fx`；v1/v2 没有 `low_class`/`publishers`/`developers`/`stats`） |
| `generated_at` | 带时区偏移的生成时刻，是消费方判断**新鲜度的唯一依据** |
| `window_hours` | 窗口小时数（当前 48），仅作记录与自检 |
| `fx` | `{date, base, rates}`，方向恒为 base=CNY **不取倒数**；**允许 null**（汇率缓存缺失不阻断快照） |
| `count` / `items` | 条目数与数组；**空快照（count 0）是合法状态**，不要因空跳过写文件 |
| `items[].key` | `game_id\|price_int\|expiry`（`classify.deal_key()`），消费方直接用作幂等键 |
| `items[].appid` / `reviews` | **允许 null** —— 消费方应据此排除该条目（详情未补齐） |
| `items[].flag` | **ITAD 口径**（**全商店**）：`N` 全网首次到该价 / `H` 全网曾到 / `S` 该店铺史低 |
| `items[].low_class` | **Steam 口径**（只看 Steam 店内价史）：`new` / `tie` / `unknown`（`storeLow` 缺失，如实标记）。**快照不写 null** —— 判定函数对「storeLow 在而 flag 缺失/非法」的异常形态返回 None，快照层收敛为 `unknown`。⭐ **两套口径别混用** —— 实测 2026-09-30：790 条里 `flag="N"` 只有 63 条，而 `low_class="new"` 有 **190** 条（127 条被 ITAD 标成 H/S、但 Steam 店内是新高） |
| `items[].publishers` / `developers` | `[{"id", "name"}]`，来源 ITAD `info/v2`（**不新增请求**）。缺键/空一律 `[]`。单项 **id/name 至少有一个就保留**（缺 name 置空串、缺 id 置 null，全缺才丢）—— 消费方按 id 匹配，id 不能丢。**用 `id` 判同厂商，别用名字**（同一厂商有多种写法） |
| `items[].stats` | `{rank, waitlisted, collected}`；**允许 null**（回填完成前就是 null）。`waitlisted`（愿望单）可作热度参考，但**有领域性偏低**：已拥有基数极大的游戏（免费/长销网游）愿望单天然低，不能当 0 用 |
| `items[].compare` | 跨区比价数组（`cc`/`label`/`currency`/`final`/`cny_minor`/`diff_pct`），**允许 null 或空**（个别区无售或拉取失败，实测约 2/280）；消费方降级省略即可，**不要过滤条目** |
| `items[].diff_pct` | 本期消费方不展示，保留备用 |

> `low_class` 是**导出前现算**的（`build_snapshot` 调 `classify.steam_low_class`），
> 与报表卡片用的是同一个函数、同一份依据 —— 不存在第二套口径。
> 它依赖条目上的 `last_low_at`（`merge_details` 从 `low_time_cache` 合并而来），
> 该字段**不导出**：消费方不需要重算，也不该重算。

**实现耦合警示**：`compare` 的透传依赖 enrich 对条目 dict 的**原地写入**（`upcoming_shown`
与快照输入是同一批对象）。若将来 `render_pass` 改成复制条目，`compare` 会**静默变 null**。
增删 `KEEP` 字段（`src/snapshot.py`）属于契约变更，需同步消费方。

## 3. 改动位置（现状记录）

| 文件 | 内容 |
|---|---|
| `src/config.py` | `expiring_snapshot_path: data/expiring.json` |
| `src/snapshot.py` | 快照组装 + 原子写（`os.replace`）；`SNAPSHOT_VERSION = 3`、`KEEP` 含 v3 四个新字段；`low_class` 在这里现算；口径与坑见其 docstring |
| `src/itad.py` | `fetch_info()` 顺带返回 `publishers` / `developers` / `stats`（同一个 `info/v2` 响应，**不新增请求**） |
| `src/state.py` | `set_meta()` 多收三个可选字段（**只更新传进来的键、不碰 `fetched_at`**）；另有 `set_meta_extras()` 专供回填 |
| `run.py` | `fetch_details()` 把新字段落库；`merge_details()` 把它们合进条目（旧条目缺键 → `[]` / `None`）；`render_pass()` 返回 `upcoming_shown_items`（列表，新增）与 `upcoming_shown`（数字，**勿改类型**，run_log/render_report 在用）；`run_daily()` 在完整版渲染后写快照 |
| `tools/backfill_game_meta.py` | **一次性回填**历史 `game_meta` 的厂商 / stats（见 §6） |
| `daily.yml` | 状态回写步骤里把 `expiring.json` 一并提交（文件不存在则跳过） |
| `tests/test_snapshot.py` / `tests/test_pipeline.py` | 契约字面量断言 + 类型防回归断言 |

## 4. 验收

`python run.py` 后核对五件事：

1. `data/expiring.json` 存在，`generated_at` 带 `+08:00` 且是当前时刻；
2. `version == 3`；抽查字段齐全（`key`/`appid`/`flag`/**`low_class`**/`expiry`/`reviews`）；
3. `count` **等于**日志里「即将过期进列表 M 条」的 M（输入已是进列表条目）；
4. ⭐ **`low_class` 交叉校验（关键）**：抽样核对 ——
   - `flag == "N"` 的条目 `low_class` **必须**是 `new`；
   - `flag == "H"/"S"` 的条目里应当有**一批** `low_class == "new"`
     （2026-09-30 实测 127/790；若一条都没有，说明 `last_low_at` 没合进来）；
   - `store_low_int` 缺失的条目应为 `unknown`。
5. `publishers` / `developers` 在回填完成前**允许为空**（`[]`）；`stats` 允许 `null`。

## 5. 观察点

- **`appid` 缺失率**：详情靠「当日新增 + 目录增量补齐（预算 300/日）」，刚进窗口的条目理论上
  已当过当日新增，但目录补齐有滞后。缺 appid 的条目由消费方排除，属预期；若长期大量缺失
  则是 detail 预算议题。
- **口径若将来变动**（窗口小时数、史低门定义），改本仓库一处即可，消费方无需同步——
  这正是本次改造的目的。但**字段是契约**，增删要双向同步。

## 6. 一次性回填（2026-09-30）

`publishers` / `developers` / `stats` 从 v3 起才导出，而**已经缓存过详情的旧条目不会再发
`info/v2`**（`meta_valid()` 判 TTL 时不会因为缺新字段而重取 —— 新字段不是 TTL 字段）。
所以存量要单独补一次：

```
python tools/backfill_game_meta.py --dry-run   # 先看要补多少条
python tools/backfill_game_meta.py             # 真跑，可随时 Ctrl-C，下次自动续跑
```

- 只处理**有 `appid` 且 `publishers` 缺键或为 null** 的条目（空列表 `[]` = 「取到了，确实
  没有」，跳过 —— 判缺不能用真值测试，否则会永远重抓）；用 `set_meta_extras()` 写，
  **不碰 `reviews` 与 `fetched_at`**（回填没有重新拿好评率，不能刷新 reviews 的 TTL）。
- 默认**每 25 条落一次盘**并打印当前限流窗口用量（`--save-every` 可调）。
- ⚠️ **耗时按请求往返算，别按 `itad_min_interval` 算**：实测 ~1.4s/条，
  5047 条量级顺序跑约 **2 小时**。2026-09-30 的首次回填为了压到「一小时左右」
  用了双线程 + 共享滑动窗口的临时脚本（同一天实测 84 条/分 ≈ 58 分钟，窗口占用 ~430/800）。
- 回填产物要**推回 `data` 分支**才会被下一次 daily 用上；否则下次 daily 从
  `data` 分支取旧 `state.json`，回填结果丢失。

