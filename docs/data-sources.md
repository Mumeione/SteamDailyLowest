# 数据源

> 描述各外部数据源的能力、端点行为与实测结论。**带 ✅ 的结论都经过真实 API 探针核实**
> （累计 20 余轮；具体轮次与请求数在实测结论处就近标注）。
> 各源的职责划分见文末「职责划分」；错误响应的处理策略见 [operations.md](operations.md)；
> 抓取流程与请求预算见 [pipeline.md](pipeline.md)。

## ITAD（主数据源）

`https://api.isthereanydeal.com`，需 API key。限额 **1000 请求 / 5 分钟**（需验证邮箱），
超限返回 429 并带 `Retry-After`，必须实现退避。

key 存放：本机 `config.json`（已 gitignore）；GitHub Actions 用 repository secret
注入环境变量 `ITAD_API_KEY`。**任何情况下不得写入入库文件。**

### 用到的端点

| 端点 | 用途 | 成本 |
| --- | --- | --- |
| `GET /deals/v2` | 抓折扣列表（**主接口，一个就够**） | 每 200 条 1 次 |
| `GET /games/info/v2?id=<uuid>` | 取 `appid` + 好评率（**降级路径**） | 一游戏一请求，无法批量 |
| `POST /games/storelow/v2` | Steam 店内史低被记录的时间（上次史低） | 批量 200 uuid/次 |
| `GET /games/history/v2?id=<uuid>` | 价格流水（**史低期回填** `tools/backfill_low_period.py`） | 一游戏一请求 |
| `POST /lookup/shop/61/id/v1` | appid → uuid 批量映射（免鉴权、不计额度） | 单次 10000 条 |

### `GET /deals/v2` 参数

`country`（国家级）、`shops`（Steam = **61**）、`limit`（≤200）、`offset`、
`sort`（如 `-cut`）、`filter`。分页靠返回的 `nextOffset` + `hasMore`。

### ✅ 返回结构（实测）

每个 item 内嵌完整 `deal` 对象，**不需要再调其它接口补价格**：

```
item:  id(ITAD uuid) / slug / title / type / mature / assets.boxart…banner600 / deal{}
deal:  shop{id:61,name:"Steam"} / price / regular / cut / voucher
       storeLow / historyLow / historyLow_1y / historyLow_3m
       flag / drm / platforms / timestamp / expiry / url
```

### ✅ 字段语义（官方文档没写，实测反推）

| 字段 | 含义 |
| --- | --- |
| `deal.storeLow` | **该商店（Steam）历史最低价，且已把当前这次折扣算进去** |
| `deal.historyLow` | **全商店**口径历史最低（全周期） |
| `deal.historyLow_1y` / `_3m` | 全商店口径的近一年 / 近三月最低 |
| `deal.flag` | **官方的新史低/平史低标记，直接套用**，见下表 |
| `deal.timestamp` | 本次折扣**开始**时间 |
| `deal.expiry` | 本次折扣**结束**时间 |

### ✅ `flag` 取值表（本项目的核心判定依据）

| flag | 含义 | 验证依据 | 报表标签 |
| --- | --- | --- | --- |
| `None` | **不是史低** | 与 `price > storeLow` **200/200 完全重合** | 不进报表 |
| `N` | **新史低**（本次刷新了历史最低记录） | `games/history/v2` 复核 **5/5** 为「现价 < 历史旧最低」 | 「新史低」红 |
| `H` | **平史低**（现价等于已有记录） | 复核 5 条，**4 条「等于」**（1 条数据稀疏异常） | 「平史低」灰 |
| `S` | **仅 Steam 店史低**（别的商店更便宜过） | 复核 3 条全部「等于」；恒满足 `storeLow > historyLow` | 「平史低」灰（H 与 S 同归平史低口径；「店史低」独立标签**已退役**，见 [../CONTEXT.md](../CONTEXT.md)） |

