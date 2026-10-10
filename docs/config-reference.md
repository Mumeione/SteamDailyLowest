# 配置项参考

> `config.json` 各键的语义。**默认值的唯一出处是 `src/config.py` 的 `DEFAULTS` 与
> `config.example.json` 注释**——本文只写键的含义与取值口径，不背书具体数值；
> 下表「默认」列仅为快速定位，改默认值时以那两处为准（本文不参与数值维护）。
> 模板里没有的键也可写在 `config.json` 中生效（`config.json` 只需覆盖想改的键）。

| 字段 | 说明 |
| --- | --- |
| `itad_api_key` | ITAD key。也可用环境变量 `ITAD_API_KEY` 覆盖；**不得写入任何入库文件** |
| `country` | 主地区（价格与判定的国家口径） |
| `compare_countries` | 比价地区列表（走 Steam 官方接口；每新增一个地区都要先跟 Steam 校准，见 [data-sources.md](data-sources.md)） |
| `min_cut` | 最小折扣百分比 |
| `max_price` | 价格上限（分），null 不限 |
| `min_positive_ratio` | 好评率下限（也是卡片好评率**普通档**的颜色分界） |
| `good_positive_ratio` | 好评率**高分档**线：≥ 它卡片上的好评率用 Steam 蓝，否则灰色（`report.rate_tier`） |
| `bad_positive_ratio` | 评价档「褒贬不一」线：`<min_positive_ratio 且 ≥ 它` = 褒贬不一（琥珀），`< 它` = 差评 |
| `min_review_count` | 评价数下限 |
| `notable_review_count` | 「高热度 · 口碑不一」档的评价数门槛（见 [domain-rules.md](domain-rules.md)） |
| `absolute_min_positive_ratio` | 绝对好评率下限；null = 不启用 |
| `only_type` | 只留本体游戏（排除 dlc/package/None） |
| `exclude_mature` | 排除成人内容（ITAD 默认已过滤，白送级防御） |
| `exclude_free` | 排除免费游戏 |
| `expired_retention_days` | 折扣过期后再保留的天数（应对「隔几天又打折」） |
| `upcoming_expiry_hours` | 「即将到期」阈值（小时）；也是到期窗口 TTL 的起点（expiry − 它） |
| `week_window_days` | 本周视图长度（天） |
| `new_game_days` | 新游判定窗口：发行 ≤ 该天数享受每日评价刷新 |
| `new_game_refresh_days` / `discount_refresh_days` / `expiry_refresh_days` | 四档 TTL 的前三档（新游 1 / 折扣期普通 3 / 到期窗口 1 天；**非折扣期不刷新**，无配置键） |
| `detail_retry_cooldown_days` | 详情抓取失败的冷却天数 |
| `timezone` | 日期判定时区 |
| `state_path` | 状态文件（不可重建）；`dynamic.json` / `cache.json` 固定与其同目录同名派生，无独立配置键（见 [data-model.md](data-model.md)） |
| `expiring_snapshot_path` | 「即将过期」快照导出路径（跨仓库数据契约，随 data 分支持久化；字段与版本以 `src/snapshot.py` 实现为准） |
| `fx_cache_path` | 汇率缓存文件（每天一份） |
| `announcements_path` | 顶部消息区的内容文件（节日/活动 + 站点通知，手工维护）。**坏了只是不显示顶部条**，不影响出报表 |
| `output_dir` | 生成物目录 |
| `stale_warn_hours` / `stale_banner_hours` | 数据陈旧**黄档 / 红档**（顶部消息区第二行） |
| `request_pause_seconds` | ITAD 请求间隔（秒） |
| `request_timeout_seconds` | ITAD 请求超时（秒） |
| `steam_timeout_seconds` | Steam 请求超时（秒） |
| `http_budget_seconds` | 一轮运行的**总墙钟预算**（秒；`0` = 不限）。同一轮所有传输客户端共享：到点主动中止（走退出码 5 + 首版报表兜底 + 下一轮自愈），避免被 job 的 `timeout-minutes` 硬杀。取「job timeout − 5 分钟」 |
| `probe_timeout_seconds` | 探测类请求的**单独短超时**（秒） |
| `itad_rate_limit` / `itad_min_interval` | ITAD 滑动窗口（官方 1000/5min 留 20% 余量）与最小间隔（秒） |
| `steam_rate_limit` / `steam_min_interval` | Steam store **全站合并计数**窗口（社区 ~200/5min 留 25%）与最小间隔（秒）。appdetails / appreviews 同 host 共享预算，串行 + 2s 间隔 + 窗口三者同时用 |
| `steam_browse_rate_limit` / `steam_browse_min_interval` | GetItems（IStoreBrowseService）链路的窗口与最小间隔（**与 store 旧链路分开计数**；新链路实测响应快，间隔可更短） |
| `steam_batch_size` | 批量 `appdetails` 一次塞几个 appid（实测 20 正常，文档说的 50 未复核） |
| `steam_lang` | Steam 中文名的语言码 |
| `sweep_mode` | 抓取口径：`low_only` = 服务端只取「本体 + 史低」（27 页）/ `full` = 无 filter 全量（162 页） |
| `detail_fallback_budget` | GetItems 失效时 `info/v2` 逐条降级的每轮上限（1 请求/条，必须设界；正常时该路径为空） |
| `prefetch_daily_budget` | 预抓任务（`run.py --prefetch`）每轮补详情的条数上限；`0` = 关闭。顺序 = 最近出现在史低的优先 |
| `fetch_last_low_time` | 是否用 `storelow/v2` 批量补「上次史低时间」（见 [pipeline.md](pipeline.md)） |
| `max_deals` | ITAD `deals/v2` 单轮抓取条数上限，null 不限（测试/试跑用） |
| `run_log_keep` | run_log 保留条数（超出裁尾巴） |
| `list_batch` / `list_auto_max` | 滚动加载：每次追加条数 / 自动追加总上限（到了只留手动） |
| `page_size_mobile` / `page_size_desktop` | 每屏条数（手机 / 桌面） |
| `mobile_breakpoint_px` / `tablet_breakpoint_px` | 断点：手机档上限 / 平板档上限。**CSS 的 `@media` 必须与它一致**（经 `payload.list` 下发 + 机械校验断言） |
| `home_new_low_days` | 大卡池「一周内」分界；也是筛选面板「日期」默认档与「N 天新增」的口径来源 |
| `home_picks` | 顶部大卡**总张数上限**（5 的整数倍）；实际取数张数的公式唯一成文处见 [report-design.md](report-design.md)「大卡」 |
| `home_section_preview` / `home_section_preview_min` | 四板块栏位数**上限 / 下限**（池子不足时如实显示池量、不凑数） |
| `big_cut_percent` | 「大额折扣」板块阈值（折扣 ≥ 它入选）；筛选面板的 implied 选项文案跟着它变 |
| `recommend_min_rate` / `recommend_min_count` | 大卡前置门槛：好评率（%）/ 评价数 |
| `recommend_weights` | 推荐公式权重（新史低板块，名气优先）。⚠️ **表里不许出现「史低类型」项**（有单测锁定） |
| `picks_weights` | **大卡专属权重档**（折扣优先，与板块刻意拆开；间隔 = 新史低取史低期记忆 / 平史低取上次同价）。⚠️ 同样不许有「史低类型」项 |
| `site_repo_url` / `site_actions_url` | 页脚与关于页的仓库 / Actions 链接 |

> 已删除的旧键（勿再使用）：`detail_daily_budget` / `detail_scope` / `max_concurrency` /
> `use_steam_side_fetch`（详情派生式欠账不设预算）、`reviews_ttl_days` /
> `reviews_empty_ttl_days`（被四档 TTL 取代）、`meta_valid`（被 dynamic.json 取代）。
