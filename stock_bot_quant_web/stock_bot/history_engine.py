# -*- coding: utf-8 -*-
"""后台引擎：每个策略以 5000 元本金逐日模拟，输出
1) 每日选股记录（标的/评分/买卖点/是否触发/T+1涨跌幅/持仓状态）
2) 历史交易明细
结果写入 data/strategy_daily.json

规则约定：
- 股票池仅保留沪深主板（剔除创业板300/301/302、科创板688/689、北交所4/8、ST、退市）
- 信号在收盘后确定；成交时点为【策略级配置】：每个策略选用最适合自己的时点组合
  （STRATEGY_TIMING，早盘=次日开盘价 09:30 附近，尾盘=次日收盘价 14:45-14:57，A股 T+1）
- T+1涨跌 = 选股标的下一个交易日开盘 / 信号日收盘 - 1（事后统计，仅复盘）
- 状态: 买入信号 / 建仓 / 持有 / 卖出信号 / 卖出成交 / 空仓 / 资金不足
"""
import sys, os, json, datetime
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _WORKSPACE)
os.chdir(_WORKSPACE)
import pandas as pd
import numpy as np
import backtest as bt
from stock_bot import advisor

START = pd.Timestamp('2026-07-01')  # 模拟交易起点：2026-07-01 起为真实模拟交易，此前历史仅作回测
CAPITAL = 5000.0
OUT = 'data/strategy_daily.json'
INTEREST_RATE = 0.02        # 空仓资金按货基/逆回购年化 2% 计入理财收益
INTEREST_DAILY = INTEREST_RATE / 365.0

# ===========================================================================
# ★ 可调参数区：改这里即可，保存后运行 `python history_engine.py` 重建数据、
#   再重启服务（start.bat）生效。无需改动任何逻辑代码。
# ===========================================================================
# 各策略每天最多信号/买入笔数：
#   0 = 该策略当天停买；1 = 每天最多 1 笔（默认）；N = 每天最多 N 笔。
# 说明：当前为单账户单持仓模型，实际每天最多成交 1 笔；该参数用于控制
#   信号频率（触发候选仅保留评分最高的 N 只）并预留未来多持仓扩展。
MAX_DAILY_SIGNALS = {'S9_趋势线突破回踩': 1}
# 指数策略（S7/S8）单批最小投入金额（元）：可用资金含费后不足该值时视为"资金不足"、不建仓，
#   避免"只投几分钱建仓"的虚假成交（此前 S7 出现过 invest=0.02 元的微批次）。
MIN_BATCH = 100.0
# ★ 买入过滤器（按策略匹配，2026-08-25）：买入信号当日收盘触发后，再按「指数条件」过滤，
#   过滤不通过 → 当天不买/不加仓（空仓），用于提高胜率、避开大盘弱势开仓。
#   类型：'pi_day_up'=选股池当日上涨  'sh_day_up'=上证当日上涨
#        'pi_gt_ma10'=选股池>MA10  'pi_gt_ma20'=选股池>MA20  'sh_gt_ma20'=上证>MA20
#   None = 该策略不过滤。依据见 _idx_out.txt 全历史分组统计（挑"胜率↑且收益↑"、
#   样本≥10 的规则；样本过小的策略不加过滤，避免过拟合）。
#   注意：该规则由全历史分组统计选出，属"事后优化"（有一定数据挖掘成分），
#   落地后胜率/收益以重放结果为准（见重建日志）。
BUY_FILTERS = {
    'S1_Christian突破回踩': None,             # A/B(2026-08-28)：sh/pi>MA20 均更差(胜率50→20、净-414→-1654)，保持无过滤
    'S2_MACD金叉KDJ死叉': ('sh_day_up', '上证当日上涨'),         # 原 pi_day_up 落地验证胜率47%→53%、净+1876→+3595；2026-08-27 选股池指数停用，改上证等价过滤
    'S3_吊灯止损趋势跟踪': None,               # 落地后胜率反降(29.4%)、净-3754，移除
    'S4_20日线回踩放量': ('sh_day_up', '上证当日上涨'),          # 原 pi_day_up 落地验证胜率持平、净-1884→+549 大幅减亏；同上改上证等价过滤
    'S5_止盈15止损5': ('pi_gt_ma20', '选股池站上20日线'),       # A/B(2026-08-28)：净-478→+55、胜率33→50，弱市追高买入被过滤
    'S6_布林下轨做T': ('sh_gt_ma20', '上证站上20日线'),         # A/B(2026-08-28)：原 sh_day_up 5笔20%净-480 → 2笔50%净-192；大盘破位不抄底
    'S9_趋势线突破回踩': ('sh_gt_ma20', '上证站上20日线'),      # A/B(2026-08-28)：7笔42.9%净+15 → 2笔100%净+209；弱市追突破5连亏全过滤
    'S10_RSI超卖超买反转': ('sh_gt_ma20', '上证站上20日线'),    # A/B(2026-08-28)：3笔0%净-957 → 1笔净-292；弱势超卖不接飞刀
    'S15_缠论三买': ('sh_day_up', '上证当日上涨'),  # A/B(2026-08-31)：全市场22,296笔胜率45.5%→49.8%、每笔+70.9→+101.9；正式系统重放4笔25%净-805→50%净+221，拦下北方华创/中科三环/仁和药业/银星能源4个弱市信号
    'S16_龙头接力': None,  # 打板策略需波动，暂不过滤；落地后 A/B 验证再定（弱市由 STRATEGY_ENV_FIT 限制不建仓）
    'S16_龙头接力V2': None,  # 同 base：连板≥3 版龙头接力，暂不过滤
    'S7_沪深300PE分批低买高卖': ('sh_day_up', '上证当日上涨'),   # 落地验证：胜率50%→63%、净-1233→+571
    'S8_红利低估值分批买入': None,             # 2笔 样本过小
}

# 模拟首日冷启动保护：True 时 START_STR 当天不建仓（仅观察），避免模拟起点无历史仓位、
# 首日对全市场信号批量追入造成"起点高位买入"失真
# A/B(2026-08-28)：开启后总净收益 -733→-367（S6 -192→+114、S10 -292→0、S4 -162→-49），
# 首日 4 笔批量建仓全亏的问题消除；S9 少赚 121、S1 因 7 月弱市追高仍亏，保持现状
SKIP_FIRST_DAY_BUY = True

# ★ 动态选股池（行情轮动自适应，2026-08-25）：
#   候选 = 沪深主板股票池（STOCKS，已剔除创业板/科创板/北交所/ST/退市；未来扩充 data/csv 主板个股
#         会自动纳入候选）。每个调仓日按「趋势+动量」组合得分选出 top_n 只最强势标的作为动态池成分，
#   行情轮动时成分自动换血；指数按链式市值加权合成（调仓日自动衔接、指数连续、历史持久化）。
#   回测：backtest_dynamic_pool() 对比 dp_day_up（动态池当日上涨）过滤 vs 现有过滤器，
#   若胜率+3pp 且样本≥10 → 自动采用并二次重放落地验证（记录持久化到 data/dynamic_pool_bt.json）。
DYNAMIC_POOL_CFG = {
    'top_n': 8,             # 每次选入成分数（候选15只中选8只）
    'rebalance_days': 10,   # 每10个交易日重选一次（约两周，行情轮动自适应）
    'min_stocks': 5,        # 候选不足时最少保留数（不足则全部纳入）
}
DYNAMIC_POOL_OUT = 'data/dynamic_pool_index.json'
DYNAMIC_POOL_BT_OUT = 'data/dynamic_pool_bt.json'

# ---------------------------------------------------------------------------
# 策略级成交时点：每个策略选择最适合自己的时点组合（不回测最优会过拟合，按
# "策略逻辑 + timing_backtest 4组合回测"综合选定）：
#   'open'  = 次日开盘价成交（早盘，约 09:30）
#   'close' = 次日收盘价成交（尾盘，14:45-14:57 收盘集合竞价）
# 时点只影响成交价格时点，信号判定始终是当日收盘（不会用到未来数据）。
# ---------------------------------------------------------------------------
STRATEGY_TIMING = {
    'S1_Christian突破回踩': ('close', 'close'),   # 尾盘买/尾盘卖：突破需收盘确认，回测最优(+4547)
    'S2_MACD金叉KDJ死叉': ('open', 'close'),       # 早盘买/尾盘卖：金叉后尽早介入，回测最优(+3758)
    'S3_吊灯止损趋势跟踪': ('open', 'close'),       # 早盘买/尾盘卖：回测最优(-697，4组合中最高)
    'S4_20日线回踩放量': ('open', 'close'),         # 早盘买/尾盘卖：回踩企稳后次日早盘介入，回测最优(+2165)
    'S5_止盈15止损5': ('close', 'close'),           # 尾盘买/尾盘卖：回测最优(-320)
    'S6_布林下轨做T': ('open', 'open'),             # 早盘买/早盘卖：做T策略，早盘低吸、早盘高抛，回测最优(+259)
    'S9_趋势线突破回踩': ('open', 'open'),           # 早盘买/早盘卖：回测最优(+78)
    'S10_RSI超卖超买反转': ('close', 'open'),       # 尾盘买/早盘卖：尾盘确认超卖、早盘卖反弹，回测最优(+435)
    'S15_缠论三买': ('open', 'open'),               # 早盘买/早盘卖：分型确认后次日开盘介入，样本外验证(2023-01~2026-08全市场)最优
    'S16_龙头接力': ('open', 'open'),               # 早盘买/早盘卖：龙头信号次日开盘打板，断板次日开盘跑
    'S16_龙头接力V2': ('open', 'open'),             # 同 base：连板≥3 版龙头接力
}
# ★ 行情驱动的策略适配（2026-08-27）：每个策略标注其适配的行情环境；当日行情不适配时，
#   该策略不建仓（状态显示"空仓"，note 注明原因）。行情环境由上证指数当日量价形态判定
#   （见 market_env_on）：trend_up=趋势向上 / range=震荡 / weak=弱势/超跌。
#   S7/S8 指数估值策略不在此表 → 不受行情限制（估值驱动，与短期行情无关）。
STRATEGY_ENV_FIT = {
    'S1_Christian突破回踩': {'trend_up', 'range'},   # 突破回踩需趋势或震荡
    'S2_MACD金叉KDJ死叉': {'trend_up', 'range'},     # 金叉在趋势/震荡中胜率更高
    'S3_吊灯止损趋势跟踪': {'trend_up'},              # 趋势跟踪仅适合趋势市
    'S4_20日线回踩放量': {'trend_up', 'range'},      # 回踩低吸需温和环境
    'S5_止盈15止损5': {'trend_up', 'range'},          # 波段持有
    'S6_布林下轨做T': {'range'},                      # 下轨做T需震荡
    'S9_趋势线突破回踩': {'trend_up', 'range'},       # 趋势线突破
    'S10_RSI超卖超买反转': {'weak', 'range'},         # 超卖反弹在弱势/震荡
    'S15_缠论三买': {'trend_up', 'range'},            # 中枢突破回踩为趋势中继形态，仅趋势/震荡适配
    'S16_龙头接力': {'trend_up', 'range'},            # 打板接力需强势/活跃环境，弱市不建仓（防2025-08式炸板连亏）
    'S16_龙头接力V2': {'trend_up', 'range'},          # 同 base：连板≥3 版龙头接力
}
# ★ 2026-09-03 次日开盘接力「二次校验」阈值：T 日收盘信号（最强龙头/强势）→ T+1 开盘接力买入。
#   但 T+1 开盘可能把昨日强势直接证伪（一字跌停/竞价大幅低开 = 接力失败，且一字跌停本就无法成交）。
#   引擎在 T+1 开盘成交前重新校验：开盘较上一交易日收盘低开 ≤ 阈值 → 判定昨日信号预测失败，
#   废弃挂单、不机械买入，并在当日与信号日记录废弃原因。
#   背景案例：2026-09-03 新赛股份（昨两连板最强龙头）一字跌停开盘，原逻辑按开盘价 6.06 记买入，实为接刀。
DISCARD_LOW_OPEN = {
    'S16_龙头接力': -0.03,
    'S16_龙头接力V2': -0.03,
}
# ★ 2026-09-04 开盘顶涨停"买不进"废弃（对称于低开废弃）：T+1 开盘若直接顶在涨停价
#   （开盘较昨收涨幅 ≥ 阈值 ≈9.8%，主板涨停约+9.9%~+10.1%），说明市场一致封板/一字，
#   普通买单无法按"开盘价"成交（模拟不假设竞价排队成功）。该信号视同买不进 → 废弃不建仓；
#   次日空仓日自动照常收盘扫描：龙头易主或开板可买后，自然产生新的接力信号。
#   背景：2026-07-03 恒尚节能开盘全天一字，原逻辑以开盘价记买入并吃到后续 +48%，实为"看得见买不到"。
UNFILLABLE_ZT_OPEN = {
    'S16_龙头接力': 0.098,
    'S16_龙头接力V2': 0.098,
}
# ★ 2026-09-07 回退：周末一度把"开盘顶板买不进"守卫与信号日顺延扩到开盘买普通个股策略，
#   经全史重放验证"买不进→顺延队内次强"显著增亏 → 只保留 S16 的真实成交校验（见上两个字典），
#   普通开盘买策略维持原"开盘价可成交"口径（官方数字仅对 S16 统计）。
ENV_NAMES = {'trend_up': '趋势向上', 'range': '震荡', 'weak': '弱势/超跌'}
TIMING_LABEL = {
    ('open', 'open'): '早盘买/早盘卖',
    ('open', 'close'): '早盘买/尾盘卖',
    ('close', 'open'): '尾盘买/早盘卖',
    ('close', 'close'): '尾盘买/尾盘卖',
}


def timing_of(sname):
    """返回策略成交时点 (buy_timing, sell_timing)，默认早盘买/尾盘卖"""
    return STRATEGY_TIMING.get(sname, ('open', 'close'))


def eligible(code, name):
    if code.startswith(('300', '301', '302', '688', '689')):  # 创业板/科创板
        return False
    if code.startswith(('4', '8')):  # 北交所
        return False
    if 'ST' in str(name).upper():    # ST
        return False
    return True


STOCKS = {c: n for c, n in bt.STOCKS.items() if eligible(c, n)}


def load_all():
    stocks = {}
    for code in STOCKS:
        try:
            df = bt.load_stock(code)
            if df is None or df.empty or len(df) < 120:
                continue  # 缺失/数据不足的标的跳过（全市场增量拉取时鲁棒；ma60 需足够窗口）
            df = bt.add_indicators(df).reset_index(drop=True)
            stocks[code] = df
        except Exception:
            continue
    return stocks


def idx_map(df):
    return {r['日期'].strftime('%Y-%m-%d'): i for i, r in df.iterrows()}


def calendar_of(stocks):
    s = set()
    for df in stocks.values():
        s.update(df['日期'].dt.strftime('%Y-%m-%d'))
    return sorted(s)


