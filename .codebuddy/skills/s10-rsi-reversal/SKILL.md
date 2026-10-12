---
name: s10-rsi-reversal
description: stock_bot 模拟交易策略【S10_RSI超卖超买反转】的技术档案 —— 入场/出场规则与全部阈值、评分函数与参考价、买卖时点与大盘过滤、backtest/advisor/history_engine 三处注册位置与行号、8081 页面入口 /s/S10_RSI%E8%B6%85%E5%8D%96%E8%B6%85%E4%B9%B0%E5%8F%8D%E8%BD%AC、实盘账本表现（2026-07-01～2026-10-08）、已知缺陷与回测结论、改动须知与验收命令。当需要查看、回放、调参、改代码、评估这个策略，或用户提到「S10、RSI、超卖反转、RSI上穿20、下穿80」时使用。
display_name: "S10_RSI超卖超买反转"
version: 0.1.0
origin: custom
---

# S10_RSI超卖超买反转（S10）

> 项目：`D:/codebuddy_workspace/stock_bot`（服务 `http://127.0.0.1:8081`）｜页面：`/s/S10_RSI%E8%B6%85%E5%8D%96%E8%B6%85%E4%B9%B0%E5%8F%8D%E8%BD%AC`
> 账本：`data/strategy_daily.json`（2026-07-01～2026-10-08，单笔本金 5000，含费）
> 本档案由 `_gen_strategy_skills.py` 从源码与账本自动提取骨架，规则与阈值以源码为准。

## 1. 策略定位

唯一的逆势策略：RSI 从超卖区上穿 20 买入（当日收盘成交），RSI 下穿 80 卖出（次日开盘成交），亏 5% 止损。环境适配是 `weak`/`range` —— 别人不做的弱市它做。

## 2. 注册位置（改代码先看这里）

本系统**没有集中式注册表**，策略由三处横向拼接：

| 环节 | 文件:位置 | 本策略取值 |
|---|---|---|
| 信号函数（回测） | `backtest.py:480` | `s10_rsi_revert` |
| 评分函数（每日推荐） | `advisor.py:300` | `_a_s10` |
| 买卖时点 | `history_engine.py:92 STRATEGY_TIMING` | 买 `close` / 卖 `open` |
| 大盘过滤 | `history_engine.py:49 BUY_FILTERS` | `上证站上20日线（sh_gt_ma20）` |
| 环境适配 | `history_engine.py:111 STRATEGY_ENV_FIT` | `{'weak','range'}（逆势型，专为弱市设计）` |
| 每日信号上限 | `history_engine.py:37 MAX_DAILY_SIGNALS` | `默认不限` |
| 低开废弃 | `history_engine.py:134 DISCARD_LOW_OPEN` | `无` |
| 顶板废弃 | `history_engine.py:143 UNFILLABLE_ZT_OPEN` | `无` |
| 单仓资金 | `history_engine.py:529 SINGLE_CAP_POLICY` | `默认（单笔≤本金）` |

驱动链：`history_engine.build()` → `_run_stock()` → `simulate_stock()` 逐日滚动 → 有持仓时以三参 `sfunc(k, df, ctx)` 调用。
每日推荐：`advisor.scan_stock_strategy()` → `STOCK_ANALYZERS[name]`（advisor.py:368）。

## 3. 入场规则（源码阈值）

空仓时（`backtest.py:480 s10_rsi_revert`，预热 `i >= 21`）：
- **RSI 上穿 20**：`RSI14[i] > 20` 且 `RSI14[i-1] <= 20`
- 大盘过滤：上证站上 20 日线

## 4. 出场规则

持仓分支：
- **RSI 下穿 80**：`RSI14[i] < 80` 且 `RSI14[i-1] >= 80` → `sell`（次日**开盘**成交）
- **止损 5%**：`ret <= -0.05` 或 `最低[i] <= entry × 0.95` → `sell_stop`
- **无时间止损** —— 这也是它当前平均持有 54 日的直接原因

## 5. 评分与参考价

`advisor.py:300 _a_s10`：RSI ≤ 30 时 `100 - |RSI-20|×5`；30~50 时 `50-(RSI-30)×2.5`；> 50 时 `25-(RSI-50)`，最低 0。

- `buy_ref` = 当日收盘价
- `stop` = 收盘 × 0.95
- `target` = None
- `tag` = RSI上穿20 ｜ note 示例：`RSI=20.2（前值18.4）`

## 6. 实盘账本表现（2026-07-01～2026-10-08）

| 指标 | 值 |
|---|---|
| 成交笔数 | 1 |
| 净盈亏 | **-341.23** |
| 均笔 | -341.23 |
| 胜率 | 0.0% |
| 平均持有 | **54.0 日**（套牢后一直等 RSI 上穿 80，最终止损） |
| 当前 | 空仓（候选：中兵红箭，`RSI=20.2（前值18.4）`） |

> 口径：净盈亏=已卖出落袋 + 当前持仓浮盈；样本极小（个位数~15 笔），**不足以推翻长期回测，也不足以证明策略有效**。

## 7. 已知缺陷 / 研究结论

- **卖出条件过于苛刻**：要求 RSI 回到 80 才卖，弱市里几乎等不到，实际只靠 -5% 止损或长期套牢出场（当前唯一那笔吃了 54 天 -341 元）。
- 无全市场体检数据（不在 S1/S2/S4/S5 名单）。改出场（比如加时间止损或改成 RSI 回到 50 就减仓）之前，**必须先跑全市场 A/B**，不能凭 1 笔样本判断。
- `ENV_FIT` 含 `weak` 是它与其他策略的差异化定位，改动它会改变"弱市由谁出手"的分布。

## 8. 改动须知

- 改**信号阈值** → 改 `backtest.py` 的 `s10_rsi_revert`；改**评分/参考价** → 改 `advisor.py` 的 `_a_s10`。**两边逻辑必须保持一致**，否则"回测选一只、页面推另一只"。
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
cd /d D:\codebuddy_workspace && python -X utf8 -u _signal_health.py --names S10
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
