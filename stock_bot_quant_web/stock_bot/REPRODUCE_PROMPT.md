# 复刻提示词：A股多策略模拟回测与复盘系统

> 用法：把本文档完整内容作为提示词输入给 AI 编程助手（可另附 `data/csv/` 数据目录或按第 3 节自动拉取），即可从零复刻本工程。
> 工程现状基准：2026-08-25。所有数值/配置均来自实际运行中的工程。

---

## 1. 一句话需求

纯本地的 A 股投资辅助系统：以 60 只沪深主板股票为选股池，8 种选股策略 + 2 种指数估值策略，每个策略用 5000 元本金从 2019-01-01 起**逐日模拟交易**（含费用、T+1、停买规则），输出：每日推荐信号、历史成交明细、策略/全账户收益率与回撤、选股池指数与动态选股池指数、当前行情判断与明日行情预判，全部通过本地浏览器（localhost:8080）访问，无数据库、无前端框架、无第三方 Web 框架。

## 2. 技术栈与整体约束

- Python 3 + pandas + numpy；数据获取用 **akshare**（股票日线）；Web 服务用标准库 **http.server（ThreadingHTTPServer）**，页面由 Python 直接渲染 HTML 字符串（SSR），图表用内联 SVG。
- 无数据库：一切持久化到 JSON / CSV（`data/` 目录）。
- 单机本地运行；Windows 环境（注意 GBK 控制台，脚本顶部用 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')` 或 `python -X utf8` 启动）。
- **红线：任何环节都不得使用未来数据**。信号在收盘后确定，成交时点由策略级配置决定（次日开盘价或次日收盘价），指数/过滤器不得提前泄露未来值。

## 3. 工程目录与文件职责（必须照此结构）

```
<工作区根>/                          # 例如 D:/codebuddy_workspace
├─ backtest.py                       # 回测引擎：股票池 STOCKS、费用、指标、逐日回测
├─ stock_bot/                        # Python 包
│  ├─ __init__.py
│  ├─ advisor.py                     # 推荐引擎：8个个股策略信号、指数策略、扫描线程、实时行情
│  ├─ history_engine.py              # 后台引擎：5000元/策略逐日模拟、指数合成、动态池A/B、行情判断/明日预判
│  ├─ app.py                         # Web 服务：页面渲染 + REST API
│  ├─ PROJECT_CONTEXT.md             # 工程演进记录（复刻后可重建）
│  └─ start.bat                      # 一键启动（后台运行 app.py）
└─ data/
   ├─ csv/                           # 60 只个股 + 上证指数 + 指数估值
   │   ├─ {股票代码}.csv             # 个股 12 列日线
   │   ├─ sh000001.csv               # 上证指数日线（过滤器必需）
   │   ├─ hs300_pe.csv               # 沪深300 PE 估值（S7 用）
   │   └─ hsly_pb.csv                # 红利低波 PB 估值（S8 用）
   ├─ strategy_daily.json            # history_engine 输出（10 策略 daily+trades+summary）
   ├─ pool_index.json                # 固定选股池指数（链式市值加权）
   ├─ dynamic_pool_index.json        # 动态选股池指数 + 调仓记录
   ├─ dynamic_pool_bt.json           # 动态池 A/B 落地验证记录
   └─ *.log                          # 运行日志
```

- 路径约定：`_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`（在 `stock_bot/*.py` 中计算工作区根），`sys.path.insert(0, _WORKSPACE)` 后 `import backtest`（模块在根目录）、`from stock_bot import advisor`。数据路径一律 `os.path.join(_WORKSPACE, 'data/...')`，服务启动时 `os.chdir(_WORKSPACE)`。

## 4. 数据规范

### 4.1 个股日线 `data/csv/{code}.csv`（60 只，12 列，utf-8-sig）
列名顺序：`日期,股票代码,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率`
- 单位：`成交量` 为**手**；`换手率` 为**百分数**（如 0.2 表示 0.2%）；`涨跌幅`/`涨跌额`/`振幅` 为百分数数值。
- 时间范围 2018-01-01 起，**前复权**，到最新交易日。
- 获取方式：`akshare.stock_zh_a_daily(symbol=('sh'+code 或 'sz'+code 按代码前缀), start_date='20180101', adjust='qfq')`，返回列含 date/open/high/low/close/volume(股)/amount/turnover(小数)。转换：成交量股÷100→手；换手率×100→百分数；日期格式化为 `YYYY-MM-DD`。
- 盘中自动更新用腾讯行情接口 `stock_zh_a_spot_em` 或新浪；离线可用。

