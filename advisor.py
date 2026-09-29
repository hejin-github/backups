# -*- coding: utf-8 -*-
"""推荐引擎：对每个策略计算当前最接近买入的股票及买卖点"""
import sys, os, math
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _WORKSPACE)
os.chdir(_WORKSPACE)
import datetime
import pandas as pd
import numpy as np
import backtest as bt

STOCKS = bt.STOCKS
STRATEGY_LIST = list(bt.STRATEGIES.keys()) + ['S7_沪深300PE分批低买高卖', 'S8_红利低估值分批买入']

def today_strategy(d=None):
    d = d or datetime.date.today()
    # ★ 行情驱动的策略选择（2026-08-27）：按当前行情环境从适配策略中轮换，替代纯日期轮换
    try:
        import history_engine
        env = history_engine.market_env_on(d.strftime('%Y-%m-%d'))
        fitted = history_engine.strategies_for_env(env)
        if fitted:
            n = d.year * 366 + d.timetuple().tm_yday
            return fitted[n % len(fitted)]
    except Exception:
        pass
    n = d.year * 366 + d.timetuple().tm_yday
    return STRATEGY_LIST[n % len(STRATEGY_LIST)]

def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)

# ============ 实时数据补齐（启动晚时修正/补齐当日数据） ============

_RT_COL = {
    '收盘': 'price', '开盘': 'open', '最高': 'high', '最低': 'low',
    '成交量': 'volume', '成交额': 'amount', '换手率': 'turnover',
    '振幅': 'amp', '涨跌幅': 'pct', '涨跌额': 'chg',
}


def _rt_row(info, date_str, code=None):
    """实时行情 → 日线行（字段对齐 CSV）"""
    row = {'日期': pd.Timestamp(date_str)}
    if code:
        row['股票代码'] = code
    for col, key in _RT_COL.items():
        val = info.get(key)
        if val is not None:
            row[col] = val
    return row


def _apply_realtime(df, info, date_str, code=None):
    """把实时行情叠加到日线 df：
    - 末行已是当日 → 就地更新收盘/最高/最低/量额/换手等（修正盘中半成品 bar）
    - 末行早于当日 → 仅在最后追加当日行（不插历史、不用未来数据）
    返回 (df, 是否新增了当日行)"""
    last_date = df['日期'].iloc[-1].strftime('%Y-%m-%d')
    if last_date == date_str:
        for col, key in _RT_COL.items():
            val = info.get(key)
            if val is not None and col in df.columns:
                df.loc[df.index[-1], col] = val
        return df, False
    if date_str > last_date:
        df = pd.concat([df, pd.DataFrame([_rt_row(info, date_str, code)])], ignore_index=True)
        return df, True
    return df, False


def backfill_today(rt):
    """启动/扫描前补齐今日数据（防止启动太晚用错数据）：
    - 个股：行情时间==今日时，用实时行情修正/补齐今日 bar（收盘/最高/最低/量额等）。
      仅已收盘（>=15:00）才写回 CSV，盘中只做内存叠加，避免把半成品写进历史。
    - 返回 asof 描述 dict：{label, detail, backfilled}"""
    today_s = datetime.date.today().strftime('%Y-%m-%d')
    now = datetime.datetime.now()
    qdate = qtime = None
    if rt:
        tss = [info.get('ts') or '' for info in rt.values() if len(info.get('ts') or '') >= 14]
        if tss:
            t = max(tss)
            qdate = f'{t[:4]}-{t[4:6]}-{t[6:8]}'
            qtime = f'{t[8:10]}:{t[10:12]}:{t[12:14]}'
    market_closed = now.weekday() < 5 and (now.hour >= 15 or bool(qtime and qtime >= '15:00:00'))
    saved = 0
    for code, info in (rt or {}).items():
        d = info.get('date') or qdate
        if d != today_s:
            continue
        try:
            fp = f'data/csv/{code}.csv'
            df = pd.read_csv(fp, encoding='utf-8-sig')
            df['日期'] = pd.to_datetime(df['日期'])
            df = df.sort_values('日期').reset_index(drop=True)
            df2, _ = _apply_realtime(df, info, d, code)
            # ★2026-09-18：绝不把空表写回 CSV（曾把 603797.csv 覆盖成 0 字节 → 全市场
            #   扫描在 _zt_col 抛 IndexError、leaders 缓存永不落盘 → 收盘扫描卡死 49 小时）
            if market_closed and df2 is not None and len(df2):
                df2.to_csv(fp, index=False, encoding='utf-8-sig')
                saved += 1
        except Exception:
            continue
    if not qdate:
        asof = {'label': '本地数据（无实时行情）', 'detail': ''}
    elif qdate == today_s:
        if market_closed:
            asof = {'label': '今日收盘价', 'detail': qtime or '15:00'}
        else:
            asof = {'label': '盘中快照', 'detail': qtime or ''}
    else:
        asof = {'label': '最近收盘价（非今日）', 'detail': f'{qdate} {qtime or ""}'.strip()}
    if saved:
        asof['backfilled'] = f'已用收盘行情补齐/修正 {saved} 只个股今日数据'
    return asof


