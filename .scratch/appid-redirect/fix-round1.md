# appid 重定向修复清单（round 1）

> 来源：`debug_2026_09_25.md`（2026-09-25 检查报告，已从仓库根移入本目录）
> 约定：本文件即工单清单，**每步做完立刻验证**；**提交需用户明确指示，本轮不提交**。

## 起点结论

`ask-matt` 路由：本批落在主流程第 3 步的「**非多会话**」分支 —— **就地实现，不拆 ticket**。

理由：诊断已经完成（报告把根因钉到行号，并用 17 次真实请求复现），走 `/diagnosing-bugs`
是重做一遍；三个 bug 各 3–5 行、相互独立，单个上下文窗口可完成，达不到 `/to-tickets` 门槛。
流程为 `/tdd`（逐 bug 红→绿）→ `/code-review`（提交前两轴审查）。

**用户裁决**：P0 只修 `info()` 单 appid 路径（报告方案 A）；批量价格路径的同类风险
**留作新调查项**，不并入本次修改面。

---

## 步骤 1：P0 修 `info()` —— 已完成

**事实**：`_appdetails()` 用 `int(key)` 建字典，key 是 **Steam 返回的 appid**；
而 `info()` 用**请求的 appid** 去 `data.get(appid)`，Steam 一旦重定向就取到 `None`
→ `enrich.py:84` 静默跳过 → 中文名永久丢失（`set_title_zh` 从未写入，无脏数据）。

**改法**（`src/steam.py`）：单 appid 请求响应至多一个条目，直接取其值：

```python
data = self._appdetails([appid], cc, SINGLE_FILTERS)
if not data:
    return None
# Steam 可能把请求的 appid 重定向到新 appid，返回 key 未必等于请求 appid
payload = next(iter(data.values()))
```

同时在 `_appdetails` docstring 补上「key 未必等于请求 appid」这条实测结论，避免后人再踩
（正是原 docstring 只讲「没中文标题属正常」，把 bug 和正常回落混为一谈）。

**验证**：新增 `tests/test_steam.py` 两个测试（seam = `SteamClient.info()` 公开接口，
假响应顶掉 `request`，不发网络）：

- `test_name_survives_appid_redirect`：请求 412020、响应 key 1952352 → 取到「地铁：离乡」
- `test_unsuccessful_entry_returns_none`：`success: false` → 返回 None，不编造数据

改前 118 个测试通过，改后 120 个通过（`python -m unittest discover -s tests -t tests`）。

**端到端验证**（`verify_fix.py`，走生产代码路径，真实请求 2 次）：

```
info(412020)  → {'name': '地铁：离乡', 'currency': 'CNY', 'initial': 14800, 'final': 1480, ...}
info(1091500) → {'name': '赛博朋克 2077', 'currency': 'CNY', 'initial': 29800, 'final': 29800, ...}
```

两者修复前必定返回 `None`（它们的返回 key 分别是 1952352 / 2441600，与请求值不同）。

---

## 步骤 2：回填 2368 条缺失中文名 —— 待启动

修复后 `state.title_zh(game_id)` 仍返回 `None`，条目会自然进入重抓路径，但受
`prefetch_daily_budget=300` 限制需 ~8 天。一次性脚本可绕过预算。

**注意**：早期统计的「prefetch 成功率 99–100%」不成立 —— 它统计的是「请求成功且拿到
name」，bug 场景下 `info()` 返回 `None` 时 `title_zh_fetched` 不计数。真实吞吐要修复后重测。

### 抽样估算收益（`probe_yield.py`，20 次请求，2026-09-25）

池：本地 `state.json`（09-24 版）3273 条 `game_meta` → 有 appid 3234 → **有 appid 且无
`title_zh` 1769 条**。等距抽 20 条，用生产 `SINGLE_FILTERS` 实跑：

| 结果 | 条数 |
| --- | --- |
| 现在能拿到中文名 ✓ | **6（30%）** |
| Steam 上只有英文名 | 14 |
| 无数据 / 失败 | 0 |
| 其中发生重定向 | 9（**45%**） |

按是否重定向拆开，正好对应 3.4 说的两种成因：

- **重定向 9 条**（修复前必定丢）：其中 2 条现在能补中文名，7 条本就是英文名
- **不重定向 11 条**（修复前就能拿到 → 说明**从未被抓过**，300/日预算没轮到）：其中 4 条有中文名