def t1_pct(df, k):
    """标的第k个交易日 → 下一交易日开盘相对第k日收盘的涨跌幅（事后统计，仅复盘用）"""
    if k is None or k + 1 >= len(df):
        return None
    c0 = float(df.loc[k, '收盘'])
    o1 = float(df.loc[k + 1, '开盘'])
    if c0 <= 0 or np.isnan(c0) or np.isnan(o1):
        return None
    return round((o1 / c0 - 1) * 100, 2)


def _r(x):
    if x is None or (isinstance(x, float) and (np.isnan(x) or not np.isfinite(x))):
        return None
    return round(float(x), 2)


START_STR = START.strftime('%Y-%m-%d')


# ========== 买入过滤器（按策略匹配，BUY_FILTERS） ==========
# 全局缓存：'pi'=选股池指数 'sh'=上证指数 'dp'=动态选股池指数 收盘序列，索引为 'YYYY-MM-DD'。
FILTER_CTX = {'pi': None, 'sh': None, 'dp': None}


def _load_filter_series():
    """加载过滤器所需的指数序列（build() 启动时调用一次）。
    'pi'：data/pool_index.json（选股池指数，build_pool_index 生成）
    'sh'：data/csv/sh000001.csv（上证指数日线，备用源缓存）
    'dp'：data/dynamic_pool_index.json（动态选股池指数，build_dynamic_pool_index 生成）"""
    import pandas as pd
    try:
        pi_raw = json.load(open(POOL_INDEX_OUT, encoding='utf-8'))['series']
        pi = pd.Series({x['date']: float(x['close']) for x in pi_raw})
        pi = pi[~pi.index.duplicated(keep='last')].sort_index()
        FILTER_CTX['pi'] = pi
    except Exception as e:
        print(f'[filter] 选股池指数加载失败: {e}')
        FILTER_CTX['pi'] = None
    try:
        sh = pd.read_csv(os.path.join(_WORKSPACE, 'data/csv/sh000001.csv'), encoding='utf-8')
        sh = pd.Series(sh['close'].astype(float).values, index=sh['date'].astype(str))
        sh = sh[~sh.index.duplicated(keep='last')].sort_index()
        FILTER_CTX['sh'] = sh
    except Exception as e:
        print(f'[filter] 上证指数加载失败: {e}')
        FILTER_CTX['sh'] = None
    try:
        dp_raw = json.load(open(DYNAMIC_POOL_OUT, encoding='utf-8'))['series']
        dp = pd.Series({x['date']: float(x['close']) for x in dp_raw})
        dp = dp[~dp.index.duplicated(keep='last')].sort_index()
        FILTER_CTX['dp'] = dp
    except Exception as e:
        print(f'[filter] 动态选股池指数加载失败: {e}')
        FILTER_CTX['dp'] = None


def _f_loc(s, date):
    """date 不在指数序列时回退到之前最近的交易日；无则返回 None"""
    if date in s.index:
        return s.index.get_loc(date)
    prior = s.index[s.index < date]
    if len(prior) == 0:
        return None
    return s.index.get_loc(prior[-1])


# ========== 行情环境判定（market_env_on） ==========
# 用上证指数历史量价，对任意历史交易日判定行情环境：trend_up=趋势向上 / range=震荡 / weak=弱势/超跌。
# 基于形态标签（量比 vr5 / MA20乖离 / RSI14），与 analyze_market 同口径；全历史预计算并缓存。
_MARKET_ENV_CACHE = None  # {date: env}


def market_env_map():
    """预计算上证指数每个交易日的行情环境，返回 {date: env}（带缓存）。"""
    global _MARKET_ENV_CACHE
    if _MARKET_ENV_CACHE is not None:
        return _MARKET_ENV_CACHE
    envs = {}
    try:
        sh = pd.read_csv(os.path.join(_WORKSPACE, 'data/csv/sh000001.csv'), encoding='utf-8')
        sh['date'] = pd.to_datetime(sh['date'])
        sh = sh.sort_values('date').reset_index(drop=True)
        c, v = sh['close'].astype(float), sh['volume'].astype(float)
        sh['v5'] = v.rolling(5).mean()
        sh['ma20'] = c.rolling(20).mean()
        up = c.diff().clip(lower=0)
        dn = -c.diff().clip(upper=0)
        sh['rsi14'] = 100 - 100 / (1 + up.ewm(alpha=1 / 14, adjust=False).mean()
                                   / dn.ewm(alpha=1 / 14, adjust=False).mean())
        for i in range(1, len(sh)):
            d = sh['date'].iloc[i].strftime('%Y-%m-%d')
            chg = float(c.iloc[i] / c.iloc[i - 1] - 1) * 100
            vr5 = float(v.iloc[i] / sh['v5'].iloc[i]) if pd.notna(sh['v5'].iloc[i]) else 1.0
            ma20 = float(sh['ma20'].iloc[i]) if pd.notna(sh['ma20'].iloc[i]) else None
            dist20 = (c.iloc[i] / ma20 - 1) * 100 if ma20 else 0.0
            rsi14 = float(sh['rsi14'].iloc[i]) if pd.notna(sh['rsi14'].iloc[i]) else None
            # 形态标签（与 analyze_market 同口径）
            if chg <= -0.5 and vr5 < 0.8:
                label = '缩量回调'
            elif chg <= -1.5 and vr5 > 1.5:
                label = '放量长阴'
            elif chg >= 1.5 and vr5 > 1.5:
                label = '放量长阳'
            elif chg <= -2.0 and dist20 < -5:
                label = '超跌'
            elif abs(chg) <= 0.4 and vr5 < 0.8:
                label = '缩量横盘'
            elif chg >= 0.5 and vr5 < 0.8:
                label = '缩量反弹'
            elif chg >= 0.5 and vr5 >= 1.2:
                label = '放量上涨'
            else:
                label = '普通波动'
            # 环境映射
            if label in ('放量长阳', '放量上涨') or (label == '缩量反弹' and dist20 >= 0):
                env = 'trend_up'
            elif label in ('缩量回调', '放量长阴', '超跌'):
                env = 'weak'
            else:
                env = 'range'
            envs[d] = env
    except Exception as e:
        print('[market_env] 行情环境预计算失败:', str(e)[:120])
    _MARKET_ENV_CACHE = envs
    return envs


def market_env_on(date):
    """返回指定交易日（YYYY-MM-DD）的行情环境：trend_up/range/weak。缺失 → 'range'（保守）。"""
    return market_env_map().get(date, 'range')


def strategies_for_env(env):
    """返回适配指定行情环境的策略列表（含 S7/S8 指数策略恒适配）。"""
    fitted = []
    for s in advisor.STRATEGY_LIST:
        fit = STRATEGY_ENV_FIT.get(s)
        if fit is None or env in fit:
            fitted.append(s)
    return fitted or list(advisor.STRATEGY_LIST)


def _filter_pass(sname, date):
    """按 BUY_FILTERS 判定策略 sname 在 date 当日收盘后是否通过买入过滤器。
    返回 (是否通过, 过滤器中文名)。未配置/指数数据缺失 → 恒通过（保守放行）。"""
    spec = BUY_FILTERS.get(sname)
    if not spec:
        return True, None
    kind, label = spec
    pi, sh, dp = FILTER_CTX['pi'], FILTER_CTX['sh'], FILTER_CTX['dp']
    if kind == 'pi_day_up':
        s = pi
    elif kind == 'sh_day_up':
        s = sh
    elif kind == 'dp_day_up':
        s = dp
    elif kind == 'pi_gt_ma10':
        s, n = pi, 10
    elif kind == 'pi_gt_ma20':
        s, n = pi, 20
    elif kind == 'sh_gt_ma20':
        s, n = sh, 20
    elif kind == 'dp_gt_ma20':
        s, n = dp, 20
    elif kind.startswith('sh_rsi_lt') or kind.startswith('dp_rsi_lt'):
        # 指数 RSI 不过热过滤器：'sh_rsi_lt_70' = 上证 RSI14 < 70；'dp_rsi_lt_75' = 动态池 RSI14 < 75
        s = sh if kind.startswith('sh') else dp
        try:
            thr = float(kind.rsplit('_', 1)[1])
        except ValueError:
            return True, None
        if s is None:
            return True, None
        i = _f_loc(s, date)
        if i is None or i < 14:
            return True, None
        chgs = s.iloc[i - 14:i + 1].diff().dropna()
        up = float(chgs[chgs > 0].sum())
        dn = float(-chgs[chgs < 0].sum())
        if up + dn <= 0:
            return True, None
        rsi = 100 * up / (up + dn)
        return bool(rsi < thr), label
    else:
        return True, None
    if s is None:
        return True, None
    i = _f_loc(s, date)
    if i is None:
        return True, None
    if kind.endswith('_day_up'):
        if i < 1:
            return True, None
        return bool(s.iloc[i] > s.iloc[i - 1]), label
    # MA 类：当日收盘 > 近 n 日均值
    if i < n - 1:
        return True, None
    ma = float(s.iloc[i - n + 1:i + 1].mean())
    return bool(s.iloc[i] > ma), label


# ---- S15「可做窗口」公开状态（页面标注/对照报告用） ----
# 口径与 BUY_FILTERS 的 'sh_gt_ma20' 完全一致：某交易日上证收盘站上其 20 日均线 ⇒ 该日可做。
_WIN_CACHE = {}
_SH_DATES = None


def sh_window(date=None):
    """返回 dict：{ok, date, sh, ma20, label}
    ok=True 表示 date（默认=上证序列最新交易日）当日收盘站上 20 日均线，属「可做窗口」。
    仅供页面标注/对照报告使用，不改变任何策略建账行为。"""
    global _SH_DATES
    if FILTER_CTX['sh'] is None:
        _load_filter_series()
    s = FILTER_CTX['sh']
    if s is None or len(s) == 0:
        return {'ok': True, 'date': date or '', 'sh': None, 'ma20': None,
                'label': '无上证序列，无法判定（默认视为可做）'}
    if _SH_DATES is None:
        _SH_DATES = [str(x) for x in s.index]
    d = date or _SH_DATES[-1]
    if d in _WIN_CACHE:
        return dict(_WIN_CACHE[d])
    # 非交易日/停更：回退到 d 之前最近一个交易日（保持口径与过滤一致：过滤只查序列内存在日）
    if d not in s.index:
        prior = [x for x in _SH_DATES if x <= d]
        if not prior:
            return {'ok': True, 'date': d, 'sh': None, 'ma20': None,
                    'label': '早于上证序列起点（默认视为可做）'}
        d = prior[-1]
    try:
        i = _SH_DATES.index(d)
        if i < 19:
            r = {'ok': True, 'date': d, 'sh': None, 'ma20': None,
                 'label': '上证序列不足20日（默认视为可做）'}
        else:
            sh_c = float(s.iloc[i])
            ma = float(s.iloc[i - 19:i + 1].mean())
            ok = sh_c > ma
            dist = round((sh_c / ma - 1) * 100, 2)
            r = {'ok': bool(ok), 'date': d, 'sh': round(sh_c, 2), 'ma20': round(ma, 2),
                 'label': f'上证{d} 收{sh_c:.2f} 站上20日线({ma:.2f},偏离{dist:+.2f}%)'
                          if ok else f'上证{d} 收{sh_c:.2f} 跌破20日线({ma:.2f},偏离{dist:+.2f}%)'}
    except Exception as e:
        r = {'ok': True, 'date': d, 'sh': None, 'ma20': None,
             'label': f'窗口判定异常:{type(e).__name__}'}
    _WIN_CACHE[d] = r
    return dict(r)


def _scan_best(stocks, maps, d, afn, exclude=None):
    """扫描当日评分最高的买入候选，返回 dict 或 None；只用当日及之前数据。
    exclude：排除的股票 code（如"卖出当日换仓"场景排除刚平仓的持仓股，避免卖后又买回同一只）。
    修复(2026-08-28)：优先返回「触发信号中评分最高」的候选（评分第一的股票常是已强势多日
    而非当日触发者，如 MACD 金叉/RSI 上穿 20 等瞬态信号），避免全市场下换仓候选永不触发。"""
    best = None
    best_trig = None
    for code in STOCKS:
        if code == exclude or code not in stocks:
            continue
        df = stocks[code]
        k = maps[code].get(d)
        if k is None or k < 1:
            continue
        try:
            r = afn(k, df)
        except Exception:
            continue
        if r is None:
            continue
        r = dict(r)
        r['code'] = code
        if r.get('triggered') and (best_trig is None or r['score'] > best_trig['score']):
            best_trig = r
        if best is None or r['score'] > best['score']:
            best = r
    return best_trig or best


# 单笔投入封顶（2026-09-03 资金口径调整）：
# S16 龙头接力两档不再把账户全部现金一次性满仓买入（此前复利滚动、单笔可破万，被认为不符合实际），
# 改为「每笔 ≤ max(本金5000, 账户总权益的一半)」：
#   · 账户 ≤ 10000（即累计盈利 ≤5000）→ 每笔上限 5000；
#   · 盈利突破 5000 后单笔上限随账户放大为账户/2（如账户 11000 → 上限 5500）；
# 预算未用完的现金闲置，每日按 INTEREST_DAILY 计理财入 int_pool。
SINGLE_CAP_POLICY = {
    'S16_龙头接力': 'half-cap',
    'S16_龙头接力V2': 'half-cap',
}


def buy_budget_of(sname, cash):
    """单笔买入预算上限。默认返回可用现金全额；S16 两档按 half-cap 封顶。"""
    if SINGLE_CAP_POLICY.get(sname) == 'half-cap':
        return max(CAPITAL, cash / 2.0)
    return cash


def _suspension_note(code, pending, ns):
    """持仓股停牌/无有效行情致卖出挂单顺延的当日标注文案（2026-09-09 用户要求）：
    信号本身没问题（仅因持仓卖不掉）→ 不废弃、挂单顺延至下一交易日；
    若换仓候选当日实际可买，提示「若实际无持仓，今天可买入」，避免真实无持仓的用户被模拟持仓卡住误导。"""
    nm = STOCKS.get(code, code)
    n = (f'持仓股{nm}今日停牌/无有效行情，昨日收盘已判卖出（{ns}）无法执行'
         f' → 卖出挂单顺延至下一交易日（信号不废弃）')
    sr = pending.get('sig_row') if pending else None
    rb = sr.get('rec_buy') if isinstance(sr, dict) else None
    if rb:
        n += f'；昨日卖出信号推荐换仓候选 {rb}，今日实际可买——若实际无持仓，今天可买入'
    elif pending and pending.get('switch_to'):
        n += (f'；换仓候选 {STOCKS.get(pending["switch_to"], pending["switch_to"])}'
              f' 今日实际可买——若实际无持仓，今天可买入')
    return n


