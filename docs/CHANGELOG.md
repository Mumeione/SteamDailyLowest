# CHANGELOG

> 跨特性的**里程碑索引**（对外可读）。逐轮细节在本地 `.scratch/<feature>/changelog.md` ——
> **`.scratch/` 是本地工作区、不入库**（见 `.gitignore`），故此处只登记「发生可见变化」的轮次。
>
> 本仓库的**公开文档面**就这几份：`README.md` · 本文件 · `docs/DEVELOPMENT.md`（当前设计） ·
> `CONTEXT.md`（领域口径）。时间一律北京时间。

---

## 2026-10-09 · 换档日两则修复 + 大卡/板块权重拆分

- **「距上次史低」换档日整行消失（热门 10/10、大额折扣 9/10 的 tie 卡）**：
  `low_time_cache` 键绑死 `game_id|expiry`，折扣期被 Steam 延长（跨周四换档续期）后
  键对不上，而「当日新增 / 即将过期」两条抓取路径都覆盖不到这种老条目 ⇒ 查找必 miss。
  修法：精确键 miss 时回退到该游戏已有缓存里折扣期最新的一条（值描述的是上一次
  到该价的时间，不随折扣期变）。
- **大卡与「新史低」板块条目/顺序完全一样**：此前共用同一套权重，大卡等于板块
  前 15 名复读。拆成两档：大卡 = `PICKS_WEIGHTS`（**折扣 40 优先** / 名气 30 /
  口碑 15 / 紧迫 10 / 新鲜 5，可 `picks_weights` 配置覆盖），新史低板块保持名气
  优先的推荐公式。两档都**不许有「史低类型」项**（分层取已保证新史低优先）。
  权重解析抽成单一出处 `report._resolve_weights`（两档共用，防以后再抄第三遍），
  并把 `report.py` / 测试里「大卡与精选同口径」的过期注释与 docstring 同步为「两档拆开」。
- 验证：pytest 353 passed + 8 subtests；本地 2026-10-08 数据实测大卡前 15 与板块前 10
  条目、顺序均已分叉；jsdom 冒烟全绿。

## 2026-10-08 · 分类页加载慢：all.js 按板块分片（20s → 首片 ~20KB）

- **问题**：首页秒开了（上一轮 data.js 瘦身），但**分类页仍要 ~20 秒**。实测线上
  `all.js` = 5,813,425 B（gzip 746,792 B），占那 20 秒的 ~95%；剩下 5% 是解析
  （5.81MB 对象字面量求值 203ms，等价 `JSON.parse` 只要 43.6ms）。
  ⇒ **不是纯 GitHub 网络问题**：慢线路是常量，是「一次性全量阻塞」把它放大成 20 秒。
- **否决掉的两个方案**（实测数据说话，别再回头试）：
  - 「把 all.js 拆成 card + compare 两份」：compare 只占 24.4%，拆掉 653KB → 550KB
    （**-16%**），20 秒 → 17 秒，感觉不出来。
  - 「共享池分片 + 板块索引」：池顺序（tier 分组 + 折扣降序）与板块排序**不相关**，
    实测「热门游戏」前 30 条散落在 18 个分片里，等于没优化。
  - 「列式排序键 `<key>_sort.js`」：只在「换排序」这条次要路径上省约 40%，
    却要在 app.js 里复制一份筛选判据（卡式 + 列式两套），与「判据只有一份」冲突 —— 已删除。
- **做法**：`all.js` 下线，改产 `all/<板块>_<片号>.js`（每片 200 条，`ALL_SHARD_SIZE`）。
  - **按板块各自顺序切片** ⇒ 顺序 = 分片的物理顺序，**不再下发 `section_order`**；
    卡片在板块间重复存储（发布体积 5.8MB → 13.1MB raw，可重建产物，换首屏速度值得）。
  - 第 0 片额外带 `total` / `shards` / `size` / `agg`（预聚合计数表：构建期算好
    5×160 个筛选组合的精确条数，约 4KB raw）⇒ **只加载 1 片也能显示精确的「共 N 条」**。
  - 前端 `window.ALL_S` 命名空间 + `loadShard()`（仍是 `<script>` 动态加载，不是 fetch ——
    file:// 下要能用）；命中率外推决定再取几片，`SHARD_CONCURRENCY = 4`。