_INDEX_FETCHED_DATE = None


def backfill_index(force=False):
    """尝试拉取最新指数估值追加到 CSV（乐咕数据有发布时滞，拉不到就保持现状）。
    每天最多尝试一次。返回 {指数名: 估值数据截至日期}"""
    global _INDEX_FETCHED_DATE
    today_s = datetime.date.today().strftime('%Y-%m-%d')
    if not force and _INDEX_FETCHED_DATE == today_s:
        return {}
    _INDEX_FETCHED_DATE = today_s
    out = {}
    jobs = [
        ('data/csv/hs300_pe.csv', '沪深300', 'stock_index_pe_lg'),
        ('data/csv/hsly_pb.csv', '上证红利', 'stock_index_pb_lg'),
    ]
    # akshare 网络请求可能长时间无响应，设置 socket 默认超时防止线程卡死
    import socket
    socket.setdefaulttimeout(20)
    for fp, sym, fn_name in jobs:
        try:
            import akshare as ak
            df = pd.read_csv(fp, encoding='utf-8-sig')
            df['日期'] = pd.to_datetime(df['日期'])
            df = df.sort_values('日期').reset_index(drop=True)
            last = df['日期'].iloc[-1].strftime('%Y-%m-%d')
            new = getattr(ak, fn_name)(symbol=sym)
            new['日期'] = pd.to_datetime(new['日期'])
            new = new.sort_values('日期').reset_index(drop=True)
            add = new[new['日期'] > df['日期'].iloc[-1]]
            if len(add):
                add = add[df.columns]
                df = pd.concat([df, add], ignore_index=True)
                df.to_csv(fp, index=False, encoding='utf-8-sig')
                out[sym] = add['日期'].iloc[-1].strftime('%Y-%m-%d')
            else:
                out[sym] = last
        except Exception:
            out[sym] = None
    return out

# ============ 个股策略分析 ============

def _a_s1(i, df):
    """Christian突破回踩：60日涨幅30-100% + 突破20日最高"""
    if i < 61: return None
    c = float(df.loc[i, '收盘']); base = float(df.loc[i-60, '收盘'])
    if base <= 0 or math.isnan(base): return None
    gain = c / base - 1
    prev20 = float(df.loc[i-20:i-1, '最高'].max())
    atr = float(df.loc[i, 'atr14'])
    ok = 0.30 <= gain <= 1.00
    triggered = ok and c > prev20
    score = 0
    if ok: score += 40
    if prev20 > 0:
        if c > prev20: score += 60
        else:
            gap = (prev20 - c) / prev20
            score += int(max(0, 60 * (1 - gap / 0.05)))
    stop = c - 1.5 * atr if not math.isnan(atr) else c * 0.92
    return dict(triggered=triggered, score=min(100, score), price=c,
                buy_ref=prev20, stop=stop, target=float(df.loc[i, 'ema20']),
                note=f'60日涨幅{gain*100:.0f}%', tag='60日涨幅区间+突破20日高')

def _a_s2(i, df):
    """MACD金叉买入，KDJ死叉/亏5%卖出"""
    if i < 35: return None
    c = float(df.loc[i, '收盘'])
    dif, dea = float(df.loc[i, 'dif']), float(df.loc[i, 'dea'])
    prev_dif, prev_dea = float(df.loc[i-1, 'dif']), float(df.loc[i-1, 'dea'])
    triggered = dif > dea and prev_dif <= prev_dea
    gap = abs(dif - dea)
    scale = max(abs(dea), abs(dif), 0.01)
    score = 0
    if dif > dea: score += 50
    else: score += int(min(50, 50 * (1 - gap / scale / 2)))
    if dif > 0 and dea > 0: score += 20
    return dict(triggered=triggered, score=min(100, score), price=c,
                buy_ref=c, stop=c * 0.95, target=None,
                note=f'DIF={dif:.2f} DEA={dea:.2f}', tag='DIF上穿DEA')

