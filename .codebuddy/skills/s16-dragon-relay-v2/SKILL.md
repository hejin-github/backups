---
name: s16-dragon-relay-v2
description: stock_bot 模拟交易策略【S16_龙头接力V2】的技术档案 —— 入场/出场规则与全部阈值、评分函数与参考价、买卖时点与大盘过滤、backtest/advisor/history_engine 三处注册位置与行号、8081 页面入口 /s/S16_%E9%BE%99%E5%A4%B4%E6%8E%A5%E5%8A%9BV2、实盘账本表现（2026-07-01～2026-10-08）、已知缺陷与回测结论、改动须知与验收命令。当需要查看、回放、调参、改代码、评估这个策略，或用户提到「S16V2、龙头接力V2、连板≥3、空间龙」时使用。
display_name: "S16_龙头接力V2"
version: 0.1.0
origin: custom
---

# S16_龙头接力V2（S16V2）

> 项目：`D:/codebuddy_workspace/stock_bot`（服务 `http://127.0.0.1:8081`）｜页面：`/s/S16_%E9%BE%99%E5%A4%B4%E6%8E%A5%E5%8A%9BV2`
> 账本：`data/strategy_daily.json`（2026-07-01～2026-10-08，单笔本金 5000，含费）
> 本档案由 `_gen_strategy_skills.py` 从源码与账本自动提取骨架，规则与阈值以源码为准。

## 1. 策略定位

与 S16 base 完全同构，唯一差别是**龙头必须连板 ≥ 3 板**。这 1 个参数的差异让它成为落地净盈亏最高的一档（+5,669 / 胜率 60%）。

## 2. 注册位置（改代码先看这里）

本系统**没有集中式注册表**，策略由三处横向拼接：

| 环节 | 文件:位置 | 本策略取值 |
|---|---|---|
| 信号函数（回测） | `backtest.py:897` | `s16_leader_relay_v2 → _s16_relay_impl(..., (3, 99))` |
| 评分函数（每日推荐） | `advisor.py:363` | `_a_s16_v2 → _a_s16_impl(i, df, 3)` |
| 买卖时点 | `history_engine.py:92 STRATEGY_TIMING` | 买 `close` / 卖 `close` |
| 大盘过滤 | `history_engine.py:49 BUY_FILTERS` | `无` |
| 环境适配 | `history_engine.py:111 STRATEGY_ENV_FIT` | `{'trend_up','range'}` |
| 每日信号上限 | `history_engine.py:37 MAX_DAILY_SIGNALS` | `每天全市场**只推 1 只**（连板最高、量比次之，且连板 ≥ 3）` |
| 低开废弃 | `history_engine.py:134 DISCARD_LOW_OPEN` | `-0.005（低开 > 0.5% 废弃）` |
| 顶板废弃 | `history_engine.py:143 UNFILLABLE_ZT_OPEN` | `0.098（顶板 ≥ 9.8% 买不进废弃）` |
| 单仓资金 | `history_engine.py:529 SINGLE_CAP_POLICY` | `half-cap（单笔 ≤ max(本金, 账户/2)）` |

驱动链：`history_engine.build()` → `_run_stock()` → `simulate_stock()` 逐日滚动 → 有持仓时以三参 `sfunc(k, df, ctx)` 调用。
每日推荐：`advisor.scan_stock_strategy()` → `STOCK_ANALYZERS[name]`（advisor.py:368）。

## 3. 入场规则（源码阈值）

与 `S16_龙头接力` 共用 `_s16_relay_impl` / `_a_s16_impl`，差别只在板位下限：

- **`backtest.py:897`**：`s16_leader_relay_v2` → `_s16_relay_impl(i, df, pos, (3, 99))`
- **`advisor.py:363`**：`_a_s16_v2` → `_a_s16_impl(i, df, 3)`
- 其余（连板最高优先、量比次之、每日 1 只、T+1 收盘打板执行、顺延、非一字放行）与 base 完全一致

> ⚠ 这两处是两套独立硬编码，注册时都不传参。改板位门槛必须**同时**改这两个数字。

## 4. 出场规则

与 base 一致：**断板当日收盘卖出** / 浮亏 -8% 止损，平均持有 2.8 日。

## 5. 评分与参考价

与 base 一致（`_a_s16_impl`）：命中即 **score = 95**，`buy_ref` = 前收，`stop` = 前收×0.92，`target` = 前收×1.20。

唯一差别是 note 文案：`龙头接力V2：全市场涨停池最强龙头(连板≥3)（5板，量比1.23）：断板/浮亏8%走`。

## 6. 实盘账本表现（2026-07-01～2026-10-08）

| 指标 | 值 |
|---|---|
| 成交笔数 | 15 |
| 净盈亏 | **+5,669.63** |
| 均笔 | **+377.98** |
| 胜率 | **60.0%** |
| 平均持有 | 2.8 日 |
| 当前 | 2026-10-08 卖出成交：深物业A @12.12（9/30 入场，8 日），净 -60.52（断板当日收盘卖） |

**13 个策略里落地表现最好的一个**。

> 口径：净盈亏=已卖出落袋 + 当前持仓浮盈；样本极小（个位数~15 笔），**不足以推翻长期回测，也不足以证明策略有效**。

## 7. 已知缺陷 / 研究结论

- 为什么 V2 优于 base：全市场扫描证明「板位越高越好」，只做首板 (1,1) 1,296 笔 **-55,861**、1~2 板 -108,971、只做 2 板 -105,617，而 ≥3 板 472 笔 **+4,924 / 41.5%**。直觉上"买晚了"其实恰恰相反。
- V2 与「空间龙盯盘」**同源但选股独立**：V2 是 T 日收盘扫描、门槛 ≥3 板、每天只取最强 1 只；空间龙是 T+1 盘中实时、只要今日封板就入池（含首板）、按 `(连板降序, 换手降序)` 取 TOP5。**所以它结构上看不见"今天才长到 3 板"的机会**（如 603949 雪龙集团 9/23 首板、9/24 才 2 板）。
- 空间龙盯盘（`/watch`）**没有独立账本**，只推送决策：首页、钉钉页、`strategy_daily.json` 里都查不到它 —— 复盘「明日行动」已改为同时读 `data/signals.db`，才补得上这类"有决策无账本"的策略。
- 同样的两个坑：改板位要同步两处；写回测脚本必须带 `S16_` 前缀否则触发幻影基线。

## 8. 改动须知

- 改**信号阈值** → 改 `backtest.py` 的 `s16_leader_relay_v2 → _s16_relay_impl(..., (3, 99))`；改**评分/参考价** → 改 `advisor.py` 的 `_a_s16_v2 → _a_s16_impl(i, df, 3)`。**两边逻辑必须保持一致**，否则"回测选一只、页面推另一只"。
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
cd /d D:\codebuddy_workspace && python -X utf8 -u _signal_health.py --names S16V2
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
