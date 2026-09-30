# changelog：expiring.json 快照契约 v1 → v4

> 版本演化的决策记录与实测数据。已并入本文件的原始交接文档（handoff-v1/v2/v2-review/v3）已删除；
> 消费方用途相关的细节不在此记录（本仓库公开）。
>
> ⚠️ **两套版本号别搞混**：本文件的「v1/v2/v3/v4」是**契约改动的轮次**，
> 快照里的 `SNAPSHOT_VERSION` 是**机器读的版本**。对应关系：
> 轮次 v1→`version 1`、v2→`version 2`、v3→`version 2`（只改口径与文件名）、**v4→`version 3`**。

## v1（2026-09-27）：快照导出

- 首版：`src/snapshot.py` 新建，`data/expiring.json` 随 data 分支持久化，原子写。
- 输入为「窗口内**全部**条目」（`upcoming_kept`），理由写的是「口碑门槛是消费方独有需求，
  不该反向污染主仓库」——**该理由后来被推翻**（见 v3）。
- 实测产出 1141 条；但其中 `reviews` 缺失 161 条、`appid` 缺失 14 条。
- 当时的一个设计意图（把全集也喂给 enrich 以补比价）在落地前即被否决，从未实现——
  实测依据：对 1141 个 appid 每天跑一遍中文名 + 比价，会显著拉长每日 job 并逼近 Steam 限流，
  收益却只覆盖「本来就冷门到不值得采用」的条目。**不扩大 `enrich_steam` 覆盖范围**这一结论
  在后续版本持续成立。

## v2（2026-09-27）：加 `compare` 与 `fx`

- `SNAPSHOT_VERSION` 升 2；`KEEP` 加 `compare`；新增 `build_fx()`（方向 base=CNY 不取倒数，
  非 dict / 无 rates → null）。
- **消费方须按 version 分支处理**——v1 文档漏了这条提醒。
- `fx: null` 属合法状态（汇率缓存缺失不阻断快照）。
- 评审发现的实现耦合（v2 评审遗留的重要结论）：`upcoming_kept` / `upcoming_shown` 是
  `build_view` 返回的**同一批 dict 对象**，enrich 原地写 `entry["compare"]` 快照才拿得到比价；
  若将来复制条目，`compare` 会**静默变 null**。KEEP 注释已写明，勿拆。
- v2 评审还指出：比价失败**不落缓存**，若当天比价恰好全失败，「compare 非空」的验收项会假阴性，
  应先查 enrich 日志再判实现问题。
- **fx 方向的正确性靠实跑人工核对**（`rates["UAH"] ≈ 6.6`，见 0.15 即被倒转）——单测只能证明
  「透传不变」，不能证明源方向。2026-09-28 实跑核对通过（UAH=6.674）。

## v4（2026-09-30）：加 Steam 口径史低分类与厂商 / 热度字段（`SNAPSHOT_VERSION` 2 → 3）

**起因（消费方的真实痛点，有实测支撑）**：消费方要用 `flag == "N"` 判「新史低」，口径不对。

- `flag` 是 ITAD 的**全商店**标记。存在「Steam 店内首次到该价、但别家更早更便宜过」
  因而被标成 `H`/`S` 的条目 —— 对只买 Steam 的读者，那**就是**新史低。
- 实测 2026-09-30（790 条）：`flag="N"` **63** 条，而 `steam_low_class() == "new"` **190** 条
  → **127 条被错分**，含彩虹六号：围攻、FF7 重制、孤岛惊魂 6、全境封锁 2、沙丘：觉醒、
  刺客信条：影、最后生还者2 重制。
- 关键点：**这个判定早就在算**（`classify.steam_low_class()`，`report.build_card()` 在用），
  只是 `KEEP` 没导出。所以本版**没有新逻辑**，纯粹是把它写出去。

**同时加的三类字段**（都来自**已经在调**的 `GET /games/info/v2`，**零新增请求**）：

- `publishers` / `developers`：`[{"id", "name"}]`，实测填充率 100%（52 条分层样本）。
  消费方要做「同厂商 / 同系列」的知名度传递（例：P3R 靠 P5R），**用 id 判、别用名字**。
- `stats`：`{rank, waitlisted, collected}`。`waitlisted`（愿望单）可作热度参考，
  但已知**领域性偏低** —— 已拥有基数极大的游戏（R6S 这类）愿望单天然低，不能当 0 用。
