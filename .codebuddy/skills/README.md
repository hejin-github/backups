# stock_bot 策略 Skill 索引（13 个）

> 生成脚本：`c:/Users/hejin/CodeBuddy/20260819154435/_gen_strategy_skills.py`
> 数据源：`backtest.py` / `advisor.py` / `history_engine.py` 源码 + `data/strategy_daily.json`（2026-10-08 build）
> 更新方式：`python -X utf8 -u _gen_strategy_skills.py --user`（`--user` 会同时同步到 `C:/Users/hejin/.codebuddy/skills`）

| Skill 目录 | 对应策略全名 | 页面入口 | 信号函数 | 评分函数 |
|---|---|---|---|---|
| `s1-christian-breakout-pullback` | S1_Christian突破回踩 | `/s/S1_Christian突破回踩` | backtest.py:365 | advisor.py:162 |
| `s2-macd-kdj-cross` | S2_MACD金叉KDJ死叉 | `/s/S2_MACD金叉KDJ死叉` | backtest.py:397 | advisor.py:184 |
| `s3-chandelier-trend` | S3_吊灯止损趋势跟踪 | `/s/S3_吊灯止损趋势跟踪` | backtest.py:411 | advisor.py:201 |
| `s4-ma20-pullback-volume` | S4_20日线回踩放量 | `/s/S4_20日线回踩放量` | backtest.py:426 | advisor.py:218 |
| `s5-tp15-sl5` | S5_止盈15止损5 | `/s/S5_止盈15止损5` | backtest.py:449 | advisor.py:235 |
| `s6-boll-lower-band-t` | S6_布林下轨做T | `/s/S6_布林下轨做T` | backtest.py:466 | advisor.py:251 |
| `s7-hs300-pe-batch` | S7_沪深300PE分批低买高卖 | `/s/S7_沪深300PE分批低买高卖` | idx_specs(history_engine.py:1832) | — 指数策略 |
| `s8-dividend-pb-batch` | S8_红利低估值分批买入 | `/s/S8_红利低估值分批买入` | idx_specs(history_engine.py:1832) | — 指数策略 |
| `s9-trendline-pullback` | S9_趋势线突破回踩 | `/s/S9_趋势线突破回踩` | backtest.py:494 | advisor.py:266 |
| `s10-rsi-reversal` | S10_RSI超卖超买反转 | `/s/S10_RSI超卖超买反转` | backtest.py:480 | advisor.py:300 |
| `s15-chan-third-buy` | S15_缠论三买 | `/s/S15_缠论三买` | backtest.py:543 | advisor.py:314 |
| `s16-dragon-relay` | S16_龙头接力 | `/s/S16_龙头接力` | backtest.py:891 | advisor.py:358 |
| `s16-dragon-relay-v2` | S16_龙头接力V2 | `/s/S16_龙头接力V2` | backtest.py:897 | advisor.py:363 |

## 每份档案包含什么

1. 策略定位（一句话）
2. **注册位置表**：本系统没有集中式注册表，策略由 `backtest.STRATEGIES` + `advisor.STOCK_ANALYZERS` + `history_engine` 七张常量表横向拼装，表里给了行号
3. 入场规则（全部阈值）
4. 出场规则
5. 评分构成与 buy_ref / stop / target / tag
6. 实盘账本表现（2026-07-01～2026-10-08）
7. 已知缺陷 / 回测结论（含"哪些结论已被证伪"）
8. 改动须知（哪些坑、哪些不能动、验收与重启命令）
9. 常用命令

## 维护约定

- **阈值改了要重跑生成脚本**，否则档案与源码脱节；archives 里的数字（回测/账本）只代表生成那一刻。
- 落地账本数字每次重建 `strategy_daily.json` 后都可能变，跑一遍脚本即可刷新。
- 第 7 节（已知缺陷）是人工维护的结论，写在生成器脚本的 `known` 字段里，改完重跑。
- S7/S8 不在 `bt.STRATEGIES` 里（硬编码在 `idx_specs`），任何"遍历 STRATEGIES"的脚本都会漏掉 —— 这条坑在每个档案里都写了一次。