→ **修复的实际收益 ≈ 30%**。外推：报告口径 2368 条 → **~710 条**；本地口径 1769 条 → ~530 条。

> ⚠️ 20 个样本的 95% 置信区间约 10%–50%（对 2368 而言是 240–1180 条），**别当精确值**。

> ⚠️ **抽样偏差**：等距抽样假设池内同质，但 `prefetch_targets` 是「最近优先」，
> 实际回填顺序偏向近期在售的游戏。报告自己抽的 12 条重定向命中率是 **100%**，
> 与本轮 45% 差得远 —— 两者总有一个有偏，所以按池子整体估计未必等于实际回填顺序的结果。

> ⚠️ **限流**：本次会话累计 **41 次**请求，已到「~40/日静默超时」阈值，**今天不宜再发
> Steam 请求**。过程中第 20 条出现过 1 次读超时，客户端退避重试后成功。

---

## 步骤 3：重定向成因已查清（原「待调查」项已关闭）

### 3.1 用户假设「一个是全球 id、一个是国区 id」—— **不成立**

| 检验 | 实测 | 结论 |
| --- | --- | --- |
| 固定 412020，只变 `cc`（cn / us / ua），`l=schinese` | 三次都返回 key `1952352` | 与区域**无关** |
| 固定 412020，只变 `l`（schinese / english），`cc=cn` | 都返回 `1952352`；name 分别为「地铁：离乡」/ `Metro Exodus` | 与语言**无关**；中文名由 `l` 决定 |
| 直接请求 `1952352`（cn/schinese、us/english 都试） | `success: false`，无 `data` | **它不是 appid**，是店铺页 id |
| 读响应的 `data.steam_appid` | 恒等于请求的 appid（412020 → 412020） | 是同一个游戏，不是「另一个版本」 |

即：不存在「全球 id 无中文名、国区 id 才有」这回事 —— 中文名只取决于 `l=schinese`，
同一 appid 切 `l=english` 就是英文名。

### 3.2 真正的触发条件：`filters` 里带 `basic`

| 请求 | 返回 key |
| --- | --- |
| 单 + 不带 `filters` | `1952352` ★ |
| 单 + `filters=basic` | `1952352` ★ |
| 单 + `filters=basic,price_overview`（**生产 `SINGLE_FILTERS`**） | `1952352` ★ |
| 单 + `filters=price_overview` | `412020` |
| 批量 + `filters=price_overview`（生产 `BATCH_FILTERS`） | 与请求**等值且同序** |

要店铺页级字段（`basic`／全量）就必须走店铺页解析 → key 变成店铺页 id；
只要 `price_overview` 就按 appid 原样返回。**`name` 只能靠 `basic` 拿，所以重定向不可回避**，
P0 的修法是唯一正确解。

### 3.3 原待调查项「批量跨区价格错配」—— **证伪，关闭**

`prices()` 用 `BATCH_FILTERS=price_overview`，实测请求 `[412020, 1091500, 597820, 1501750]`
返回同四个 key、顺序一致，**不做重定向** → `enrich.py:119` 按请求 appid 查表是安全的。
（`price_overview` filter 下响应不含 `steam_appid`，无法反查，但现在不需要了。）

### 3.4 顺带修正：2368 条缺失**不全是**这个 bug 造成的

抽查缺 `title_zh` 的条目中，`Potio Mellow`(2280740)、`Night Feeder`(2669390)、
`Touch Grass VR Simulator`(3105020) **都不重定向**，Steam 上本就只有英文名 —— 属正常回落，
修 bug 也拿不到中文名。`title_zh=None` 把三种情况混在一起：

1. 从未抓过（300/日预算没轮到）
2. 抓过但被 bug 丢掉
3. Steam 本无中文名（抓到也会写英文名，如 1299510 存的就是英文）

**报告 §2.7 的 2368 是上限，不是实际收益**；回填前若要评估收益需先区分这三类。

### 3.5 附带观察：重定向映射会随时间变化

`1299510`（Vampire: The Masquerade – Swansong）库里**已存** `title_zh`，说明当初 `info()`
成功过（key 当时匹配）；但今天请求它会重定向到 `2407740`。`set_title_zh` 只有两个调用方
（`run.py:731`、`enrich.py:87`），都取自 `info()` 结果 —— 所以这不是脏数据，而是**映射在变**。
缓存的是名字不是 key，不影响正确性，但「曾经拿得到」推不出「以后拿得到」。

### 3.6 证据文件