- **不进 `KEEP` 的**：`tags` / `releaseDate`（体积大、暂无用）、`last_low_at`
  （消费方不该重算 `low_class`）。

**实测/成本**：

- 52 条分层样本填充率：developers / publishers / tags / stats / reviews **100%**，releaseDate 98%。
- 体积：`state.json` 4.26 MB → ≈5.0 MB（每游戏 +≈150 B）；`expiring.json` 每条 +≈150 B。
- 一次性回填 5047 条 `game_meta`：见 `spec.md` §6（**顺序跑约 2 小时，别按 min_interval 估**）。
- **网页不展示这些字段**，`report.build_card()` 与前端**未改动**；口碑分档也**未改动**（用户裁决）。

**踩坑记录**：

- `low_class` 依赖 `store_low_int`（`low_kind()` 的门），而它**不在快照契约里** ——
  在消费方「重算」`low_class` 是行不通的（这也是必须导出而不是导出原料的原因）。
- `game_meta` 是**多代字段共存**的：旧条目缺新字段属正常，`meta_valid()` 不能因此重取；
  `set_meta()` 只更新传进来的键，`None` = 「本次没取到」→ 保留旧值。
- `fetch_info()` 响应缺 `stats` 时必须返回 **None**（不是全 null 的 dict）——
  全 null dict 会绕过 `set_meta` 的保留旧值保护、把已回填的有效 stats 覆盖掉
  （code-review 2026-09-30 抓到，`tests/test_itad_info.py` 钉住）。
- `classify.steam_low_class()` 在「storeLow 在而 flag 缺失/非法」时返回 **None**，
  快照层收敛为 `unknown` —— 契约是 new/tie/unknown 三值域，不写 null。
- `_party_list` 单项缺 name：**保留**、name 置空串（消费方按 id 判同厂商，id 不能丢）；
  id/name 全缺才丢。

**落地验收（2026-09-30，code-review 后）**：162 单测全绿；评审修复 6 项
（stats None 保护、low_class 三值域收敛、回填脚本判缺/落盘/窗口打印、set_meta 去重）。
换行符归一化**不在本版内**，单独 chore 提交处理（见 `.scratch/expiring-snapshot/issues/01-itad-mixed-eol.md`）。

## v3（2026-09-28）：口径归位 + 契约改名

- **输入从「窗口全部条目」改为「已进列表」（`is_shown`）条目。** v1 的理由站不住：
  口碑门槛不是消费方独有需求，它就是本仓库的进列表口径；导出全集会迫使消费方自建一套
  **近似**门槛（score≥70 & count≥100）——同一口径两处实现、静默漂移，正是本项目要消除的。
- **命名归位**：`upcoming.json` → `expiring.json`、配置键 `upcoming_snapshot_path` →
  `expiring_snapshot_path`（upcoming 表达「48h 内到期」不准确，仅契约层改名，内部报表视图名不动）。
- ⚠️ **类型坑（本版最易踩）**：**不要**把 `info["upcoming_shown"]` 从数字改成列表——它有 4 个
  引用点，其中 `run.py` 的 run_log 会把整个数组塞进 `state.json`（污染状态库 schema）、
  `tools/render_report.py` 会打印整坨数组。正确做法：新增并存的 `upcoming_shown_items`。
- 复现口径时的教训：判 tier 必须按 merge_details 的逻辑（`state.meta()` 缺失或无 `fetched_at`
  → `TIER_PENDING`），再过 `classify.is_shown`。

## 落地验收（2026-09-28）

- 本地实跑：候选 1183 → 进列表 **280** → 快照 **280**（count = 进列表数 ✓）；
- `version=2`、`fx.rates.UAH=6.674`（方向未倒转 ✓）、缺 `appid`/`reviews` 均 0、
  `compare` 空 2/280（预期抖动）、flag 分布 H 194 / S 51 / N 35；
- `state.json` 的 `run_log.upcoming_shown` 仍为 int（类型坑未踩 ✓）；全量 154 单测通过。

## 实测口径补充（v2 时期数据，决策依据备查）

- 窗口条目的到期时刻**高度集中**（24–48h 占 938/1141；同一场促销统一结束），
  每天面对的是「当天到期的一大批」而非均匀涓流——消费方的去重设计需按此建模。
- 「新史低无门槛」在真实数据下不可行（v2 时期 flag=N 达 140 条，其中评价数 ≥500 的仅 13 条）。
