# CHANGELOG

> 跨特性的**里程碑索引**（对外可读）。逐轮细节在本地 `.scratch/<feature>/changelog.md` ——
> **`.scratch/` 是本地工作区、不入库**（见 `.gitignore`），故此处只登记「发生可见变化」的轮次。
>
> 本仓库的**公开文档面**就这几份：`README.md` · 本文件 · `docs/DEVELOPMENT.md`（当前设计） ·
> `CONTEXT.md`（领域口径）。时间一律北京时间。

---

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