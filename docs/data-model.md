# 数据模型与模块布局

> 描述三层状态库的结构、留存清理、崩溃恢复 runbook，以及当前模块与文件布局。
> 缓存的生命周期语义（四档 TTL / 永久 / 折扣期暂存）见 [pipeline.md](pipeline.md) 的「缓存策略」；
> 字段的领域含义见 [../CONTEXT.md](../CONTEXT.md)。

## 三层存储

**重构 S6 起切三份**（均可重复迁移且幂等；`State.load()` 自迁移持续兜底）：

| 文件 | 语义 | 内容 |
| --- | --- | --- |
| `state.json` | **不可重建**（丢了就是丢数据） | `seen_deal` + `game_meta` + `run_log` |
| `dynamic.json` | **动态重要数据**（会变且有消费方） | `reviews` / `stats` / `fetched_at` / `detail_failed_at` / `detail_attempts`——跟随 `seen_deal` 留存清理，动态数据离开 seen_deal 即删 |
| `cache.json` | **可重建**（丢了重拉） | `compare_cache` + `low_time_cache` + `heybox_miss` |

「不变」= 不随时间变化或变化可忽略；「动态重要」= 会变且有消费方。
`meta()` 读取时做**合并视图**：不变层与动态层按 game_id 拼装，消费方无感。

⚠️ `cache.json` 的 `compare_cache`（原价键）是**持久积累**：enrich 只真查**当前在列表**
的条目，缓存丢失后历史 appid 的原价**无法重拉重建**——只能对重现条目重新校准，
或从 data 分支的 git 历史找回。「可重建」对它只部分成立，删除需谨慎。

`cache.json` 固定与 `state.json` 同目录同名（由 `state_path` 派生，无独立配置键）；
`dynamic.json` 同理。三份都是滚动 JSON（非按天归档，体积可控），
用 `GITHUB_TOKEN` 提交回 data 分支。

### state.json —— 不可重建

```jsonc
{
  "version": 1,
  "updated_at": "2026-09-21T03:00:12+08:00",
  "seen_deal": {
    // 键 = "<itad_uuid>|<price_int>|<expiry>"，保证同日重复运行幂等
    "018d937f-…|995|2026-09-28T19:00:00+02:00": {
      "game_id": "018d937f-…",
      "title": "Crown Wars: The Black Prince",
      "price_int": 995,
      "regular_int": 17300,
      "cut": 95,
      "currency": "CNY",
      "flag": "N",
      "start": "2026-09-14T19:21:10+02:00",
      "expiry": "2026-09-28T19:00:00+02:00",
      "store_low_int": 995,
      "history_low_int": 995,
      "history_low_1y_int": 995,
      "boxart": "https://assets.isthereanydeal.com/…/boxart.jpg",
      "low_kind": "new",
      "first_seen_at": "2026-09-21T03:00:05+08:00",
      "last_seen_at": "2026-09-21T03:00:05+08:00"
    }
  },
  "game_meta": {
    // 键 = itad uuid，详情缓存（不变层字段），控制请求量的关键
    "018d937f-…": {
      "appid": 1658920,
      "title_zh": "Crown Wars: The Black Prince",
      "title_zh_at": "2026-09-21T03:00:07+08:00",
      "publishers": [{"id": null, "name": "…"}],
      "developers": [{"id": null, "name": "…"}],
      "release_date": 1717718400,
      "cover": null
      // 可选键：unlisted: {"at": …, "start": …}（未入列表标记）
      //         low_period: {"cur": …, "prev": …}（史低期记忆）
    }
  },
  "run_log": [ /* 见下文「run_log」 */ ]
}
```

`seen_deal` 条目是**精简落库**（`SEEN_KEEP` 白名单，见 `classify.py`）：
原始 item 22 个字段 5.09MB，砍掉 `banner`（与 boxart 重复）、`slug`、`shop_id`（恒 61）、
`type`（恒 game）、`mature`（恒 False）、`itad_url`（302 直跳 Steam）等
「存了也不会变」的字段。

`unlisted` 机制（分类为 COLD/OTHER 的条目）：不建动态条目、不入待补、
同一折扣期间不重抓；下次折扣（`start` 变化）时重抓一次重判，
翻案则转正常记录并删标记。seen_deal 仍照常记录（留存 7 天自清，
兼防当日新增重复计数）。

`game_meta` 的动态字段（reviews/stats/fetched_at/detail_failed_at/detail_attempts）
不在 state.json 里——它们住在 dynamic.json，`meta()` 合并视图读取。

### dynamic.json —— 动态重要数据

