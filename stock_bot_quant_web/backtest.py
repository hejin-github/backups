# -*- coding: utf-8 -*-
"""A股方法论回测引擎：5000元/笔，支持8个策略"""
import sys, io, os, math, json, time
if sys.stdout is not None and getattr(sys.stdout, 'buffer', None) is not None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd
import numpy as np
try:
    import chan_core as _cc
except Exception:
    _cc = None  # 缠论策略依赖 chan_core.py，缺失时该策略自动失效但不影响其他策略

# 数据目录统一锚定到脚本所在根目录（D:/codebuddy_workspace），避免被服务进程 CWD（如 stock_bot）改变
DATA_ROOT = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(DATA_ROOT, 'data'), exist_ok=True)

COMMISSION_RATE = 0.00025   # 佣金万2.5
COMMISSION_MIN = 5.0        # 最低5元
STAMP_TAX_BEFORE = 0.001    # 印花税（卖出）2023-08-27前
STAMP_TAX_AFTER = 0.0005    # 印花税（卖出）2023-08-28后
STAMP_CUTOFF = pd.Timestamp('2023-08-28')
TRANSFER_RATE = 0.00001     # 过户费（双向，沪市为主）

# 主板选股池 56 只，覆盖约 23 个板块（无创业板/科创板/北交所/ST/退市）。
# 扩充来源：akshare 拉取前复权日线 2018 起（data/csv/{code}.csv，12 列与存量一致）。
STOCKS = {
    # —— 银行 ——
    '601398': '工商银行', '601288': '农业银行', '600036': '招商银行',
    '601988': '中国银行', '601166': '兴业银行', '000001': '平安银行',
    # —— 保险 ——
    '601318': '中国平安', '601601': '中国太保', '601628': '中国人寿',
    # —— 券商 ——
    '600030': '中信证券', '601688': '华泰证券', '600999': '招商证券',
    # —— 白酒/消费 ——
    '600519': '贵州茅台', '000858': '五粮液', '000568': '泸州老窖',
    '601888': '中国中免', '600887': '伊利股份',
    # —— 家电 ——
    '000651': '格力电器', '000333': '美的集团', '600690': '海尔智家',
    # —— 医药 ——
    '600276': '恒瑞医药', '600085': '同仁堂', '000538': '云南白药', '600196': '复星医药',
    # —— 新能源/光伏 ——
    '601012': '隆基绿能', '600438': '通威股份',
    # —— 汽车 ——
    '600104': '上汽集团', '000625': '长安汽车', '601633': '长城汽车',
    # —— 石油/煤炭/有色 ——
    '600028': '中国石化', '601857': '中国石油', '601088': '中国神华',
    '601899': '紫金矿业', '601600': '中国铝业',
    # —— 钢铁/化工/建材 ——
    '600010': '包钢股份', '600019': '宝钢股份', '600309': '万华化学', '600585': '海螺水泥',
    # —— 建筑/基建 ——
    '601668': '中国建筑', '601390': '中国中铁', '601186': '中国铁建',
    # —— 地产 ——
    '000002': '万科A',
    # —— 电力/公用 ——
    '600900': '长江电力', '601985': '中国核电', '600011': '华能国际', '600886': '国投电力',
    # —— 交运 ——
    '601006': '大秦铁路', '600018': '上港集团',
    # —— 通信/科技 ——
    '600941': '中国移动', '000063': '中兴通讯', '600703': '三安光电',
    # —— 电子/半导体 ——
    '000725': '京东方A', '002415': '海康威视', '603986': '兆易创新', '002371': '北方华创',
    # —— 军工 ——
    '600893': '航发动力', '600760': '中航沈飞',
    # —— 机械/高端制造 ——
    '600031': '三一重工', '601766': '中国中车',
    # —— 农业 ——
    '600598': '北大荒',
}

# ---- 动态选股池：优先加载 data/stock_universe.csv（全市场主板，已剔除 ST/退市/科创/创业/北交）；缺失时回退内置精选池 ----
def _load_universe():
    try:
        u = pd.read_csv(os.path.join(DATA_ROOT, 'data', 'stock_universe.csv'), encoding='utf-8-sig')
        s = {str(r['code']).zfill(6): str(r['name']) for _, r in u.iterrows()}
        if len(s) >= 100:
            return s
    except Exception:
        pass
    return None

_UNIVERSE = _load_universe()
if _UNIVERSE:
    STOCKS = _UNIVERSE

def load_stock(code):
    df = pd.read_csv(os.path.join(DATA_ROOT, 'data', 'csv', f'{code}.csv'), encoding='utf-8-sig')
    df['日期'] = pd.to_datetime(df['日期'])
    df = df.sort_values('日期').reset_index(drop=True)
    df.attrs['_code'] = code  # 供缠论等策略按 code 缓存信号（跨请求命中）
    return df

