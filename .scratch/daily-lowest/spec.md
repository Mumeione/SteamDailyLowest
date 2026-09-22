# Spec: 每日新增 Steam 史低查询工具

Status: superseded —— **以 `docs/DEVELOPMENT.md` 为准**

> ⚠️ 本文是早期规格，部分内容已被后续 23 轮探针推翻或细化（好评分档、大促爆发对策、
> 数据源职责划分、三档滑动窗口限流、上次史低时间、第一版范围）。
> **实现请只读 `docs/DEVELOPMENT.md`**；本文仅保留作为设计沿革记录。


## 问题

Steam 每个交易日会有一批新折扣上线。想买游戏的人真正关心的是两个集合的交集：

1. **史低** —— 当前价格等于该游戏在 Steam 的历史最低价
2. **当日新增** —— 这个折扣是今天才出现的，不是昨天就已经在打折的

Steam 自己的促销页没法做这个筛选（只有"当前折扣"，没有历史价，也没有"何时开始打折"）。
所以需要一个本地工具，每天跑一次，只把「今天新出现 + 是史低」的游戏列出来。

## 范围

**做：**

- 拉取 Steam 当前全部折扣游戏（国区为主，附乌克兰区/印度区比价）
- 判定每个游戏是否处于 Steam 史低
- 判定该折扣是否为当日新增
- 输出静态 HTML 报表（手机可看）+ 供程序消费的 JSON

**不做：**

- 不做价格提醒/推送（邮件、微信等）
- 不做愿望单同步（不接 Steam 账号）
- 不做非 Steam 商店的单独列表（GOG / Epic 等）
- 不做历史价格走势图

## 数据源

**IsThereAnyDeal 官方 API**（`https://api.isthereanydeal.com`），需要 API key。
Valve 官方 Web API 不提供任何历史价格，所以"史低"只能靠第三方历史库。

key 的存放：本机放 `config.json`（已 gitignore）；GitHub Actions 用 repository secret
注入环境变量 `ITAD_API_KEY`。**任何情况下都不得把 key 写进入库文件。**

### 实测结论（2026-09-21，真实 key 探测所得）

**`GET /deals/v2` 一个接口就够拿全部展示数据**，每个 item 内嵌完整 `deal` 对象：

```
item:  id(ITAD uuid) / slug / title / type / mature / assets.boxart... banner600 / deal{}
deal:  shop{id=61,name=Steam} / price / regular / cut / voucher
       storeLow / historyLow / historyLow_1y / historyLow_3m
       flag / drm / platforms / timestamp / expiry / url
```

参数：`country`、`shops`（Steam=61）、`limit`（≤200）、`offset`、`sort`（如 `-cut`）、`filter`。
分页靠 `nextOffset` + `hasMore`。

**字段语义（文档未定义，以下为实测推断，置信度高）**

- `deal.storeLow` —— **该商店（Steam）的历史最低价，且已包含当前这次折扣**。
  因此判据是 `price <= storeLow`；付费游戏里出现 `price < storeLow` 的情况实测为 0。
- `deal.historyLow` / `_1y` / `_3m` —— **全商店**口径的历史最低（全周期 / 近一年 / 近三月）。
- `deal.flag` —— **这就是官方给的新史低/平史低标记，直接套用**：
  - `None` 与 `price > storeLow` **完全等价**（200/200 条重合）→ **不是史低**
  - **`N` = 新史低**（本次刷新了历史最低记录）。用 `games/history/v2` 复核 5 条，
    **5/5 全部「现价 < 历史旧最低」**
  - **`H` = 平史低**（现价等于已有记录）。复核 5 条，4 条「等于」；
    1 条例外（Zodiac Hentai）其历史只有 4 条、数据稀疏，属异常样本
  - **`S` = 仅打到 Steam 店史低**（恒满足 `storeLow > historyLow`，即别家更便宜过）；
    复核 3 条全部「等于」旧最低

  ⚠️ 之前"用 `price` vs `storeLow` 区分新/平史低"的做法是错的：因为 `storeLow`
  **把当前这次折扣算进去了**，所以即便是新记录，`price` 也等于 `storeLow`
  （实测 1228 个付费史低候选里 `price < storeLow` 为 0 个）。**判新/平必须看 `flag`。**

**实测规模（国区，2026-09-21 当天）**

- Steam 折扣条目**超过 5000 条**（翻到 25 页上限仍未到底）
- `type` 分布：`game` 2727 / `dlc` 1092 / `package` 1007 / 无 type 174
- **`type = None` 是什么？→ 实测抽 5 条，5/5 全是原声带（Soundtrack）**：
  Zodiac Hentai - Hellish Memory Soundtrack / Mecha Blade Soundtrack /
  Tormented Souls Soundtrack / Bean Stalker Soundtrack / In Sound Mind - The Original Soundtrack。
  共同特征：标题带 Soundtrack、`tags=[]`、`achievements=False`、多数无封面。
  → 结论：**只保留 `type == "game"`**，`None` / `dlc` / `package` 一律排除