脚本与原始输出都在 **`tools/probes/appid-redirect/`**（`tools/probes/` 已 gitignore，
是仓库既定的「探针归档不入库」位置，与 `src/steam.py` docstring 引用
`data/probe/summary27_steam_batch.txt` 同一惯例）。

| 文件 | 内容 | 请求数 |
| --- | --- | --- |
| `probe_cc_lang.py` / `.txt` | `cc` × `l` 交叉，5 个变量组合 | 8 |
| `probe_predict.py` / `.txt` | 从 state.json 抽样验预测 + 批量对比 | 7 |
| `probe_batch.py` → `probe_batch.txt`、`probe_filters.txt` | 批量 / filters 隔离 | 4 |
| `verify_fix.py` | 走生产代码路径端到端验证修复 | 2 |
| `probe_yield.py` / `.txt` | 抽 20 条估回填收益（见步骤 2） | 20 |

合计 **41** 次请求，全部串行 + ≥4 秒间隔（抽样那轮 5 秒），未触发限流封禁
（有 1 次读超时被客户端退避重试救回）。

---

## code-review 记录（2026-09-25，固定点 `3b72077`）

两轴（Standards / Spec）各跑一个子代理，结论摘录：

**采纳并已改：**

| 发现（轴） | 处理 |
| --- | --- |
| `_appdetails` docstring 写「批量请求下 Steam 不告诉哪个返回 key 对应哪个请求 appid，调用方需自行留意」—— 与 §3.3 已证伪的事实**矛盾**，会误导后人重复排查（Spec 轴 (c)） | 改为「`price_overview` 批量按请求 appid 原样返回且同序，`prices()` 查表安全」 |
| 模块 docstring「`data.steam_appid` **始终**等于请求的 appid」过头 —— `price_overview` 下不带该字段（Standards 轴 (b)） | 改成「带 `basic` 时…（`price_overview` 下不带该字段）」 |
| 重定向说明在模块 docstring / `_appdetails` docstring / `info` 行内**三处重述**（Standards 轴：Duplicated Code） | 模块 docstring 保留完整版，另两处压成一两句 |
| `docs/DEVELOPMENT.md` §2.2 是实测结论的归档处（该节自称权威、且原有 filters 表就在这里），本次未回写（Standards 轴 (a)） | 新增 §2.2 子节「appid 重定向」 |
| 测试用实例赋值 `client.request = lambda`，与仓库既有的子类覆盖手法（`tests/test_storelow.py::FakeRequestClient`）不一致（Standards 轴，判断项） | 改为 `FakeSteamRequestClient` 子类，并顺带断言请求形状（path / cc / l / `filters == SINGLE_FILTERS`） |

**明确不采纳：**

- ~~**把 `debug_2026_09_25.md` 移进 `.scratch/<slug>/`**~~ → **已按用户要求移入本目录**
  （2026-09-25 追加）。我原先以「该报告跨多个 feature，AGENTS.md 的目录约定只覆盖 spec 与
  issue」为由拒绝，用户否决了这个理由：检查报告就该放进 `.scratch/`，不该留在仓库根。
- **加 `len(data) == 1` 断言**（Spec 轴 (c) 提过、报告 §2.6 也只是「**可**加断言防御」）：
  对「不应发生」的场景做防御属 Speculative Generality。不加。

**两轴均确认无碍：** `next(iter(data.values()))` + `if not data` 与报告方案 A 逐字一致；
用户裁决「只修 `info()`」未越界；测试无网络请求、seam 取公开接口。Spec 轴另实跑全量测试 120 项全绿。

---

## 本轮范围外（已另处理）

- P1 `report.py::build_groups` 详情区三栏 + P1 `report.py::format_amount` 砍尾零
  → 见 `.scratch/report-ui/spec.md` 的 **批 G（2026-09-25）**。
  注意：报告对问题 2 的诊断（「只差 1 分钟的抖动」）经实测**不成立** —— 该组实际有 6 种
  开始时刻，用户改判为「按多数派上提 + 卡片一律不显示开始时刻」。
- ~~P2 `.github/workflows/daily.yml` 补跑 guard 窗口与间隔矛盾~~ → **已撤销，不是缺陷**：
  用户裁决（2026-09-25）——补跑是补救措施，被跳过 = 主跑成功，正是设计意图；12h 窗口宽裕
  恰为覆盖「主跑还在排队 / 被调度器丢弃」两种情况。**不要再当待修项提出。**