def simulate_stock(sname, sfunc, afn, stocks, maps, buy_timing='open', sell_timing='open',
                   min_hold_days=0, intraday_date=None):
    """个股策略逐日模拟。sfunc=买卖信号(backtest)，afn=每日评分/买卖点(advisor)。
    信号收盘后确定、T+1 成交；卖出信号日扫出触发买入候选时 → 卖出成交日先卖后买（动态换仓）。
    buy_timing/sell_timing：'open'=次日开盘价成交(早盘)，'close'=次日收盘价成交(尾盘)；
    只影响成交价格时点，信号判定仍是当日收盘。
    min_hold_days：策略卖出信号（返回值 'sell'）需持有满 N 个交易日才可执行；
    止损信号（返回值含 'stop'，如 'sell_stop'）不受持有期限制（风控优先）。
    每日记录 equity（本金5000+已实现+持仓浮盈+空仓理财收益，资金曲线用）与 pos_pnl*
    （持仓浮盈，页面展示用）；成交日行带 deal_price（实际成交价）。"""
    calendar = calendar_of(stocks)
    records, trades = [], []
    pos = None      # {code, buy_date, buy_idx, buy_price, shares, cost, peak_close}
    pending = None  # {'action': 'buy'/'sell', 'code': code, 'switch_to': code|None, 'from_switch': bool}
    realized = 0.0  # 已实现净盈亏累计
    int_pool = 0.0  # 空仓资金理财收益累计（年化2%复利）
    nb = '次日开盘买入' if buy_timing == 'open' else '次日收盘买入'
    ns = '次日收盘卖出' if sell_timing == 'close' else '次日开盘卖出'

    def _interest(pos_cost=0.0):
        nonlocal int_pool
        cash = max(CAPITAL + realized + int_pool - pos_cost, 0.0)
        int_pool += cash * INTEREST_DAILY

    for d in calendar:
        if d < START_STR:
            continue  # 模拟交易起点之前不累计理财/不建仓，保证 7.1 起权益=本金5000
        row = {'date': d, 'status': '空仓', 'name': '', 'code': '', 'score': None,
               'price': None, 'buy_ref': None, 'stop': None, 'target': None,
               'triggered': False, 't1_pct': None, 'note': '', 'rec_buy': None}
        bought_today = False
        sold_today = False
        abandon_skip = False  # 纯买入挂单因开盘大幅低开被废弃：跳过当日再扫描，保证废弃决策单独成行
        blocked_sell = False  # 持仓股停牌/无有效行情致卖出挂单无法执行：信号不废弃、顺延，当日落标注行

        # 1) 执行挂单（今日开盘成交）；卖出成交后若需换仓则同一开盘时段继续买入
        while pending:
            act = pending['action']
            code = pending['code']
            df = stocks[code]
            k = maps[code].get(d)
            op = float(df.loc[k, '开盘']) if k is not None else float('nan')
            if k is None or not np.isfinite(op) or op <= 0:
                # 停牌/无有效行情（无 bar，或停牌帧今开为0/空）：挂单保留至下一交易日，不取消、不废弃。
                # 卖出挂单被持仓股停牌阻塞时置 blocked_sell → 当日生成标注行：信号本身没问题
                #（仅因持仓卖不掉）不废弃；换仓候选当日实际可买 → 提示「若实际无持仓，今天可买入」。
                if act == 'sell' and pos and pos['code'] == code:
                    blocked_sell = True
                break
            close = float(df.loc[k, '收盘'])
            tl_b = '开盘' if buy_timing == 'open' else '收盘'
            tl_s = ('开盘止损' if pending.get('stop')
                    else ('开盘' if sell_timing == 'open' else '收盘'))
            # ★盘中推进日：尾盘(收盘价)执行的挂单盘中不成交——收盘价尚未定，
            #   若盘中推进会用现价当"假收盘"导致尾盘策略提前成交，故保留挂单等收盘正式 build。
            if intraday_date is not None and d == intraday_date:
                if act == 'sell':
                    _tail = (not pending.get('stop')) and sell_timing == 'close'
                else:
                    _tail = ((not pending.get('from_switch')) and buy_timing == 'close') or \
                            (pending.get('from_switch') and (not pending.get('stop')) and sell_timing == 'close')
                if _tail:
                    break
            if act == 'sell' and pos and pos['code'] == code and not np.isnan(op) and op > 0:
                if pending.get('stop'):
                    # 止损：最快离场，T+1 次日开盘价成交（不受策略 sell_timing 尾盘时点影响）
                    sp = op
                else:
                    sp = close if (sell_timing == 'close' and not np.isnan(close)) else op
                net = bt.sell_net(sp, pos['shares'], df.loc[k, '日期']) - pos['cost']
                gross = (sp - pos['buy_price']) * pos['shares']
                trades.append({
                    'strategy': sname, 'stock_code': code, 'stock_name': STOCKS[code],
                    'buy_date': pos['buy_date'], 'buy_price': _r(pos['buy_price']),
                    'shares': pos['shares'], 'invest': _r(pos['cost']),
                    'sell_date': d, 'sell_price': _r(sp),
                    'gross_pnl': _r(gross), 'net_pnl': _r(net),
                    'ret_pct': round((sp / pos['buy_price'] - 1) * 100, 2),
                    'hold_days': (pd.Timestamp(d) - pd.Timestamp(pos['buy_date'])).days,
                    'sell_tag': '策略卖出',
                })
                realized += net
                pos = None
                sold_today = True
                row['status'] = '卖出成交'
                row['name'] = STOCKS[code]
                row['code'] = code
                row['price'] = _r(close)
                row['deal_price'] = _r(sp)
                row['sell_price'] = _r(sp)  # 实际卖出价（换仓时 deal_price 会被覆盖为买入价，保留此字段避免歧义）
                row['t1_pct'] = t1_pct(df, k)
                row['note'] = f'{tl_s}{sp:.2f}平仓'
                row['trade_pnl'] = _r(net)
                row['trade_pnl_pct'] = trades[-1]['ret_pct']
                # 动态换仓：卖出信号日已扫出触发候选 → 同一成交时段买入
                pending = ({'action': 'buy', 'code': pending['switch_to'], 'from_switch': True,
                            'stop': pending.get('stop'),
                            'sig_row': pending.get('sig_row')}
                           if pending.get('switch_to') else None)
            elif act == 'buy' and pos is None and not np.isnan(op) and op > 0:
                # ★ 2026-09-03 开盘接力二次校验（T+1 执行日「重新扫描」）：
                #   昨日 T 收盘信号"最强龙头/强势"→ 今日开盘接力买入。但今日开盘若较上一交易日收盘
                #   大幅低开（一字跌停/竞价核按钮），说明昨日强势预期已被证伪：买不进也不该买。
                #   此时废弃挂单、不机械买入；预测错误的信号日行回写标注，废弃决策写入当日行。
                dsc = DISCARD_LOW_OPEN.get(sname)
                if dsc is not None and k > 0:
                    _pc = float(df.loc[k - 1, '收盘']) if (k - 1) in df.index else float('nan')
                    if _pc > 0 and not np.isnan(_pc) and op / _pc - 1 <= dsc:
                        _ret = (op / _pc - 1) * 100
                        _nm = STOCKS[code]
                        if pending.get('from_switch'):
                            # 换仓目标大幅低开：卖出已完成，不接力低开标的 → 追加废弃说明，当日照常再扫收盘候选
                            row['note'] = (row['note'] +
                                           f'；换仓候选{_nm}开盘{op:.2f}较昨收{_pc:.2f}低开{_ret:.1f}%，'
                                           '昨日接力信号预测失败，废弃不买入，暂空仓').strip()
                            row['rec_buy'] = None
                            row['abandon_note'] = (f'原计划换仓买入 {_nm}（{code}）已废弃未成交：'
                                                   f'今日开盘 {op:.2f} 较昨收 {_pc:.2f} 低开 {_ret:.1f}%，'
                                                   '昨日接力预测失败，不机械买入，资金暂空仓')
                            # 回写信号日（卖出信号）行：昨日推荐的换仓候选预测失败 → 标注废弃
                            _sr = pending.get('sig_row')
                            if _sr is not None:
                                _rb = _sr.get('rec_buy')
                                if _rb and _nm in str(_rb):
                                    _sr['rec_buy'] = f'{_rb}（已废弃：次日开盘低开{_ret:.1f}%）'
                                _sr['note'] = (str(_sr.get('note') or '') +
                                               f'；[执行日开盘低开{_ret:.1f}%（今开{op:.2f}/昨收{_pc:.2f}），'
                                               '换仓候选预测失败，信号废弃未执行]')
                            pending = None
                            break  # 跳出挂单循环，当日第 2 段照常扫描收盘新信号
                        # 纯买入挂单废弃：回写信号日行，并在当日单独记录"废弃决策"
                        _sr = pending.get('sig_row')
                        if _sr is not None:
                            if _sr.get('status') in ('买入信号', '买入信号(待成交)'):
                                _sr['status'] = '信号废弃'
                            _sr['note'] = (str(_sr.get('note') or '') +
                                           f'；[执行日开盘低开{_ret:.1f}%（今开{op:.2f}/昨收{_pc:.2f}），'
                                           '昨日预测错误，信号废弃未执行]')
                        row['status'] = '信号废弃'
                        row['name'] = _nm
                        row['code'] = code
                        row['price'] = _r(op)
                        row['note'] = (f'信号本身有问题（开盘大幅低开{_ret:.1f}%，昨日强势已被证伪）→ 废弃：'
                                       f'{_nm}今日开盘{op:.2f}较昨收{_pc:.2f}低开{_ret:.1f}%，'
                                       '不机械买入，暂空仓')
                        abandon_skip = True
                        pending = None
                        break  # 挂单已废，跳出循环；下方 abandon_skip 单独成行记录今日决策
                # ★ 2026-09-04 开盘顶涨停「买不进」废弃（对称于低开废弃）：
                #   昨日 T 收盘信号"最强龙头"→ 今日开盘接力买入。但今日开盘若直接顶在涨停价
                #   （一字/竞价秒板），普通挂单无法按开盘价成交（模拟不假设排队成功），
                #   该信号视同买不进 → 废弃不建仓。次日空仓日自动照常收盘扫描，龙头易主
                #   或开板可买后自然产生新接力信号（满足"买不进就继续扫描推荐"的实际操作）。
                #   ★2026-09-07 回退：该守卫仅对 S16（UNFILLABLE_ZT_OPEN），不再扩展到普通开盘买策略。
                ztu = UNFILLABLE_ZT_OPEN.get(sname)
                if ztu is not None and k > 0:
                    _pc = float(df.loc[k - 1, '收盘']) if (k - 1) in df.index else float('nan')
                    if _pc > 0 and not np.isnan(_pc) and op / _pc - 1 >= ztu:
                        _ret = (op / _pc - 1) * 100
                        _nm = STOCKS[code]
                        if pending.get('from_switch'):
                            # 换仓目标开盘顶涨停：卖出已完成但买不进 → 追加说明暂空仓，当日照常再扫收盘候选
                            row['note'] = (row['note'] +
                                           f'；换仓候选{_nm}开盘{op:.2f}较昨收{_pc:.2f}涨{_ret:.1f}%'
                                           '（开盘顶在涨停价），买不进，暂空仓').strip()
                            row['rec_buy'] = None
                            row['abandon_note'] = (f'原计划换仓买入 {_nm}（{code}）已废弃未成交：'
                                                   f'今日开盘 {op:.2f} 较昨收 {_pc:.2f} 涨 {_ret:.1f}%'
                                                   '（开盘即涨停价），普通挂单买不进，资金暂空仓')
                            # 回写信号日（卖出信号）行：昨日推荐的换仓候选开盘顶涨停买不进 → 标注
                            _sr = pending.get('sig_row')
                            if _sr is not None:
                                _rb = _sr.get('rec_buy')
                                if _rb and _nm in str(_rb):
                                    _sr['rec_buy'] = f'{_rb}（已废弃：次日开盘涨停买不进）'
                                _sr['note'] = (str(_sr.get('note') or '') +
                                               f'；[执行日开盘顶涨停{_ret:.1f}%（今开{op:.2f}/昨收{_pc:.2f}），'
                                               '买不进，换仓候选废弃未执行]')
                            pending = None
                            break  # 跳出挂单循环，当日第 2 段照常扫描收盘新信号
                        # 纯买入挂单买不进废弃：回写信号日行，并在当日单独记录"废弃决策"
                        _sr = pending.get('sig_row')
                        if _sr is not None:
                            if _sr.get('status') in ('买入信号', '买入信号(待成交)'):
                                _sr['status'] = '信号废弃'
                            _sr['note'] = (str(_sr.get('note') or '') +
                                           f'；[执行日开盘顶涨停{_ret:.1f}%（今开{op:.2f}/昨收{_pc:.2f}），'
                                           '开盘买不进，信号废弃未执行]')
                        row['status'] = '信号废弃'
                        row['name'] = _nm
                        row['code'] = code
                        row['price'] = _r(op)
                        row['note'] = (f'信号本身有问题（今日开盘{op:.2f}较昨收{_pc:.2f}涨{_ret:.1f}%，'
                                       '直接顶在涨停价买不进）→ 废弃：暂空仓，'
                                       '收盘后按最新龙头重新扫描')
                        abandon_skip = True
                        pending = None
                        break  # 挂单已废，跳出循环；下方 abandon_skip 单独成行记录今日决策
                if pending.get('from_switch'):
                    # 换仓：先卖后买，与卖出同一成交时点（止损卖出=开盘，换仓买入同为开盘价）
                    if pending.get('stop') and not np.isnan(op) and op > 0:
                        px, tl_sw = op, '开盘'
                    else:
                        px = close if (sell_timing == 'close' and not np.isnan(close)) else op
                        tl_sw = '开盘' if sell_timing == 'open' else '收盘'
                else:
                    px = close if (buy_timing == 'close' and not np.isnan(close)) else op
                    tl_sw = tl_b
                cash = CAPITAL + realized + int_pool  # 可用现金 = 本金 + 已实现盈亏 + 理财收益（买入时无持仓）
                budget = buy_budget_of(sname, cash)   # 单笔投入预算上限（S16 两档：≤ max(本金5000, 账户/2)）
                shares = int(budget / (px * 100)) * 100
                while shares >= 100 and bt.buy_cost(px, shares) > budget:
                    shares -= 100  # 含费总成本不超过预算上限，手数尽量接近（预算本身恒≤现金）
                if shares >= 100:
                    cost = bt.buy_cost(px, shares)
                    pos = {'code': code, 'buy_date': d, 'buy_idx': k, 'buy_price': px,
                           'shares': shares, 'cost': cost, 'peak_close': close}
                    if pending.get('from_switch'):
                        # 换仓：先卖后买，保持"卖出成交"行并附换仓信息
                        row['note'] += f'；同日{tl_sw}{px:.2f}换仓买入{STOCKS[code]} {shares}股（投入{_r(cost)}元）'
                        row['rec_buy'] = f"{STOCKS[code]}（已换仓）"
                        row['deal_price'] = _r(px)
                        row['pos_cost'] = _r(px)
                        row['pos_pnl_pct'] = round((close / px - 1) * 100, 2)
                        row['pos_pnl'] = round(pos['shares'] * close - pos['cost'], 2)
                        row['invest'] = _r(cost)  # 换仓买入实际投入金额（含费）
                    else:
                        bought_today = True
                        row['status'] = '建仓'
                        row['name'] = STOCKS[code]
                        row['code'] = code
                        row['price'] = _r(close)
                        row['buy_ref'] = _r(px)
                        row['deal_price'] = _r(px)
                        row['t1_pct'] = t1_pct(df, k)
                        row['invest'] = _r(cost)  # 实际投入金额（含费，≤可用现金、尽量满仓）
                        row['note'] = f'{tl_sw}{px:.2f}买入{shares}股（投入{_r(cost)}元）'
                else:
                    if pending.get('from_switch'):
                        row['note'] += f'；换仓目标{STOCKS[code]}{tl_sw}{px:.2f}，可用{cash:.2f}元买不起1手，暂空仓'
                    else:
                        row['status'] = '资金不足'
                        row['name'] = STOCKS[code]
                        row['code'] = code
                        row['price'] = _r(close)
                        row['note'] = f'{tl_sw}{px:.2f}，可用{cash:.2f}元买不起1手'
                pending = None
            else:
                pending = None  # 成交条件不满足，取消挂单

        # 1b) ★盘中推进日（intraday_date，2026-09-03）：当日尚未收盘，
        #     只落「已发生的开盘成交 + 持仓预览」，不做任何当日收盘信号判定——
        #     盘中数据是实时快照（非收盘终值），收盘后正式 build() 会用当日
        #     收盘数据覆盖本日行。这样早盘买入/卖出当天就能出现在记录里。
        if intraday_date is not None and d == intraday_date:
            if blocked_sell:
                # 持仓股停牌/无有效行情 → 卖出挂单顺延（信号不废弃）。盘中当日落标注行：
                # 说明顺延原因 + 换仓候选提示「若实际无持仓，今天可买入」（2026-09-09 用户要求）
                code = pos['code']
                df = stocks[code]
                k = maps[code].get(d)
                row['status'] = '卖出顺延'
                row['name'] = STOCKS[code]
                row['code'] = code
                row['note'] = _suspension_note(code, pending, ns) + '（盘中标注，收盘后按正式数据复核）'
                # 换仓候选一并落行（页面据此渲染「候选买入信号」提示条；模拟账户因持仓停牌未动）
                _sr = (pending or {}).get('sig_row') if pending else None
                if isinstance(_sr, dict) and _sr.get('rec_buy'):
                    row['rec_buy'] = _sr['rec_buy']
                if k is not None:
                    close = float(df.loc[k, '收盘'])
                    row['price'] = _r(close)
                    row['pos_cost'] = _r(pos['buy_price'])
                    row['pos_pnl_pct'] = round((close / pos['buy_price'] - 1) * 100, 2)
                    row['pos_pnl'] = round(pos['shares'] * close - pos['cost'], 2)
            elif pos and not sold_today:
                code = pos['code']
                df = stocks[code]
                k = maps[code].get(d)
                if k is not None:
                    close = float(df.loc[k, '收盘'])
                    row['price'] = _r(close)
                    row['pos_cost'] = _r(pos['buy_price'])
                    row['pos_pnl_pct'] = round((close / pos['buy_price'] - 1) * 100, 2)
                    row['pos_pnl'] = round(pos['shares'] * close - pos['cost'], 2)
                    if bought_today:
                        # 第1段已填「建仓」行：补盘中现价/浮盈，不改状态
                        row['note'] += (f'；盘中现价{close:.2f} '
                                        f'浮盈{(close/pos["buy_price"]-1)*100:.1f}%'
                                        '（盘中预览，收盘后复核）')
                    else:
                        row['status'] = '持有'
                        row['name'] = STOCKS[code]
                        row['code'] = code
                        row['note'] = (f'盘中预览：成本{pos["buy_price"]:.2f} 现价{close:.2f} '
                                       f'浮盈{(close/pos["buy_price"]-1)*100:.1f}%'
                                       '（收盘后按收盘价复核）')
                        if pending and pending['action'] == 'sell':
                            # 尾盘卖出挂单在盘中保留：提示今日尾盘将执行离场
                            row['note'] += '；今日尾盘卖出挂单待收盘执行'
                else:
                    row['status'] = '持有'
                    row['name'] = STOCKS[code]
                    row['code'] = code
                    row['note'] = f'持仓股{STOCKS[code]}停牌，无盘中行情，继续持有'
            elif pos is None:
                if pending and pending['action'] == 'buy':
                    # 盘中保留的挂单：区分「尾盘时点未到」与「标的当日无行情（停牌）」
                    _tail_hit = ((not pending.get('from_switch')) and buy_timing == 'close') or \
                                (pending.get('from_switch') and (not pending.get('stop')) and sell_timing == 'close')
                    if pending.get('from_switch'):
                        # 保留第1段已填好的"卖出成交"行，仅注明换仓买入挂单顺延
                        row['note'] += (f'；换仓目标{STOCKS[pending["code"]]}尾盘价成交，盘中不推进（收盘后正式记录）'
                                        if _tail_hit else
                                        f'；换仓目标{STOCKS[pending["code"]]}当日无行情，买入挂单顺延至下一交易日')
                    else:
                        row['status'] = '买入信号(待成交)'
                        row['name'] = STOCKS[pending['code']]
                        row['code'] = pending['code']
                        row['note'] = ('标的尾盘时点才成交，盘中不推进（收盘后正式记录）'
                                       if _tail_hit else '标的当日无行情，挂单顺延至下一交易日')
                elif not sold_today:
                    # 空仓且无成交：当日收盘信号尚未判定，不生成盘中行（等收盘正式记录）
                    continue
            _interest(pos['cost'] if pos else 0.0)
            if pos and row.get('pos_pnl') is not None:
                row['equity'] = round(CAPITAL + realized + int_pool + row['pos_pnl'], 2)
            else:
                row['equity'] = round(CAPITAL + realized + int_pool, 2)
            if d >= START_STR:
                records.append(row)
            continue

        # 2a) 买入挂单被"开盘大幅低开"拦截废弃后：当日决策 = 空仓 + 废弃说明，单独成行。
        #     不再做当日二次扫描，避免覆盖废弃记录（若当日收盘又出新信号属罕见场景，宁可漏记也保证废弃可见）
        if abandon_skip:
            _interest(0.0)
            row['equity'] = round(CAPITAL + realized + int_pool, 2)
            if d >= START_STR:
                records.append(row)
            continue

        # 2) 当日：持仓判定（含刚建仓）/ 空仓扫描候选
        if pos and not sold_today:
            code = pos['code']
            df = stocks[code]
            k = maps[code].get(d)
            if k is None:
                # 持仓股停牌：保持持有状态显示，避免误判为空仓
                row['status'] = '卖出顺延' if blocked_sell else '持有'
                row['name'] = STOCKS[code]
                row['code'] = code
                if blocked_sell:
                    # 昨日已判卖出、今日持仓停牌卖不掉 → 挂单顺延并落标注（信号不废弃，含换仓候选可买提示）
                    row['note'] = _suspension_note(code, pending, ns)
                    # 换仓候选一并落行（页面据此渲染「候选买入信号」提示条；模拟账户因持仓停牌未动）
                    _sr = (pending or {}).get('sig_row') if pending else None
                    if isinstance(_sr, dict) and _sr.get('rec_buy'):
                        row['rec_buy'] = _sr['rec_buy']
                else:
                    row['note'] = f'持仓股{STOCKS[code]}停牌，无行情，继续持有'
            elif k is not None:
                close = float(df.loc[k, '收盘'])
                pos['peak_close'] = max(pos['peak_close'], close)
                ctx = {'entry_price': pos['buy_price'], 'peak_close': pos['peak_close'],
                       'ret': close / pos['buy_price'] - 1,
                       'ret_from_peak': close / pos['peak_close'] - 1}
                try:
                    sig = sfunc(k, df, ctx)
                except Exception:
                    sig = None
                row['name'] = STOCKS[code]
                row['code'] = code
                row['price'] = _r(close)
                row['t1_pct'] = t1_pct(df, k)
                is_stop = (sig == 'sell_stop')  # 止损信号：不受持有期限制
                held_days = (k - pos.get('buy_idx', k)) if pos.get('buy_idx') is not None else min_hold_days
                defer_sell = (sig == 'sell' and min_hold_days > 0 and held_days < min_hold_days)
                if defer_sell:
                    sig = None  # 策略卖出信号但持有期未满：延迟卖出，继续持有
                elif sig == 'sell' or sig == 'sell_stop':
                    pending = {'action': 'sell', 'code': code, 'stop': is_stop,
                               'sig_row': row}  # 信号日行引用：执行日开盘接力失败时可回写"废弃"
                    # 同日收盘再扫描买入候选：动态换仓候选（仅当日及之前数据，排除刚平仓的持仓股）
                    rec = _scan_best(stocks, maps, d, afn, exclude=code)
                    if rec and rec.get('triggered'):
                        _ok, _flt = _filter_pass(sname, d)  # 换仓候选同样过买入过滤器
                        if _ok:
                            row['rec_buy'] = f"{STOCKS[rec['code']]}（评分{rec.get('score')}）"
                            pending['switch_to'] = rec['code']
                        else:
                            row['rec_buy'] = f"{STOCKS[rec['code']]}（评分{rec.get('score')}，被{_flt}过滤）"
                if bought_today:
                    if sig == 'sell' or sig == 'sell_stop':
                        row['note'] += (f'；收盘触发{"止损" if is_stop else "卖出"}信号（{ns}）')
                    elif defer_sell:
                        row['note'] += f'；持有期未满({held_days}/{min_hold_days}日)延迟卖出'
                    else:
                        row['note'] += '；T+1可持有'
                else:
                    if sig == 'sell' or sig == 'sell_stop':
                        row['status'] = '卖出信号'
                        row['note'] = (f'收盘判定{"止损" if is_stop else "卖出"}（{ns}）'
                                       f'成本{pos["buy_price"]:.2f} 现价{close:.2f} '
                                       f'浮盈{(close/pos["buy_price"]-1)*100:.1f}%')
                    elif defer_sell:
                        row['status'] = '持有'
                        row['note'] = (f'成本{pos["buy_price"]:.2f} 现价{close:.2f} '
                                       f'浮盈{(close/pos["buy_price"]-1)*100:.1f}%'
                                       f'（持有期未满{held_days}/{min_hold_days}日，延迟卖出）')
                    else:
                        row['status'] = '持有'
                        row['note'] = f'成本{pos["buy_price"]:.2f} 现价{close:.2f} 浮盈{(close/pos["buy_price"]-1)*100:.1f}%'
                row['pos_cost'] = _r(pos['buy_price'])
                row['pos_pnl_pct'] = round((close / pos['buy_price'] - 1) * 100, 2)
                row['pos_pnl'] = round(pos['shares'] * close - pos['cost'], 2)
        elif pos is None:
            if pending and pending['action'] == 'buy':
                if pending.get('from_switch'):
                    # 换仓买入挂单顺延：已卖出、待换仓，标的当日无行情
                    row['status'] = '卖出成交'
                    row['name'] = STOCKS[pending['code']]
                    row['code'] = pending['code']
                    row['note'] = f'已卖出，待换仓买入{STOCKS[pending["code"]]}（当日无行情，挂单顺延）'
                else:
                    # 上一交易日买入信号标的今日停牌/无数据，挂单顺延
                    row['status'] = '买入信号(待成交)'
                    row['name'] = STOCKS[pending['code']]
                    row['code'] = pending['code']
                    row['note'] = '标的当日无行情，挂单顺延至下一交易日'
                _interest(0.0)
                row['equity'] = round(CAPITAL + realized + int_pool, 2)
                if d >= START_STR:
                    records.append(row)
                continue
            # ★ 行情驱动的策略适配：当日行情不适配本策略 → 不建仓（空仓，注明行情原因）
            env = market_env_on(d)
            fit = STRATEGY_ENV_FIT.get(sname)
            if fit is not None and env not in fit:
                row['status'] = '空仓'
                row['note'] = f'当前行情[{ENV_NAMES[env]}]不适配本策略（适配：{"、".join(ENV_NAMES[x] for x in sorted(fit))}），空仓'
                _interest(0.0)
                row['equity'] = round(CAPITAL + realized + int_pool, 2)
                if d >= START_STR:
                    records.append(row)
                continue
            max_signals = MAX_DAILY_SIGNALS.get(sname, 1)  # 每日信号/买入笔数上限（可配置）
            best = None
            trig = []
            for code in STOCKS:
                if code not in stocks:
                    continue
                df = stocks[code]
                k = maps[code].get(d)
                if k is None or k < 1:
                    continue
                try:
                    r = afn(k, df)
                except Exception:
                    continue
                if r is None:
                    continue
                r = dict(r)
                r['code'] = code
                if best is None or r['score'] > best['score']:
                    best = r
                if r.get('triggered'):
                    trig.append(r)
            if best:
                if max_signals == 0:
                    best = dict(best); best['triggered'] = False  # 限额0：当天禁买
                elif trig:
                    trig.sort(key=lambda x: x['score'], reverse=True)
                    trig = trig[:max_signals]  # 仅评分最高的 max_signals 个触发信号有效
                    if not any(t['code'] == best['code'] for t in trig):
                        # 修复(2026-08-28)：评分第一的股票常是「已强势多日」而非「当日触发」的股票
                        #（如 MACD 金叉/RSI 上穿 20 等瞬态信号），全市场 2600+ 只下二者几乎不重合，
                        # 导致 S2/S10 等策略常年空仓。改为从触发信号中选评分最高者买入，不浪费信号。
                        best = dict(trig[0])
            if best and d >= START_STR:
                bdf = stocks[best['code']]
                bk = maps[best['code']].get(d)
                if best['triggered']:
                    if SKIP_FIRST_DAY_BUY and d == START_STR:
                        # 模拟首日冷启动：仅观察不建仓
                        best = dict(best)
                        best['triggered'] = False
                        best['note'] = (best.get('note', '') + '；模拟首日仅观察，不建仓')
                    # 按策略买入过滤器：指数条件不满足 → 当天不买（空仓，避免弱势开仓）
                    _ok, _flt = _filter_pass(sname, d)
                    if not _ok:
                        best = dict(best)
                        best['triggered'] = False
                        best['filter'] = _flt
                # ★ 2026-09-07 回退：删除了"信号日收盘预判次日可买性+顺延队内次强"逻辑
                #   （全史重放证明顺延接弱势备选显著增亏）→ 信号日正常推荐最强候选，
                #   次日开盘是否买得进完全交给执行日守卫（低开证伪/顶板买不进→废弃成行记录）。
                if best['triggered']:
                    pending = {'action': 'buy', 'code': best['code'],
                               'sig_row': row}  # 信号日行引用（次日开盘低开废弃时回写标注）
                if sold_today or row['status'] == '资金不足':
                    if best['triggered']:
                        row['note'] = (row['note'] + f"；收盘再现买入信号：{STOCKS[best['code']]}（评分{best.get('score')}，{nb}可买）").strip()
                        row['rec_buy'] = f"{STOCKS[best['code']]}（评分{best.get('score')}）"
                    elif best.get('filter'):
                        row['note'] = (row['note'] + f"；{STOCKS[best['code']]}买入信号被过滤（{best['filter']}）").strip()
                else:
                    row['status'] = '买入信号' if best['triggered'] else '空仓'
                    row['name'] = STOCKS[best['code']]
                    row['code'] = best['code']
                    if best.get('filter'):
                        row['note'] = (row['note'] + f"（{best['filter']}未满足，空仓）").strip()
                    row['score'] = best.get('score')
                    row['price'] = _r(best.get('price'))
                    row['buy_ref'] = _r(best.get('buy_ref'))
                    row['stop'] = _r(best.get('stop'))
                    row['target'] = _r(best.get('target'))
                    row['triggered'] = bool(best['triggered'])
                    row['t1_pct'] = t1_pct(bdf, bk)
                    row['note'] = (best.get('note', '') +
                                   (f'（收盘判定，{nb}）' if best['triggered'] else ''))
            elif not sold_today:
                row['status'] = '空仓'
                row['note'] = '无买入信号'
        # 每日权益：本金 + 已实现盈亏 + 持仓浮盈 + 空仓理财收益（资金曲线用）
        _interest(pos['cost'] if pos else 0.0)
        if pos and row.get('pos_pnl') is not None:
            row['equity'] = round(CAPITAL + realized + int_pool + row['pos_pnl'], 2)
        else:
            row['equity'] = round(CAPITAL + realized + int_pool, 2)
        if d >= START_STR:
            records.append(row)
    return records, trades