def _a_s3(i, df):
    """吊灯止损趋势：站上MA20且突破前日高"""
    if i < 21: return None
    c = float(df.loc[i, '收盘']); ma20 = float(df.loc[i, 'ma20'])
    prev_high = float(df.loc[i-1, '最高'])
    atr = float(df.loc[i, 'atr14'])
    triggered = c > ma20 and c > prev_high
    score = 0
    if c > ma20: score += 50
    else: score += int(max(0, 50 * (1 - abs(c/ma20-1) / 0.05)))
    if c > prev_high: score += 50
    else: score += int(max(0, 50 * (1 - (prev_high-c)/prev_high/0.03)))
    stop = c - 3 * atr if not math.isnan(atr) else c * 0.90
    return dict(triggered=triggered, score=min(100, score), price=c,
                buy_ref=max(ma20, prev_high), stop=stop, target=float(df.loc[i, 'ma10']),
                note=f'MA20={ma20:.2f} 前日高={prev_high:.2f}', tag='站上MA20且破前日高')

def _a_s4(i, df):
    """20日线回踩放量：收盘贴近MA20±2% + 阳线 + 放量1.5倍"""
    if i < 21: return None
    c = float(df.loc[i, '收盘']); ma20 = float(df.loc[i, 'ma20'])
    if math.isnan(ma20) or ma20 <= 0: return None
    dev = abs(c / ma20 - 1)
    is_yang = c > float(df.loc[i, '开盘'])
    vol_ok = float(df.loc[i, '成交量']) > 1.5 * float(df.loc[i, 'vol_ma5'])
    triggered = dev <= 0.02 and is_yang and vol_ok
    score = 0
    if dev <= 0.05: score += int(max(0, 50 * (1 - dev / 0.05)))
    if is_yang: score += 25
    if vol_ok: score += 25
    return dict(triggered=triggered, score=min(100, score), price=c,
                buy_ref=ma20, stop=ma20, target=None,
                note=f'偏离MA20={dev*100:.1f}% 阳线={is_yang} 放量={vol_ok}', tag='回踩MA20±2%且放量')

def _a_s5(i, df):
    """止盈15止损5：MA20向上 + 突破20日收盘新高"""
    if i < 26: return None
    c = float(df.loc[i, '收盘']); ma20 = float(df.loc[i, 'ma20'])
    prev20_high = float(df.loc[i-20:i-1, '收盘'].max())
    ma20_up = ma20 > float(df.loc[i-5, 'ma20'])
    triggered = c > ma20 and ma20_up and c > prev20_high
    score = 0
    if ma20_up: score += 30
    if c > ma20: score += 30
    if c > prev20_high: score += 40
    else: score += int(max(0, 40 * (1 - (prev20_high-c)/prev20_high/0.05)))
    return dict(triggered=triggered, score=min(100, score), price=c,
                buy_ref=prev20_high, stop=c*0.95, target=None,
                note=f'20日高={prev20_high:.2f} MA20向上={ma20_up}', tag='突破20日新高')

def _a_s6(i, df):
    """布林下轨做T：收盘<下轨买入，中轨卖出"""
    if i < 21: return None
    c = float(df.loc[i, '收盘']); low = float(df.loc[i, 'boll_low'])
    mid = float(df.loc[i, 'boll_mid'])
    triggered = c < low
    score = 0
    if c < low: score = 100
    else:
        band = max(low - mid, 0.01)
        score = int(max(0, 80 * (1 - (c - low) / band)))
    return dict(triggered=triggered, score=min(100, score), price=c,
                buy_ref=low, stop=c*0.95, target=mid,
                note=f'布林下轨={low:.2f} 中轨={mid:.2f}', tag='收盘<布林下轨')