> ⚠️ **不能**用 `price` 与 `storeLow` 比大小来区分新/平史低。因为 `storeLow` 已含当前折扣，
> 即便刷新了记录，`price` 也等于 `storeLow`（实测 1228 个付费史低候选里
> `price < storeLow` 为 **0** 个）。**判新/平必须看 `flag`**；报表展示口径的最终判定
> 见 [domain-rules.md](domain-rules.md) 的「史低分类」。

### ✅ 服务端 filter：一次查询就拿到「本体游戏 + 全部史低」

`/deals/v2` 的 `filter` 参数直接接受 JSON（形如 `filter={"cut":{"min":50,"max":null}}`），
**这是把每轮成本压下来的唯一有效手段**。可用字段（第 24 轮实测，共 148 次请求）：

| 字段 | 语义 | 实测 |
| --- | --- | --- |
| `type` | `[1]`=本体 / `[2]`=DLC / `[3]`=包 / `[7]`=软件 | 单页 200/200 全是 `type=game` |
| `flag` | 史低标记，**层级语义** | 见下 |
| `cut` / `price` / `regular` | `{"min":…,"max":…}` 区间 | 可用 |
| `steamCount` | Steam 评价数区间 | `{"min":100}` → 20 页 / 3864 条 |
| `steamPerc` | Steam 好评率 | ⚠️ 值域见下 |
| `added` | 「ITAD 在最近 N 小时**发现**的折扣」 | `added=24` + `type=[1]` + `flag=N` → **1 页 / 69 条** |
| `expiry` / `releaseDays` / `releaseDate` / `shops` / `drm` / `platform` / `mature` | 同类 | 未逐个测 |

#### ⚠️ `flag` 是**层级**语义，不是等值匹配

实测（国区、`shops=61`、`limit=200`、逐页翻完）：

| 查询 | 页数 | 条数 | 对照状态库 |
| --- | --- | --- | --- |
| `{"type":[1], "flag":"N"}` | 7 页 | **1,223** | `flag=N` 有 **1,224** 条 ✅ |
| `{"type":[1], "flag":"H"}` | 23 页 | **4,530** | = N(1224) + H(3310) = **4,534** ✅ |
| `{"type":[1], "flag":"S"}` | 27 页 | **5,243** | = 全部史低 **5,246** ✅ |

即 `N ⊂ H ⊂ S`：`flag="S"` 返回**全部史低**（所以它那一栏最多）。

→ **`filter={"type":[1],"flag":"S"}` 一次查询 = 「本体游戏 + 全部史低」= 27 页 / 5243 条**，
替代无 filter 的 162 页 / 32364 条。且**不影响攒库** —— 状态库本来就只记录史低条目。

**交叉验证**：`{"type":[1]}` 单查得 **48 页 / 9531 条**，与客户端筛出的 **9535 条**吻合
（差 4 条是两次调用之间的数据抖动）→ **服务端 `type` 过滤与客户端过滤结果一致，可放心用**。

#### ⚠️ 三个会静默出错的坑

1. **`steamPerc` 的值域文档写错了。** OpenAPI 写 `minimum: 0, maximum: 100`，
   实测**必须传 0~1 的小数**：传 `{"min":70}` → **0 条**（不报错，静默返回空）；
   传 `{"min":0.7}` 才正常。
2. **`flag` 传数组会被静默忽略。** `{"flag":["N","H","S"]}` 的返回结果与**完全不传 filter 一样**，
   不报错、不过滤。只能传单值（要「全部史低」就是 `"S"`）。
3. **`sort` 只接受网站折扣列表那几个值。** `-cut` / `price` 可用；
   `-timestamp` / `timestamp` / `added` / `-added` / `release` / `-release`
   一律 **400 `Unknown 'sort' value`** → 「按时间排序」做不到。

以上三条的一次性代价：单跑 `--audit`（无 filter 全量）即可保住
`type_not_game` / `not_historical_low` / `flag_price_mismatch` 这些诊断计数。

### ✅ 实测规模（国区，2026-09-21 全量复核）