### 4.2 上证指数 `data/csv/sh000001.csv`（6 列，utf-8-sig）
列：`date,open,high,low,close,volume`；1990-12-19 起全量。历史固定不重拉；`refresh_sh_index()` 检测文件末日 < 截止日时，用腾讯接口 `CN_MarketDataService.getKLineData`（scale=240，按自然日+5 求 datalen，上限 1023 条）拉最近缺失区间写回。**截止日规则**：已收盘（交易日 >=15:00）→ 今日收盘；否则 → 上一交易日，避免写入盘中半成品。文件缺失/网络不可用时过滤器放行（不阻塞回放）。

### 4.3 股票池（STOCKS，约 60 只，覆盖约 23 个板块；无创业板 300/301/302、科创板 688/689、北交所 4/8、ST、退市）

> 具体标的是项目内部人工精选，复刻时**无需照抄清单**，按下列板块分布自行挑选同等数量、同板块的沪深主板代表股即可（每板块 1~6 只龙头/代表股，合计约 60 只）。

- 板块分布（约 19 大类、细分约 23 个）：银行、保险、券商、白酒/消费、家电、医药、新能源/光伏、汽车、石油/煤炭/有色、钢铁/化工/建材、建筑/基建、地产、电力/公用、交运、通信/科技、电子/半导体、军工、机械/高端制造、农业。
- 挑选原则：仅沪深主板；每板块选 1~6 只市值大、流动性好的龙头/代表股；若某些代码已退市/改名，用同类龙头替换并在 `STOCKS` 注释说明即可。
- 股票池扩展后 `data/csv/` 新增个股会自动纳入动态池候选。

## 5. 回测引擎 `backtest.py`

- 费用常量：佣金 `COMMISSION_RATE=0.00025`（最低 `COMMISSION_MIN=5.0`）；印花税卖出 `STAMP_TAX_BEFORE=0.001`（2023-08-28 前）/`STAMP_TAX_AFTER=0.0005`（后，`STAMP_CUTOFF=2023-08-28`）；过户费 `TRANSFER_RATE=0.00001` 双向。
- `buy_cost(price,shares)` / `sell_net(price,shares,date)` 计算含费金额。
- `add_indicators(df)` 计算：MA5/10/20/60、EMA10/20、MACD(12,26,9)（dif/dea/macd_hist）、KDJ(9,3,3)（k/d）、BOLL(20,2)（boll_mid/up/low）、ATR(14)（atr14）、RSI(14)（rsi14）、量能 vol_ma5。
- `run_stock_strategy(df, code, name, strategy, max_cash=5000)`：信号收盘判定 → T+1 开盘价成交、整手（100 股倍数）、金额 ≤ max_cash；持有期扫描卖出信号；返回 trades（含买卖日期/价格/盈亏）。
- 通用策略函数签名 `strategy(t, df, pos) -> 'buy'/'sell'/None`，pos 含 peak_close/ret/ret_from_peak/hold_days。

## 6. 推荐引擎 `stock_bot/advisor.py`

