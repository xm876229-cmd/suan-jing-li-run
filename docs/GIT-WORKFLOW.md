# Git 工作流规范

## 1. 分支策略

| 分支 | 用途 | 说明 |
| --- | --- | --- |
| `main` | 稳定线 | 始终可交付、结果可复现。只接受已完成验证的合并 |
| `feat/<简述>` | 新功能 / 新口径 | 例：`feat/ph-country-rate` |
| `fix/<简述>` | 修数据或算错 | 例：`fix/refund-double-count` |
| `chore/<简述>` | 结构调整、配置、文档 | 例：`chore/git-init` |

小改动（改一个费率、补一份文档）可直接提交到 `main`；涉及公式、口径、脚本重写的，一律走分支。

```bash
git switch -c feat/xxx        # 开分支
# ... 干活 ...
git add -A && git commit -m "feat(净利润): 新增菲律宾国家费率达到 2%"
git switch main && git merge --no-ff feat/xxx
git push origin main
```

## 2. 提交信息规范

格式：`<type>(<scope>): <subject>`

- **type**：`feat` 新增 / `fix` 修复 / `data` 数据更新 / `chore` 杂务 / `docs` 文档 / `refactor` 重构
- **scope**：`净利润`、`汇率`、`成本`、`订单`、`广告`、`佣金`、`工作簿`、`脚本` 等
- **subject**：中文，动词开头，说明改了什么，不写「update」这种无信息量的词

示例：

```
feat(汇率): 马来西亚平台佣金率 8.1% -> 8.5%
data(订单): 导入 10 月上半月订单 1,234 行
fix(净利润): 修正退款订单重复计入 GMV
chore(仓库): 初始化版本管理结构与规范
```

一次提交只做一件事。参数变更与代码重构不要混在同一个 commit。

## 3. 二进制文件的版本管理

`.xlsx` 是二进制文件，Git 无法做行级 diff（已在 `.gitattributes` 中标记为 `binary`）。

- 每次改动工作簿都要提交，**commit 信息里写清改了哪张表、影响哪些行**，这是唯一的历史线索。
- 体积暴涨（> 50 MB）时，再引入 Git LFS：`git lfs track "*.xlsx"`。
- 不要把中间产物、临时另存文件放进仓库（`output/` 已忽略）。

## 4. 版本号与标签

采用语义化版本 `v<主>.<次>.<修>`：

- **主**：核算口径发生不兼容变更（例如净利定义改变）
- **次**：新增国家 / 新增费用项 / 新增报表
- **修**：修错、补数据、调单个费率

```bash
git tag -a v0.2.0 -m "新增泰国站点核算"
git push origin v0.2.0
```

打标签同时更新 `CHANGELOG.md`。

## 5. 日常同步

```bash
git status                 # 看改动
git add -A                 # 暂存
git commit -m "..."        # 提交
git push                   # 推送到 GitHub
git log --oneline -10      # 看最近提交
git diff HEAD~1 --stat     # 看上一版改了哪些文件
```

## 6. 回滚

```bash
git log --oneline                 # 找到目标提交
git restore --source=<sha> -- "V1-净利  -15.xlsx"   # 只回滚工作簿
git revert <sha>                  # 生成反向提交（推荐，保留历史）
```

> 严禁 `git reset --hard` + `git push --force` 处理已推送的历史，会让远程与本地脱节。