- 折扣条目 **32,364 条 / 162 页**（`limit=200`）
- `type` 分布：本体 `game` **9,535** · 非本体 **22,829**（占 70.5%）
- **`type = None` 是什么？→ 实测抽 15 条，15/15 全是原声带（Soundtrack）**
  → 结论：**只保留 `type == "game"`**，`None`/`dlc`/`package` 一律排除
- `mature` **恒为 False** —— ITAD 默认就不返回成人内容，无需自己过滤
  （`exclude_mature` 配置只是白送级防御）
- 价格为 0 的条目极少（5000 条里 4 条）
- **史低（`type=game` + 付费 + `flag != None`）= 5,246 个**，
  其中 **`N`（新史低）1,224 · `H`（平史低）3,310 · `S`（店史低）712**

### ✅ `GET /games/info/v2` 细节

- **返回的是对象（不是数组）**，且**严格一游戏一请求**：
  `?ids=a,b` 报 400，`?id=a&id=b` 只返回最后一个
- 返回 `appid`（Steam AppID）、`reviews[]`、`tags`、`developers`、`publishers`、
  `releaseDate`、`players`、`stats`、`urls.game`
- `reviews[]` 结构：

```json
[
  {"score": 100, "source": "Steam", "count": 10, "url": "https://store.steampowered.com/app/2484180/"},
  {"score": null, "source": "Metacritic User Score", "count": 1, "url": "..."}
]
```

`score` 是 0–100 好评率，`count` 是评价数。**必须只取 `source == "Steam"` 那条**，
并**同时卡 `count`**。

### ✅ ITAD ↔ Steam appid 映射能力

**映射能力双向都存在**（第 16–23 轮实测）：

| 方向 | 端点 | 批量 | 结果 |
| --- | --- | --- | --- |
| appid → uuid | `POST /lookup/shop/61/id/v1` | ✅ **单次 10000 条** | body 必须是 `["app/<appid>"]`；**前缀 `app/` 不可省** |
| appid → uuid | `GET /games/lookup/v1?appid=` | ❌ 单条 | 与上者 **15/15 一致** |
| uuid → appid | `GET /games/info/v2?id=<uuid>` | ❌ 单条 | type=game **15/15 都有 `appid`** |

- 错误传参 `["447040"]` / `["steam:447040"]` / `[447040]` 一律返回 `null`
- 官方**无可下载的全量映射表/dump**；`/v01/game/plain/list/` 与 `/games/plain/v2` 都是 **404**

**⚠️ 映射不是双射。** 同一 appid 在 ITAD 可能有**多个条目**——Steam 页面改版时 ITAD 会另建
新条目，而**旧条目仍留在 deals 里流通**。实测 15 条里 4 条如此：

| appid | deals 里的条目 | 批量端点返回的"规范条目" |
| --- | --- | --- |
| 266230 | Last Dream - **Deluxe Edition** (Game + OST) | Last Dream |
| 1019590 | Lovely Planet 2 | Lovely Planet 2: **April Skies** |
| 944220 | Gachi clicker | Gachi **Heroes**（type=None） |
| 770070 | Swords and Sandals 5 Redux | …: **Maximus Edition**（type=None） |

所以：**appid → uuid 是一对多**（端点只返回"规范主条目"，与 deals 的 uuid 一致率 11/15）；
**uuid → appid 是多对一、可靠**。详情管线只用「uuid → appid」方向（每游戏 1 次 `info/v2`，
顺带拿好评率），见 [pipeline.md](pipeline.md)。

**规模参考**：`GET /service/shops/v1` 显示 Steam(61) `games = 310,642`、`deals = 35,785`。
自建 appid 全量映射表方向不可行（枚举命中率仅 7.24%），**结论是不做**。

## Steam 官方 API（**元数据主源** + 跨区比价）

中文名 / 好评率 / 评价数 / 厂商 / 封面的主源是
**`IStoreBrowseService/GetItems/v1`**（批量，见下文）；`appdetails` 用于**跨区价格**，
`storesearch` 用于降级反查；`appreviews` 端点已于 2026-10-22 PT 停用，本项目不再依赖。