def _q_target(pct):
    """估值历史分位 → 季度再平衡目标批次数（S7 用）：
    <20% 满仓3批，<40% 两批，<60% 一批，≥60% 空仓"""
    if pct < 0.20:
        return 3
    if pct < 0.40:
        return 2
    if pct < 0.60:
        return 1
    return 0


def simulate_index(path, name, buy_lines, sell_lines, vkey, max_hold_days=None,
                   index_name=None, stop_loss_pct=None, rebalance_fn=None):
    """指数策略完整状态机（含未平仓持仓），与 run_scaled_index_strategy 同规则。
    - 卖出：估值上穿卖出线；持仓浮亏达 stop_loss_pct（如-0.08）止损离场；超过 max_hold_days 天强制平仓
    - rebalance_fn：每季度末按当前估值全历史分位调整批次数（如 _q_target），避免长期空仓
    - 信号按当日收盘估值判定；每日记录 equity（本金5000+已实现+持仓浮盈+空仓理财收益）
      与持仓浮盈字段"""
    index_name = index_name or name
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['日期'] = pd.to_datetime(df['日期'])
    df = df.sort_values('日期').reset_index(drop=True)
    n = len(df)
    start_idx = 0
    for k in range(n):
        if df.loc[k, '日期'] >= START:
            start_idx = k
            break
    buy_line = max(t for t, _ in buy_lines)   # 最宽松买入线（最先触发）
    sell_line = sell_lines[0][0]              # 最宽松卖出线（最先触发）
    positions = []       # FIFO {buy_idx, buy_date, buy_price, tag}
    bought_levels = {}
    trades = []
    realized = 0.0       # 已实现净盈亏累计
    int_pool = 0.0       # 空仓资金理财收益累计（年化2%复利）

    def _interest():
        nonlocal int_pool
        pos_cost = sum(p['invest'] for p in positions)  # 各批实际投入金额
        cash = max(CAPITAL + realized + int_pool - pos_cost, 0.0)
        int_pool += cash * INTEREST_DAILY

    def settle(pi, ri, tag):
        t = positions[pi]
        price = df.loc[ri, '指数']
        ret = price / t['buy_price'] - 1
        invest = t.get('invest', 5000.0)  # 该批实际投入金额（买入佣金计入成本）
        cost = invest + max(invest * bt.COMMISSION_RATE, bt.COMMISSION_MIN)
        sell_amt = invest * (1 + ret)
        fee = max(sell_amt * bt.COMMISSION_RATE, bt.COMMISSION_MIN) + sell_amt * bt.stamp_tax(df.loc[ri, '日期'])
        return {
            'strategy': name, 'stock_code': '', 'stock_name': index_name,
            'buy_date': t['buy_date'].strftime('%Y-%m-%d'),
            'buy_price': round(t['buy_price'], 2), 'shares': 1, 'invest': round(invest, 2),
            'sell_date': df.loc[ri, '日期'].strftime('%Y-%m-%d'),
            'sell_price': round(price, 2),
            'gross_pnl': round(sell_amt - invest, 2),
            'net_pnl': round(sell_amt - fee - cost, 2),
            'ret_pct': round(ret * 100, 2),
            'hold_days': int((df.loc[ri, '日期'] - t['buy_date']).days),
            'sell_tag': tag,
        }

    def settle_and_pop(ri, tag):
        nonlocal realized
        trades.append(settle(0, ri, tag))
        realized += trades[-1]['net_pnl']
        positions.pop(0)
        bought_levels.clear()
        return True

    idx_invest = 5000.0  # 单批名义投入上限（实际按可用现金投入，含费后不超过现金）
    records = []
    for i in range(start_idx, n):
        if i == 0:
            continue
        prev_v = df.loc[i - 1, vkey]
        cur_v = df.loc[i, vkey]
        d = df.loc[i, '日期']
        dstr = d.strftime('%Y-%m-%d')
        px = float(df.loc[i, '指数'])
        # T+1 口径：下一交易日开盘相对当日收盘的涨跌幅（指数估值CSV无开盘列时用次日指数点位）
        if i + 1 < n:
            nxt = df.loc[i + 1, '开盘'] if '开盘' in df.columns else df.loc[i + 1, '指数']
            t1 = (float(nxt) / px - 1) * 100
        else:
            t1 = None
        sold_today = False
        sold_tag = None
        # 卖出：上穿
        for th, tag in sell_lines:
            if prev_v < th <= cur_v or (prev_v <= th and cur_v > th):
                if positions:
                    sold_today = settle_and_pop(i, tag)
                    sold_tag = tag
        # 止损：持仓浮亏达阈值（相对最早批次成本），按当日收盘离场
        if stop_loss_pct and not sold_today and positions:
            p0 = positions[0]['buy_price']
            if px / p0 - 1 <= stop_loss_pct:
                sold_today = settle_and_pop(i, '止损离场')
                sold_tag = '止损离场'
        # 买入：下穿（每日最多一笔）
        bought_today = False
        b_inv = 0.0
        insufficient = False
        filter_block = None  # 买入过滤器未通过（指数条件不满足），当日不建仓
        for th, tag in buy_lines:
            if prev_v > th >= cur_v or (prev_v >= th and cur_v < th):
                if th not in bought_levels and len(positions) < 3:
                    _ok, _flt = _filter_pass(name, dstr)  # 按策略买入过滤器
                    if not _ok:
                        filter_block = _flt
                        break
                    cash_avail = CAPITAL + realized + int_pool - sum(p['invest'] for p in positions)
                    b_inv = min(idx_invest, max(cash_avail - bt.COMMISSION_MIN, 0.0))  # 含买入费后不超可用现金
                    if b_inv >= MIN_BATCH:
                        positions.append({'buy_idx': i, 'buy_date': d, 'buy_price': px, 'tag': tag, 'invest': b_inv})
                        bought_levels[th] = True
                        bought_today = True
                        break
                    insufficient = True  # 触发信号但可用资金不足（含费后<MIN_BATCH元），不建仓
        # 最长持有期强制平仓
        if max_hold_days and positions and i - positions[0]['buy_idx'] >= max_hold_days:
            sold_today = settle_and_pop(i, '持有期强制平仓')
            sold_tag = '持有期强制平仓'
        # 每日状态（优先记录当日建仓/卖出成交时点，持仓信息放说明里）
        if bought_today:
            status, score, trig = '建仓', 100, True
            extra = f'；同日平仓1批' if sold_today else ''
            note = (f'估值{cur_v:.3f} 下穿买入线{buy_line}，建仓1批（投入{b_inv:.2f}元）{extra}'
                    if not sold_today else
                    f'估值{cur_v:.3f} 卖出1批后再下穿买入线，建仓1批（投入{b_inv:.2f}元）')
        elif sold_today:
            status, score, trig = '卖出成交', None, False
            if positions:
                note = f'估值{cur_v:.3f} 触发卖出（{sold_tag}），减仓1批，剩{len(positions)}批 成本{positions[0]["buy_price"]:.2f} 现价{px:.2f}'
            else:
                note = f'估值{cur_v:.3f} 触发卖出（{sold_tag}），全部清仓'
        elif positions:
            cost0 = positions[0]['buy_price']
            status, score, trig = '持有', None, False
            note = f'持有{len(positions)}批 成本{cost0:.2f} 现价{px:.2f} 浮盈{(px/cost0-1)*100:.1f}%'
            if filter_block:
                note += f'；{filter_block}未满足，未加仓'
        elif filter_block:
            status, score, trig = '空仓', int(max(30, 100 * (1 - (buy_line - cur_v) / buy_line))), False
            note = f'估值{cur_v:.3f} 下穿买入线{buy_line}，{filter_block}未满足，暂不建仓'
        elif insufficient:
            status, score, trig = '资金不足', 0, False
            note = f'估值{cur_v:.3f} 下穿买入线{buy_line}，可用资金不足（含费需≥{MIN_BATCH:.0f}元），暂不建仓'
        elif cur_v < buy_line:
            status, score, trig = '空仓', int(max(30, 100 * (1 - (buy_line - cur_v) / buy_line))), False
            note = f'估值{cur_v:.3f} 已在买入区（<{buy_line}），等回踩下穿信号'
        elif cur_v <= sell_line:
            status, score, trig = '空仓', int(max(10, 60 * (sell_line - cur_v) / max(sell_line - buy_line, 1e-9))), False
            note = f'估值{cur_v:.3f} 未到买入线{buy_line}，继续等待'
        else:
            status, score, trig = '空仓', 0, False
            note = f'估值{cur_v:.3f} 高于卖出线{sell_line}，空仓'
        row = {
            'date': dstr, 'status': status, 'name': index_name, 'code': '',
            'score': score, 'price': _r(px), 'buy_ref': buy_line, 'stop': None,
            'target': sell_line, 'triggered': trig, 't1_pct': _r(t1),
            'note': note, 'rec_buy': None,
        }
        if bought_today:
            row['invest'] = _r(b_inv)
        if sold_today and trades:  # 本次卖出成交的模拟收益（含手续费/印花税）
            row['trade_pnl'] = trades[-1]['net_pnl']
            row['trade_pnl_pct'] = trades[-1]['ret_pct']
        # 季度估值分位再平衡（rebalance_fn）：每季度末按当前估值全历史分位调整批次数
        if rebalance_fn and d.month in (3, 6, 9, 12) and (i + 1 >= n or df.loc[i + 1, '日期'].month != d.month):
            hist = df.loc[:i, vkey]
            pct = float((hist < cur_v).mean())
            target = rebalance_fn(pct)
            cur_b = len(positions)
            if target > cur_b:
                rb_block = None  # 再平衡加仓被买入过滤器拦截
                for _ in range(min(target, 3) - cur_b):
                    _ok, _flt = _filter_pass(name, dstr)
                    if not _ok:
                        rb_block = _flt
                        break
                    cash_avail = CAPITAL + realized + int_pool - sum(p['invest'] for p in positions)
                    b_inv = min(idx_invest, max(cash_avail - bt.COMMISSION_MIN, 0.0))
                    if b_inv < MIN_BATCH:
                        break  # 可用资金不足（含费后<MIN_BATCH元），无法继续加仓
                    positions.append({'buy_idx': i, 'buy_date': d, 'buy_price': px, 'tag': '再平衡加仓', 'invest': b_inv})
                    bought_today = True
                if bought_today:
                    row['status'] = '建仓'
                    row['score'] = 100
                    row['triggered'] = True
                    row['invest'] = _r(b_inv)
                    row['note'] = (f'季度再平衡：估值分位{pct * 100:.0f}%，加仓至{len(positions)}批'
                                   f'（投入{b_inv:.2f}元/批）')
                elif rb_block:
                    row['note'] = (f'季度再平衡：估值分位{pct * 100:.0f}%，目标{min(target, 3)}批，'
                                   f'{rb_block}未满足，未加仓')
                else:
                    row['note'] = (f'季度再平衡：估值分位{pct * 100:.0f}%，目标{min(target, 3)}批，'
                                   f'可用资金不足，维持{cur_b}批')
            elif target < cur_b:
                while cur_b > target and positions:
                    settle_and_pop(i, '再平衡减仓')
                    cur_b -= 1
                    sold_today = True
                row['status'] = '卖出成交'
                if trades:
                    row['trade_pnl'] = trades[-1]['net_pnl']
                    row['trade_pnl_pct'] = trades[-1]['ret_pct']
                row['note'] = f'季度再平衡：估值分位{pct * 100:.0f}%，减仓至{target}批'
        # 持仓浮盈与每日权益（资金曲线）
        if positions:
            row['pos_batches'] = len(positions)
            row['pos_cost'] = _r(positions[0]['buy_price'])
            row['pos_pnl_pct'] = round((px / positions[0]['buy_price'] - 1) * 100, 2)
            unreal = sum(p['invest'] * (px / p['buy_price'] - 1) for p in positions)
            row['pos_pnl'] = round(unreal, 2)
        else:
            unreal = 0.0
        _interest()
        row['equity'] = round(CAPITAL + realized + int_pool + unreal, 2)
        records.append(row)
    return records, trades