- 8 个个股策略信号函数 `_a_s1(i, df) ... _a_s6, _a_s9, _a_s10`，每个返回 dict：`triggered, score(0-100), price, buy_ref, stop, target, note, tag`。触发条件（评分用于每日多策略择优/同策略排序）：
  - **S1 Christian突破回踩**：60 日涨幅 30%~100% 且收盘突破前 20 日最高；止损 收盘-1.5×ATR；目标 EMA20；`if i<61 return None`。
  - **S2 MACD金叉KDJ死叉**：DIF 上穿 DEA 买；KDJ 死叉或亏 5% 卖；止损买价×0.95。
  - **S3 吊灯止损趋势跟踪**：站上 MA20 且突破前日最高；止损 收盘-3×ATR；目标 MA10。
  - **S4 20日线回踩放量**：收盘偏离 MA20≤2% + 阳线 + 量>1.5×vol_ma5；止损 MA20。
  - **S5 止盈15止损5**：MA20 向上（今日>5日前）+ 站上 MA20 + 突破前 20 日收盘新高；止损买价×0.95。
  - **S6 布林下轨做T**：收盘<布林下轨买、中轨卖；止损买价×0.95。
  - **S9 趋势线突破回踩**：MA60 向上 + 收盘>MA60 + 偏离 MA20≤2%，且（创前 20 日最高 或 近 40 日内曾创新高+近期回撤≥5%+昨日收≤MA20 后今日回踩 MA20 企稳再启动）；止损买价×0.95；目标 MA10。
  - **S10 RSI超卖超买反转**：RSI14 上穿 20 买、下穿 80 卖；止损买价×0.95。
- 指数策略（`_a_s7/_a_s8`）：S7 沪深300 PE 分批低买高卖（用 hs300_pe.csv，低 PE 分位分批买入、高 PE 分批卖出）；S8 红利低估值分批买入（用 hsly_pb.csv）。
- 实时行情：`fetch_realtime(symbols)` 用腾讯行情接口 `https://qt.gtimg.cn/q=sh{六位代码},sz{六位代码}`（返回 GBK 分号行，字段 1 名称、3 现价、4 昨收等），失败降级。
- `do_scan(force)`：盘中每 5 分钟扫描（仅交易时段 09:30-11:30/13:00-15:00），输出今日推荐（各策略评分最高触发信号 + 沪深300PE 提示）；收盘后（>=15:00）或 `force` 时：增量补选股池/动态池指数、把"昨日信号→今日 T+1 结果"写入 `strategy_daily.json` 当日行、刷新推荐。
- `ensure_history()`：若 `strategy_daily.json` 缺失或版本旧，调用 `history_engine.build()` 重建（带 `_build()` 后台线程）。

## 7. 后台引擎 `stock_bot/history_engine.py`（核心）

### 7.1 常量与可调参数区
```python
START = pd.Timestamp('2019-01-01')
CAPITAL = 5000.0
INTEREST_RATE = 0.02        # 空仓资金按货基年化 2% 计理财收益，INTEREST_DAILY=RATE/365
OUT = 'data/strategy_daily.json'
MAX_DAILY_SIGNALS = {'S9_趋势线突破回踩': 1}   # 每策略每日最多信号数，0=停买
MIN_BATCH = 100.0           # 指数策略单批最小投入（防 0.02 元微批次）
```
- `STRATEGY_TIMING`（策略级成交时点，'open'=次日开盘，'close'=次日收盘）：
  S1(close,close) 尾盘买/尾盘卖；S2(open,close)；S3(open,close)；S4(open,close)；S5(close,close)；S6(open,open) 做T；S9(open,open)；S10(close,open) 尾盘买/早盘卖；S7(open,close)；S8(open,close)。
  `TIMING_LABEL` 映射中文标签。
- `BUY_FILTERS`（买入过滤器，信号日收盘判定后再按指数条件过滤；None=不过滤）：
```python
BUY_FILTERS = {
    'S1_Christian突破回踩': None,             # 样本过小
    'S2_MACD金叉KDJ死叉': ('pi_day_up', '选股池当日上涨'),
    'S3_吊灯止损趋势跟踪': None,
    'S4_20日线回踩放量': ('pi_day_up', '选股池当日上涨'),
    'S5_止盈15止损5': None,
    'S6_布林下轨做T': ('sh_day_up', '上证当日上涨'),
    'S9_趋势线突破回踩': None,
    'S10_RSI超卖超买反转': None,
    'S7_沪深300PE分批低买高卖': ('sh_day_up', '上证当日上涨'),
    'S8_红利低估值分批买入': None,
}
```
  过滤器类型：`'pi_day_up'`=选股池当日上涨、`'sh_day_up'`=上证当日上涨、`'pi_gt_ma10'/'pi_gt_ma20'`=选股池>MA10/MA20、`'sh_gt_ma20'`=上证>MA20、`'dp_day_up'`=动态池当日上涨、`'dp_gt_ma20'`。过滤器仅用于买入/加仓，卖出/减仓不受影响。`_load_filter_series()` 加载序列（sh 来自 sh000001.csv、pi 来自 pool_index.json、dp 来自 dynamic_pool_index.json），`_filter_pass(sname, d)` 判定。文件缺失时放行。