```jsonc
{
  "version": 1,
  "updated_at": "2026-09-21T03:00:12+08:00",
  "game_meta": {
    // 键 = itad uuid，只存动态五键（_DYN_KEYS）
    "018d937f-…": {
      "reviews": {"score": 61, "count": 472},
      "stats": {"rank": …, "waitlisted": …},
      "fetched_at": "2026-09-21T03:00:07+08:00",
      "detail_failed_at": null,
      "detail_attempts": 0
    }
  }
}
```

### cache.json —— 可重建（丢了重拉）

```jsonc
{
  "version": 1,
  "updated_at": "2026-09-27T12:00:00+08:00",
  "compare_cache": {
    // 区域原价永久缓存：键 = "<appid>|<cc>"，存 price_overview.initial；
    // 不随折扣期失效、不参与留存清理。现价每轮真查不进缓存（真查即校准）。
    "1658920|UA": {
      "initial": 6700,
      "currency": "UAH",
      "fetched_at": "2026-10-05T12:00:00+08:00"
    }
  },
  "low_time_cache": {
    // 折扣期暂存：上次史低时间，键 = "<game_id>|<expiry>"。
    // 每轮对当日新增 + 即将过期候选整批重取覆盖；随留存清理一起删。
    "018d937f-…|2026-09-28T19:00:00+02:00": "2025-09-18T03:00:00+08:00"
  },
  "heybox_miss": {
    // 小黑盒落空负缓存（TTL 90 天）：期内不重查，到期自动重查自愈
    "1658920": "2026-10-09"
  }
}
```

### run_log

每次运行追加一条（保留 `run_log_keep` 条，超出裁尾巴）。**键集与键序是稳定契约
（消费方按名读），别动**。日常模式主要键：

```
run_at / mode / sweep / deals_fetched / itad_requests / steam_requests
filtered{…}                  各筛选条件砍掉的条数
new_by_reason / new_today_raw / new_today_shown
upcoming_raw / upcoming_shown
detail_pipeline / detail_targets / detail_new_today / detail_derived_backlog
detail_rejudge / detail_unlisted_marked / detail_relisted
detail_fetched / detail_fallback_fetched / detail_fallback_skipped / detail_backlog
last_low_fetched / detail_pending / title_zh_fetched / title_zh_cached
compare_repriced / compare_fetched / price_mismatch[]
deduped_versions / tier{…} / fx{date,base}
limiter / steam_limiter / steam_browse_limiter / errors[]
```

baseline 模式另有精简键集；prefetch 键集见 `run.py` 的 `run_prefetch`。

## 留存清理

每次运行开始时，删除 `expiry` 已超过 `expired_retention_days`（默认 7 天）的
`seen_deal` 条目；`low_time_cache`（键含 expiry）随同一保留期一起清理，
expiry 缺失或解析不了的暂存键一并删（无法与折扣期绑定，可重建）。
⚠️ `compare_cache`（`appid|cc` 永久键）**不在清理范围**——键不含 expiry，
若仍走 expiry 清理会被当成「绑定不了折扣期」整把误删。

## 崩溃恢复 runbook（跨文件非原子）

`State.save` 按 state → dynamic → cache **顺序做三次独立原子替换**，**做不到跨文件原子**：
进程若恰好死在「state 已写、dynamic 未写」之间，磁盘上会留下**新的 state + 旧的 dynamic**，
旧 `fetched_at` 会被下一轮的四档 TTL 当成「详情已过期」而**整批重抓**
（一轮全量详情请求，消耗 Steam 限流预算）。

判定与处置（三件套都属可收敛，**不丢数据**，只是重抓一轮）：
1. 某轮 `detail_fetched` 异常放大、或 about 页「中文名 / 评价」新取量突增 → 怀疑撞上该窗口；
2. 与 data 分支历史比对：`git show origin/data:data/dynamic.json` 里的 `fetched_at`
   是否**早于**同目录 `state.json` 的 `updated_at` 一大截；
3. 属实则**什么都不用做**——跑完一轮即自愈（`seen_deal` 用幂等键，重抓覆盖）。
   预算紧张时用手动 `workflow_dispatch` 只跑 `--prefetch`、分几天补完，不必改代码。

> **不引入 `.bak` 备份方案**：该窗口每轮仅约 10 次落盘、命中概率极低，恢复代价
> 只是「多抓一轮」；给保存主路径加备份/回滚会引入新的失败面，收益不划算。

## 模块与文件布局