def calc_summary(sname, trades, records):
    if trades:
        pnl = [t['net_pnl'] for t in trades]
        wins = sum(1 for p in pnl if p > 0)
        summary = {
            'trades': len(trades),
            'total_net_pnl': round(sum(pnl), 2),
            'avg_pnl_per_trade': round(sum(pnl) / len(pnl), 2),
            'win_rate': round(wins / len(pnl) * 100, 1),
            'avg_hold_days': round(sum(t['hold_days'] for t in trades) / len(trades), 1),
        }
    else:
        summary = {'trades': 0, 'total_net_pnl': 0.0, 'avg_pnl_per_trade': 0.0,
                   'win_rate': 0.0, 'avg_hold_days': 0.0}
    summary['cur_status'] = records[-1]['status'] if records else '无数据'
    return summary


def compound_series(trades):
    """复利模拟：本金5000起，每轮卖出后把净盈亏并入本金，下一轮按新本金买入（滚动复利）。
    - 个股（stock_code 非空）：按当前本金整手（100股）买入，含费总成本尽量接近且不超过当前本金；
    - 指数（stock_code 为空）：按 5000/批 等比例缩放（整手规则不适用）。
    返回每轮结束后 [{buy_date, sell_date, stock, shares, invest, gain, pool}]，
    pool = 本金 + 截至该轮的累计净盈亏。"""
    pool = CAPITAL
    out = []
    for t in sorted(trades, key=lambda x: (str(x['sell_date']), str(x['buy_date']))):
        px = float(t['buy_price'])
        sp = float(t['sell_price'])
        if t.get('stock_code'):
            shares = int(pool / (px * 100)) * 100
            while shares >= 100 and bt.buy_cost(px, shares) > pool:
                shares -= 100
            if shares < 100:
                out.append({'buy_date': t['buy_date'], 'sell_date': t['sell_date'],
                            'stock': t.get('stock_name', ''), 'shares': 0,
                            'invest': 0.0, 'gain': 0.0, 'pool': round(pool, 2)})
                continue
            cost = bt.buy_cost(px, shares)
            net = bt.sell_net(sp, shares, pd.Timestamp(t['sell_date'])) - cost
        else:
            invest0 = float(t.get('invest') or 0) or CAPITAL
            scale = pool / invest0
            cost = invest0 * scale
            net = float(t['net_pnl']) * scale
            shares = t.get('shares', 1)
        pool += net
        out.append({'buy_date': t['buy_date'], 'sell_date': t['sell_date'],
                    'stock': t.get('stock_name', ''), 'shares': shares,
                    'invest': round(cost, 2), 'gain': round(net, 2), 'pool': round(pool, 2)})
    return out