### 7.2 逐日模拟
- `simulate_stock(...)`：单股票单策略逐日扫描，遵循 STRATEGY_TIMING 成交时点、MAX_DAILY_SIGNALS 信号上限、BUY_FILTERS 过滤；产出 daily 记录（日期/标的/评分/触发/买卖点/持仓状态/T+1涨跌幅/备注）。
- `simulate_index(...)`：S7/S8 指数策略按估值分位分批，MIN_BATCH 防微批次。
- `build()` 全流程：① `refresh_sh_index()` 补齐上证指数 → ② `build_pool_index()` 增量补固定池指数 → ③ 各策略 simulate 全池 → ④ 动态池：`build_dynamic_pool_index()`（指数+成分）→ `backtest_dynamic_pool()`（dp_day_up 过滤 A/B 对比，胜率≥原+3pp 且样本≥10 才采用）→ ⑤ 若采用新过滤器则**二次重放全策略落地**（避免"采用前用过的信号"污染回测）→ ⑥ 写 `strategy_daily.json`（含各策略 daily+trades+summary）与 `dynamic_pool_bt.json`。全程打印重建日志到控制台/日志文件。
- 输出结构（strategy_daily.json）：`{meta:{...}, strategies:{sname:{daily:[{date,sname,code,name,price,score,triggered,buy_ref,stop,target,status,note,buy_date,buy_price,sell_date,sell_price,pnl,pnl_pct,hold_days,t1_ret,filter}], trades:[...], summary:{n_trades,win_rate,total_pnl,avg_hold_days,...}}}}`。

### 7.3 指数合成（固定池 + 动态池）
- `build_pool_index(incremental=True)`：**链式市值加权**。历史固定：增量模式只按股票日线补算新增交易日，由历史末点位反推基期总市值（base_mcap=末日总市值×100/末日点位），无新增日期时直接复用原文件（幂等，历史不漂移）。全量模式（文件缺失/权重不可复用）从基准日（2018-01-02）逐日合成。序列写入 `data/pool_index.json`：`{meta:{base_date,base_value}, series:[{date,close,...}]}`。**坑：全量合成时 base_date 等 Timestamp 必须 `.strftime('%Y-%m-%d')` 转字符串，否则 JSON 序列化报错。**
- `build_dynamic_pool_index(incremental=True)`：候选=全部 STOCKS；每个调仓日（每 `rebalance_days=10` 个交易日，`top_n=8`，`min_stocks=5`）按**趋势+动量组合得分**（如：相对指数强弱、MA20 上方、近期涨幅、量能等综合）选出 top_n 最强势标的为成分；指数按成分**链式市值加权**（调仓日自动衔接，成分变化不断裂）。调仓记录（rebalances）持久化在 json 中。动态池指数量纲不同，独立基日。
- **扩池/权重变更后必须删除 `pool_index.json`、`dynamic_pool_index.json`、`dynamic_pool_bt.json` 强制全量重建**（否则增量路径只复用旧权重）。
- 指数估值卡（app 端）：pool_index_card 显示基日以来/今年/区间涨幅+Top 权重；dynamic_pool_card 显示指数曲线+当前成分（最近调仓 Top6）+最近调仓日期+A/B 采用状态。

### 7.4 当前行情判断 + 明日预判 `analyze_market()`
- 数据：上证指数日线（data/csv/sh000001.csv，量价全）+ 选股池/动态池指数。
- ① 当前形态标签：由当日涨跌幅 chg、量比（今日量/5日均量 vr5）、相对 MA20 乖离 dist20、RSI14、连涨/连跌天数（streak 带符号）判定：
  - `chg≤-0.5 且 vr5<0.8` → **缩量回调**；`chg≤-1.5 且 vr5>1.5` → **放量长阴**；`chg≥1.5 且 vr5>1.5` → **放量长阳**；`chg≤-2.0 且 dist20<-5%` → **超跌**；`|chg|≤0.4 且 vr5<0.8` → **缩量横盘**；`chg≥0.5 且 vr5<0.8` → **缩量反弹**；`chg≥0.5 且 vr5≥1.2` → **放量上涨**；否则 **普通波动**。