def _a_s9(i, df):
    """趋势线突破回踩：MA60向上+回踩MA20±2%+创20日新高；
    或突破后回踩（近60日内曾创20日新高，今日回踩MA20企稳）→ 分批入场/加仓"""
    if i < 61: return None
    c = float(df.loc[i, '收盘']); ma20 = float(df.loc[i, 'ma20']); ma60 = float(df.loc[i, 'ma60'])
    if math.isnan(ma20) or math.isnan(ma60): return None
    prev20 = float(df.loc[i-20:i-1, '最高'].max())
    ma60_up = ma60 > float(df.loc[i-5, 'ma60'])
    dev = abs(c / ma20 - 1)
    fresh_high = c > prev20
    # 突破后回踩再启动：近40日内曾创20日新高，且从近期高点回撤≥5%后回踩到MA20
    #（昨日收盘≤MA20）再重新站上（今日在MA20±2%内），过滤横盘震荡
    dip = float(df.loc[i-1, '收盘']) <= float(df.loc[i-1, 'ma20'])
    broke_recently = False
    for j in range(max(21, i - 39), i + 1):
        if float(df.loc[j, '收盘']) > float(df.loc[j-20:j-1, '最高'].max()):
            broke_recently = True
            break
    recent_high = float(df.loc[max(0, i - 40):i - 1, '最高'].max())
    pullback = recent_high > 0 and (c / recent_high - 1) <= -0.05
    reentry = broke_recently and dip and pullback
    triggered = c > ma60 and ma60_up and dev <= 0.02 and (fresh_high or reentry)
    score = 0
    if ma60_up and c > ma60: score += 40
    if dev <= 0.05: score += int(max(0, 30 * (1 - dev / 0.05)))
    if fresh_high: score += 30
    elif reentry: score += 20
    else: score += int(max(0, 30 * (1 - (prev20-c)/prev20/0.05)))
    return dict(triggered=triggered, score=min(100, score), price=c,
                buy_ref=ma20, stop=c*0.95, target=float(df.loc[i, 'ma10']),
                note=f'MA60向上={ma60_up} 偏离MA20={dev*100:.1f}%'
                     f'{"（今日新高）" if fresh_high else "（突破后回踩再启动）"}',
                tag='MA60向上+回踩MA20+新高' if fresh_high else 'MA60向上+突破后回踩MA20再启动')

def _a_s10(i, df):
    """RSI超卖超买反转：RSI上穿20买入，下穿80卖出"""
    if i < 21: return None
    c = float(df.loc[i, '收盘'])
    rsi = float(df.loc[i, 'rsi14']); prev = float(df.loc[i-1, 'rsi14'])
    triggered = rsi > 20 and prev <= 20
    score = 0
    if rsi <= 30: score = int(100 - abs(rsi - 20) * 5)
    elif rsi <= 50: score = int(max(0, 50 - (rsi - 30) * 2.5))
    else: score = int(max(0, 25 - (rsi - 50)))
    return dict(triggered=triggered, score=min(100, max(0, score)), price=c,
                buy_ref=c, stop=c*0.95, target=None,
                note=f'RSI={rsi:.1f}（前值{prev:.1f}）', tag='RSI上穿20')

def _a_chan(i, df):
    """缠论三买：前一日（i-1）为三买信号日（分型确认日），当日收盘确认、次日开盘买入；
    止盈8%/止损5%。非触发日返回 None（全市场每日扫描开销低）。"""
    if i < 1:
        return None
    sigs = bt._chan_signals(df, {'三买'})
    price = sigs.get(i - 1)
    if price is None:
        return None
    c = float(df.loc[i, '收盘'])
    return dict(triggered=True, score=80, price=c,
                buy_ref=round(price, 2), stop=round(price * 0.95, 2),
                target=round(price * 1.08, 2),
                note=f'缠论三买：突破中枢后回调不破ZG（信号价{price:.2f}，+8%止盈/-5%止损）',
                tag='缠论三买')


def _a_s16_impl(i, df, min_lb):
    """龙头接力共用：当日（i）收盘该股为【全市场涨停池最强龙头】（连板数最高、量比次，
    连板≥min_lb）→ 次日开盘买入，与回测 lag=1 完全对齐（sfunc 亦用当日 leader）；
    次日开盘顶板买不进/低开证伪 → 执行日废弃不机械买入（引擎执行守卫处理），
    断板/浮亏8%止损由卖出逻辑处理。非龙头日返回 None。"""
    code = df.attrs.get('_code')
    if not code:
        return None
    leaders = bt._s16_leaders(min_lb=min_lb)
    _d = df.loc[i, '日期']
    sig = leaders.get(_d)
    if sig is None or sig[0] != code:
        return None
    prev_c = float(df.loc[i - 1, '收盘'])
    c = float(df.loc[i, '收盘'])
    if min_lb >= 3:
        _head = '龙头接力V2：全市场涨停池最强龙头(连板≥3)'
    else:
        _head = '龙头接力：全市场涨停池最强龙头'
    note = '%s（%d板，量比%.2f）：断板/浮亏8%%走' % (_head, sig[1], sig[2])
    return dict(triggered=True, score=95, price=c,
                buy_ref=round(prev_c, 2), stop=round(prev_c * 0.92, 2),
                target=round(prev_c * 1.20, 2),
                note=note,
                tag='龙头接力')


def _a_s16(i, df):
    """龙头接力 base：当日（i）收盘该股为全市场涨停池最强龙头（不限连板）→ 次日开盘买入。"""
    return _a_s16_impl(i, df, 1)


def _a_s16_v2(i, df):
    """龙头接力 V2：当日（i）收盘该股为全市场涨停池最强龙头且连板≥3 → 次日开盘买入。"""
    return _a_s16_impl(i, df, 3)


