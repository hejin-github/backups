# quant_trader 工程备份

- 备份日期：2026-09-09
- 来源工程：本地 `quant_trader`（量化交易策略工程）
- 远程仓库：https://github.com/hejin-github/backups.git

## 本目录文件

| 文件 | 说明 |
|------|------|
| `quant_trader_code_20260909.zip` | 完整源码快照（66 个受版本控制文件：全部 `.py` / `requirements.txt` / `README.md` / `CHANGELOG.md` 等）。**不含** `data/`、`*.log`、`*.pkl`、依赖等生成物。可直接解压使用。 |
| `quant_trader_20260909.bundle` | 完整 git 仓库包（含全部提交历史 + 标签 `backup-20260909`），用于整体恢复工程与历史。 |

## 恢复方法

### 方式一：只要代码（最快）
直接解压 `quant_trader_code_20260909.zip` 即可得到完整源码，无需 git。

### 方式二：恢复含完整 git 历史（推荐）
```bash
git clone quant_trader_20260909.bundle quant_trader_restore
cd quant_trader_restore
git checkout backup-20260909   # 切到备份当天的标签（即备份时 HEAD）
```
> 克隆后默认位于主分支最新提交；按标签 `backup-20260909` 可精确回到备份点。

### 方式三：把备份历史并入已有仓库
```bash
cd <你的现有仓库>
git fetch quant_trader_20260909.bundle backup-20260909:backup-20260909
git checkout backup-20260909
```

## 备注
- 备份时本地工程 HEAD 已打标签 `backup-20260909`。
- zip 与 bundle 均为完整代码备份；bundle 额外包含 git 历史，可二选一使用。