- ② 明日预判：近 10 年历史样本中找与当日「chg±0.35pp、vr5±0.15、dist20±2%、涨跌方向一致」的相似交易日，统计次日「**强修复**（次日≥+0.5%）、**弱修复**（0~+0.5%）、**回落**（<0）」三档概率与平均次日涨跌；再修正：RSI<25 强修复+8pp / RSI>75 回落+8pp；dist20<-5% 强修复+6pp / dist20>+6% 回落+5pp；动态池相对强弱（动态池今日涨跌−上证今日涨跌）≥+0.5pp 强修复+5pp / ≤-0.5pp 回落+5pp；三档归一化后取最大为结论（强修复/偏强修复/弱修复/偏弱修复/调整回落 + 概率）。返回 dict：`{asof, sh:{close,chg,vr5,dist20,rsi14,streak,ma20}, label, dyn_chg, pool_chg, rel, next:{verdict,prob,strong,weak,down,avg_next,n_sim,recent}}`。
- 实测基准（2026-08-24 收盘）：上证 3882.01 ▼0.59%、量能正常、RSI 45.6、连跌1日 → 预判「偏弱/调整」48%（样本 360 例，平均次日 +0.03%）。

### 7.5 A/B 落地验证（防过拟合）
- 任何候选规则（过滤器/动态池）先用**全历史分组统计**看「胜率↑且收益↑、样本≥10」才候选；落地后必须**二次重放**对比落地前后胜率/收益，不达标准自动回滚并记录原因。落地记录持久化于 `dynamic_pool_bt.json`（adopted 列表 + 每项{策略,候选规则,落地前胜率/净,落地后胜率/净,样本,状态:adopted/rolled_back}）。
- 当前状态：10 策略 total_trades=885；固定池指数 146.29；动态池 409.61；动态池 A/B adopted=[]（S5/S6 候选落地均未达标自动回滚）。

## 8. Web 服务 `stock_bot/app.py`

- 单文件 http.server；`Handler` 处理 GET（路径分派：`/` 首页、`/strategy/<sid>` 策略详情、`/logs` 日志、`/api/refresh` 强制刷新、`/api/sim` 模拟测试）；POST 仅日志记录。静态资源内联（CSS 在页面头部，A股习惯**红涨绿跌**：`.up{color:#c22}` `.down{color:#0a7a2e}`）。
- 页面渲染函数：`page_frame`（侧边栏 10 策略+日志）、`render_index`（首页）、`render_strategy`（策略详情）、`svg_line_chart`（内联 SVG 曲线，含均线/十字线/区间选择）。
- **首页（render_index）自上而下**：
  1. 昨日信号→今日 T+1 结果卡（`yesterday_signal_card`）
  2. 今日推荐卡（评分最高触发信号 + 沪深300PE 提示 + 买入过滤器说明 + 动态池相对强弱）
  3. `sh_aid_card()`「📈 上证指数辅助选股」：上证最新点位/当日涨跌/今年/MA20 强弱 + 今日买入过滤器最近收盘放行/拦截判定
  4. `market_card()`「🧭 当前行情判断 · 明日预判」：形态/量能/乖离/RSI/连涨连跌/动态池相对强弱 + 预判结论概率条（强修复/弱修复/回落，CSS `.prob-bar`/`.pb-up/.pb-flat/.pb-down`）+ 相似样本数/平均次日/最近3例 + 判读提示
  5. `dynamic_pool_card()`「📊 动态选股池指数辅助」：动态池指数曲线（?idx=1m/3m/ytd/1y/all）+ 最新点位/今年/基日以来/区间涨幅 + 当前成分（最近调仓 Top6）+ 最近调仓日期 + A/B 采用状态
  6. `pool_index_card()`「📊 选股池指数（市值加权」：固定池指数 + Top 权重 + 区间切换
  7. 收益率汇总卡（`compound_html`：各策略 2019 起/今年/去年/近3月 收益率 + 全账户曲线）
  8. 资金利用率卡（`utilization_html`）
