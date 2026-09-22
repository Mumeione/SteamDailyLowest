AGENTS.md  



## 输出约定

- 结果要求使用中文输出，操作执行过程的输出也需要使用中文说明正在干什么

## 项目

**SteamDailyLowest** —— 每日抓取 Steam 折扣，筛出「当日新增的史低」与「史低」游戏，  
生成静态 HTML 报表（手机可看）。

数据源为 IsThereAnyDeal 官方 API（Valve 官方 API 不提供历史价格）。  
判定与输出规则见 `.scratch/daily-lowest/spec.md`。

## 环境约定

- Windows + PyCharm，Python 3.13
- 依赖仅 `requests` + `jinja2`（模板渲染），不引入 ORM / 前端框架
- 存储用标准库 `sqlite3` / JSON 状态文件，报表为 jinja2 模板渲染的静态 HTML（`index.html` + `data.js`）
- API key 只放本机 `config.json`（已 gitignore），**不得写入任何入库文件**

## Agent skills

### Issue tracker

Spec 与 issue 以 markdown 文件存放在 `.scratch/<feature-slug>/` 下。  
见 `docs/agents/issue-tracker.md`。

### Domain docs

单 context 布局：仓库根目录 `CONTEXT.md` + `docs/adr/`。  
见 `docs/agents/domain.md`。
