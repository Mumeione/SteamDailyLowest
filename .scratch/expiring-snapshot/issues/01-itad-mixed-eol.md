Status: resolved

# 01: `src/itad.py` 混合换行符需要一次性归一化

**背景**：2026-09-30 做快照 v3 时发现，本文件在**已提交的 blob 里**就是混合换行
（221 行 = 185 CRLF + 36 孤立 LF；全仓库 47 个 .py/.md 里唯一一个）。
本机 `core.autocrlf = true` 且仓库无 `.gitattributes`，git 对这种 blob 不做安全转换，
导致**任何编辑都会让 diff 爆炸**（本次实测：真实改动 +32/-1，diff 显示 401 行）。

**当时的处置**：工作区还原成混合换行版本 + 用 `git apply --ignore-whitespace` 重放
真实改动，保证验收 diff 干净。归一化本身**留到下一轮单独做**（用户 09-30 裁决：
问题记录，下一轮再修改，不混进功能提交）。

## 建议做法（下一轮动手时参考）

1. 新增 `.gitattributes`：`* text=auto`（或按类型细列）；
2. 单独一个 chore 提交：`git add --renormalize .`，把全仓库换行符归一；
3. 该提交**不夹带任何功能改动**，否则 blame 会被污染（要看穿需 `git blame -w`）；
4. 做完抽查 `git diff --stat HEAD~1` 确认只有换行变化（`--ignore-cr-at-eol` 下应为空）。

## Comments

## Answer

2026-09-30 当日处理：功能改动先行提交（保持 itad.py 混合换行下的干净 diff），
随后单独 chore 提交：新增 `.gitattributes`（`* text=auto`）+ `git add --renormalize .`
把全仓库 blob 归一为 LF。验证方式：`git diff --ignore-cr-at-eol HEAD~1 HEAD` 为空
（纯换行变化、零内容改动）。