✅ 实测本地可直连。**全部不消耗 ITAD 配额。**

### `IStoreBrowseService/GetItems/v1` —— 元数据主源

- **批量**：单批 ≤250 个 appid（实测 250 完整通过、300 → HTTP 400）；服务端上限是
  **URL 编码后长度**而非固定条数，客户端按「条数闸 + URL 字符预算」双闸切片
- 请求参数 `language=schinese` + `country_code=US`（US 区游戏收录覆盖最广）+
  `steam_realm=1`（全球商店区）
- 按需返回（`data_request` 显式开关）：`include_basic_info`（厂商）、`include_reviews`
  （好评率）、`include_release`（发行日期）、`include_assets`（封面，2026-10-10 起
  为「ITAD 无 boxart 的条目」补缺而开）；没开的字段响应里是 null
- **封面**：`assets.library_capsule`（300×450 竖版，卡片 3:4 容器吻合）。文件名/子目录
  **不固定**（`library_600x900.jpg` / `library_600x900_schinese.jpg` / `portrait.png` /
  `<hash>/library_capsule.jpg` 都出现过）——**必须用服务端给的真实值**，不能由 appid 现拼
- **语言回退**：`language=schinese` 时有官方中文标题/中文本地化封面的游戏直接返回中文版，
  没有的回落英文 —— 与 appdetails 同款行为，属正常，不是失败

### `GET /api/appdetails` —— 批量**只能拿各区价格**

```text
GET https://store.steampowered.com/api/appdetails
    ?appids=<appid1>,<appid2>,…      ← 逗号分隔
    &cc=ua|in|cn                     ← Steam 区码，小写
    &l=schinese                      ← 本地化名（**只在单 appid 时有效**）
    &filters=price_overview
```

**实测出来的 filters × appid 个数规则（第 27~28 轮，共 30 次请求）**：

| 场景 | `filters` | 结果 |
| --- | --- | --- |
| **单** appid | 任意值 / 不带 | 200，`data` 里有 `name` + `price_overview` |
| **多** appid | **只有 `price_overview`** | 200，每个 appid 都有 `price_overview`，**没有 `name`** |
| **多** appid | 其他任何值（`basic` / `release_date` / …）/ 不带 | **400** |

- ✅ **批量有效（仅价格）**：实测一次 20 个正常；文档说的 50 未复核，`steam_batch_size` 默认取 **20**
- ⚠️ `filters=basic` 会带上 `detailed_description`，单 appid 就有约 21 KB
- 永久免费游戏（CS2 / Dota2 / TF2）返回 `data: []`，属正常（客户端已跳过并计数）

#### ⚠️ appid 重定向：带 `basic` 时响应的 key 不是 appid

单 appid 且 `filters` 含 `basic` 时，**响应的 key 是「店铺页 id」**，与请求的 appid 不同：

| 请求 | 返回 key |
| --- | --- |
| 单 + 不带 `filters` / `basic` / `basic,price_overview` | **店铺页 id**（412020 → 1952352 等） |
| 单 + `price_overview` | 请求的 appid |
| 多 + `price_overview` | 与请求**等值且同序** |

- 该 key **不是 appid**：直接请求 1952352 会 `success: false`
- 与区域、语言**都无关**：中文名只取决于 `l=schinese`
- `basic` 响应里的 `data.steam_appid` 等于请求的 appid → 是同一个游戏
- 客户端取「响应里的唯一值」而不是按 key 查（按 key 查会静默拿不到名字）
- ✅ **批量价格路径不受影响**：`price_overview` 批量返回的 key 与请求等值同序，
  按请求 appid 查表安全

#### ⚠️ 探针也不能突发打 Steam

无间隔地连打约 40 次 `store.steampowered.com` 之后，**该 host 的全部端点开始静默超时**
（表现为 `ConnectTimeout` / `SSLError`），约十几分钟后自行恢复——**短时爆发性高频请求
本身就是触发条件**，与全天累计次数无关。探针脚本必须走同一套节流。

