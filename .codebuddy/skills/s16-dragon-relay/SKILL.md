---
name: s16-dragon-relay
description: stock_bot 模拟交易策略【S16_龙头接力】的技术档案 —— 入场/出场规则与全部阈值、评分函数与参考价、买卖时点与大盘过滤、backtest/advisor/history_engine 三处注册位置与行号、8081 页面入口 /s/S16_%E9%BE%99%E5%A4%B4%E6%8E%A5%E5%8A%9B、实盘账本表现（2026-07-01～2026-10-08）、已知缺陷与回测结论、改动须知与验收命令。当需要查看、回放、调参、改代码、评估这个策略，或用户提到「S16、龙头接力、连板、空间龙、打板、涨停池最强龙头」时使用。
display_name: "S16_龙头接力"
version: 0.1.0
origin: custom
---

# S16_龙头接力（S16）

> 项目：`D:/codebuddy_workspace/stock_bot`（服务 `http://127.0.0.1:8081`）｜页面：`/s/S16_%E9%BE%99%E5%A4%B4%E6%8E%A5%E5%8A%9B`
> 账本：`data/strategy_daily.json`（2026-07-01～2026-10-08，单笔本金 5000，含费）
> 本档案由 `_gen_strategy_skills.py` 从源码与账本自动提取骨架，规则与阈值以源码为准。

## 1. 策略定位

全市场涨停池里选最强龙头接力：每天只推 1 只（连板数最高、量比次之），T 日收盘出信号，T+1 **收盘**以涨停价/收盘价打板买入（须当日封板且非一字/秒板），断板或浮亏 -8% 走。**它与「空间龙盯盘」同源，但选股口径不同**（见第 7 节）。

## 2. 注册位置（改代码先看这里）

本系统**没有集中式注册表**，策略由三处横向拼接：

| 环节 | 文件:位置 | 本策略取值 |
|---|---|---|
| 信号函数（回测） | `backtest.py:891` | `s16_leader_relay → _s16_relay_impl(..., (1, 99))` |
| 评分函数（每日推荐） | `advisor.py:358` | `_a_s16 → _a_s16_impl(i, df, 1)` |
| 买卖时点 | `history_engine.py:92 STRATEGY_TIMING` | 买 `close` / 卖 `close` |
| 大盘过滤 | `history_engine.py:49 BUY_FILTERS` | `无` |
| 环境适配 | `history_engine.py:111 STRATEGY_ENV_FIT` | `{'trend_up','range'}` |
| 每日信号上限 | `history_engine.py:37 MAX_DAILY_SIGNALS` | `每天全市场**只推 1 只**（连板最高、量比次之）` |
| 低开废弃 | `history_engine.py:134 DISCARD_LOW_OPEN` | `-0.005（低开 > 0.5% 废弃）` |
| 顶板废弃 | `history_engine.py:143 UNFILLABLE_ZT_OPEN` | `0.098（顶板 ≥ 9.8% 买不进废弃）` |
| 单仓资金 | `history_engine.py:529 SINGLE_CAP_POLICY` | `half-cap（单笔 ≤ max(本金, 账户/2)）` |

驱动链：`history_engine.build()` → `_run_stock()` → `simulate_stock()` 逐日滚动 → 有持仓时以三参 `sfunc(k, df, ctx)` 调用。
每日推荐：`advisor.scan_stock_strategy()` → `STOCK_ANALYZERS[name]`（advisor.py:368）。

## 3. 入场规则（源码阈值）

信号来自连板底表 `_s16_leaders`（`backtest.py`）：

- **base 板位下限 = 1**（`_s16_relay_impl(..., (1,99))`，`backtest.py:891`），即每日全市场涨停池里**连板最高**的那只（V2 才要求 ≥3）
- 评分侧另一处硬编码在 `advisor.py:358 _a_s16 → _a_s16_impl(i, df, min_lb=1)`
- 判定方式：`leaders.get(日期)`，若当日最强龙头不是本股则 `return None`
- **执行口径（打板）**：T 日收盘出具信号 → T+1 **收盘**执行，用 `_s16_daban_pick`（`history_engine.py:615`）校验 T+1 当日**封板且非一字/秒板**，成交价取当日收盘；候选取 `_s16_cand_pool`（`history_engine.py:591`，调 `bt._s16_space_pool`）
- 挂靠方式不是注册而是**前缀判断**：`str(sname).startswith('S16')`（`history_engine.py:767` 执行日校验、`:1396` 信号日带候选池进挂单）
- 买不进判定：`_s16_is_unbuyable_limit`（一字/秒板）；换手 ≥ 3% 的一字板视为**排得到**
- **顺延**：龙一买不进时按名次顺延次强（`S16_DABAN_POOL_TOPN=5`）

