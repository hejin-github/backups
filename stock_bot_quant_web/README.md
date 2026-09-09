# stock_bot_quant_web — StockBot 策略选股回放 + 每日推荐 Web 服务（A 股）

本地运行于 `localhost:8081` 的策略服务：后台引擎（history_engine）按策略逐日模拟交易并生成每日选股记录/历史交易列表；页面按策略展示买卖时点速览、每日选股、历史交易明细、今日盘中提醒；交易日每 5 分钟扫描行情，盘中触发信号与收盘简报通过配置的渠道推送。

## 目录布局（还原本地可运行结构）

```
stock_bot_quant_web/
├── backtest.py           # A股方法论回测引擎（5000元/笔，多策略）。数据锚定 DATA_ROOT=本文件所在目录
├── chan_core.py          # 缠论核心算法（K线包含/分型/笔/中枢/三类买卖点），S15 依赖（backtest 可选导入）
├── stock_bot/
│   ├── app.py            # Web 服务入口（ThreadingHTTPServer :8081），扫描调度/页面/API/收盘任务
│   ├── advisor.py        # 推荐引擎：行情拉取、当日数据补齐、策略轮换
│   ├── history_engine.py # 每日模拟/重建引擎（build），写 {根}/data/strategy_daily.json
│   ├── notify.py         # 推送模块：盘中信号/收盘简报/告警（pushplus|qywx|dingtalk|feishu|qmail）
│   ├── start.bat         # 启动器：自动杀旧实例 → 独立窗口启动服务 → 打开页面
│   ├── stop.bat
│   └── *.md              # 项目上下文 / 复现提示文档（供 AI 开发用）
```

> 为什么 `backtest.py` 在顶层而服务在 `stock_bot/`？`advisor.py`/`history_engine.py`
> 通过 `sys.path.insert(0, _WORKSPACE)`（两级父目录）+ `os.chdir(_WORKSPACE)` 把工程根挂到
> `sys.path`，再 `import backtest as bt`、`from stock_bot import advisor`。因此**必须保持
> 「工程根放 backtest.py/chan_core.py + 同名子包 stock_bot/」的布局**，否则模块找不到。
> 若改文件夹名，需同步改 advisor/history_engine 里的相对路径推算。

## 运行

- 环境：Python 3.9+，依赖 `pandas numpy requests akshare`（行情/估值接口），推送可选。
  ```bash
  pip install pandas numpy requests akshare
  ```
- 启动：`cd stock_bot && start.bat`（或 `python -X utf8 -u stock_bot/app.py`，在工程根执行）。
- 页面：http://127.0.0.1:8081 ；重启服务后首轮扫描在后台进行（几分钟）。

## 数据与配置（本备份不含 data/）

运行时读取工程根下 `data/` 目录（备份刻意排除：体积大、含行情数据/日志/密钥等生成物）：

| 路径 | 用途 | 缺失影响 |
|---|---|---|
| `data/csv/*.csv` | 全市场主板日线（约 2600+ 只，前复权 12 列） | 全市场动态选股不可用（内置 56 只精选池可回退） |
| `data/stock_universe.csv` | 主板选股池（剔除 ST/退市/创业/科创/北交） | 同上 |
| `data/sh_index.csv` 等 | 上证指数/指数估值 | 大盘过滤与行情判断不完整 |
| `data/notify_config.json` | 推送渠道配置（channel + token/webhook/密钥） | 无推送 |
| `data/strategy_daily.json` | 引擎产出：各策略每日记录 | 空库可后台重建（30~60s+） |

首次部署到新机器需自行补齐上述数据（akshare 拉取脚本在本仓库之外），并在
`data/notify_config.json` 填写推送渠道。核心源码不包含任何密钥。

## 策略清单

10 个正式策略 + 实验策略，核心定义见 `backtest.py`（STRATEGIES / STRATEGY_TIMING /
BUY_FILTERS）与 `history_engine.py`：
S1 Christian突破回踩 / S2 MACD金叉KDJ死叉 / S3 吊灯止损趋势跟踪 / S4 20日线回踩放量 /
S5 止盈15止损5 / S6 布林下轨做T / S7 沪深300PE分批低买高卖（指数）/ S8 红利低估值分批买入（指数）/
S9 趋势线突破回踩 / S10 RSI超卖超买反转 / S15 缠论三买（实验）/ S16 龙头接力 base+V2（实验，官方放弃式口径）。

## 版本快照说明

- 快照时间：2026-09-09（含当日收盘简版升级：盘中信号邮件带"具体扫描触发时刻 trigger_time"
  与"推荐理由"，去重持久化跨重启）。
- 页面 / 引擎已知机制：信号收盘后判定、T+1 按策略时点执行；收盘后每交易日一次后台 build
  滚入当日记录；盘中提供即时记录槽（am/pm）。