- `mature` **恒为 False** —— ITAD 默认就不返回成人内容，无需自己过滤
- 价格为 0 的条目极少（5000 条里 4 条）
- **史低候选（type=game、付费、`price <= storeLow`）1228 个**

**拿 Steam AppID / 好评率的接口：`GET /games/info/v2?id=<uuid>`**

- **严格一游戏一请求，无法批量**（`ids` 逗号报 400；重复 `id` 只取最后一个）
- 返回 `appid`（Steam AppID）、`reviews[]`、`tags`、`developers`、`publishers`、
  `releaseDate`、`players`、`stats`、`urls.game` 等
- `reviews[]` 结构（**关键**）：

```json
[
  {"score": 100, "source": "Steam", "count": 10, "url": "https://store.steampowered.com/app/2484180/"},
  {"score": null, "source": "Metacritic User Score", "count": 1, "url": "..."}
]
```

  `score` 是 0–100 好评率，`count` 是评价数。**必须同时卡 `count`**——
  实测样本里有「10 条评价 / 100%」和「6 条评价 / 83%」，纯按百分比过滤会放进无意义的小样本。

**兜底路径**：`deal.url` 是 `https://itad.link/...` 短链，跟随跳转后落地
`https://store.steampowered.com/app/<appid>/<Slug>`，可用正则解析出 appid。
**不消耗 ITAD 配额**（是普通 HTTP 请求），可在 info/v2 异常时兜底。

**好评率与评价数**（实测 12 个史低候选）：评价数 `[104,128,472,628,628,829,1006,3274,30589,95242×3]`，
最小值就有 104；好评率 `[51,60,60,61,67,71,77,77,82,82,82,82]`。
→ 阈值定 **好评率 ≥ 70% 且评价数 ≥ 100**：100 在这批样本里一条都不误伤，
但能挡掉早前实测到的「6 条评价 83%」「10 条评价 100%」这类新游戏。
注意 12 个样本取自高折扣榜，偏热门，真实全集的裁掉比例会更高——
**每次运行都要把"各过滤条件各砍掉多少条"写进 run_log**，便于按真实数字调阈值。

### 跨区比价：不能用 ITAD，改用 Steam 官方 API

实测同一个游戏（Space Menace，appid 2000040）：

| 来源 | 币种 | 原价 |
|---|---|---|
| Steam 官方 `appdetails?cc=ua` | **UAH** | **170₴**（≈4.10 USD） |
| ITAD `country=UA` | USD | **6.99** |
| ITAD `country=CN` | CNY | 29（≈4.08 USD） |
| ITAD `country=IN` | INR | 359（≈4.08 USD） |

### ✅ 已验证：ITAD 不能用于跨区比价（UA 是静默回退）

实测同一个游戏（appid 2000040 Space Menace）各地区报价：

| 来源 | 币种 | 原价 |
|---|---|---|
| Steam 官方 `appdetails?cc=ua` | **UAH** | **170₴**（≈4.10 USD） |
| ITAD `country=UA` | USD | **6.99** |
| ITAD `country=ua`（小写） | USD | **6.99** |
| ITAD `country=US` | USD | **6.99** ← 与 UA 完全相同 |
| ITAD `country=GB` / `DE` / `BR` / `TR` | GBP / EUR / BRL / TRY | 6.29 / 7.19 / 26.75 / 228.87 |
| ITAD `country=CN` / `IN` | CNY / INR | 29 / 359 |

**结论：ITAD 不识别 `UA`，静默返回了默认地区（US）的价格** —— 不报错，只是悄悄给美国的价。
`GB`/`DE`/`BR`/`TR`/`CN`/`IN` 都正常。

→ **跨区比价一律改用 Steam 官方接口**，且**以后每新增一个比价地区都要先与 Steam 校准一次**
（否则会重演这种静默错误）。

```
GET https://store.steampowered.com/api/appdetails
    ?appids=<appid1>,<appid2>,...   ← 支持逗号批量
    &cc=ua|in|cn
    &filters=price_overview
```

- 返回各区**原生币种**（UAH / INR / CNY），是权威口径
- **支持批量 appid**（实测三个 appid 返回三个独立 key），一次请求可查多个游戏
- **不消耗 ITAD 配额**
- 注意：永久免费游戏（CS2 / Dota2 / TF2）的 `data` 为空数组，属正常
- 汇率仍需外部来源（三个币种不统一），且必须**在页面上标注汇率数值与取数日期**

**限额**：1000 请求 / 5 分钟（需验证邮箱）。超限返回 429 并带 `Retry-After`，必须实现退避。

## 判定规则

### 史低

```
店史低   = price <= storeLow                      （等价于 flag != None）
全站史低 = price <= storeLow <= historyLow         （Steam 价即全网最低过）
```

用金额整数比较（`amountInt`，单位分），容差 0。