## 4. 出场规则

- **断板卖出**：持有期内当日不再涨停 → **断板当日收盘**成交（`S16_SELL_SAME_CLOSE=True`，2026-09-23 上线的出场 A/B 结论）
- **止损 -8%**：浮亏 -8% 出场（另有 `S16_DABAN_STOP_PCT` 可调）
- 平均持有约 2.7 日

## 5. 评分与参考价

`advisor.py:331 _a_s16_impl`：命中即 **score = 95**（连板最强即高分，不做连续打分）。

- `buy_ref` = **前一交易日收盘**（注意不是当日收盘，接的是 T+1 的打板价基准）
- `stop` = 前收 × 0.92
- `target` = 前收 × 1.20
- `tag` = 龙头接力 ｜ note 示例：`龙头接力：全市场涨停池最强龙头（5板，量比1.23）：断板/浮亏8%走`

## 6. 实盘账本表现（2026-07-01～2026-10-08）

| 指标 | 值 |
|---|---|
| 成交笔数 | 15 |
| 净盈亏 | **+4,041.37** |
| 均笔 | **+269.42** |
| 胜率 | **53.3%** |
| 平均持有 | 2.7 日 |
| 当前 | 2026-10-08 卖出成交：深物业A @12.12（9/30 入场，8 日），净 -60.52（断板当日收盘卖） |

**落地表现最好的一档**（另一个 V2 更好）。

> 口径：净盈亏=已卖出落袋 + 当前持仓浮盈；样本极小（个位数~15 笔），**不足以推翻长期回测，也不足以证明策略有效**。

## 7. 已知缺陷 / 研究结论

- **面板现行打板口径 = `daban`（T+1 收盘打板买）**，不是回测最优的 `same_day`：原因只剩实操——`build` 在收盘后才跑，用户看到 `same_day` 信号时已无法下单。核心常量在 `history_engine.py`：`S16_DABAN_ENABLE` / `S16_DABAN_POOL_TOPN=5` / `S16_DABAN_SKIP_LIMIT_OPEN=True` / `S16_SELL_SAME_CLOSE=True`。
- **回测（全市场 2614 只，2019 起，每笔 5000）**：daban/topn5/跳秒板 **779 笔 +154,650、均笔 198.5、胜率 50.6%，逐年 9 年全部为正**；topn1 版 264 笔 +74,673、均笔 282.9、胜率 56.1%（单笔质量更高）；`same_day/topn5` 1,195 笔 +200,329 但不可执行。
- **改板位下限必须两处同步改**：`backtest.py:897` 的 `(3,99)` 和 `advisor.py:363` 的 `min_lb=3`（V2 的两个数字）；否则「推荐一只、模拟买另一只」。
- **静默陷阱**：新 `lb_range` 必须在白名单 `_S16_LB_RANGES`（`backtest.py:786`）内，否则 leaders 为空 dict → 永远无信号**且不报错**。
- **S16 专属守卫按 `sf.__name__` 是否含 's16' 分支**：写对照脚本时策略名必须带 `S16_` 前缀，否则会静默退化成"开盘必买"的幻影基线（曾出现 881 笔 +170,008 这种假数）。
- `_s16_space_pool` 的 key 是 `pd.Timestamp`，而引擎日历 `d` 是字符串 → `pool.get(d)` 恒 None、候选池静默退化成"只买首选"且**无任何报错**（表现为笔数偏少）。
- 已验证：**降级到「降低板位=更早」是错误的**（首板 1,296 笔 -55,861、1~2 板 -108,971、而 ≥3 板 +4,924），板位越高越好。

## 8. 改动须知

- 改**信号阈值** → 改 `backtest.py` 的 `s16_leader_relay → _s16_relay_impl(..., (1, 99))`；改**评分/参考价** → 改 `advisor.py` 的 `_a_s16 → _a_s16_impl(i, df, 1)`。**两边逻辑必须保持一致**，否则"回测选一只、页面推另一只"。
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
cd /d D:\codebuddy_workspace && python -X utf8 -u _signal_health.py --names S16
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
