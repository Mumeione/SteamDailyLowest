# 部署（第 9 步：GitHub Actions + Pages）spec

> Status: spec 待审
> 决策时间：2026-09-22，含用户对 git 历史体积的裁决（第 3、4 条想法）

## 用户决策

1. **报表与状态不进 main 历史**：`state.json` 每天 ~4MB，日提交会让 git 历史线性膨胀。
   用户方案：**单独开一个 `data` 分支放 json（与报表），每月定时清理** —— 采纳。
   术语说明：git 无法真正「覆盖上一条」——append 提交历史必然增长；
   「每月清理」落地为 **orphan 重置 `data` 分支**（历史清零、只留当前一版），达成同样效果。
2. `docs/DEVELOPMENT.md §8` 的四个坑必须全部处理（见下）。

## 方案

### D1 仓库与分支布局

- `main`：代码 + docs + spec，**不含 `data/state.json`**（`git rm --cached` +
  `.gitignore` 加 `data/state.json`；本机文件保留在磁盘上供本地运行）。
- `data` 分支：只放 `data/state.json`。日常任务每次运行后 append 一个提交；
  **每月 1 日**由定时 workflow orphan 重置（保留当前内容、历史清零、force push）。
- 报表（`output/`）**不进任何分支**：Pages 用 `actions/deploy-pages` 从 workflow
  artifact 直接发布（§8 坑 1：`GITHUB_TOKEN` 推的 commit 不触发 Pages 构建，
  所以发布必须在同一次运行内完成）。

### D2 `daily.yml`（一个 workflow，三个入口）

```yaml
on:
  schedule:
    - cron: "0 19 * * *"    # 03:00 Asia/Shanghai（UTC 表达）
    - cron: "0 7 1 * *"     # 每月 1 日 15:00 预抓（UTC 表达）
    - cron: "0 7 2 * *"     # 每月 2 日跑 orphan 清理（UTC 表达）
  workflow_dispatch: {}
permissions:
  contents: write
  pages: write
  id-token: write
```

⚠️ cron 用 **UTC**：03:00 CST = 19:00 UTC（前一天）；`timezone:` 字段 GitHub 不支持。

- **主任务 job**（03:00）：
  1. checkout `main` + 单独 checkout `data` 分支到子目录（`actions/checkout` 两次，
     `with: {ref: data, path: statedata}`）；
  2. 把 `statedata/data/state.json` 复制到工作区 `data/state.json`（没有则空跑起步）；
  3. `python run.py`（ITAD_API_KEY 从 `secrets` 注入环境变量，`src/config.py` 已支持覆盖）；
  4. `actions/configure-pages` + `actions/upload-pages-artifact`（path: `output/`）+
     `actions/deploy-pages` —— **同一次运行内**发布（§8 坑 1）；
  5. 把更新后的 `data/state.json` 提交回 `data` 分支（`git -C statedata` 提交 + push，
     §8 坑 3：runner 无持久磁盘，状态只能回写仓库）。
- **预抓 job**（15:00）：同 1-2、3 改 `python run.py --prefetch`、5 照旧；不出报表不发 Pages。
- **清理 job**（每月 2 日）：`git checkout --orphan data-new` → 提交当前 `state.json` →
  force push 覆盖 `data` → 删临时分支。历史清零，实现用户要的「覆盖上一条」。

### D3 其余坑（§8 对照）

- 坑 2（60 天无活动禁用定时任务）：主任务每日都提交 `data` 分支，天然规避；
  清理 job 每月也产生活动。
- 坑 4（`.gitignore` 必须 `data/*` + `!data/state.json` 才能提交状态）：
  本方案改为 `data` 分支持有状态，**main 的 .gitignore 直接加 `data/state.json`**，
  不再依赖取反写法（坑 4 的写法只对「状态留在 main」的方案有意义）。

### D4 失败告警

- 「仅失败时通知」：主任务 / 预抓 job 末尾加 `if: failure()` 步骤。
  第一版用 workflow 失败邮件通知（GitHub 默认发给仓库所有者，收件箱可配），
  不引入第三方依赖；后续可换 webhook。

### D5 密钥

- `ITAD_API_KEY` 走 repo secrets（`gh secret set ITAD_API_KEY`，值从本机 `config.json`
  注入，**不落任何文件**）；`GITHUB_TOKEN` 由 Actions 自动提供，无需配置。

## 验收

- [ ] 手动 `workflow_dispatch` 跑通全链路：报表发布到 Pages、`data` 分支有新提交。
- [ ] Pages 地址手机可访问；每日 03:00 自动跑。
- [ ] 主任务与预抓 job 都能正确「取出 → 更新 → 回写」`data/state.json`。
- [ ] 清理 job 执行后 `data` 分支只剩 1 个提交且内容不丢。
- [ ] 故意置错（如改错 API key）能收到失败通知。

## Comments