def add_indicators(df):
    c, h, l, v = df['收盘'], df['最高'], df['最低'], df['成交量']
    df['ma5'] = c.rolling(5).mean()
    df['ma10'] = c.rolling(10).mean()
    df['ma20'] = c.rolling(20).mean()
    df['ma60'] = c.rolling(60).mean()
    df['ema10'] = c.ewm(span=10, adjust=False).mean()
    df['ema20'] = c.ewm(span=20, adjust=False).mean()
    # MACD(12,26,9)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    df['dif'] = dif
    df['dea'] = dea
    df['macd_hist'] = (dif - dea) * 2
    # KDJ(9,3,3)
    llv = l.rolling(9).min()
    hhv = h.rolling(9).max()
    rsv = (c - llv) / (hhv - llv).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    df['k'] = k
    df['d'] = d
    # BOLL(20,2)
    std20 = c.rolling(20).std()
    df['boll_mid'] = df['ma20']
    df['boll_up'] = df['ma20'] + 2 * std20
    df['boll_low'] = df['ma20'] - 2 * std20
    # ATR(14)
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df['atr14'] = tr.rolling(14).mean()
    # RSI(14)
    diff = c.diff()
    gain = diff.where(diff > 0, 0.0)
    loss = (-diff).where(diff < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df['rsi14'] = 100 - 100 / (1 + rs)
    # 量能
    df['vol_ma5'] = v.rolling(5).mean()
    return df

def stamp_tax(date):
    return STAMP_TAX_AFTER if date >= STAMP_CUTOFF else STAMP_TAX_BEFORE

def buy_cost(price, shares):
    amt = price * shares
    return amt + max(amt * COMMISSION_RATE, COMMISSION_MIN) + amt * TRANSFER_RATE

def sell_net(price, shares, date):
    amt = price * shares
    fee = max(amt * COMMISSION_RATE, COMMISSION_MIN) + amt * stamp_tax(date) + amt * TRANSFER_RATE
    return amt - fee

# ★ 2026-09-04 S16 龙头接力专用：T+1 开盘「真实成交校验」（与落地引擎 history_engine 同口径）。
#   信号=信号日收盘该股属全市场涨停池龙头（池内按连板数/量比排序，最强在前）。实际行情下：
#     - T+1 开盘低开 >3%   → 该龙头接力信号被证伪（核按钮），不机械买入；
#     - T+1 开盘顶在涨停价(≥+9.8%) → 一字/竞价秒板，普通挂单按开盘价根本买不进。
#   ★ 2026-09-07 全史重放后回退「放弃式」：周末一度实作"最强买不进→顺延队内第 2..5 强"，
#     经 2614 只全史重放验证顺延显著增亏（562/293 笔顺延成交合计约 -26.0 万/-23.6 万，
#     即比"买不进即放弃"多亏约 -7.7 万/-5.2 万）→ 正式/落地维持：每日只选最强龙头 1 只，
#     T+1 开盘买不进/证伪该信号废弃，不机械降级买入（全弃/废弃日空仓，次日重新扫描）。
S16_T1_LOW_DISCARD = -0.03      # 低开证伪阈值（对应当地引擎 DISCARD_LOW_OPEN）
S16_T1_ZT_UNFILLABLE = 0.098    # 开盘顶涨停"买不进"阈值（对应当地引擎 UNFILLABLE_ZT_OPEN）
_S16_T1_SKIP = None             # 废弃统计：{策略名: {'low_discard': n, 'zt_unfillable': n}}，__main__ 前重置


def _s16_t1_skip(name, kind):
    """记录一次 S16 废弃（low_discard=低开证伪 / zt_unfillable=顶板买不进）。"""
    if _S16_T1_SKIP is None:
        return
    c = _S16_T1_SKIP.setdefault(name, {'low_discard': 0, 'zt_unfillable': 0})
    c[kind] += 1


def run_stock_strategy(df, code, name, strategy, max_cash=5000):
    """通用个股策略回测。strategy(t, df, pos) -> 'buy'/'sell'/None（信号在收盘后确定）
    采用 T+1 开盘成交，整手买入，金额<=5000。"""
    n = len(df)
    trades = []
    i = 0
    dates = df['日期']
    while i < n - 1:
        # 无持仓：扫描买入信号
        sig = strategy(i, df, None)
        if sig == 'buy':
            buy_date = dates[i + 1]
            open_price = df.loc[i + 1, '开盘']
            if not np.isnan(open_price) and open_price > 0:
                # ★ 2026-09-04 S16 龙头接力专用 T+1 开盘真实成交校验（仅 S16，其他策略口径不变）：
                #   低开>3% 证伪废弃；开盘顶涨停(≥+9.8%)买不进废弃。废弃则跳过该信号，继续扫描。
                if strategy.__name__.lower().startswith('s16'):
                    _pc = float(df.loc[i, '收盘'])
                    _gap = (open_price / _pc - 1) if (_pc and _pc > 0) else float('nan')
                    if not np.isnan(_gap):
                        if _gap <= S16_T1_LOW_DISCARD:
                            _s16_t1_skip(strategy.__name__, 'low_discard')
                            i += 1
                            continue
                        if _gap >= S16_T1_ZT_UNFILLABLE:
                            _s16_t1_skip(strategy.__name__, 'zt_unfillable')
                            i += 1
                            continue
                # 整手买入，金额不超过 max_cash
                shares = int(max_cash / (open_price * 100)) * 100
                if shares >= 100:
                    cost = buy_cost(open_price, shares)
                    # 持有扫描卖出（T+1后，最快T+2卖出，A股T+1制度）
                    j = i + 1
                    peak_close = df.loc[i + 1, '收盘']
                    peak_close_ret = 0.0
                    exit_sig = None
                    exit_j = None
                    while j < n - 1:
                        close = df.loc[j, '收盘']
                        peak_close = max(peak_close, close)
                        # 涨跌幅基于买入价（简化使用开盘买入价）
                        base = open_price
                        ret = close / base - 1
                        ret_from_peak = close / peak_close - 1
                        sig_sell = strategy(j, df, {
                            'peak_close': peak_close,
                            'ret': ret,
                            'ret_from_peak': ret_from_peak,
                            'entry_price': open_price,
                        })
                        if sig_sell in ('sell', 'sell_stop'):   # 'sell_stop'=止损，回测同样次日开盘离场
                            exit_sig = True
                            exit_j = j
                            break
                        j += 1
                    if exit_sig:
                        sell_date = dates[exit_j + 1]
                        sell_price = df.loc[exit_j + 1, '开盘']
                        if np.isnan(sell_price) or sell_price <= 0:
                            # 无次日开盘价，用当日收盘平仓
                            sell_date = dates[exit_j]
                            sell_price = df.loc[exit_j, '收盘']
                        gross = (sell_price - open_price) * shares
                        net = sell_net(sell_price, shares, sell_date) - cost
                        trades.append({
                            'strategy': strategy.__name__, 'stock_code': code, 'stock_name': name,
                            'buy_date': buy_date.strftime('%Y-%m-%d'),
                            'buy_price': round(open_price, 2), 'shares': shares,
                            'invest': round(cost, 2),
                            'sell_date': sell_date.strftime('%Y-%m-%d'),
                            'sell_price': round(sell_price, 2),
                            'gross_pnl': round(gross, 2), 'net_pnl': round(net, 2),
                            'ret_pct': round((sell_price / open_price - 1) * 100, 2),
                            'hold_days': int((sell_date - buy_date).days),
                        })
                        i = exit_j + 1
                        continue
                    else:
                        # 持有至最后无卖出信号，忽略（未平仓）
                        i = n
                        continue
        i += 1
    return trades

# ============ 各策略信号 ============

def s1_christian_breakout(i, df, pos):
    """Christian 突破回踩顺势：近60日涨幅30-100% + 突破近20日最高买入；
    止损（-1.5*ATR）/ 分批止盈 / 跌破EMA20 清仓。卖出判定独立于买入条件。"""
    if i < 61:
        return None
    c = df.loc[i, '收盘']
    if pos is not None:
        # 持仓：止损/止盈/EMA20 清仓（不受买入条件限制，避免死扛）
        atr = df.loc[i, 'atr14']
        # 止损：盘中最低价触及买入价-1.5*ATR 即触发（避免收盘判定滞后，T+1 次日开盘离场）
        if min(c, float(df.loc[i, '最低'])) < pos['entry_price'] - 1.5 * atr:
            return 'sell_stop'
        # 分批止盈：浮盈≥8% 后回落到 <4%
        if pos.get('peak_close', 0) and c / pos['entry_price'] - 1 >= 0.08:
            if c / pos['entry_price'] - 1 < 0.04:
                return 'sell'
        # 跌破 EMA20 清仓
        if c < df.loc[i, 'ema20']:
            return 'sell'
        return None
    # 空仓：买入条件（近60日涨幅30-100% + 突破近20日最高）
    base = df.loc[i - 60, '收盘']
    if not np.isfinite(base) or base <= 0:
        return None
    gain = c / base - 1
    if not (0.30 <= gain <= 1.00):
        return None
    prev20_high = df.loc[i - 20:i - 1, '最高'].max()
    if c > prev20_high:
        return 'buy'
    return None

def s2_macd_kdj(i, df, pos):
    """MACD金叉买/KDJ死叉卖，浮亏5%止损"""
    if i < 35:
        return None
    if pos is None:
        if df.loc[i, 'dif'] > df.loc[i, 'dea'] and df.loc[i - 1, 'dif'] <= df.loc[i - 1, 'dea']:
            return 'buy'
    else:
        if df.loc[i, 'k'] < df.loc[i, 'd'] and df.loc[i - 1, 'k'] >= df.loc[i - 1, 'd']:
            return 'sell'
        if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
            return 'sell_stop'  # 止损：收盘浮亏5%或盘中最低触及-5%即触发，不受持有期下限限制
    return None

def s3_chandelier_trend(i, df, pos):
    """吊灯止损趋势：收盘站上MA20且突破前日高点买入，3*ATR吊灯止损/跌破MA10离场"""
    if i < 21:
        return None
    if pos is None:
        if df.loc[i, '收盘'] > df.loc[i, 'ma20'] and df.loc[i, '收盘'] > df.loc[i - 1, '最高']:
            return 'buy'
    else:
        atr = df.loc[i, 'atr14']
        if pos['peak_close'] > 0 and df.loc[i, '收盘'] < pos['peak_close'] - 3 * atr:
            return 'sell'
        if df.loc[i, '收盘'] < df.loc[i, 'ma10']:
            return 'sell'
    return None

def s4_ma20_pullback(i, df, pos):
    """20日均线回踩+放量阳线买入，跌破MA20止损，浮盈15%回撤10%止盈"""
    if i < 21:
        return None
    if pos is None:
        c, ma20 = df.loc[i, '收盘'], df.loc[i, 'ma20']
        if np.isnan(ma20) or ma20 <= 0:
            return None
        dev = abs(c / ma20 - 1)
        is_yang = df.loc[i, '收盘'] > df.loc[i, '开盘']
        vol_ok = df.loc[i, '成交量'] > 1.5 * df.loc[i, 'vol_ma5']
        if dev <= 0.02 and is_yang and vol_ok:
            return 'buy'
    else:
        if df.loc[i, '收盘'] < df.loc[i, 'ma20']:
            if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
                return 'sell_stop'  # 跌破且浮亏超5%或盘中触及-5%：止损（不受持有期限制）
            return 'sell'           # 跌破MA20：策略离场（受持有期下限约束）
        if pos.get('peak_close') and (pos['peak_close'] / pos['entry_price'] - 1) >= 0.15:
            if df.loc[i, '收盘'] / pos['entry_price'] - 1 <= 0.10:
                return 'sell'
    return None

def s5_tp15_sl5(i, df, pos):
    """MA20向上且突破20日新高买入；盈利15%后回落到10%止盈；亏损5%止损"""
    if i < 26:
        return None
    if pos is None:
        c = df.loc[i, '收盘']
        prev20_high = df.loc[i - 20:i - 1, '收盘'].max()
        ma20_up = df.loc[i, 'ma20'] > df.loc[i - 5, 'ma20']
        if c > df.loc[i, 'ma20'] and ma20_up and c > prev20_high:
            return 'buy'
    else:
        if (pos['peak_close'] / pos['entry_price'] - 1) >= 0.15 and pos['ret'] <= 0.10:
            return 'sell'
        if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
            return 'sell_stop'  # 止损：收盘浮亏5%或盘中最低触及-5%，T+1 次日开盘离场
    return None

def s6_boll_revert(i, df, pos):
    """布林下轨买入、中轨卖出（做T），止损5%"""
    if i < 21:
        return None
    if pos is None:
        if df.loc[i, '收盘'] < df.loc[i, 'boll_low']:
            return 'buy'
    else:
        if df.loc[i, '收盘'] > df.loc[i, 'boll_mid']:
            return 'sell'
        if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
            return 'sell_stop'  # 止损：收盘浮亏5%或盘中最低触及-5%，T+1 次日开盘离场
    return None

def s10_rsi_revert(i, df, pos):
    """RSI 超卖超买反转（idx11 图7）：RSI 上穿 20 买入，下穿 80 卖出，止损5%"""
    if i < 21:
        return None
    if pos is None:
        if df.loc[i, 'rsi14'] > 20 and df.loc[i - 1, 'rsi14'] <= 20:
            return 'buy'
    else:
        if df.loc[i, 'rsi14'] < 80 and df.loc[i - 1, 'rsi14'] >= 80:
            return 'sell'
        if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
            return 'sell_stop'  # 止损：收盘浮亏5%或盘中最低触及-5%，T+1 次日开盘离场
    return None

def s9_trendline_pullback(i, df, pos):
    """趋势线突破回踩再启动（idx3图解）：MA60向上，价格回踩MA20附近（±2%）且创20日新高买入；跌破MA10/亏损5%/回撤8%卖出"""
    if i < 61:
        return None
    if pos is None:
        c, ma20, ma60 = df.loc[i, '收盘'], df.loc[i, 'ma20'], df.loc[i, 'ma60']
        if np.isnan(ma20) or np.isnan(ma60) or c <= ma60:
            return None
        if ma60 <= df.loc[i - 5, 'ma60']:
            return None
        if abs(c / ma20 - 1) > 0.02:
            return None
        prev20_high = df.loc[i - 20:i - 1, '最高'].max()
        if c <= prev20_high:
            return None
        return 'buy'
    else:
        if df.loc[i, '收盘'] < df.loc[i, 'ma10']:
            return 'sell'
        if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
            return 'sell_stop'  # 止损：收盘浮亏5%或盘中最低触及-5%，T+1 次日开盘离场
        if pos.get('ret_from_peak') is not None and pos['ret_from_peak'] <= -0.08:
            return 'sell_stop'  # 高位回撤8%：风控离场（同样次日开盘执行）
    return None


# ============ 缠论策略（S15，2026-08-31 接入） ============
_CHAN_SIG_CACHE = {}  # code/id(df) -> {'n': 行数, 'sigs': {原始K线下标: 信号价}}


def _chan_signals(df, types):
    """缠论信号缓存：返回 {原始K线下标: 信号价}（仅含 types 中类型）。
    优先按 df.attrs['_code'] 缓存（跨请求命中，避免每次重新全量计算）；无 code 时按 id+行数校验。"""
    key = df.attrs.get('_code') if getattr(df, 'attrs', None) else None
    key = key or id(df)
    hit = _CHAN_SIG_CACHE.get(key)
    if hit is not None and hit['n'] == len(df):
        return hit['sigs']
    sigs = {}
    if _cc is not None:
        try:
            res = _cc.chan_analyze(df)
            sigs = {i: p for (i, t, p) in res['signals'] if t in types}
        except Exception:
            sigs = {}
    _CHAN_SIG_CACHE[key] = {'n': len(df), 'sigs': sigs}
    return sigs


def s15_chan_sanbuy(i, df, pos):
    """缠论三买：中枢上移、向上突破后回调不破ZG，回调笔末端买入；止盈8%/止损5%。
    无前视：分型信号 orig_idx 需 orig_idx+1 日收盘确认 → 买入在 orig_idx+2 日开盘
    （sfunc 在 i=orig_idx+1 日收盘判定，i+1 日开盘成交；本系统 sfunc 在空仓时不驱动买入，
    买入由 advisor._a_chan 驱动，此分支供 backtest 独立回测 run_stock_strategy 使用）。"""
    if pos is not None:
        if pos['ret'] >= 0.08:
            return 'sell'
        if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
            return 'sell_stop'
        return None
    sigs = _chan_signals(df, {'三买'})
    return 'buy' if (i - 1) in sigs else None


# ---- S15「可做窗口」对照变体（gtma20，2026-09-08 加） ----
# 口径：与落地引擎 BUY_FILTERS 的 'sh_gt_ma20' 一致——信号【确认日】(sfunc 判定日 i) 上证收盘
# 站上其 20 日均线才算「可做窗口」，窗口外的三买信号一律不开仓；卖出规则与原版完全相同。
# 该变体不加入 STRATEGIES（避免影响正式建账），仅由对照脚本引用做长期表现比较。
_SH_MA20_OK = None  # set: 上证收盘>MA20 的交易日 'YYYY-MM-DD'
_SH_MA20_DATES = None


def _sh_gt_ma20(date):
    """date('YYYY-MM-DD') 上证是否收盘站上20日均线（可做窗口）。非交易日/缺数据按 False（保守禁做）。"""
    global _SH_MA20_OK, _SH_MA20_DATES
    if _SH_MA20_OK is None:
        sh = pd.read_csv(os.path.join(DATA_ROOT, 'data', 'csv', 'sh000001.csv'), encoding='utf-8-sig')
        sh['date'] = sh['date'].astype(str)
        sh = sh.drop_duplicates('date', keep='last').sort_values('date').reset_index(drop=True)
        c = pd.to_numeric(sh['close'], errors='coerce')
        ma = c.rolling(20).mean()
        ok = (c > ma) & ma.notna()
        _SH_MA20_DATES = sh['date'].tolist()
        _SH_MA20_OK = set(sh.loc[ok.fillna(False), 'date'])
    if date is None or date not in _SH_MA20_OK:
        return False
    return True


def s15_chan_sanbuy_gtma20(i, df, pos):
    """S15 gtma20 变体：原版三买规则 + 只在「上证>MA20 可做窗口」开仓（对照长期表现用）。"""
    if pos is not None:
        if pos['ret'] >= 0.08:
            return 'sell'
        if pos['ret'] <= -0.05 or float(df.loc[i, '最低']) <= pos['entry_price'] * 0.95:
            return 'sell_stop'
        return None
    sigs = _chan_signals(df, {'三买'})
    if (i - 1) not in sigs:
        return None
    if not _sh_gt_ma20(df.loc[i, '日期'].strftime('%Y-%m-%d')):
        return None  # 可做窗口外 → 禁做，放弃该三买信号
    return 'buy'


# ============ 龙头接力策略（S16，2026-09-01 接入） ============
# 信号：当日收盘为【全市场涨停池最强龙头】（连板数最高、量比次），次日开盘买入；
# 持有中：断板（当日不涨停）→ 次日开盘卖；收盘浮亏-8% 止损。
# 无前视：信号收盘确定，T+1 开盘成交。跨票全局信号（每日全市场只选 1 只最强）。
# 2026-09-01 起 base 与 V2 并存：
#   S16_龙头接力   = base（不限连板）
#   S16_龙头接力V2 = 连板≥3（最强龙头连板<3 当日无信号）
# ★2026-09-04 官方口径同步「真实成交校验」（顶板买不进+低开证伪，见 run_stock_strategy）。
#   2026-09-07 全史重放复核（2019-01 起，2614 只主板，数据至 2026-09-04）：
#     base：642 笔 净 -18,087 胜率 37.5%（弃 low189/zt755）  |  V2：563 笔 净 -15,868 胜率 38.5%（弃 low172/zt748）
#   同底基线（开盘价必成交）：base 878 笔 +165,445 / V2 783 笔 +167,723——差额恰为
#   "开盘顶涨停买不进 + 低开证伪"虚增的假成交。此前存档 798笔+120,618 / 718笔+124,752 已作废。
# ★2026-09-07 全史重放试点「买不进→顺延队内第2~5强」被否决：顺延多成交 562/293 笔、
#   净亏约 -26.0万/-23.6万（接盘的是开盘即弱于龙头的备选）→ 维持"每日最强 1 只、命中不了即放弃"。
_S16_LEADERS_CACHE = None  # {'mtime': 个股CSV修改时间, 'data': {min_lb: {日期: (code, 连板数, 量比)|None}}}


def _zt_col(df):
    """真实行情口径涨跌幅(%)向量。
    优先读 CSV『涨跌幅』列（行情接口基于除权后昨收计算，除权除息日价格断层不污染涨幅）；
    列缺失或空值/停牌行回退 收盘/上一交易日收盘-1（兼容无该列的旧数据）。
    返回 numpy 数组，NaN=无法判定（首日等）。"""
    z = None
    if '涨跌幅' in df.columns:
        z = pd.to_numeric(df['涨跌幅'], errors='coerce').to_numpy(dtype=float)
    c = df['收盘'].to_numpy(dtype=float)
    prev_c = np.roll(c, 1); prev_c[0] = np.nan
    with np.errstate(invalid='ignore'):
        calc = np.where(prev_c > 0, (c / prev_c - 1) * 100, np.nan)
    if z is None:
        return calc
    return np.where(np.isfinite(z), z, calc)


def _zt_ret(df, i):
    """当日真实涨跌幅(%)（标量版，供持仓断板 / 快速失败判定）。无法判定返回 nan。"""
    try:
        z = _zt_col(df)[i]
        return float(z) if z == z else float('nan')
    except Exception:
        return float('nan')


def _s16_leaders(force=False, min_lb=1):
    """每日全市场涨停池龙头映射（min_lb=1 即 base 全量 / 3 仅连板≥3）。
    每交易日取池内按（连板数,量比）降序的最强龙头 1 只 → (code, 连板数, 量比)。
    T+1 开盘是否实际买得进由调用方判定（回测 run_stock_strategy 守卫 / 引擎执行日校验）：
    顶板买不进或低开证伪 → 该信号废弃不机械买入，空仓等下一交易日重新扫描。
    ★2026-09-07 全史重放否决"买不进顺延队内第 2..N 强"（顺延接的是开盘即弱于龙头的备选，
    562/293 笔合计净亏约 -26.0 万/-23.6 万）→ 不再预筛可买性，维持放弃式。
    惰性全量构建（一次构建生成 min_lb=1/3 两份映射）；个股数据更新（mtime 变化）时自动重建。
    涨停判定/连板计数规则同旧版。"""
    global _S16_LEADERS_CACHE
    mtime = None
    try:
        mtime = os.path.getmtime(os.path.join(DATA_ROOT, 'data', 'csv', '%s.csv' % next(iter(STOCKS))))
    except Exception:
        pass
    if not force and _S16_LEADERS_CACHE is not None and (mtime is None or mtime <= _S16_LEADERS_CACHE['mtime']):
        return _S16_LEADERS_CACHE['data'].get(min_lb, {})
    t0 = time.time()
    by_date = {}
    for code in STOCKS:
        try:
            df = add_indicators(load_stock(code)).reset_index(drop=True)
        except Exception:
            continue
        c = df['收盘'].to_numpy(dtype=float)
        h = df['最高'].to_numpy(dtype=float)
        with np.errstate(invalid='ignore'):
            is_zt = (_zt_col(df) >= 9.5) & (h <= c * 1.002)
        streak = np.zeros(len(df), dtype=int)
        s = 0
        for j in range(len(df)):
            s = s + 1 if is_zt[j] else 0
            streak[j] = s
        v = df['成交量'].to_numpy(dtype=float)
        vma = df['vol_ma5'].to_numpy(dtype=float)
        dts = df['日期']
        for j in range(len(df)):
            if not is_zt[j]:
                continue
            d = dts.iloc[j]
            vr = float(v[j] / vma[j]) if vma[j] > 0 else 1.0
            by_date.setdefault(d, []).append((code, int(streak[j]), vr))
    leaders = {1: {}, 3: {}}
    for d, plist in by_date.items():
        plist.sort(key=lambda x: (-x[1], -x[2]))
        leaders[1][d] = plist[0]  # 当日最强龙头（连板最高、量比次）
        for _cd, _st, _vr in plist:  # V2：连板≥3 的最强龙头（排序保证首个≥3板即最强）
            if _st >= 3:
                leaders[3][d] = (_cd, _st, _vr)
                break
    _S16_LEADERS_CACHE = {'mtime': mtime, 'data': leaders}
    print(f'[S16] 龙头候选构建完成 {int(time.time()-t0)}s '
          f'base信号日={sum(1 for v in leaders[1].values() if v)} | '
          f'V2信号日={sum(1 for v in leaders[3].values() if v)}', flush=True)
    return _S16_LEADERS_CACHE['data'].get(min_lb, {})


def _s16_relay_impl(i, df, pos, min_lb):
    """龙头接力共用逻辑：min_lb 过滤连板数。"""
    if pos is not None:
        if pos['ret'] <= -0.08:
            return 'sell_stop'
        if i >= 1 and _zt_ret(df, i) < 9.5:  # 断板：当日不涨停（真实口径，除权日不误判）
            return 'sell'
        return None
    code = df.attrs.get('_code')
    if not code or i < 1:
        return None
    if not _zt_ret(df, i) >= 9.5:  # 非涨停日不可能是龙头（快速失败；真实口径，除权日不误判）
        return None
    leaders = _s16_leaders(min_lb=min_lb)
    d = df.loc[i, '日期']
    if d not in leaders or leaders[d] is None:
        return None
    return 'buy' if leaders[d][0] == code else None


def s16_leader_relay(i, df, pos):
    """龙头接力 base：信号日 i 收盘若该股为全市场涨停池最强龙头（不限连板）→ 买入；
    持仓：断板（当日不涨停）卖出 / 收盘浮亏-8% 止损。"""
    return _s16_relay_impl(i, df, pos, 1)


def s16_leader_relay_v2(i, df, pos):
    """龙头接力 V2：同 base，但龙头须连板≥3（2026-09-01 A/B：718笔+124,752 优于 base 798笔+120,618）。"""
    return _s16_relay_impl(i, df, pos, 3)


STRATEGIES = {
    'S1_Christian突破回踩': s1_christian_breakout,
    'S2_MACD金叉KDJ死叉': s2_macd_kdj,
    'S3_吊灯止损趋势跟踪': s3_chandelier_trend,
    'S4_20日线回踩放量': s4_ma20_pullback,
    'S5_止盈15止损5': s5_tp15_sl5,
    'S6_布林下轨做T': s6_boll_revert,
    'S9_趋势线突破回踩': s9_trendline_pullback,
    'S10_RSI超卖超买反转': s10_rsi_revert,
    'S15_缠论三买': s15_chan_sanbuy,
    'S16_龙头接力': s16_leader_relay,
    'S16_龙头接力V2': s16_leader_relay_v2,
}

def main_stocks():
    all_trades = []
    for code, name in STOCKS.items():
        try:
            df = load_stock(code)
        except Exception as _e:
            print(f'[skip] {code} {name} 行情数据缺失/异常: {_e}')
            continue
        if df is None or df.empty:
            print(f'[skip] {code} {name} 空行情数据')
            continue
        df = add_indicators(df)
        for sname, sfunc in STRATEGIES.items():
            # 动态改名用于 trades 记录
            sfunc.__name__ = sname
            tr = run_stock_strategy(df, code, name, sfunc)
            all_trades.extend(tr)
    return all_trades

# ============ 指数策略 ============

BACKTEST_START = pd.Timestamp('2019-01-01')

def run_scaled_index_strategy(df, name, buy_lines, sell_lines, value_key, max_lots=3, max_hold_days=None):
    """分批低买高卖状态机：下穿买入线建仓/加仓，上穿卖出线减仓/清仓（FIFO配对）
    buy_lines/sell_lines: list of (threshold, tag)，买入线按阈值从高到低
    触发判断用 value_key（估值），成交价用 '指数'（点位）
    max_hold_days: 超过该持有天数（交易日数）强制平仓，None 表示不强制
    """
    n = len(df)
    start_idx = 0
    for k in range(n):
        if df.loc[k, '日期'] >= BACKTEST_START:
            start_idx = k
            break

    positions = []      # FIFO: {buy_idx, buy_date, buy_price, tag}
    bought_levels = {}  # threshold -> 是否已触发
    trades = []

    def settle(pi, ri, tag=None):
        t = positions[pi]
        price = df.loc[ri, '指数']
        ret = price / t['buy_price'] - 1
        cost = 5000 + max(5000 * COMMISSION_RATE, COMMISSION_MIN)
        sell_amt = 5000 * (1 + ret)
        fee = max(sell_amt * COMMISSION_RATE, COMMISSION_MIN) + sell_amt * stamp_tax(df.loc[ri, '日期'])
        tr = {
            'strategy': name,
            'stock_code': '', 'stock_name': '',
            'buy_date': t['buy_date'].strftime('%Y-%m-%d'),
            'buy_price': round(t['buy_price'], 2), 'shares': 1,
            'invest': 5000.0,
            'sell_date': df.loc[ri, '日期'].strftime('%Y-%m-%d'),
            'sell_price': round(price, 2),
            'gross_pnl': round(sell_amt - 5000, 2),
            'net_pnl': round(sell_amt - fee - cost, 2),
            'ret_pct': round(ret * 100, 2),
            'hold_days': int((df.loc[ri, '日期'] - t['buy_date']).days),
        }
        if tag:
            tr['sell_tag'] = tag
        return tr

    for i in range(start_idx, n):
        if i == 0:
            continue
        prev_v = df.loc[i - 1, value_key]
        cur_v = df.loc[i, value_key]
        d = df.loc[i, '日期']
        # 卖出：上穿
        for th, tag in sell_lines:
            if prev_v < th <= cur_v or (prev_v <= th and cur_v > th):
                if positions:
                    trades.append(settle(0, i, tag))
                    positions.pop(0)
                    bought_levels.clear()   # 新周期，重置买入档位
        # 买入：下穿（每日最多一笔）
        for th, tag in buy_lines:
            if prev_v > th >= cur_v or (prev_v >= th and cur_v < th):
                if th not in bought_levels and len(positions) < max_lots:
                    positions.append({'buy_idx': i, 'buy_date': d, 'buy_price': df.loc[i, '指数'], 'tag': tag})
                    bought_levels[th] = True
                    break
        # 最长持有期强制平仓（用交易日差）
        if max_hold_days and positions:
            if i - positions[0]['buy_idx'] >= max_hold_days:
                trades.append(settle(0, i, '持有期强制平仓'))
                positions.pop(0)
                bought_levels.clear()
    return trades

def main_index():
    trades = []
    # S7 沪深300 PE 分批：PE<12.5建仓、<8.75加仓；>12.5减仓、>16.25清仓
    df_pe = pd.read_csv(os.path.join(DATA_ROOT, 'data', 'csv', 'hs300_pe.csv'), encoding='utf-8-sig')
    df_pe['日期'] = pd.to_datetime(df_pe['日期'])
    df_pe = df_pe.sort_values('日期').reset_index(drop=True)
    buy_lines = [(12.5, 'PE<12.5建仓'), (8.75, 'PE<8.75加仓')]
    sell_lines = [(12.5, 'PE>12.5减仓'), (16.25, 'PE>16.25清仓')]
    t1 = run_scaled_index_strategy(df_pe, 'S7_沪深300PE分批低买高卖', buy_lines, sell_lines, '滚动市盈率')
    trades.extend(t1)

    # S8 上证红利 PB 分批：PB<0.85建仓、<0.75加仓；>0.90减仓、>1.0清仓
    df_pb = pd.read_csv(os.path.join(DATA_ROOT, 'data', 'csv', 'hsly_pb.csv'), encoding='utf-8-sig')
    df_pb['日期'] = pd.to_datetime(df_pb['日期'])
    df_pb = df_pb.sort_values('日期').reset_index(drop=True)
    buy_lines2 = [(0.85, 'PB<0.85建仓'), (0.75, 'PB<0.75加仓')]
    sell_lines2 = [(0.90, 'PB>0.90减仓'), (1.00, 'PB>1.0清仓')]
    t2 = run_scaled_index_strategy(df_pb, 'S8_红利低估值分批买入', buy_lines2, sell_lines2, '市净率', max_hold_days=700)
    trades.extend(t2)
    return trades

if __name__ == '__main__':
    _S16_T1_SKIP = {}
    all_trades = main_stocks() + main_index()
    # 严格限制回测区间从 2019-01-01 起
    all_trades = [t for t in all_trades if pd.Timestamp(t['buy_date']) >= BACKTEST_START]
    df_out = pd.DataFrame(all_trades)
    df_out.to_csv(os.path.join(DATA_ROOT, 'data', 'backtest_trades.csv'), index=False, encoding='utf-8-sig')
    print(f'TOTAL TRADES: {len(df_out)}')

    # 按策略汇总
    summary = []
    for sname in list(STRATEGIES.keys()) + ['S7_沪深300PE分批低买高卖', 'S8_红利低估值分批买入']:
        sub = df_out[df_out['strategy'] == sname]
        if len(sub) == 0:
            continue
        total_pnl = sub['net_pnl'].sum()
        wins = (sub['net_pnl'] > 0).sum()
        summary.append({
            'strategy': sname,
            'trades': len(sub),
            'total_net_pnl': round(total_pnl, 2),
            'avg_pnl_per_trade': round(sub['net_pnl'].mean(), 2),
            'win_rate': round(wins / len(sub) * 100, 1),
            'avg_hold_days': round(sub['hold_days'].mean(), 1),
            'avg_ret_pct': round(sub['ret_pct'].mean(), 2),
        })
    sdf = pd.DataFrame(summary).sort_values('total_net_pnl', ascending=False)
    sdf.to_csv(os.path.join(DATA_ROOT, 'data', 'backtest_summary.csv'), index=False, encoding='utf-8-sig')
    print(sdf.to_string(index=False))
    if _S16_T1_SKIP:
        print('\nS16 T+1 开盘废弃统计（与落地引擎同口径，废弃信号不计入成交）:')
        for sname, c in _S16_T1_SKIP.items():
            print(f'  {sname}: 低开>3%证伪废弃 {c["low_discard"]} 次 | 开盘顶涨停买不进废弃 {c["zt_unfillable"]} 次')