- **实测**（线上 7276 张真实卡片）：首片 gzip `new_low` 19.8KB / `expiring` 18.2KB /
  `popular` 21.8KB / `big_cut` 18.6KB / `__all__` 19.4KB —— **降幅 97%**；
  构建耗时 0.5s；800 个预聚合计数与暴力重算 **0 个不一致**。
- **附带修复**：
  - `slim_for_all_js` 改幂等（原先 `all_section_orders` 的 `__all__` 池先瘦一次、
    写分片再瘦一次，第二次拿不到 `banner` ⇒ `art` 被抹成 None，实测 7100 → 0）。
  - `write_all_shards` 先清空 `output/all/`，防昨天的旧分片被一起发布。
  - `tools/check_payload.py` 修掉一个既有的 `NameError`（`bad_pair` 未定义）。
  - jsdom 冒烟「无封面占位」断言曾因封面多了 `art` 来源而**静默失效**，现已修（同时抹 `banner`+`art`）。
- 细节：本地 `.workbuddy/memory/2026-10-08.md`。

## 2026-10-08（晚） · 比价估算定案 + 板块排序重定 + UI 收尾批次

- **跨区比价「现查与估算分工」定案**：真查只覆盖「当日新增 + 即将到期」（防打折中途降价），
  其余板块历史条目用估算「外区原价缓存 × 国区折扣比例」（`enrich.estimate_compare` 单一出处，
  口径见 `CONTEXT.md`）。本地产物实测比价覆盖 98.8%。
- **四板块排序各自维度**（原三板块共用推荐公式，大促时名气头部全是大折扣 →「热门」与
  「大额折扣」头 10 逐项相同）：新史低 = 推荐公式（与大卡同口径）、即将到期 = 到期近 → 远、
  热门游戏 = 评价数降序（平手比好评率）、大额折扣 = 折扣降序（平手比评价数）。
- **UI 收尾六则**：顶部大卡张数上限 5 → 15（`home_picks`，min(新史低整数页×5, 15)、
  最低 5、平史低补位不硬凑）；四板块栏位数统一动态 `clamp(最小池量, 5, 10)`、池子不足如实显示；
  「今日新增」摘要移进大卡区；筛选面板全端统一为贴浮层按钮上沿的浮层（删手机专属分支）；
  返回顶部按钮换实心箭头；关于页删「仓库与运行状态」块。
- **配套**：`tools/sync_state_from_remote.py`（本机拉远端 data 分支四份 JSON 校验后原子替换，
  无备份、远端只读）；板块「一列 → 两列」断点改 `@container`（与大卡 4 列同一内容宽，
  冒烟测机械校验锁定阈值）。
- 细节：本地 `.workbuddy/memory/2026-10-08.md`。

## 2026-10-08 · 修复首屏加载慢（data.js 瘦身）

- 线上 `data.js` 被「即将过期」内联卡片撑到 **7.4MB**（大促统一到期 → 48h 窗口吞下整个活动池），
  首屏要几十秒。改为：payload 不再写 `groups` / `view_groups`，分组卡片只在 `all.js`
  （懒加载）；首页两个 `<script>` 加 `defer`。`data.js` 回落到 ~42KB。
- 连带修复：`tools/check_payload.py` 改从 `all.js` 读分组卡片（原读 `data.js.groups` → 会误报/假绿）。
- payload 瘦身顺带清掉无前端消费者的视图按钮组（`VIEWS` / `payload["views"]` /
  `render(upcoming_items=…, extra_counts=…)`），`app.js` 懒加载 `all.js` 补上 `?v=`；
  `latest.json` 的 `shown` 顺序加端到端测试；README / DEVELOPMENT 的旧视图描述同步。