#### ⚠️ 本机对 `store.steampowered.com` 有 TLS 拦截：必须用系统证书库

本机装了 **SteamTools（Watt Toolkit）**，它会对 `store.steampowered.com` 做 TLS 中间人加速，
而 `requests` 默认的 certifi 证书列表里**没有** SteamTools 的根证书
→ `SSLError: CERTIFICATE_VERIFY_FAILED`。

做法（`src/httpclient.py`）：**不关掉校验**，而是把**可信来源**换成系统证书库 ——
把 Windows 的 `ROOT`+`CA` 证书导成 PEM（`ssl.enum_certificates`，encoding 字段是
**`x509_asn`**），缓存到 `data/ca_bundle.pem`，再赋给 `session.verify`。

优先顺序：`REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` / `SSL_CERT_FILE` 环境变量
→ 系统证书库 → 退回 requests 默认（certifi）。
**GitHub Actions 的 Linux runner 上没有这个拦截**，自然走 certifi。

### `GET /api/storesearch` —— 英文名 → appid + 中文名

```
GET https://store.steampowered.com/api/storesearch/?term=<英文名>&cc=cn&l=schinese
```

返回 `items[]`，每项有 `id`(appid) / `name`(本地化名) / `price`。
⚠️ 结果是**排名列表**且会混入 DLC / 通行证 / Demo，必须做名称精确匹配后才采用。

### ⚠️ Steam 限流（按社区经验设定，**不做实测**）

**先分清三个完全不同的表面**，混为一谈是各种错误数字的来源：

| 表面 | 限流机制 | 本方案 |
| --- | --- | --- |
| `store.steampowered.com/api/*` | **未公布**，只有经验观测；**per-IP** | ✅ 我们用这个 |
| `api.steampowered.com`（带 key） | 官方条款 **10 万次/天/密钥**；另有未公布的 per-IP 突发上限 | 不使用 |
| Steam 社区市场 | 独立节流机制 | 不使用 |

**Valve 从未公布 Storefront 的任何数字阈值**，下面所有数字都是社区经验观测。

两个约束，缺一不可：

1. **平均速率**：多数来源口径 **约 200 次 / 5 分钟 / IP**（= 每 1.5 秒 1 次）。
   `apis.io` 的限流档案另给「约 1 次/秒」，更宽松——**取保守的那个**。
2. **并发**：**并发本身就是触发条件**（`apis.io`：「Sustained high concurrency from a
   single IP returns 429」）。只做速率控制不够，**必须同时限制并发数**。

**本方案：store.steampowered.com 全站按同一个预算合并计数 + 串行 + 最小间隔 2 秒**——
`appdetails` 与 `appreviews` 在同一个 host 下，**保守假设共享 per-IP 预算**，
因此不设两套窗口只设一套总窗口。具体参数（150/300s、0.5s/2s 间隔）见
[config-reference.md](config-reference.md) 的 `steam_*` 键。

> ⚠️ 429/403/200+null/success:false/503 五种响应必须分开处理，**绝不可以用轮换 IP
> 规避限流**。处理策略见 [operations.md](operations.md) 的「错误处理」。

### ✅ ITAD 不能用于跨区比价

实测同一个游戏（appid 2000040 Space Menace）各地区报价：

| 来源 | 币种 | 原价 |
| --- | --- | --- |
| Steam 官方 `appdetails?cc=ua` | **UAH** | **170₴**（≈4.10 USD） |
| ITAD `country=UA` | USD | **6.99** ← 和 US 一模一样 |
| ITAD `country=US` | USD | 6.99 |
| ITAD `country=GB` / `DE` / `BR` / `TR` | GBP / EUR / BRL / TRY | 6.29 / 7.19 / 26.75 / 228.87 |
| ITAD `country=CN` / `IN` | CNY / INR | 29 / 359 |

**ITAD 不识别 `UA`，静默返回了默认地区（US）的价格**——不报错，只是悄悄给你美国的价。
`GB`/`DE`/`BR`/`TR`/`CN`/`IN` 都正常。这是个阴险的坑。

