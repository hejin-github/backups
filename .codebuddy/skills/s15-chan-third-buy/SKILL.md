---
name: s15-chan-third-buy
description: stock_bot 模拟交易策略【S15_缠论三买】的技术档案 —— 入场/出场规则与全部阈值、评分函数与参考价、买卖时点与大盘过滤、backtest/advisor/history_engine 三处注册位置与行号、8081 页面入口 /s/S15_%E7%BC%A0%E8%AE%BA%E4%B8%89%E4%B9%B0、实盘账本表现（2026-07-01～2026-10-08）、已知缺陷与回测结论、改动须知与验收命令。当需要查看、回放、调参、改代码、评估这个策略，或用户提到「S15、缠论、三买、中枢、ZG、笔、分型」时使用。
display_name: "S15_缠论三买"
version: 0.1.0
origin: custom
---

# S15_缠论三买（S15）

> 项目：`D:/codebuddy_workspace/stock_bot`（服务 `http://127.0.0.1:8081`）｜页面：`/s/S15_%E7%BC%A0%E8%AE%BA%E4%B8%89%E4%B9%B0`
> 账本：`data/strategy_daily.json`（2026-07-01～2026-10-08，单笔本金 5000，含费）
> 本档案由 `_gen_strategy_skills.py` 从源码与账本自动提取骨架，规则与阈值以源码为准。

## 1. 策略定位

自研缠论引擎（`chan_core.py`，在 `D:/codebuddy_workspace`）找第三类买点：中枢上移后向上突破，回调不破中枢上沿 ZG，在回调笔末端买入。止盈 8%、止损 5%。**这是全市场回测样本最大、历史表现最好的一个策略**。

## 2. 注册位置（改代码先看这里）

本系统**没有集中式注册表**，策略由三处横向拼接：

| 环节 | 文件:位置 | 本策略取值 |
|---|---|---|
| 信号函数（回测） | `backtest.py:543` | `s15_chan_sanbuy` |
| 评分函数（每日推荐） | `advisor.py:314` | `_a_chan` |
| 买卖时点 | `history_engine.py:92 STRATEGY_TIMING` | 买 `open` / 卖 `open` |
| 大盘过滤 | `history_engine.py:49 BUY_FILTERS` | `上证当日上涨（sh_day_up）` |
| 环境适配 | `history_engine.py:111 STRATEGY_ENV_FIT` | `{'trend_up','range'}` |
| 每日信号上限 | `history_engine.py:37 MAX_DAILY_SIGNALS` | `默认不限` |
| 低开废弃 | `history_engine.py:134 DISCARD_LOW_OPEN` | `无` |
| 顶板废弃 | `history_engine.py:143 UNFILLABLE_ZT_OPEN` | `无` |
| 单仓资金 | `history_engine.py:529 SINGLE_CAP_POLICY` | `默认（单笔≤本金）` |

驱动链：`history_engine.build()` → `_run_stock()` → `simulate_stock()` 逐日滚动 → 有持仓时以三参 `sfunc(k, df, ctx)` 调用。
每日推荐：`advisor.scan_stock_strategy()` → `STOCK_ANALYZERS[name]`（advisor.py:368）。

## 3. 入场规则（源码阈值）

由 `chan_core.py` 计算，经 `_chan_signals(df, {'三买'})`（结果按 `df.attrs['_code']` 缓存）给出信号：

- 底层：K线包含处理 → 分型 → 笔 → 中枢 → 判定三买（中枢上移、向上突破后回调不破 ZG）
- **无前视必须的 lag = 2**：信号 `orig_idx` 需要 `orig_idx+1` 日**收盘确认**，买入在 **`orig_idx+2` 日开盘**
- 引擎里的买入由评分侧驱动（`_a_chan` 在 `i-1` 是信号日触发）；`backtest.py:543` 的 `sfunc` 分支仅供 `run_stock_strategy` 独立回测使用
- 大盘过滤：上证当日上涨

## 4. 出场规则

- **止盈 +8%**：`ret >= 0.08` → `sell`
- **止损 -5%**：`ret <= -0.05` 或 `最低[i] <= entry × 0.95` → `sell_stop`
- 无时间止损（平均持有约 8 日）

## 5. 评分与参考价

`advisor.py:314 _a_chan`：固定 **score = 80**（缠论信号本身不给分，命中即 80）。

- `buy_ref` = 信号价（三买位置）
- `stop` = 信号价 × 0.95
- `target` = 信号价 × 1.08
- `tag` = 缠论三买 ｜ note 示例：`缠论三买：突破中枢后回调不破ZG（信号价14.20，+8%止盈/-5%止损）`

## 6. 实盘账本表现（2026-07-01～2026-10-08）

| 指标 | 值 |
|---|---|
| 成交笔数 | 8 |
| 净盈亏 | **+174.22** |
| 均笔 | +21.78 |
| 胜率 | 37.5% |
| 平均持有 | 8.1 日 |
| 当前 | 持有 特  力Ａ(000025) 成本 14.80 / 现价 15.42 / **+4.2%** |

注：名字含空格的股票（如「特  力Ａ」）在换仓 note 解析时容易被截断，换仓展示曾有专门的 `_SW_BUY_RE` 正则修复（`app.py`）。

> 口径：净盈亏=已卖出落袋 + 当前持仓浮盈；样本极小（个位数~15 笔），**不足以推翻长期回测，也不足以证明策略有效**。

## 7. 已知缺陷 / 研究结论

- **全市场样本外（2023-01-01~2026-08-28，2601 只）40,967 笔 净 +2,530,965、胜率 44.5%**（止盈 8% / 止损 5%，每笔 5000 含费）；`tp12/sl6` +1,842,096、`tp8/sl4` +1,496,665，参数邻域稳健。
- **不要加「上证 > MA20 可做窗口」闸门**：`s15_chan_sanbuy_gtma20` 变体只有 26,387 笔 +1,690,399（**-33.2%**），而被滤掉的 14,873 笔窗口外交易贡献 +838,337 —— 三买在**弱市回调期反而是主要利润来源**。对照脚本 `_s15_ab.py`，结果在 `/s15ab` 页面。
- 口径必须 lag=2，「信号当天买入」会产生前视偏差。
- 落地小样本（8 笔 +174）远低于回测强度属正常（回测是逐票独立、引擎是每日全市场只选 1 只），**不要用落地 8 笔去否定 4 万笔的回测**。

## 8. 改动须知

- 改**信号阈值** → 改 `backtest.py` 的 `s15_chan_sanbuy`；改**评分/参考价** → 改 `advisor.py` 的 `_a_chan`。**两边逻辑必须保持一致**，否则"回测选一只、页面推另一只"。
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
cd /d D:\codebuddy_workspace && python -X utf8 -u _signal_health.py --names S15
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
