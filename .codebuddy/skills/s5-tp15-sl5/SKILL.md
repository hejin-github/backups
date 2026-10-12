---
name: s5-tp15-sl5
description: stock_bot 模拟交易策略【S5_止盈15止损5】的技术档案 —— 入场/出场规则与全部阈值、评分函数与参考价、买卖时点与大盘过滤、backtest/advisor/history_engine 三处注册位置与行号、8081 页面入口 /s/S5_%E6%AD%A2%E7%9B%8815%E6%AD%A2%E6%8D%9F5、实盘账本表现（2026-07-01～2026-10-08）、已知缺陷与回测结论、改动须知与验收命令。当需要查看、回放、调参、改代码、评估这个策略，或用户提到「S5、止盈15止损5、20日新高、MA20向上」时使用。
display_name: "S5_止盈15止损5"
version: 0.1.0
origin: custom
---

# S5_止盈15止损5（S5）

> 项目：`D:/codebuddy_workspace/stock_bot`（服务 `http://127.0.0.1:8081`）｜页面：`/s/S5_%E6%AD%A2%E7%9B%8815%E6%AD%A2%E6%8D%9F5`
> 账本：`data/strategy_daily.json`（2026-07-01～2026-10-08，单笔本金 5000，含费）
> 本档案由 `_gen_strategy_skills.py` 从源码与账本自动提取骨架，规则与阈值以源码为准。

## 1. 策略定位

均线多头 + 创新高突破入场；盈利 15% 后回落到 10% 才止盈（让利润跑），亏损 5% 止损。名字就是它的出场参数。

## 2. 注册位置（改代码先看这里）

本系统**没有集中式注册表**，策略由三处横向拼接：

| 环节 | 文件:位置 | 本策略取值 |
|---|---|---|
| 信号函数（回测） | `backtest.py:449` | `s5_tp15_sl5` |
| 评分函数（每日推荐） | `advisor.py:235` | `_a_s5` |
| 买卖时点 | `history_engine.py:92 STRATEGY_TIMING` | 买 `close` / 卖 `close` |
| 大盘过滤 | `history_engine.py:49 BUY_FILTERS` | `选股池站上20日线（pi_gt_ma20）` |
| 环境适配 | `history_engine.py:111 STRATEGY_ENV_FIT` | `{'trend_up','range'}` |
| 每日信号上限 | `history_engine.py:37 MAX_DAILY_SIGNALS` | `默认不限` |
| 低开废弃 | `history_engine.py:134 DISCARD_LOW_OPEN` | `无` |
| 顶板废弃 | `history_engine.py:143 UNFILLABLE_ZT_OPEN` | `无` |
| 单仓资金 | `history_engine.py:529 SINGLE_CAP_POLICY` | `默认（单笔≤本金）` |

驱动链：`history_engine.build()` → `_run_stock()` → `simulate_stock()` 逐日滚动 → 有持仓时以三参 `sfunc(k, df, ctx)` 调用。
每日推荐：`advisor.scan_stock_strategy()` → `STOCK_ANALYZERS[name]`（advisor.py:368）。

## 3. 入场规则（源码阈值）

空仓时（`backtest.py:449 s5_tp15_sl5`，预热 `i >= 26`）：
- `收盘 > MA20`
- **MA20 向上**：`MA20[i] > MA20[i-5]`
- **突破**：`收盘[i] > max(收盘[i-20 .. i-1])`（近 20 日**收盘**新高，注意与 S1 用「最高」不同）
- 大盘过滤：**选股池站上 20 日线**（`pi_gt_ma20`，看的是选股池整体而非上证指数本身）

## 4. 出场规则

持仓分支：
- **止盈**：持有期最高 / 成本 - 1 ≥ **15%** 且当日 `ret ≤ 10%` → `sell`
- **止损 5%**：`ret <= -0.05` 或 `最低[i] <= entry × 0.95` → `sell_stop`（次日开盘离场）
- 无时间止损（这是它被诟病"平均占仓 47 日"的原因）

## 5. 评分与参考价

`advisor.py:235 _a_s5`：MA20 向上 +30；收盘站上 MA20 +30；创 20 日新高 +40（未创新高按距离 /0.05 衰减）。

- `buy_ref` = 近 20 日收盘最高价
- `stop` = 收盘 × 0.95
- `target` = None
- `tag` = 突破20日新高

## 6. 实盘账本表现（2026-07-01～2026-10-08）

| 指标 | 值 |
|---|---|
| 成交笔数 | 4 |
| 净盈亏 | **+167.71** |
| 均笔 | +41.93 |
| 胜率 | 50.0% |
| 平均持有 | 11.5 日 |
| 当前 | 持有 云南白药(000538) 成本 51.30 / 现价 50.89 / **-0.8%** |

> 口径：净盈亏=已卖出落袋 + 当前持仓浮盈；样本极小（个位数~15 笔），**不足以推翻长期回测，也不足以证明策略有效**。

## 7. 已知缺陷 / 研究结论

- **全市场（2019 起，每笔 5000 含费）base 4,665 笔 净 -513,601、均笔 -110.1、胜率 26.6%、平均持有 47 日、回撤 -53.0%** —— 八年全亏，`f1~f20` 前向收益只有 +0.14%~+0.63%，接近随机买入。
- 移动止盈改造（峰值回撤 10%）把它救到 ≈ 持平：6,686 笔 -12,955、回撤 -17.9%、持有 28 日；436 只分片 2023 起：-582,335 → -130,873。**治的是回撤，不是负期望**。
- 60 日最高浮盈均值 +19.16% 说明它确实能抓到波段，但固定止盈/不止损时间让它把浮盈走完又吐回去。
- 结论：**若要动 S5，优先改出场（移动止盈 + 时间止损），不要改入场**；入场无 edge。

## 8. 改动须知

- 改**信号阈值** → 改 `backtest.py` 的 `s5_tp15_sl5`；改**评分/参考价** → 改 `advisor.py` 的 `_a_s5`。**两边逻辑必须保持一致**，否则"回测选一只、页面推另一只"。
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
cd /d D:\codebuddy_workspace && python -X utf8 -u _signal_health.py --names S5
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