→ **跨区比价一律走 Steam 官方接口**，且**以后每新增一个地区都要先跟 Steam 校准一次**。

## 汇率源

UA→UAH、IN→INR、CN→CNY，三币种不统一，算「差价百分比」必须换汇。
**每日取一次并缓存**，页面上标注汇率数值与取数日期。

实测两个候选：

| 源 | 有 UAH？ | 结论 |
| --- | --- | --- |
| **`open.er-api.com/v6/latest/CNY`** | ✅ 有 | **采用** |
| `frankfurter.app/latest?from=CNY` | ❌ 没有 UAH | 不能用（基于 ECB 币种表） |

`rates[X]` 的含义是 **1 个基准币能换多少 X**，所以 `X → CNY` 是**除以**它：

```
UA 45₴ = 4500 戈比 → 4500 / 6.67391 ≈ 674 分 = ¥6.74
IN ₹149 = 14900 paise → 14900 / 14.303287 ≈ 1042 分 = ¥10.42
```

方向写反不会报错、只会给出荒唐的比价结论，所以 `tests/test_fx.py` 专门钉了这一点。
汇率源挂了**不让整轮失败**：沿用缓存里的旧值并在日志里告警。

## 已排除的方案

| 方案 | 排除原因 |
| --- | --- |
| Valve 官方 Web API 判史低 | **完全不提供历史价格** |
| SteamDB 抓取 | Cloudflare 反爬 + 违反 ToS |
| SteamDB 第三方代理（Anysite） | $49/月起，个人工具不划算 |
| 自建价格快照库判史低 | 需从零攒几个月才准，头几个月只能报「当前折扣最低」 |
| `ISteamApps/GetAppList` 批量拿 appid | 要按**游戏名模糊匹配**，版本后缀/本地化差异会错配；而且**不省请求** |
| 好评率改从 Steam 评测接口逐条取 | 同样逐游戏调用，绕一圈请求数不减反增（GetItems 批量已解决） |

## 职责划分

| 职责 | 首选 | 备选 | 说明 |
| --- | --- | --- | --- |
| 判史低（`flag`）/ 折扣起止 | ITAD `deals/v2` | — | ITAD 唯一不可替代的能力 |
| 上一次史低时间 | ITAD `storelow/v2`（**批量**） | — | 只有 ITAD 有这个数据 |
| appid 映射 | ITAD `info/v2`（uuid→appid，顺带给好评率） | `lookup/shop/61/id/v1`（appid→uuid 批量） | 见「映射能力」节 |
| 好评率 / 评价数 / 中文名 / 厂商 | **Steam `GetItems`（批量）** | ITAD `info/v2` **仅作降级** | 批量一次全带；降级路径每轮有预算上限 |
| 封面 | **ITAD boxart**（`assets.boxart`，随 deals 自带） | Steam `GetItems` `library_capsule` **补缺** | ITAD 无 boxart 的条目（进列表里实测约 9.5%）才用 Steam 小封面；ITAD 侧 URL 可由 `game_id` 现拼，故为主源 |
| 各区价格 | **Steam `appdetails`（批量）** | — | 每区 1 次请求覆盖整个列表 |
| 社区中文名 | 小黑盒 `get_game_detail` | — | 官方中文名缺位时人工触发回填（`tools/backfill_cn_names.py`），只补缺不覆盖 |
| 汇率 | `open.er-api.com` | — | 每日一次并缓存 |

> 跨区比价另有**估算**路径（外区现价 = 外区原价缓存 × 国区比例，口径见
> [../CONTEXT.md](../CONTEXT.md) 的「现查与估算的分工」）。抽测 20 条：
> 15 条与真查完全一致、5 条差 1~2 个百分点（各区四舍五入反算所致）。

各数据的**缓存策略**（永久 / 四档 TTL / 折扣期暂存）统一见
[pipeline.md](pipeline.md) 的「缓存策略」。
