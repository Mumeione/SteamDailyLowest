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
  "version": 2,
  "generated_at": "2026-09-28T10:53:10+08:00",
  "window_hours": 48,
  "fx": {"date": "2026-09-28", "base": "CNY", "rates": {"UAH": 6.67, "INR": 12.1}},
  "count": 280,
  "items": [
    {
      "game_id": "8a1c...",
      "title": "Some Game",
      "title_zh": "某游戏",
      "appid": 570,
      "flag": "N",
      "price_int": 2990,
      "regular_int": 9990,
      "cut": 70,
      "currency": "CNY",
      "start": "2026-09-25T07:00:00+02:00",
      "expiry": "2026-09-29T07:00:00+02:00",
      "reviews": {"score": 92, "count": 12345},
      "compare": [],
      "key": "8a1c...|2990|2026-09-29T07:00:00+02:00"
    }
  ]
}
```

**字段语义与 null 约定**：

| 字段 | 说明 |
|---|---|
| `version` | 当前 `2`。消费方**必须按版本分支处理**，不得硬编码 `== 1`（v1 没有 `compare`/`fx`） |
| `generated_at` | 带时区偏移的生成时刻，是消费方判断**新鲜度的唯一依据** |
| `window_hours` | 窗口小时数（当前 48），仅作记录与自检 |
| `fx` | `{date, base, rates}`，方向恒为 base=CNY **不取倒数**；**允许 null**（汇率缓存缺失不阻断快照） |
| `count` / `items` | 条目数与数组；**空快照（count 0）是合法状态**，不要因空跳过写文件 |
| `items[].key` | `game_id\|price_int\|expiry`（`classify.deal_key()`），消费方直接用作幂等键 |
| `items[].appid` / `reviews` | **允许 null** —— 消费方应据此排除该条目（详情未补齐） |
| `items[].compare` | 跨区比价数组（`cc`/`label`/`currency`/`final`/`cny_minor`/`diff_pct`），**允许 null 或空**（个别区无售或拉取失败，实测约 2/280）；消费方降级省略即可，**不要过滤条目** |
| `items[].diff_pct` | 本期消费方不展示，保留备用 |

**实现耦合警示**：`compare` 的透传依赖 enrich 对条目 dict 的**原地写入**（`upcoming_shown`
与快照输入是同一批对象）。若将来 `render_pass` 改成复制条目，`compare` 会**静默变 null**。
增删 `KEEP` 字段（`src/snapshot.py`）属于契约变更，需同步消费方。

## 3. 改动位置（现状记录）

| 文件 | 内容 |
|---|---|
| `src/config.py` | `expiring_snapshot_path: data/expiring.json` |
| `src/snapshot.py` | 快照组装 + 原子写（`os.replace`）；口径与坑见其 docstring |
| `run.py` | `render_pass()` 返回 `upcoming_shown_items`（列表，新增）与 `upcoming_shown`（数字，**勿改类型**，run_log/render_report 在用）；`run_daily()` 在完整版渲染后写快照 |
| `daily.yml` | 状态回写步骤里把 `expiring.json` 一并提交（文件不存在则跳过） |
| `tests/test_snapshot.py` / `tests/test_pipeline.py` | 契约字面量断言 + 类型防回归断言 |

## 4. 验收

`python run.py` 后核对三件事：

1. `data/expiring.json` 存在，`generated_at` 带 `+08:00` 且是当前时刻；
2. 抽查字段齐全（`key`/`appid`/`flag`/`expiry`/`reviews`）；
3. `count` **等于**日志里「即将过期进列表 M 条」的 M（输入已是进列表条目）。

## 5. 观察点

- **`appid` 缺失率**：详情靠「当日新增 + 目录增量补齐（预算 300/日）」，刚进窗口的条目理论上
  已当过当日新增，但目录补齐有滞后。缺 appid 的条目由消费方排除，属预期；若长期大量缺失
  则是 detail 预算议题。
- **口径若将来变动**（窗口小时数、史低门定义），改本仓库一处即可，消费方无需同步——
  这正是本次改造的目的。但**字段是契约**，增删要双向同步。