STOCK_ANALYZERS = {
    'S1_Christian突破回踩': _a_s1,
    'S2_MACD金叉KDJ死叉': _a_s2,
    'S3_吊灯止损趋势跟踪': _a_s3,
    'S4_20日线回踩放量': _a_s4,
    'S5_止盈15止损5': _a_s5,
    'S6_布林下轨做T': _a_s6,
    'S16_龙头接力': _a_s16,
    'S16_龙头接力V2': _a_s16_v2,
    'S9_趋势线突破回踩': _a_s9,
    'S10_RSI超卖超买反转': _a_s10,
    'S15_缠论三买': _a_chan,
}

# ============ 指数策略 ============

def _idx_state(path, name, buy_line, add_line, sell_line, clear_line, vkey):
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['日期'] = pd.to_datetime(df['日期'])
    df = df.sort_values('日期').reset_index(drop=True)
    row = df.iloc[-1]
    v = float(row[vkey]); px = float(row['指数'])
    d = row['日期'].strftime('%Y-%m-%d')
    if v < buy_line:
        action, tag, score = '触发建仓', f'估值{v:.3f} < {buy_line}', 100
    elif v <= add_line * 1.02:
        action, tag, score = '接近建仓', f'估值{v:.3f} 接近{buy_line}', int(max(20, 80*(1-(v-buy_line)/buy_line/0.2)))
    elif v < sell_line:
        action, tag, score = '持有/观望', f'估值{v:.3f} 在{buy_line}-{sell_line}区间', int(max(10, 40*(sell_line-v)/(sell_line-buy_line)))
    else:
        action, tag, score = '减仓/清仓区', f'估值{v:.3f} 已到{sell_line}上方', 0
    return dict(triggered=(v < buy_line), score=min(100, score), price=px,
                buy_ref=buy_line, stop=None, target=sell_line,
                note=f'当前估值{v:.3f}，买入线{buy_line}，卖出线{sell_line}', tag=action, date=d)

def analyze_index(strategy):
    if strategy == 'S7_沪深300PE分批低买高卖':
        r = _idx_state('data/csv/hs300_pe.csv', '沪深300', 12.5, 8.75, 12.5, 16.25, '滚动市盈率')
        r['name'] = '沪深300指数(PE)'
    else:
        r = _idx_state('data/csv/hsly_pb.csv', '上证红利', 0.85, 0.75, 0.90, 1.00, '市净率')
        r['name'] = '上证红利指数(PB)'
    r['code'] = ''
    r['score'] = min(100, r['score'])
    return r

# ============ 对外接口 ============

def scan_stock_strategy(sname, realtime=None, codes=None):
    """扫描策略 sname 下所有股票，返回 [(score, result), ...] 降序

    ★ 2026-09-18 新增 codes：只扫指定的股票（**盘中持仓跟踪**用）。
      轮值策略每天全市场扫一次已足够；有持仓的非轮值策略若也全市场扫，成本 ×N 不可接受，
      故只对其**持仓股**（通常 1 只）做窄扫描，留痕到 /signals（watch 行）。
      codes 传 None（默认）行为不变 = 全市场；传列表则只扫这些代码（不在 STOCKS 里的自动忽略）。
    """
    if sname in ('S7_沪深300PE分批低买高卖', 'S8_红利低估值分批买入'):
        r = analyze_index(sname)
        return [r]
    fn = STOCK_ANALYZERS[sname]
    if codes is not None:
        _want = {str(c) for c in codes}
        _pool = [(c, n) for c, n in STOCKS.items() if c in _want]
    else:
        _pool = list(STOCKS.items())
    out = []
    for code, name in _pool:
        try:
            df = bt.load_stock(code)
            rt = (realtime or {}).get(code)
            if rt and rt.get('date'):
                df, _ = _apply_realtime(df, rt, rt['date'], code)
            df = bt.add_indicators(df)
            i = len(df) - 1
            r = fn(i, df)
            if r is None:
                continue
            r = dict(r)
            r['code'] = code; r['name'] = name
            r['buy_ref'] = _num(r.get('buy_ref'))
            r['stop'] = _num(r.get('stop'))
            r['target'] = _num(r.get('target'))
            r['price'] = _num(r['price'])
            if rt and rt.get('date'):
                r['data_date'] = rt['date']
            out.append(r)
        except Exception as e:
            out.append(dict(code=code, name=name, price=None, score=-1, note=f'err {e}',
                            triggered=False, buy_ref=None, stop=None, target=None, tag=''))
    out.sort(key=lambda x: x['score'], reverse=True)
    return out