```
SteamDailyLowest/
├── AGENTS.md                     agent 工作区规则
├── CONTEXT.md                    领域口径词汇表
├── README.md                     面向读者
├── config.example.json           配置模板
├── config.json                   本地配置，含 key，已 gitignore
├── requirements.txt              requests + jinja2
├── run.py                        CLI 入口：默认 daily / --baseline / --audit / --prefetch / --probe
├── src/
│   ├── __init__.py               包标记（空）
│   ├── config.py                 读配置；支持 ITAD_API_KEY 环境变量覆盖
│   ├── httpclient.py             **共用网络底座**：限流 + 五种响应分开处理（ITAD/Steam 共用）
│   ├── ratelimit.py              滑动窗口限流器
│   ├── itad.py                   ITAD 客户端：deals/v2 翻页、info/v2 降级、storelow/v2、lookup
│   ├── steam.py                  Steam 旧链路：单 appid（重定向处理）、批量取各区价格
│   ├── steam_browse.py           Steam 新链路 GetItems：批量元数据 + 封面（include_assets）
│   ├── heybox.py                 小黑盒客户端（社区中文名回填）
│   ├── announcements.py          顶部消息区内容（节日/通知）解析
│   ├── fx.py                     汇率：每日取一次并缓存，X → CNY 换算
│   ├── enrich.py                 编排：给「进列表」的条目补中文名 + 跨区比价
│   ├── classify.py               史低分类、当日新增、多版本去重、好评分档、视图窗口（纯函数无 IO）
│   ├── state.py                  三层状态库读写、幂等、精简落库、留存清理、原子写
│   ├── pipeline.py               可复用管线核心：render_pass / merge_details / build_stats / count_backlog
│   ├── snapshot.py               「即将过期」快照导出（data/expiring.json，跨仓库数据契约）
│   └── report.py                 Jinja2 渲染 index.html + about.html + data.js + latest.json + all/ 分片
├── templates/
│   ├── index.html.j2 / about.html.j2 / _brand.j2
│   └── static/app.css, app.js
├── content/
│   └── announcements.json        顶部消息区内容（手工维护，改内容不动代码）
├── data/                         gitignore；state.json + dynamic.json + cache.json + expiring.json
│                                 + fx_cache.json 入库 data 分支（probe/ 仅本地）
├── output/                       gitignore：生成物（Actions 里用 deploy-pages 发布）
├── tests/                        pytest 单测 + jsdom/ 前端冒烟
├── tools/                        探针、校验、回填与预览脚本（probes/ 不入库）
└── .github/
    ├── actions/                  composite actions：state-restore / data-writeback
    └── workflows/                daily.yml / backfill.yml / probe.yml / checks.yml
```

**职责边界**：`httpclient.py` / `ratelimit.py` / `itad.py` / `steam.py` /
`steam_browse.py` / `heybox.py` / `fx.py` 只负责网络与解析；
`classify.py` 是纯函数、无 IO，便于对判定规则写单元测试；
`state.py` 与 `snapshot.py` 是碰磁盘的模块（均临时文件 + `os.replace` 原子写；
`atomic_write_text` / `atomic_write_json` 定义在 `state.py`，供
`snapshot.py` / `report.py` / `fx.py` 复用）；
`enrich.py` 是唯一同时用到 steam + fx + state 的地方；
`pipeline.py` 住可复用管线核心，`run.py` 只编排。

> **为什么要有 `httpclient.py`**：ITAD 与 Steam 都要「滑动窗口限流 + 五种响应分开处理」，
> 抽成共用底座后两个客户端只保留各自的端点解析，限流策略只有一份。
>
> **为什么要有 `enrich.py`**：中文名与跨区比价的编排要用到 steam（网络）、
> fx（汇率）、state（缓存）三方，放进 `run.py` 会把编排和业务细节混在一起。

## tools 速查

| 脚本 | 用途 |
| --- | --- |
| `tools/render_report.py` | 本地渲染报表（零网络；本地 state 过期时先 `git show origin/data:data/state.json > data/state.json`） |
| `tools/make_preview.py` | 单文件手机预览（内联 all 分片，`preview_single.html`） |
| `tools/check_payload.py` | 产物载荷校验 |
| `tools/check_workflow_yaml.py` | 提交前 YAML 校验（依赖 PyYAML） |
| `tools/check_steam.py` / `check_all.py` / `probe_api_limits.py` | 探针（走同一套节流） |
| `tools/backfill_low_period.py` | 史低期回填（ITAD history/v2，backfill.yml 手动触发） |
| `tools/backfill_steam_cover.py` | Steam 封面存量补缺（一次性，backfill.yml 手动触发） |
| `tools/backfill_cn_names.py` | 小黑盒社区中文名回填（backfill.yml 手动触发，想起来了跑一把） |
| `tools/sync_state_from_remote.py` | 本机拉远端 data 分支四份 JSON 原子替换 |