`storeLow` 缺失 → 标记 `unknown`，**不列入史低**，但在报表里单独计数，
避免把"数据缺失"静默当成"不是史低"。

**新史低 / 平史低直接用 `flag` 判定**（见上）：

| flag | 含义 | 报表标签 |
|---|---|---|
| `N` | 新史低（刷新了历史最低记录） | 「新史低」红色 |
| `H` | 平史低（等于已有最低记录） | 「平史低」橙色 |
| `S` | 仅 Steam 店史低，别的商店更便宜过 | 「店史低」灰色 + 标注 |
| `None` | 不是史低 | 不进报表（按 Q12：报表只收史低） |

用 `price` / `storeLow` / `historyLow` 做**交叉校验**：若 `flag is None` 但
`price <= storeLow`，记一条异常日志，便于发现 ITAD 口径变化。

### 当日新增

满足任一即为当日新增：

1. `deal.timestamp` 的日期 == 运行当天（`Asia/Shanghai`）；或
2. 本地库中不存在该 `(game_id, price_int, expiry)` 组合

**首次运行的例外**：规则 2 会让所有史低都算"当日新增"。因此必须提供 `--baseline` 模式：
只写状态、不产出报表，用于建立基线。

### 时区

一律按 **`Asia/Shanghai`** 判定日期。GitHub Actions 的 cron 现已支持 `timezone:` 字段。

## 存储

状态文件（滚动 JSON，非按天归档，控制体积）：

- `seen_deal` —— 每个见过的折扣，键为 `(game_id, price_int, expiry)`，含 `first_seen_at`
- `game_meta` —— 缓存 `appid`（永久有效）与 `reviews`（TTL 7 天），
  这是控制 info/v2 请求量的关键
- `run_log` —— 每次运行的时间、抓取量、命中量、请求数，便于事后核对

**留存窗口**：折扣过期后**再保留 7 天**再删除（应对"隔几天又打折"的情况）。

## 输出

`output/` 目录下：

- `index.html` —— 报表页面，响应式，手机浏览器可打开
- `data.js` —— `window.__STEAM_LOWEST__ = {...}` 形式的内联数据
- `latest.json` —— 原始结构化结果

视图（可筛选）：当日新增 / 本周（周一 00:00 起 14 天）/ 折扣中 / 即将过期（<48h）/ 已过期。
窗口一律按**折扣开始时间** `timestamp` 分组；状态一律按 `expiry` 判定。

卡片信息：封面、名称、原价、现价、折扣、Steam 史低、好评率(+评价数)、
Steam 链接、小黑盒链接（`https://www.xiaoheihe.cn/games/detail/<appid>`）、
乌克兰区/印度区价格与相对国区的差价百分比。

## 运行方式

```
python run.py --baseline   # 首次：建立基线 + 预热 game_meta 缓存，不产出报表
python run.py              # 日常：抓取并产出报表
python run.py --probe      # 诊断：落盘原始返回供核对（已实现于 tools/）
```

## 未决 / 风险

- **info/v2 的请求量是最大的成本项**：一游戏一请求、无法批量，而史低候选实测 ≥1228 个。
  缓解靠 `game_meta` 缓存（`appid` 永久、`reviews` TTL 7 天）。
  **注意换数据源并不省请求**：Steam 的评测接口同样只有逐游戏调用，
  而且还要额外做「游戏名 → appid」匹配（`ISteamApps/GetAppList` 一次拿全量再模糊匹配），
  反而多一层错配风险。真正有效的是**分层 + 惰性补齐**（见下）。
- **`storeLow` 的时间窗口未知**：1228/2727 ≈ 45% 的付费游戏都"处于店史低"，占比偏高。
  可能 ITAD 的历史回溯窗口有限。若如此，"史低"会比 SteamDB 口径宽松。
  报表可同时展示「全周期最低」与「近一年最低」（`historyLow_1y`）供对照。
- **跨区比价的汇率**：UA→UAH、IN→INR、CN→CNY，三币种必须换汇。
  需一个免费汇率源，每日取一次并缓存，**页面标注汇率与取数日期**。
- **Steam `appdetails` 批量返回需实现时再确认一次**：已确认多 appid 会返回多个独立 key，
  但当时测的三个都是永久免费游戏（`data` 为空），付费游戏批量返回非空数据未直接验证。
- **区域价格不代表可购买**：Steam 乌克兰/印度区有区域限制，需要在页面上标注"仅供参考"。
- **Pages 有约 10 分钟缓存且不支持自定义响应头**，需给资源加 `?v=<生成时间戳>`。
- **用 `GITHUB_TOKEN` 推送的 commit 不会触发 Pages 构建**，发布必须用 `deploy-pages`
  在同一次运行内完成。
- **公开仓库 60 天无活动会禁用定时任务**，每日提交状态文件正好规避。
- **cron 在高峰期可能延迟甚至被丢弃**，需要一次补跑。
- **失败会静默**：网页停留在旧数据。需要 GitHub 失败通知 + 页面内建"数据陈旧"横幅。