- 数据加载：启动时读 `strategy_daily.json` 进全局，`ensure_history()` 后台补建；每日收盘扫描后自动增量补指数；`/api/refresh` 支持 force。
- 启动：`python -X utf8 stock_bot/app.py`，监听 0.0.0.0:8080，启动即执行 `boot_scan()` 启动扫描线程。

## 9. 运行与验证步骤

1. 建目录 `data/csv/`，按第 4 节拉取 60 只个股 + sh000001 + 估值 csv。
2. 首次/数据变更后：`python -X utf8 history_engine.py`（自动 build：补上证→补固定池→模拟 10 策略→动态池+A/B→落地重放→写 json）。耗时约几分钟。
3. 启动服务：`python -X utf8 stock_bot/app.py`（或 start.bat），浏览器开 `http://localhost:8080/`。
4. 验证点（与基准对比，允许数据日期不同但口径一致）：
   - 首页 8 张卡齐全、无 Traceback；10 个策略侧边栏可进详情页。
   - 模拟成交笔数、胜率、收益率在合理范围（基准 885 笔）。
   - 固定池指数连续（基日 100）、动态池指数调仓记录存在、`dynamic_pool_bt.json` 有 adopted/rolled_back 记录。
   - 行情判断卡显示最新收盘形态 + 明日预判概率条（样本数 ≥ 数十例）。
   - 盘中刷新：推荐随行情变化；收盘后自动补指数。
5. 幂等性：连续跑两次 build，strategy_daily.json 与指数 json 内容不变（增量幂等）。

## 10. 关键设计决策与坑（复刻时勿踩）

- 编码：个股 csv 用 utf-8-sig（Excel 兼容）；控制台 GBK 用 `-X utf8` 或包装 stdout；腾讯实时行情返回 GBK 需 decode('gbk')。
- 全量 vs 增量：指数/数据变更（扩池、改基日）后必须**删除 pool_index.json、dynamic_pool_index.json、dynamic_pool_bt.json** 强制全量重建，否则增量复用旧权重导致指数错误。
- Timestamp 序列化：`base_date` 等必须 `.strftime()` 转字符串。
- 未来数据红线：信号收盘判定、成交次日时点、过滤器信号日数据；指数用当日收盘值（不偷看次日）。
- 非交易日/未收盘：指数补齐只写到"上一个完整交易日"，避免盘中半成品入持久化。
- 晚上市股票（如某 2022 年新上市的主板标的）：上市前自动跳过、上市后自动纳入，无需特殊处理。
- 过滤器文件缺失：自动放行（不拦截），保证系统可用，日志提示补数据。
- 过拟合防御：候选规则必须满足「全历史分组统计：胜率↑且收益↑、样本≥10」，落地必须二次重放验证，不达标自动回滚——这是本项目规则演进的核心纪律。
- 买入过滤器为"事后优化"有数据挖掘成分，落地效果以重放结果为准（见日志）。

## 11. 当前实测数字（2026-08-25，作为复刻完成后的对照基准）

- 股票池 60 只、数据截止 2026-08-24。
- 固定选股池指数（基日 2018-01-02=100）：**146.29**；动态池指数：**409.61**。
- 10 策略全历史总成交 **885 笔**。
- 过滤器落地效果：S2 池当日涨 胜率 47.2%→53.3%、净+1876→+3595；S4 池当日涨 净-1884→+549；S6 上证当日涨 净-564→+341 转正；S7 上证当日涨 胜率 50%→62.5%、净-1233→+571。
- 动态池 A/B：adopted=[]（S5/S6 候选未达标回滚）。
- 行情判断实测：2026-08-24 上证 3882.01 ▼0.59% → 形态"普通波动"，预判「偏弱/调整」48%（强修复26%/弱修复26%/回落48%，样本360，平均次日+0.03%）。

---

> 复刻完成后建议把演进记录（如新规则、调参、重建日志）追加到 `PROJECT_CONTEXT.md`，保持"提示词 + 记录"双文档可追溯。