def fixed5000_pnl(trades):
    """固定5000口径：假设每笔都按 5000 元本金、实际买卖价、整手买入、真实费用重算净盈亏（收益不滚入）。
    - 个股（stock_code 非空）：5000 元整手（100股）买入，5000 买不起 1 手则该笔按 0 计（跳过）；
    - 指数（stock_code 为空）：每批按 5000 投入，费用按 5000 与卖出金额真实计算。
    返回 (合计净盈亏, 有效笔数)。"""
    total = 0.0
    n = 0
    for t in trades:
        px = float(t['buy_price'])
        sp = float(t['sell_price'])
        sdate = pd.Timestamp(t['sell_date'])
        if t.get('stock_code'):
            shares = int(5000.0 / (px * 100)) * 100
            if shares < 100:
                continue  # 5000元买不起1手：该笔按0计（真实账本中该笔同样未成交）
            gain = bt.sell_net(sp, shares, sdate) - bt.buy_cost(px, shares)
        else:
            cost = 5000.0 + max(5000.0 * bt.COMMISSION_RATE, bt.COMMISSION_MIN)
            sell_amt = 5000.0 * (sp / px)
            fee = max(sell_amt * bt.COMMISSION_RATE, bt.COMMISSION_MIN) + sell_amt * bt.stamp_tax(sdate)
            gain = sell_amt - fee - cost
        total += gain
        n += 1
    return round(total, 2), n


def build(realtime=None, intraday=False):
    """收盘/盘中策略记录重建。

    - 默认（收盘）build()：先补齐指数/动态池数据，再以收盘价完整重放全部策略记录。
    - 盘中 build(realtime=..., intraday=True)：★盘中即时记录（2026-09-03 新增）。
      当日尚未收盘，把 fetch 到的实时行情（open=今开/price=现价/high/low/volume…）叠加成
      当日虚拟 bar 后重放，只落「已发生的开盘成交与持仓预览」行——早盘买入/卖出/换仓
      当天就出现在记录里，不再等收盘补。当日收盘信号尚未判定，故盘中行不含买卖信号，
      收盘后正式 build() 会用当日收盘数据覆盖本日行。
    """
    if not intraday:
        refresh_sh_index()      # ① 上证指数：补齐缺失交易日（持久化；网络不可用则跳过，不阻塞）
    _load_filter_series()   # ② 加载最新指数序列（买入过滤器用，须在 refresh_sh_index 之后）
    if not intraday:
        # ②b 选股池指数已停用（2026-08-27 用户决策：选股池指数意义不大，去掉；BUY_FILTERS 改用 sh 过滤）
        try:
            build_dynamic_pool_index()  # ②c 动态选股池指数（行情轮动自适应成分）增量补齐
        except Exception as e:
            print('build_dynamic_pool_index ERR', type(e).__name__, str(e)[:120])
        _load_filter_series()   # ②d 重新加载（含动态池指数 dp，供过滤器与回测使用）
    _load_adopted_filters()  # ②e 加载历史已落地采用的动态池过滤器（保证重跑幂等）
    stocks = load_all()
    intraday_date = None
    if intraday and realtime:
        today_s = datetime.date.today().strftime('%Y-%m-%d')
        n_ov = 0
        for code, df in list(stocks.items()):
            info = (realtime or {}).get(code)
            if not info or str(info.get('date')) != today_s:
                continue
            try:
                df2, added = advisor._apply_realtime(df, info, today_s, code)
                if added:
                    stocks[code] = df2
                    n_ov += 1
            except Exception as e:
                print('盘中叠加失败', code, str(e)[:80])
        if n_ov:
            intraday_date = today_s
            print('盘中叠加实时行情 %d 只：推进当日已开盘成交入账（早盘买/卖/换仓已进记录，收盘后复核）' % n_ov)
        else:
            print('build(intraday) 未取得当日实时行情，本次仅重放历史，不追加当日行')
    maps = {code: idx_map(df) for code, df in stocks.items()}
    out = {
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'start': START_STR,
        'end': max(calendar_of(stocks)),
        'capital': CAPITAL,
        'pool': [{'code': c, 'name': n} for c, n in STOCKS.items()],
        'note': '股票池已剔除创业板(300)/科创板(688)/ST/退市，仅沪深主板；信号收盘后确定，各策略按最适合自己的成交时点执行（见各策略标注：早盘=次日开盘价、尾盘=次日收盘价，经 timing_backtest 回测选优+策略逻辑综合），卖出后按当日触发候选自动换仓；指数策略含浮亏8%止损/最长持有期；T+1涨跌=选股标的下一个交易日开盘相对信号日收盘的涨跌幅。',
        'strategies': [],
    }
    # 短持仓策略持有期下限（交易日）：策略卖出信号需持有满 N 日才执行；止损信号不受限
    MIN_HOLD = {'S2_MACD金叉KDJ死叉': 10, 'S4_20日线回踩放量': 10}

    def _run_stock(sname):
        sfunc = bt.STRATEGIES[sname]
        afn = advisor.STOCK_ANALYZERS[sname]
        buy_timing, sell_timing = timing_of(sname)
        records, trades = simulate_stock(sname, sfunc, afn, stocks, maps,
                                         buy_timing=buy_timing, sell_timing=sell_timing,
                                         min_hold_days=MIN_HOLD.get(sname, 0),
                                         intraday_date=intraday_date)
        return {
            'name': sname,
            'timing': TIMING_LABEL[(buy_timing, sell_timing)],
            'timing_buy': buy_timing,
            'timing_sell': sell_timing,
            'summary': calc_summary(sname, trades, records),
            'trades': trades,
            'daily': records,
        }

    def _run_index(spec):
        records, trades = simulate_index(*spec)
        return {
            'name': spec[1],
            'timing': '估值收盘判定/估值价成交',
            'timing_buy': 'close',
            'timing_sell': 'close',
            'summary': calc_summary(spec[1], trades, records),
            'trades': trades,
            'daily': records,
        }

    # 指数策略
    idx_specs = [
        ('data/csv/hs300_pe.csv', 'S7_沪深300PE分批低买高卖',
         [(12.5, 'PE<12.5建仓'), (8.75, 'PE<8.75加仓')],
         [(12.5, 'PE>12.5减仓'), (16.25, 'PE>16.25清仓')], '滚动市盈率', None, '沪深300指数', -0.08,
         _q_target),   # 季度估值分位再平衡：避免长期空仓
        ('data/csv/hsly_pb.csv', 'S8_红利低估值分批买入',
         [(0.85, 'PB<0.85建仓'), (0.75, 'PB<0.75加仓')],
         [(0.90, 'PB>0.90减仓'), (1.00, 'PB>1.0清仓')], '市净率', 700, '红利指数', -0.08, None),
    ]
    out['strategies'] = ([_run_stock(s) for s in bt.STRATEGIES]
                         + [_run_index(s) for s in idx_specs])

    # ★ 动态选股池回测 + A/B 落地验证（盘中模式跳过：当日无收盘数据且开销大，收盘正式 build 再做）：
    #   1) backtest_dynamic_pool() 评估候选（dp 放行子集胜率≥当前+3pp 且样本≥10，不改 BUY_FILTERS）
    #   2) 对候选切换 dp_day_up 完整重放，落地胜率 ≥ 当前过滤器落地胜率 +3pp 且样本≥10 才真正采用；
    #      否则回滚（恢复原过滤器与原重放结果）——避免「分组统计≠落地重放」教训
    adopted = []
    if not intraday:
        try:
            candidates = backtest_dynamic_pool(out)
        except Exception as e:
            print('backtest_dynamic_pool ERR', type(e).__name__, str(e)[:120])
            candidates = []
        if candidates:
            print('dynamic_pool A/B 落地验证候选:', candidates)
            for i, s in enumerate(out['strategies']):
                if s['name'] not in candidates:
                    continue
                cur_spec = BUY_FILTERS.get(s['name'])
                cur_sum = s['summary']
                cur_win = cur_sum.get('win_rate')
                BUY_FILTERS[s['name']] = ('dp_day_up', '动态选股池当日上涨')
                new = _run_stock(s['name']) if s['name'] in bt.STRATEGIES else \
                    _run_index(next(sp for sp in idx_specs if sp[1] == s['name']))
                new_win = new['summary'].get('win_rate')
                new_n = new['summary'].get('trades', 0)
                ok = (cur_win is not None and new_win is not None and new_n >= 10
                      and new_win >= cur_win + 3.0)
                if ok:
                    out['strategies'][i] = new
                    adopted.append(s['name'])
                    print(f'  ✓ {s["name"]} 落地验证通过：胜率 {cur_win}%→{new_win}%'
                          f'（样本{new_n}），采用 dp_day_up')
                else:
                    BUY_FILTERS[s['name']] = cur_spec  # 回滚
                    print(f'  ✗ {s["name"]} 落地验证未达标准：胜率 {cur_win}%→{new_win}%'
                          f'（样本{new_n}），回滚为 {cur_spec}')
        try:
            rep = json.load(open(DYNAMIC_POOL_BT_OUT, encoding='utf-8'))
            rep['adopted'] = [a for a in rep.get('adopted', []) if a['strategy'] in adopted]
            for a in rep['adopted']:
                i = next((j for j, s in enumerate(out['strategies'])
                          if s['name'] == a['strategy']), None)
                if i is not None:
                    a['landed_win_rate'] = out['strategies'][i]['summary'].get('win_rate')
                    a['landed_n'] = out['strategies'][i]['summary'].get('trades')
            rep['landed'] = True
            with open(DYNAMIC_POOL_BT_OUT, 'w', encoding='utf-8') as f:
                json.dump(rep, f, ensure_ascii=False)
        except Exception as e:
            print('dynamic_pool_bt 回写 adopted 失败', str(e)[:100])
    # ★ 2026-09-07 护栏：S16 整史账本防回退。盘中/异常上下文下若 S16 龙头映射未就绪，
    #   会表现为"整史成交数=0、每日全空仓"且无任何报错地覆盖磁盘 json（2026-09-07 13:07
    #   盘中构建已踩坑一次，页面总账因此停在 9/2）。→ 新结果 S16 成交数为 0、而磁盘上一版
    #   有完整成交时，沿用上一版完整记录，留待收盘正式 build() 以当日数据覆盖。
    if os.path.exists(OUT):
        try:
            _prev = json.load(open(OUT, encoding='utf-8'))
            _prev_by = {s['name']: s for s in _prev.get('strategies', [])}
        except Exception:
            _prev_by = {}
        for g in out['strategies']:
            if g['name'].startswith('S16') and len(g.get('trades') or []) == 0:
                p = _prev_by.get(g['name'])
                if p and len(p.get('trades') or []) > 0:
                    print(f"★[{g['name']}] 本次构建成交数为0（{'盘中' if intraday else '重建'}上下文疑似信号全空），"
                          f'沿用上一收盘版完整账本 trades={len(p["trades"])}；收盘正式 build 将覆盖')
                    g['daily'] = p.get('daily', [])
                    g['trades'] = p['trades']
                    g['summary'] = p['summary']
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('built', OUT, 'strategies=', len(out['strategies']),
          'total_trades=', sum(len(s['trades']) for s in out['strategies']),
          'dp_adopted=', adopted or '无')
    return out


POOL_INDEX_OUT = 'data/pool_index.json'