- 细节：本地 `.workbuddy/memory/2026-10-08.md`。

## 2026-10-08 · 文档整理

- 建立本 CHANGELOG；`.scratch/` 全部移出版本库（`.gitignore` 忽略），旧跟踪文件 `git rm --cached`。
- `docs/DEVELOPMENT.md` 回归「纯设计文档」：抽走 S9/视觉调优工作日志（迁至本地记录），
  回补现行口径，修正与重构 spec 打架的旧结论。
- `.scratch/refactor-2026q4/`：spec 恢复为 live 文档 + 新增 `changelog.md`（本地）。
- `.scratch/ui-polish-2026q4/`：新增 `spec.md` + `changelog.md`（本地）。

## 2026-10-07 · S9 UI 重做 + 视觉调优 + S10 收尾

- 报表页重做：吸顶导航 + 首页四板块行列表 + 顶部大卡（推荐公式 v2）+ 「关于网站」页 +
  筛选面板 + 精简模式 + 返回顶部 + 顶部消息区（节日/告警/通知）+ 空状态插画。
- 随后 **9 轮视觉调优**：配色收敛四档 / 卡片改色条去徽章 / 筛选按钮与面板 / 摘要行 /
  站点图标与 favicon / 精简模式两行网格 / 关于页网格对齐 / 无效选项置灰 / 列表「精选」改用推荐公式。
- S10 收尾：P2 延后项清理（`unlisted_frozen` / `refresh_ttl_days` / `_scope` 常量化）。
- 细节：本地 `.scratch/ui-polish-2026q4/{spec,changelog}.md`。

## 2026-10-04 ~ 10-07 · 大重构 S1–S10

- **S1–S3 止血组**：批量详情管线（Steam `GetItems` 主源 + ITAD 降级）+ **派生式欠账** +
  秋促欠账一次性回填（覆盖率 appid **99.2%** / reviews **89.8%**）。
- **S4–S7 状态与报表**：状态库切分（`state` / `dynamic` / `cache` 三层）+ 折扣感知四档 TTL +
  `unlisted` 机制 + 跨区比价换模型（区域原价永久缓存 + 现价真查即校准）。
- **S8 工作流整理**：job 超时 / 失败自动开 Issue / `$GITHUB_STEP_SUMMARY` / permissions 最小化 /
  报表发布韧性（`!cancelled` 兜底）/ prefetch 去 list 化 / actions 升当前主版本 / 退出码细分。
- 细节：本地 `.scratch/refactor-2026q4/`（`spec.md` + `changelog.md` + `archive/`）。

## 2026-09-30 · expiring.json 快照契约 v4（`SNAPSHOT_VERSION` 2 → 3）

- 加 **Steam 口径史低分类**（`low_class`）+ 厂商 / 热度字段；改用 `steam_low_class()` 后
  实测修正 127/790 条被 `flag=="N"` 错分的条目。
- 细节：本地 `.scratch/expiring-snapshot/changelog.md`。

## 2026-09-27 · 「即将过期」视图上线 + 快照契约 v1/v2/v3

- 新增「即将过期」视图（批 H）；`expiring.json` 导出上线
  （v1 首版 → v2 加 `compare`/`fx` → v3 输入口径归位 + 改名 `upcoming.json`→`expiring.json`）。

## 更早 · 初版与多轮迭代

- 每日抓取 Steam 折扣 → 筛出史低 → 生成静态 HTML 报表（`index.html` + `data.js` + `all.js`）。
- 迭代记录（批 A~H、报表 UI、部署、预抓）在本地 `.scratch/{report-ui,deploy,prefetch,daily-lowest}/`。