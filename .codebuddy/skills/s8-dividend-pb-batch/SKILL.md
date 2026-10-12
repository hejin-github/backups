---
name: s8-dividend-pb-batch
description: stock_bot 模拟交易策略【S8_红利低估值分批买入】的技术档案 —— 入场/出场规则与全部阈值、评分函数与参考价、买卖时点与大盘过滤、backtest/advisor/history_engine 三处注册位置与行号、8081 页面入口 /s/S8_%E7%BA%A2%E5%88%A9%E4%BD%8E%E4%BC%B0%E5%80%BC%E5%88%86%E6%89%B9%E4%B9%B0%E5%85%A5、实盘账本表现（2026-07-01～2026-10-08）、已知缺陷与回测结论、改动须知与验收命令。当需要查看、回放、调参、改代码、评估这个策略，或用户提到「S8、红利、市净率、PB、低估值分批」时使用。
display_name: "S8_红利低估值分批买入"
version: 0.1.0
origin: custom
---

# S8_红利低估值分批买入（S8）

> 项目：`D:/codebuddy_workspace/stock_bot`（服务 `http://127.0.0.1:8081`）｜页面：`/s/S8_%E7%BA%A2%E5%88%A9%E4%BD%8E%E4%BC%B0%E5%80%BC%E5%88%86%E6%89%B9%E4%B9%B0%E5%85%A5`
> 账本：`data/strategy_daily.json`（2026-07-01～2026-10-08，单笔本金 5000，含费）
> 本档案由 `_gen_strategy_skills.py` 从源码与账本自动提取骨架，规则与阈值以源码为准。

## 1. 策略定位

红利指数的 PB 估值策略：读 `data/csv/hsly_pb.csv`（中证红利-红利指数 PB），PB 越低越买、越高越卖，-8% 止损，另有一条 700 日的持有上限，不设 `_q_target` 季度再平衡。

## 2. 注册位置（改代码先看这里）

本系统**没有集中式注册表**，策略由三处横向拼接：

| 环节 | 文件:位置 | 本策略取值 |
|---|---|---|
| 信号函数（回测） | `无独立信号函数（指数策略，走 simulate_index）` | `history_engine.py:1451 simulate_index + idx_specs` |
| 评分函数（每日推荐） | `不适用（指数策略不参与个股评分）` | `—` |
| 买卖时点 | `history_engine.py:92 STRATEGY_TIMING` | 买 `close` / 卖 `close` |
| 大盘过滤 | `history_engine.py:49 BUY_FILTERS` | `无` |
| 环境适配 | `history_engine.py:111 STRATEGY_ENV_FIT` | `不适用（指数策略不参与 ENV_FIT 轮值）` |
| 每日信号上限 | `history_engine.py:37 MAX_DAILY_SIGNALS` | `不适用` |
| 低开废弃 | `history_engine.py:134 DISCARD_LOW_OPEN` | `不适用` |
| 顶板废弃 | `history_engine.py:143 UNFILLABLE_ZT_OPEN` | `不适用` |
| 单仓资金 | `history_engine.py:529 SINGLE_CAP_POLICY` | `分批，每批 5000` |

驱动链：`history_engine.build()` → `_run_stock()` → `simulate_stock()` 逐日滚动 → 有持仓时以三参 `sfunc(k, df, ctx)` 调用。
每日推荐：`advisor.scan_stock_strategy()` → `STOCK_ANALYZERS[name]`（advisor.py:368）。

## 3. 入场规则（源码阈值）

由 `history_engine.py:1832 idx_specs` 定义（`simulate_index`，`history_engine.py:1451`）：

- 数据源 `data/csv/hsly_pb.csv`，估值列 = **市净率**
- **建仓**：PB < **0.85**
- **加仓**：PB < **0.75**
- **最大持有 700 日**（`max_hold_days=700`）、**止损 -8%**
- 无 `_q_target`（与 S7 的区别，**S8 没有季度再平衡**）
- 无大盘过滤

## 4. 出场规则

- **减仓**：PB > **0.90**
- **清仓**：PB > **1.00**
- **止损 -8%**
- **持有满 700 日**强制离场

## 5. 评分与参考价

指数策略无评分/无参考价。页面展示「持有 N 批 / 成本 / 现价 / 浮盈」。

当前（2026-09-30）：`持有1批 成本3220.68 现价3271.90 浮盈1.6%`。

## 6. 实盘账本表现（2026-07-01～2026-10-08）

| 指标 | 值 |
|---|---|
| 成交笔数 | 0（未触发减仓/清仓/止损） |
| 净盈亏 | 0.00 |
| 当前 | 持有 1 批 红利指数，成本 3220.68 / 现价 3271.90 / **+1.6%** |