def build_pool_index(incremental=True):
    """按市值加权指数算法（流通股本近似）合成/增量补齐「选股池指数」：
        报告期指数 = Σ(当日收盘价 × 权重股本) / Σ(基日收盘价 × 权重股本) × 100
    口径说明：
    - 权重股本：以「换手率」估算的流通股本（流通股本 = 成交量 / 换手率），取数据尾段中位数。
      注意：与上证综指「发行总股本加权」口径不同——本池无总股本数据，以流通股本近似，
      同为市值加权类指数，但权重口径有差异（非严格上证口径）。
    - 基日 = 股票池首个完整交易日（2018-01-02），基期 100 点。
    - 成分固定为股票池 15 只、无调仓，除数恒为基期总市值，故无需"除数修正法"。
    - 停牌/缺失价格沿用上一交易日收盘价（与上证指数一致）。
    - ★持久化（incremental=True，默认）：历史指数已固定——权重股本、基期、历史点位一律复用
      已生成的 data/pool_index.json，只按股票日线增量补算【新增交易日】；
      无新增日期时直接复用原文件（幂等，历史不会因重跑而漂移）。
    结果写入 data/pool_index.json。"""
    hist = None
    if incremental and os.path.exists(POOL_INDEX_OUT):
        try:
            hist = json.load(open(POOL_INDEX_OUT, encoding='utf-8'))
        except Exception:
            hist = None
    dfs = []
    for code, name in bt.STOCKS.items():
        df = bt.load_stock(code)
        df = df[['日期', '收盘', '成交量', '换手率']].sort_values('日期').reset_index(drop=True)
        dfs.append((code, name, df))
    if hist and hist.get('weights'):
        # 1) 复用已固定的权重股本（历史指数不漂移）
        weights = {c: float(w) for c, w in hist['weights'].items()}
    else:
        # 1) 估算权重股本（尾段中位数，剔除 0/NaN）
        weights = {}
        for code, name, df in dfs:
            r = (df['成交量'] * 100.0) / (df['换手率'] / 100.0)  # 成交量(手)*100 = 股
            r = r[np.isfinite(r) & (r > 0)]
            weights[code] = float(r.median()) if len(r) else 0.0
        hist = None  # 权重无法复用 → 走全量合成
    all_dates = sorted(set(d for _, _, df in dfs for d in df['日期']))
    if hist:
        # ---- 增量：只补算 股票数据中有、且晚于指数末日的 新交易日 ----
        last_hist = pd.Timestamp(hist['series'][-1]['date'])
        new_dates = [d for d in all_dates if d > last_hist]
        if not new_dates:
            print('pool_index 已是最新（', hist['series'][-1]['date'],
                  '），历史已固定，直接复用')
            return hist
        # 各股在指数末日的最新收盘价（增量期间停牌沿用前收的起点）
        last_px = {}
        for code, _, df in dfs:
            sub = df.loc[df['日期'] <= last_hist, '收盘']
            last_px[code] = float(sub.iloc[-1]) if len(sub) else None
        # 由历史末点位反推基期总市值：base_mcap = mcap_last * 100 / close_last
        mcap_last = sum((last_px.get(c) or 0.0) * weights.get(c, 0.0) for c in weights)
        base_mcap = mcap_last * 100.0 / float(hist['series'][-1]['close'])
        base_date = hist['base_date']
        series = list(hist['series'])
        for d in new_dates:
            mcap = 0.0
            for code, _, df in dfs:
                sub = df.loc[df['日期'] == d]
                if len(sub):
                    px = float(sub['收盘'].iloc[0])
                    last_px[code] = px
                else:
                    px = last_px.get(code)
                if px is None or weights.get(code, 0) <= 0:
                    continue
                mcap += px * weights[code]
            if mcap > 0:
                series.append({'date': d.strftime('%Y-%m-%d'),
                               'close': round(mcap / base_mcap * 100.0, 2)})
    else:
        # ---- 全量合成 ----
        mcap_series, last_px = [], {}
        for d in all_dates:
            mcap = 0.0
            for code, _, df in dfs:
                sub = df.loc[df['日期'] == d]
                if len(sub):
                    px = float(sub['收盘'].iloc[0])
                    last_px[code] = px
                else:
                    px = last_px.get(code)
                if px is None or weights.get(code, 0) <= 0:
                    continue
                mcap += px * weights[code]
            if mcap > 0:
                mcap_series.append((d, mcap))
        base_date = mcap_series[0][0].strftime('%Y-%m-%d')
        base_mcap = mcap_series[0][1]
        series = [{'date': d.strftime('%Y-%m-%d'),
                   'close': round(m / base_mcap * 100.0, 2)} for d, m in mcap_series]
    out = {
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'name': '选股池指数（市值加权·流通股本近似）',
        'base_date': base_date,
        'base': 100,
        'n_stocks': len(dfs),
        'series': series,
        'weights': {c: round(weights[c], 0) for c in weights},
    }
    with open(POOL_INDEX_OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('built', POOL_INDEX_OUT, 'points=', len(series),
          'last=', series[-1]['date'], series[-1]['close'],
          'base=', base_date)
    return out


# ================= 动态选股池指数（行情轮动自适应） =================

def _dp_score(k, df):
    """动态选股得分 = 0.5·动量(近20日涨幅) + 0.3·趋势(站上MA20幅度) + 0.2·均线斜率(MA20较5日前)。
    指标自算（不依赖数据列的 MA 指标）；k<24（均线斜率需要 24 日历史）或价格异常 → None。"""
    try:
        c = float(df.loc[k, '收盘'])
        if k < 24 or c <= 0:
            return None
        c20 = float(df.loc[k - 20, '收盘'])
        ma20 = float(df.loc[k - 19:k + 1, '收盘'].mean())
        ma20_5 = float(df.loc[k - 24:k - 4, '收盘'].mean())  # 5 个交易日前收盘的 20 日均线
        if c20 <= 0 or ma20 <= 0 or ma20_5 <= 0:
            return None
        return 0.5 * (c / c20 - 1) + 0.3 * (c / ma20 - 1) + 0.2 * (ma20 / ma20_5 - 1)
    except Exception:
        return None


def _dp_select(d, stocks, maps):
    """在候选（沪深主板 STOCKS）中按得分选出 top_n 只最强势标的作为动态池成分；
    可评分候选不足 min_stocks 时全部纳入，仍为空（如基期数据不足）则回退全候选。"""
    scored = []
    for code in STOCKS:
        if code not in stocks:
            continue
        k = maps[code].get(d)
        if k is None:
            continue
        sc = _dp_score(k, stocks[code])
        if sc is not None:
            scored.append((sc, code))
    scored.sort(key=lambda x: -x[0])
    if len(scored) < DYNAMIC_POOL_CFG['min_stocks']:
        return [c for _, c in scored] or list(STOCKS)
    return [c for _, c in scored[:DYNAMIC_POOL_CFG['top_n']]]


def build_dynamic_pool_index(incremental=True):
    """合成/增量补齐「动态选股池指数」（链式市值加权，成分随行情轮动）：
    - 候选：沪深主板股票池 STOCKS（已剔除创业板/科创板/北交所/ST/退市）。
    - 选池：每个调仓日按「趋势+动量」得分选出 top_n 只最强势标的，行情轮动自动换血。
    - 指数：链式 Laspeyres 加权 —— 指数_d = 指数_{d-1} × Σ(w·p_d)/Σ(w·p_{d-1})，
      调仓日成分重选后立即生效，链式衔接保证指数连续（无需除数修正法）。
      权重股本口径与 build_pool_index 相同（换手率估算流通股本，尾段中位数）。
    - 停牌/缺失价格沿用前收；调仓日、历史点位固定持久化，增量只补算新增交易日。
    结果写入 data/dynamic_pool_index.json。"""
    hist = None
    if incremental and os.path.exists(DYNAMIC_POOL_OUT):
        try:
            hist = json.load(open(DYNAMIC_POOL_OUT, encoding='utf-8'))
        except Exception:
            hist = None
    dfs = []
    for code, name in bt.STOCKS.items():
        df = bt.load_stock(code)
        cols = [c for c in ['日期', '收盘', '成交量', '换手率', 'MA20'] if c in df.columns]
        df = df[cols].sort_values('日期').reset_index(drop=True)
        dfs.append((code, name, df))
    if hist and hist.get('weights'):
        weights = {c: float(w) for c, w in hist['weights'].items()}
    else:
        weights = {}
        for code, _, df in dfs:
            if '换手率' not in df.columns or '成交量' not in df.columns:
                weights[code] = 0.0
                continue
            r = (df['成交量'] * 100.0) / (df['换手率'] / 100.0)
            r = r[np.isfinite(r) & (r > 0)]
            weights[code] = float(r.median()) if len(r) else 0.0
        hist = None
    all_dates = sorted(set(d for _, _, df in dfs for d in df['日期']))
    step = max(DYNAMIC_POOL_CFG['rebalance_days'], 1)
    rebal_dates = set(d for i, d in enumerate(all_dates) if i % step == 0)
    stocks = {}
    maps = {}
    for code, _, df in dfs:
        stocks[code] = df
        maps[code] = {d: k for k, d in enumerate(df['日期'])}
    last_px, idx_prev = {}, None

    if hist:
        last_hist = pd.Timestamp(hist['series'][-1]['date'])
        new_dates = [d for d in all_dates if d > last_hist]
        if not new_dates:
            print('dynamic_pool_index 已是最新（', hist['series'][-1]['date'],
                  '），历史已固定，直接复用')
            return hist
        # 指数末日各股最新收盘（增量起点的前收）；当前生效成分 = 最近一次调仓
        for code, _, df in dfs:
            sub = df.loc[df['日期'] <= last_hist, '收盘']
            last_px[code] = float(sub.iloc[-1]) if len(sub) else None
        idx_prev = float(hist['series'][-1]['close'])
        codes = list(hist['rebalances'][-1]['codes'])
        rebalances = list(hist['rebalances'])
        series = list(hist['series'])
        for d in new_dates:
            if d in rebal_dates:
                codes = _dp_select(d, stocks, maps)
                rebalances.append({'date': d.strftime('%Y-%m-%d'), 'codes': codes})
            today_px = {}
            for code in STOCKS:
                if code not in stocks:
                    continue
                sub = stocks[code].loc[stocks[code]['日期'] == d]
                if len(sub):
                    today_px[code] = float(sub['收盘'].iloc[0])
                elif code in last_px:
                    today_px[code] = last_px[code]
            mcap_num = sum(today_px.get(c, 0.0) * weights.get(c, 0.0) for c in codes
                           if weights.get(c, 0) > 0)
            mcap_den = sum(last_px.get(c, 0.0) * weights.get(c, 0.0) for c in codes
                           if weights.get(c, 0) > 0)
            idx = idx_prev * (mcap_num / mcap_den) if mcap_den > 0 else idx_prev
            series.append({'date': d.strftime('%Y-%m-%d'), 'close': round(idx, 2)})
            idx_prev, last_px = idx, today_px
    else:
        # ---- 全量合成（链式）----
        series, rebalances, codes = [], [], []
        for i, d in enumerate(all_dates):
            if d in rebal_dates:
                codes = _dp_select(d, stocks, maps)
                rebalances.append({'date': d.strftime('%Y-%m-%d'), 'codes': codes})
            today_px = {}
            for code in STOCKS:
                if code not in stocks:
                    continue
                sub = stocks[code].loc[stocks[code]['日期'] == d]
                if len(sub):
                    today_px[code] = float(sub['收盘'].iloc[0])
                elif code in last_px:
                    today_px[code] = last_px[code]
            if i == 0:
                idx_prev = 100.0
                last_px = today_px
                series.append({'date': d.strftime('%Y-%m-%d'), 'close': 100.0})
                continue
            mcap_num = sum(today_px.get(c, 0.0) * weights.get(c, 0.0) for c in codes
                           if weights.get(c, 0) > 0)
            mcap_den = sum(last_px.get(c, 0.0) * weights.get(c, 0.0) for c in codes
                           if weights.get(c, 0) > 0)
            idx = idx_prev * (mcap_num / mcap_den) if mcap_den > 0 else idx_prev
            series.append({'date': d.strftime('%Y-%m-%d'), 'close': round(idx, 2)})
            idx_prev, last_px = idx, today_px
    base_date = all_dates[0].strftime('%Y-%m-%d')
    out = {
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'name': '动态选股池指数（链式市值加权·行情轮动自适应）',
        'base_date': base_date,
        'base': 100,
        'cfg': DYNAMIC_POOL_CFG,
        'n_candidates': len(STOCKS),
        'series': series,
        'rebalances': rebalances,
        'weights': {c: round(weights[c], 0) for c in weights},
    }
    with open(DYNAMIC_POOL_OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('built', DYNAMIC_POOL_OUT, 'points=', len(series),
          'last=', series[-1]['date'], series[-1]['close'],
          'rebalances=', len(rebalances))
    return out


def analyze_market():
    """当前行情判断 + 明日行情预判（纯本地、秒级）：
    数据源：上证指数日线（量价全）+ 选股池/动态池指数 + 动态池当前成分涨跌。
    1) 当前形态：当日涨跌/量比(量vs MA5)/相对MA20乖离/RSI14/连涨连跌 → 形态标签
       （缩量回调、放量长阴、超跌、放量长阳、缩量横盘、缩量反弹、放量上涨…）。
    2) 明日预判：近10年找「涨跌幅度+量比+MA20乖离+方向」相似的历史交易日，
       统计次日「强修复(≥+0.5%)/弱修复(0~+0.5%)/回落(<0)」概率与平均次日涨跌；
       再按超买超卖、MA20位置、动态池相对强弱(结构性)修正概率，输出预判结论。
    返回 dict；数据缺失时返回仅含 asof=None 的最小结构。"""
    import numpy as np
    out = {'asof': None}
    try:
        sh = pd.read_csv(SH_INDEX_CSV, encoding='utf-8-sig')
        sh['date'] = pd.to_datetime(sh['date'])
        sh = sh.sort_values('date').reset_index(drop=True)
    except Exception:
        return out
    if len(sh) < 30:
        return out
    c, v = sh['close'].astype(float), sh['volume'].astype(float)
    sh['ma5'] = c.rolling(5).mean()
    sh['ma20'] = c.rolling(20).mean()
    sh['ma60'] = c.rolling(60).mean()
    sh['v5'] = v.rolling(5).mean()
    up = c.diff().clip(lower=0)
    dn = -c.diff().clip(upper=0)
    sh['rsi14'] = 100 - 100 / (1 + up.ewm(alpha=1 / 14, adjust=False).mean()
                               / dn.ewm(alpha=1 / 14, adjust=False).mean())
    last = sh.iloc[-1]
    asof = last['date'].strftime('%Y-%m-%d')
    out['asof'] = asof
    # 预判目标日 = asof 之后的下一个交易日。系统无节假日表，按「周一~五」推进近似：
    # 盘中未收盘时 asof=上一交易日 → 目标即今日；收盘后 asof=当日 → 目标为次一交易日。
    tgt = last['date'] + pd.Timedelta(days=1)
    while tgt.weekday() >= 5:
        tgt += pd.Timedelta(days=1)
    out['target'] = tgt.strftime('%Y-%m-%d')
    chg = float(c.iloc[-1] / c.iloc[-2] - 1) * 100
    vr5 = float(v.iloc[-1] / sh['v5'].iloc[-1]) if pd.notna(sh['v5'].iloc[-1]) else 1.0
    ma20 = float(sh['ma20'].iloc[-1]) if pd.notna(sh['ma20'].iloc[-1]) else None
    dist20 = (c.iloc[-1] / ma20 - 1) * 100 if ma20 else 0.0
    rsi14 = float(sh['rsi14'].iloc[-1]) if pd.notna(sh['rsi14'].iloc[-1]) else None
    d = c.diff().dropna()
    streak = 0
    if len(d):
        sign = 1 if d.iloc[-1] > 0 else (-1 if d.iloc[-1] < 0 else 0)
        for x in d.iloc[::-1]:
            s_ = 1 if x > 0 else (-1 if x < 0 else 0)
            if s_ == sign and s_ != 0:
                streak += 1
            else:
                break
        streak = streak * sign  # 带符号：连涨为正、连跌为负
    # 形态标签
    if chg <= -0.5 and vr5 < 0.8:
        label = '缩量回调'
    elif chg <= -1.5 and vr5 > 1.5:
        label = '放量长阴'
    elif chg >= 1.5 and vr5 > 1.5:
        label = '放量长阳'
    elif chg <= -2.0 and dist20 < -5:
        label = '超跌'
    elif abs(chg) <= 0.4 and vr5 < 0.8:
        label = '缩量横盘'
    elif chg >= 0.5 and vr5 < 0.8:
        label = '缩量反弹'
    elif chg >= 0.5 and vr5 >= 1.2:
        label = '放量上涨'
    else:
        label = '普通波动'
    # 明日预判：近10年相似样本
    win = sh.iloc[:-1].copy()
    win = win[win['date'] >= last['date'] - pd.Timedelta(days=365 * 10)].copy()
    win['chg'] = (win['close'] / win['close'].shift(1) - 1) * 100
    win['next_chg'] = (win['close'].shift(-1) / win['close'] - 1) * 100
    win['vr'] = win['volume'] / win['v5']
    win['dist20'] = (win['close'] / win['ma20'] - 1) * 100
    win = win.dropna(subset=['chg', 'next_chg', 'vr', 'dist20'])
    m = (win['chg'].between(chg - 0.35, chg + 0.35)
         & win['vr'].between(vr5 - 0.15, vr5 + 0.15)
         & win['dist20'].between(dist20 - 2.0, dist20 + 2.0)
         & (win['chg'] * chg >= 0))
    sim = win[m]
    n_sim = len(sim)
    if n_sim:
        avg_next = round(float(sim['next_chg'].mean()), 2)
        strong = round(float((sim['next_chg'] >= 0.5).mean()) * 100, 1)
        weak = round(float(((sim['next_chg'] >= 0) & (sim['next_chg'] < 0.5)).mean()) * 100, 1)
        down = round(float((sim['next_chg'] < 0).mean()) * 100, 1)
        recent = [(r.date.strftime('%Y-%m-%d'), round(float(r.chg), 2),
                   round(float(r.next_chg), 2)) for r in sim.tail(3).itertuples()]
    else:
        avg_next = strong = weak = down = None
        recent = []
    # 动态池相对强弱（结构性）与市场宽度
    dyn_chg = pool_chg = rel = None
    try:
        dj = json.load(open(DYNAMIC_POOL_OUT, encoding='utf-8'))
        ds = dj['series']
        dyn_chg = round((ds[-1]['close'] / ds[-2]['close'] - 1) * 100, 2)
        dyn_asof = ds[-1]['date']
    except Exception:
        dyn_asof = None
    try:
        pj = json.load(open(POOL_INDEX_OUT, encoding='utf-8'))
        ps = pj['series']
        pool_chg = round((ps[-1]['close'] / ps[-2]['close'] - 1) * 100, 2)
    except Exception:
        pass
    if dyn_chg is not None:
        rel = round(dyn_chg - chg, 2)
    # 修正：超买超卖 / 跌破MA20 / 动态池相对强弱
    adj_strong = adj_down = 0.0
    if rsi14 is not None:
        if rsi14 < 25:
            adj_strong += 8
        elif rsi14 > 75:
            adj_down += 8
    if dist20 < -5:
        adj_strong += 6
    elif dist20 > 6:
        adj_down += 5
    if rel is not None:
        if rel >= 0.5:
            adj_strong += 5
        elif rel <= -0.5:
            adj_down += 5
    if n_sim:
        p_strong = max(0.0, min(95.0, strong + adj_strong))
        p_weak = max(0.0, weak)
        p_down = max(0.0, min(95.0, down + adj_down))
        s = p_strong + p_weak + p_down
        p_strong, p_weak, p_down = p_strong / s * 100, p_weak / s * 100, p_down / s * 100
        if p_strong >= p_weak and p_strong >= p_down:
            verdict = '强修复' if p_strong >= 50 else '偏强修复'
        elif p_weak >= p_down:
            verdict = '弱修复' if p_weak >= 45 else '偏弱修复'
        else:
            verdict = '调整/回落' if p_down >= 50 else '偏弱/调整'
        prob = max(p_strong, p_weak, p_down)
    else:
        p_strong = p_weak = p_down = None
        verdict = f'样本不足，{int(out["target"][5:7])}月{int(out["target"][8:10])}日 预判仅供参考'
        prob = None
    out.update({
        'sh': {'close': round(float(c.iloc[-1]), 2), 'chg': round(chg, 2),
               'vr5': round(vr5, 2), 'dist20': round(dist20, 2),
               'rsi14': None if rsi14 is None else round(rsi14, 1),
               'streak': streak, 'ma20': round(ma20, 2) if ma20 else None},
        'label': label, 'env': market_env_on(asof), 'dyn_chg': dyn_chg, 'dyn_asof': dyn_asof,
        'pool_chg': pool_chg, 'rel': rel,
        'next': {'verdict': verdict, 'prob': None if prob is None else round(prob, 1),
                 'strong': p_strong, 'weak': p_weak, 'down': p_down,
                 'avg_next': avg_next, 'n_sim': n_sim, 'recent': recent},
    })
    return out


def _load_adopted_filters():
    """加载历史已落地采用的动态池过滤器（data/dynamic_pool_bt.json 的 adopted 中
    含 landed_win_rate 的条目）→ 写入 BUY_FILTERS，使重跑 build() 幂等：
    已采用过滤器不再每次重新 A/B 验证（比较基准已变为 dp 自身）。"""
    try:
        rep = json.load(open(DYNAMIC_POOL_BT_OUT, encoding='utf-8'))
        for a in rep.get('adopted', []):
            if a.get('landed_win_rate') is not None and a.get('strategy'):
                BUY_FILTERS[a['strategy']] = ('dp_day_up', '动态选股池当日上涨')
    except Exception:
        pass


def _filter_pass_on_series(s, date, kind):
    """用指数序列 s 判定 kind 过滤器在 date 是否放行；数据缺失/序列不足 → None（不计）。"""
    i = _f_loc(s, date)
    if i is None:
        return None
    if kind.endswith('_day_up'):
        if i < 1:
            return None
        return bool(s.iloc[i] > s.iloc[i - 1])
    if kind.endswith('_gt_ma20'):
        if i < 19:
            return None
        return bool(s.iloc[i] > float(s.iloc[i - 19:i + 1].mean()))
    if kind.startswith('sh_rsi_lt') or kind.startswith('dp_rsi_lt'):
        if i < 14:
            return None
        try:
            thr = float(kind.rsplit('_', 1)[1])
        except ValueError:
            return None
        chgs = s.iloc[i - 14:i + 1].diff().dropna()
        up = float(chgs[chgs > 0].sum())
        dn = float(-chgs[chgs < 0].sum())
        if up + dn <= 0:
            return None
        return bool(100 * up / (up + dn) < thr)
    return None


def _signal_date(daily, trade):
    """由成交记录反推买入信号触发日：普通建仓=买入日前最近一条「买入/建仓」记录；
    换仓/同日触发 = 买入日当天。"""
    bd = str(trade.get('buy_date', ''))[:10]
    i = None
    for j, r in enumerate(daily):
        if str(r.get('date', ''))[:10] == bd:
            i = j
            break
    if i is None:
        return bd
    for j in range(i - 1, max(i - 6, -1), -1):
        st = daily[j].get('status', '')
        if '买入' in st or '建仓' in st:
            return str(daily[j].get('date', ''))[:10]
    return bd


def backtest_dynamic_pool(out=None, min_n=10):
    """回测动态选股池过滤器（dp_day_up）对 10 策略信号胜率/收益的影响：
    对每笔历史成交，反推信号日，统计各过滤器（不过滤/固定池当日上涨/上证当日上涨/
    动态池当日上涨）放行子集的胜率与净盈亏。
    候选规则（评估口径）：dp 放行样本 ≥ min_n(10) 且胜率 ≥ 当前过滤器胜率 +3pp。
    ★ 注意：该口径是「当前成交子集」统计，不等于落地效果——真正的采用必须由
    build() 对候选做 A/B 完整重放验证（切换 dp_day_up 重放，落地胜率 ≥ 当前+3pp
    才保留，否则回滚），避免重蹈「分组统计≠落地重放」的教训。
    对比报告持久化到 data/dynamic_pool_bt.json。返回候选策略名列表（不修改 BUY_FILTERS）。"""
    if out is None:
        out = json.load(open(OUT, encoding='utf-8'))
    report = {
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'cfg': DYNAMIC_POOL_CFG,
        'note': '动态选股池指数辅助：行情轮动自适应成分；dp_day_up=动态池当日上涨过滤器。'
                '候选规则=样本≥10且评估胜率≥当前过滤器+3pp；最终采用需 A/B 落地重放验证'
                '（落地胜率≥当前+3pp 才保留，否则回滚），结果见 adopted。',
        'strategies': [],
        'adopted': [],
    }
    changed = []
    kinds = [('none', '不过滤'), ('pi_day_up', '固定池当日上涨'),
             ('sh_day_up', '上证当日上涨'), ('dp_day_up', '动态池当日上涨')]
    for st in out['strategies']:
        sname = st['name']
        daily, trades = st['daily'], st['trades']
        if not trades:
            report['strategies'].append({'name': sname, 'rows': {}, 'cur_filter': None,
                                         'adopt_dp': False, 'reason': '无成交'})
            continue
        rows = {}
        for kind, label in kinds:
            n = w = 0
            net = 0.0
            for t in trades:
                sd = _signal_date(daily, t)
                if kind == 'none':
                    ok = True
                else:
                    s = FILTER_CTX['pi' if kind.startswith('pi') else (
                        'sh' if kind.startswith('sh') else 'dp')]
                    ok = _filter_pass_on_series(s, sd, kind) if s is not None else None
                if ok is None:
                    continue
                if ok:
                    n += 1
                    if float(t.get('net_pnl', 0)) > 0:
                        w += 1
                    net += float(t.get('net_pnl', 0))
            rows[kind] = {'n': n, 'win_rate': round(w / n * 100, 1) if n else None,
                          'sum_net': round(net, 2)}
        cur_spec = BUY_FILTERS.get(sname)
        cur_kind = cur_spec[0] if cur_spec else 'none'
        base_rate = rows.get(cur_kind, {}).get('win_rate')
        if base_rate is None:
            base_rate = rows['none']['win_rate']
        dp_rate = rows['dp_day_up']['win_rate']
        adopt = (dp_rate is not None and base_rate is not None
                 and rows['dp_day_up']['n'] >= min_n
                 and dp_rate >= base_rate + 3.0)
        if adopt:
            report['adopted'].append({'strategy': sname, 'from': cur_spec,
                                      'dp_win_rate': dp_rate, 'base_win_rate': base_rate,
                                      'dp_n': rows['dp_day_up']['n']})
            changed.append(sname)
        report['strategies'].append({
            'name': sname, 'rows': rows, 'cur_filter': cur_spec, 'adopt_dp': adopt,
            'reason': (f'候选：评估dp胜率{dp_rate}%≥当前{base_rate}%+3pp、样本{rows["dp_day_up"]["n"]}≥{min_n}'
                       if adopt else '未达候选'),
        })
    with open(DYNAMIC_POOL_BT_OUT, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False)
    print('[dynamic_pool_bt] 候选（待A/B落地验证）:', changed or '无')
    return changed


SH_INDEX_CSV = os.path.join(_WORKSPACE, 'data/csv/sh000001.csv')


def _fetch_sh_range(start, end):
    """从新浪行情接口拉取上证指数，筛出 [start, end] 闭区间日线，返回
    [[date, open, high, low, close, volume], ...]（6 位小数、含 volume，与原 csv 格式一致）
    或 None（网络/解析失败）。新浪接口只支持按 datalen 取最近 N 条，故按
    start 距今自然日估算条数（上限 1023 条 ≈ 4 年，超过则打印提示）。
    注意：闭区间，start==end（仅缺一天）时也能筛出当日数据。"""
    import urllib.request, re
    days = (datetime.date.today() - datetime.date.fromisoformat(start)).days + 5
    n = min(max(days, 5), 1023)
    url = ('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_='
           f'/CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen={n}')
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            txt = r.read().decode('utf-8', errors='replace')
        m = re.search(r'=\(\[(.*)\]\);?', txt, re.S)
        if not m:
            print(f'[sh_index] 新浪返回格式异常（len={len(txt)}）')
            return None
        data = json.loads('[' + m.group(1) + ']')
        rows = []
        for x in data:
            d = str(x.get('day', ''))
            if start <= d <= end:
                rows.append([d, float(x['open']), float(x['high']), float(x['low']),
                             float(x['close']), int(float(x.get('volume', 0)))])
        return rows or None
    except Exception as e:
        print(f'[sh_index] 拉取失败 {start}~{end}: {str(e)[:80]}')
        return None


def refresh_sh_index():
    """持久化上证指数（data/csv/sh000001.csv）：历史已固定不重拉，只补齐缺失交易日。
    若文件末日 < 今日，按 120 天分块从腾讯接口拉取缺失区间并写回（utf-8-sig，与原格式一致）。
    网络不可用/文件缺失时仅打印日志并返回 False（过滤器将放行，不阻塞回放）。"""
    import csv as _csv
    path = SH_INDEX_CSV
    if not os.path.exists(path):
        print('[sh_index] 缺失文件，跳过自动补齐（过滤器将放行）')
        return False
    rows = []
    with open(path, encoding='utf-8-sig') as f:
        rd = _csv.reader(f)
        header = next(rd, None)
        rows = [r for r in rd if len(r) >= 5 and r[0]]
    if not rows:
        return False
    last = max(r[0] for r in rows)
    # 补齐截止日：已收盘（>=15:00 交易日）→ 今日收盘值；未收盘/非交易日 → 上一交易日，
    # 避免把盘中"半成品"写入持久化（今日数据等收盘后由扫描或下次手动重跑补齐）。
    now = datetime.datetime.now()
    if now.hour >= 15 and now.weekday() < 5:
        cutoff = now.date()
    else:
        cutoff = now.date() - datetime.timedelta(days=1)
    cutoff = cutoff.strftime('%Y-%m-%d')
    if last >= cutoff:
        print('[sh_index] 已最新（', last, '）')
        return True
    added = []
    cur = last
    while cur < cutoff:
        s = (datetime.date.fromisoformat(cur) + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        e = (datetime.date.fromisoformat(s) + datetime.timedelta(days=120)).strftime('%Y-%m-%d')
        if e > cutoff:
            e = cutoff
        got = _fetch_sh_range(s, e)
        if not got:
            print('[sh_index] 网络不可用，暂无法补齐（当前末日', last, '）')
            return False
        for g in got:
            if g[0] > last:
                rows.append(g)
                last = g[0]
                added.append(g)
        cur = last
    # 去重 + 按日期排序写回
    seen = {}
    for r in rows:
        seen[r[0]] = r
    rows = [seen[d] for d in sorted(seen)]
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = _csv.writer(f)
        w.writerow(header or ['date', 'open', 'high', 'low', 'close', 'volume'])
        w.writerows(rows)
    print(f'[sh_index] 补齐 {len(added)} 天: {added[0][0]} ~ {added[-1][0]}')
    return True


if __name__ == '__main__':
    build()  # 选股池指数已移除（2026-08-27 用户决策：意义不大），仅 build() 全流程
