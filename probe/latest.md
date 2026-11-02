# 接口边界探针报告

- 材料来源：`statedata/data/state.json`（真实 appid 5,744 个 / uuid 32,643 个）
- 请求间隔：2.0s　请求硬上限：120
- 要跑的探针：`reviews,mix`
- ⚠️ 全程**不请求 ITAD `/deals/v2`**

### P6 · `appdetails` 与 `GetItems` 是否共用一个限流桶（观察，不定论）

两者轮换各打 6 次，看是否有一方先出现 429。**样本太小，只能作线索。**

| 轮次 | appdetails | GetItems |
|---|---|---|
| 1 | 200 | 200 |
| 2 | 200 | 200 |
| 3 | 200 | 200 |
| 4 | 200 | 200 |
| 5 | 200 | 200 |
| 6 | 200 | 200 |

### P7 · 好评率口径对照（ITAD `info/v2` vs `GetItems`）—— 上线前硬阻塞

- `GetItems` 一次批量（25 个 appid）返回 **25** 条

| appid | 标题 | ITAD score | ITAD count | GM filtered | count | unfiltered | lang_specific | 判定 |
|---|---|---|---|---|---|---|---|---|
| 1299510 | Vampire: The Masquerade - Sw | 65 | 561 | 64 | 581 | None | None | 一致（-1） |
| 4620430 | Meet Me In The Well | 100 | 4 | 100 | 7 | None | None | 一致（+0） |
| 1647420 | GROSS | 79 | 270 | 79 | 270 | None | None | 一致（+0） |
| 2486730 | ART IS RIFLE | 76 | 67 | 76 | 67 | None | None | 一致（+0） |
| 1850840 | MAFIA: Family's Secret | 75 | 32 | 75 | 32 | None | None | 一致（+0） |
| 1285080 | Gordian Rooms 1: A curious h | 85 | 526 | 85 | 526 | None | None | 一致（+0） |
| 2894820 | Velvet Bite: Softly, with Te | 54 | 85 | 53 | 86 | None | None | 一致（-1） |
| 4991980 | Cape Warrior | 81 | 11 | 81 | 11 | None | None | 一致（+0） |
| 2631920 | Poly Kingdom: Siege | 0 | 1 | 0 | 1 | None | None | 一致（+0） |
| 3863920 | Exit Plan b | None | None | 0 | 0 | None | None | 数据缺失 |
| 4003990 | Grieving | 87 | 16 | 87 | 16 | None | None | 一致（+0） |
| 1691190 | Rogue Waters | 75 | 975 | 75 | 975 | None | None | 一致（+0） |
| 294590 | Wars and Warriors  Joan of  | 93 | 367 | 93 | 367 | None | None | 一致（+0） |
| 3842910 | Impact to the Head | 83 | 18 | 83 | 18 | None | None | 一致（+0） |
| 2638240 |  | 45 | 22 | 45 | 22 | None | None | 一致（+0） |
| 1528220 | Gordian Rooms 2: A curious i | 75 | 151 | 75 | 151 | None | None | 一致（+0） |
| 2148970 | Skygard Arena | 76 | 94 | 76 | 94 | None | None | 一致（+0） |
| 1098280 | Mago | 93 | 934 | 93 | 934 | None | None | 一致（+0） |
| 4700390 | MUTS NO TYPO | None | None | 0 | 0 | None | None | 数据缺失 |
| 4992270 | Sad Virus Tokyo | 100 | 1 | 100 | 1 | None | None | 一致（+0） |
| 3965860 | Shady O'Grady's™ Overnight S | 90 | 21 | 90 | 21 | None | None | 一致（+0） |
| 4426470 | Zombie Survival 3D | 76 | 46 | 71 | 56 | None | None | **偏差 -5** |
| 3403890 | Good For Nothing | 16 | 6 | 16 | 6 | None | None | 一致（+0） |
| 2138560 | Dissent on Mars | 47 | 19 | 47 | 19 | None | None | 一致（+0） |
| 3190560 | Acid Web | 100 | 8 | 100 | 8 | None | None | 一致（+0） |

→ 可比样本 **23** 个：\|偏差\| ≤1 的占 **22/23**，平均 \|偏差\| **0.30** 点

> `summary_filtered` = Steam 页面默认展示口径（已剔除异常刷评时段）。本项目现有阈值（好评率 ≥70% / 评价数 ≥100 / 高热度 ≥10000）建立在 ITAD 口径上，若平均偏差 >2 点就需要重标定。

---

## 运行元信息

- 实际发出的 HTTP 请求数：**38**（上限 120）
- ✅ 全部探针跑完，未触发停止条件

> 说明：本报告只记录**观测事实**。未出现 429 不等于「上限就是这么多」，只说明在这个量级上安全。
