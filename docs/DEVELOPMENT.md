# 开发文档索引

> 本仓库的**设计文档按主题拆分**在 `docs/` 下，本文件是入口：给出阅读顺序、
> 各文档的定位，以及**文档维护规则**（防止同一口径写多处、改一处要改多处）。
> 版本里程碑见 [CHANGELOG.md](CHANGELOG.md)；各特性的方案与迭代史是维护者/agent 的
> 本地工作文档（`.scratch/`，**不入库**，见 `.gitignore`），本文档面不重复它们。

## 文档地图

| 文档 | 回答什么问题 |
| --- | --- |
| [../README.md](../README.md) | 这个网站能看什么、怎么本地跑（面向读者） |
| [data-sources.md](data-sources.md) | 各外部数据源（ITAD / Steam / 汇率）的端点行为、实测结论与职责划分 |
| [pipeline.md](pipeline.md) | 抓取流水线、请求预算、缓存策略、上一次史低时间、**设计不变量** |
| [domain-rules.md](domain-rules.md) | 判定规则：什么算史低/当日新增、好评分档、去重、留存、视图窗口 |
| [data-model.md](data-model.md) | 三层状态库结构、留存清理、崩溃恢复 runbook、模块与文件布局 |
| [report-design.md](report-design.md) | 报表页面结构、大卡与板块、分片懒加载、视觉规范 |
| [operations.md](operations.md) | Actions 定时与加固、Pages 部署、错误处理、**待观察清单** |
| [config-reference.md](config-reference.md) | `config.json` 各键语义 |
| [CHANGELOG.md](CHANGELOG.md) | 版本里程碑（对外可读，追加式） |
| [../CONTEXT.md](../CONTEXT.md) | **领域口径词汇表**（词条的唯一定义处） |
| [agents/](agents/issue-tracker.md) | agent 技能约定（issue tracker / domain docs） |

新会话/新人的推荐阅读顺序：README → CONTEXT → data-sources → pipeline → domain-rules →
按当前任务再读 data-model / report-design / operations / config-reference。

## 项目目标与边界

每天定时抓取 Steam 折扣，筛出**处于历史最低价**的游戏，生成公开静态网页，
首页四板块 + 全部折扣懒加载，手机和 PC 都能舒服地看。

核心诉求（按优先级）：只看**史低**；突出**当日新增**；手机可看、多人可看；
全程自动化，跑挂了要能发现。

**明确不做**：价格提醒/推送、Steam 账号与愿望单同步、非 Steam 商店的独立列表、
价格走势图、PWA。

## 文档维护规则

本仓库文档曾因「同一口径写多处」吃过亏（阈值改一处漏五处、文档内新旧结论并存）。
以下规则是**硬约定**，改文档/代码注释前先对照：

1. **单一事实来源（SSOT）**：每个口径只在一处成文。
   - **数值默认值**：唯一出处 = `src/config.py` 的 `DEFAULTS` + `config.example.json`
     注释。文档（含 README 筛选表）可以引用数值帮助阅读，但必须以「出厂默认，可配置」
     表述，改默认值**不需要**同步文档正文。
   - **API 行为与实测结论**：唯一出处 = [data-sources.md](data-sources.md)。
     代码注释只留「为什么这么做」的一两句 + 指向，不复制实测过程。
   - **领域词汇**：唯一定义处 = [../CONTEXT.md](../CONTEXT.md)。其他文档使用词汇、
     不复制定义；需要新词或词义漂移时先改 CONTEXT。
   - **缓存生命周期**：唯一出处 = [pipeline.md](pipeline.md) 的「缓存策略」；
     存储结构唯一出处 = [data-model.md](data-model.md)。
2. **文档写「为什么/怎么用」，代码注释写「怎么实现」**：决策理由留在代码注释
   （就近可读）；实测数据清单、历史演进、多轮迭代叙事归文档或 CHANGELOG。
3. **禁止行号引用**：代码会漂移。引用一律用相对路径 + 符号名/锚点
   （如 `state.py` 的 `SEEN_KEEP`、`pipeline.md` 的「缓存策略」节）。
4. **修订不堆叠**：正文永远写**当前事实**，不写「X 月起改为…」「旧方案已废弃」的
   多层括注——沿革进 [CHANGELOG.md](CHANGELOG.md)，正文改写完即删旧说法。
5. **入库面最小**：本文档面（`docs/` + 根目录 md）只写「当前设计」与「永久性结论」；
   过程性内容（spec、review、changelog、debug）全部留在 `.scratch/` 本地工作区。

## 附：旧章节号对照（历史引用专用）

2026-10-10 拆分前，本文档是 1560 行的一体文档，代码注释里的裸 `§X.Y` 引用都指它。
存量注释里的旧编号按下表解析（**新代码不得再新增裸 `§` 引用**，一律写文档名 + 节名）：

| 旧编号 | 现位置 |
| --- | --- |
| §1 / §1.1 | 本页「项目目标与边界」 + [report-design.md](report-design.md) |
| §2.1~2.5 | [data-sources.md](data-sources.md) |
| §3.1~3.3 | [pipeline.md](pipeline.md) |
| §3.4 / §3.5 | [domain-rules.md](domain-rules.md)「好评分档」 |
| §3.6 | [pipeline.md](pipeline.md)「上一次史低时间」 |
| §4.1~4.6 | [domain-rules.md](domain-rules.md) |
| §5 / §6 | [data-model.md](data-model.md) |
| §7.x | [report-design.md](report-design.md) |
| §8 | [operations.md](operations.md) |
| §9 | [config-reference.md](config-reference.md) |
| §10 | [operations.md](operations.md)「错误处理」 |
| §11 / §12 / §12.x / §13 已解决条目 | 已删除（里程碑见 [CHANGELOG.md](CHANGELOG.md)；结构不变量进 [pipeline.md](pipeline.md)） |
| §13 待观察 | [operations.md](operations.md)「待观察清单」 |