> 口径：净盈亏=已卖出落袋 + 当前持仓浮盈；样本极小（个位数~15 笔），**不足以推翻长期回测，也不足以证明策略有效**。

## 7. 已知缺陷 / 研究结论

- 同 S7：**硬编码在 `idx_specs`，不在 `bt.STRATEGIES`**，遍历 STRATEGIES 的脚本会漏掉。
- 2026-10-08 复盘：**中证红利 PB = 0.87，近 3 年 99% 分位** —— 处于历史极贵区间，已在 0.85 建仓线之上、接近 0.90 减仓线。「99% 分位」意味着按 PB 口径它几乎从未这么贵过，继续持有要留意估值均值回归风险。
- 没有 `_q_target` 再平衡，PB 长期高位时会一直持有着，不会像 S7 那样被季度再平衡主动降批。

## 8. 改动须知

- 改**信号阈值** → 改 `backtest.py` 的 `history_engine.py:1451 simulate_index + idx_specs`；改**评分/参考价** → 改 `advisor.py` 的 `—`。**两边逻辑必须保持一致**，否则"回测选一只、页面推另一只"。
- `sfunc` 签名不能动（`sfunc(i, df, pos)` 与盘中有持仓时的 `sfunc(k, df, ctx)` 三参调用），加形参直接 TypeError；要参数化只能提模块级常量。
- 改买卖时点/过滤/单仓资金 → 改 `history_engine.py` 顶部常量表，**改完必须重建账本**（`data/strategy_daily.json` 不会自动重算历史）。
- **重启服务不会重建账本**（`last_build_date.json` 跨启动去重，同日只跑一次），改代码后要先清标记或单独跑 build（全量约 25 分钟，期间停服务避免与刷新争写 `strategy_daily.json`）。
- 验收：`python -X utf8 -u stock_bot/_runtest.py`（compile + 起服 + 冒烟 + 内容断言 + 接口断言）。
- 重启**只能**用按端口杀的脚本（`c:/Users/hejin/CodeBuddy/20260819154435/_restart_sb_1008.ps1`，只杀 8081）；旧脚本 `_restart_sb2.ps1` 按 CommandLine 匹配 `app.py` 批量杀，会同时干掉 8501 QuantTrader 与 8000 hubin_daban。
- 全市场回测前必须 `set OPENBLAS_NUM_THREADS=1 && set OMP_NUM_THREADS=1`（本机内存紧张，否则进程静默被杀）。

## 9. 常用命令

```bat
:: 重建模拟账本（收盘后跑，约 25 分钟）
cd /d D:\codebuddy_workspace && python -X utf8 -u -c "import sys; sys.path.insert(0,'stock_bot'); import history_engine; history_engine.build()"
:: 一键验收
cd /d D:\codebuddy_workspace && python -X utf8 -u stock_bot\_runtest.py
:: 裸选股体检（剥离卖出规则，只看前向收益）
cd /d D:\codebuddy_workspace && python -X utf8 -u _signal_health.py --names S8
```

## 附：13 个策略一览（同一套机组里的位置）

| 策略 | 时点(买/卖) | 一句话 |
|---|---|---|
| S1_Christian突破回踩 | close/close | 60日涨幅30~100% + 突破20日高 |
| S2_MACD金叉KDJ死叉 | open/close | 金叉买、死叉卖、-5%止损 |
| S3_吊灯止损趋势跟踪 | open/close | 站上MA20破前日高，3×ATR吊灯止损（仅趋势市） |
| S4_20日线回踩放量 | open/close | 贴MA20±2% + 阳线 + 放量1.5倍 |
| S5_止盈15止损5 | close/close | MA20向上 + 20日收盘新高 |
| S6_布林下轨做T | open/open | 破下轨买、中轨卖（仅震荡市） |
| S7_沪深300PE分批低买高卖 | close/close | PE<12.5建仓、<8.75加仓、>12.5减仓、>16.25清仓 |
| S8_红利低估值分批买入 | close/close | PB<0.85建仓、<0.75加仓、>0.90减仓、>1.0清仓 |
| S9_趋势线突破回踩 | open/open | MA60向上 + 贴MA20±2% + 创新高（每日限1只） |
| S10_RSI超卖超买反转 | close/open | RSI上穿20买、下穿80卖（弱市专用） |
| S15_缠论三买 | open/open | 中枢上移后回调不破ZG，+8%/-5% |
| S16_龙头接力 | close/close | 涨停池最强龙头，T+1收盘打板买、断板当日收盘卖 |
| S16_龙头接力V2 | close/close | 同上，但龙头须连板≥3 |

> S7/S8 定义在 `history_engine.py:1832 idx_specs`（**不在 `bt.STRATEGIES` 里**），遍历 STRATEGIES 的脚本会漏掉它们。
