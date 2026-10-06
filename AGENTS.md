AGENTS.md  



## 输出约定

- 结果要求使用中文输出，操作执行过程的输出也需要使用中文说明正在干什么
- 所有时间一律用北京时间（UTC+8）表述，不要直接用 UTC；引用 Actions 日志等系统的 UTC 原始时间时，先换算成北京时间再输出（必要时可括注原始 UTC）
- 工作流 cron 字段必须是 UTC（GitHub 不认 timezone 字段），但注释里同步标注北京时间

## 项目

**SteamDailyLowest** —— 每日抓取 Steam 折扣，筛出「当日新增的史低」与「史低」游戏，  
生成静态 HTML 报表（手机可看）。

数据源分工（2026-10-04 起）：

- **ITAD**（IsThereAnyDeal）：判定史低（`deal.flag`）、上次史低时间、uuid→appid 批量映射
  （`POST /lookup/shop/61/id/v1`，免鉴权、不计额度）。历史价只能靠 ITAD（Valve 官方 API 不提供）。
- **Steam 官方 API**：元数据主源 `IStoreBrowseService/GetItems/v1`
  （中文名、好评率、评价数、厂商、价格，单批 ≤250）；ITAD `info/v2` 仅作降级。

领域口径（状态定义、好感分档、判定与展示规则）见仓库根 `CONTEXT.md`；  
当前重构方案与实施进度见 `.scratch/refactor-2026q4/spec.md`。

## 环境约定

- Windows + PyCharm，Python 3.13
- 运行时依赖仅 `requests` + `jinja2`（模板渲染），不引入 ORM / 前端框架
  - `tools/check_workflow_yaml.py` 依赖 PyYAML（已列入 `requirements.txt` 并装入主
    Python；运行管线不依赖它，Actions 的 daily/prefetch 用不到，属提交前校验工具）
- 存储用标准库 `sqlite3` / JSON 状态文件，报表为 jinja2 模板渲染的静态 HTML（`index.html` + `data.js` + `all.js`）
- API key 只放本机 `config.json`（已 gitignore），**不得写入任何入库文件**

## Agent skills

### Issue tracker

Spec 与 issue 以 markdown 文件存放在 `.scratch/<feature-slug>/` 下。  
见 `docs/agents/issue-tracker.md`。

### Domain docs

单 context 布局：仓库根目录 `CONTEXT.md` + `docs/adr/`。  
见 `docs/agents/domain.md`。
