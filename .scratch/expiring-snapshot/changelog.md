# changelog：expiring.json 快照契约 v1 → v3

> 版本演化的决策记录与实测数据。已并入本文件的原始交接文档（handoff-v1/v2/v2-review/v3）已删除；
> 消费方用途相关的细节不在此记录（本仓库公开）。

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
