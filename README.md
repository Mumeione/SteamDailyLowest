# SteamDailyLowest

每日抓取 Steam 折扣，筛出「当日新增的史低」游戏，生成手机友好的静态 HTML 报表。

**在线报表（GitHub Pages）：<https://mumeione.github.io/SteamDailyLowest/>**

## 它能看什么

- **当日新增史低**：按 ITAD 官方 flag 只收本体游戏的史低（新史低 N / 平史低 H / 店史低 S）
- **好评分档**：好评达标（≥70% 且 ≥100 评价）/ 高热度口碑不一，冷门游戏不展示
- 每张卡片：现价、折扣、Steam/小黑盒直达链接、好评率；
  展开可看 Steam 史低、全周期/近一年最低、**上次史低时间**、乌克兰/印度区比价（±百分比）

## 运行方式

- GitHub Actions 每天自动运行（**05:14** 主跑 + **07:24** 补跑，15:14 预抓详情缓存），
  跑完自动发布到 Pages 并把状态库回写到 `data` 分支
- 数据源：[IsThereAnyDeal](https://docs.isthereanydeal.com/) 官方 API（Steam 官方 API 不提供历史价格）+ Steam 商店（中文名/比价）
- 状态（SQLite 之外用 JSON）增量攒库：好评率、appid、中文名、史低时间永久/TTL 缓存，逐轮补齐

## 本地运行

```bash
pip install requests jinja2
# config.json 放入 ITAD API key（已 gitignore，勿提交）
python run.py            # 全流程：抓取 → 报表 → output/index.html
python run.py --prefetch # 只补详情缓存，不出报表
```

> 区域价格与好评数据来自第三方，仅供参考；以 Steam 实际结算为准。
