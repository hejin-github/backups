# -*- coding: utf-8 -*-
"""统一多头日频回测引擎 + V1–V4 策略变体。

约定:
 - 信号 T 日收盘计算，订单 T+1 开盘成交
 - 买入成交=开盘×(1+SLIP)，卖出成交=开盘×(1−SLIP)
 - 成本: 买入佣金0.025%；卖出佣金0.025%+印花税0.05%
 - 开盘涨停不能买；开盘跌停延后卖；T+1 不能卖当日买入

用法: python backtest_engine.py   （读取当前目录 prepared.pkl）
输出: backtest_results/nav_curves.csv, trades_summary.csv, run_result.pkl
策略变体定义（详见 references/三杰策略规则.md）:
  V1 钧哥绿盘低吸: 近30日有涨停+当日绿盘非涨跌停; 单票<=33%×2只; 自高回撤-10%/3日不创新高/7日到期
  V2 童辰冰点半路: 样本池涨停<=3 + 低位<=120日低*1.25 + 60日内涨停 + 涨幅[5%,8%); -4%止损/5日到期/3只等权
  V3 核弹小市值: 流通市值<100亿 + 股价<20 + 量比[2,5] + 250日涨停>=3 + 涨幅<9%未涨停; -6%或破MA5/5日到期/单票10%×3只
  V4 索普仓位: V3信号 + 半Kelly滚动仓位(EV<=0不开仓, 回撤20%减半, 30%停手, 单笔风险2%, 单票20%, 总仓80%)
"""
import pickle, os
import pandas as pd
import numpy as np

WORK = os.getcwd()
OUT = os.path.join(WORK, "backtest_results")
os.makedirs(OUT, exist_ok=True)
BT_START = pd.Timestamp("2023-10-01")
RF = 0.02

BUY_COMM = 0.00025
SELL_COMM = 0.00025 + 0.0005
SLIP = 0.002

with open(os.path.join(WORK, "prepared.pkl"), "rb") as f:
    B = pickle.load(f)
F = B["F"]; calendar = B["calendar"]; idx300 = B["idx300"]; idx500 = B["idx500"]
pool_up = B["pool_up"]

bt_dates = [d for d in calendar if d >= BT_START]
bt_idx0 = calendar.index(bt_dates[0])

def row_at(df, d):
    try:
        return df.loc[d]
    except KeyError:
        return None

def eligibility(df, r):
    if r is None: return False
    if not np.isfinite(r['close']): return False
    if r['tradestatus'] != 1: return False
    if r['isST'] != 0: return False
    return True

def build_signals():
    sig = {v: {d: [] for d in bt_dates} for v in ['V1','V2','V3','V4']}
    for code, df in F.items():
        for i in range(bt_idx0, len(calendar)):
            d = calendar[i]
            r = row_at(df, d)
            if not eligibility(df, r): continue
            pos = df.index.get_loc(d)
            if pos < 250: continue
            if r['limcnt30'] >= 1 and r['pctChg'] < 0 and not r['is_up'] and not r['is_down']:
                sig['V1'][d].append(code)
            if (np.isfinite(r['roll_low120']) and
                    r['close'] <= r['roll_low120']*1.25 and
                    r['limcnt60'] >= 1 and
                    r['pctChg'] >= 5.0 and r['pctChg'] < 8.0 and
                    not r['is_up']):
                sig['V2'][d].append(code)
            if (np.isfinite(r['float_mcap']) and r['float_mcap'] < 1e10 and
                    r['close'] < 20.0 and
                    np.isfinite(r['volratio']) and 2.0 <= r['volratio'] <= 5.0 and
                    r['limcnt250'] >= 3 and
                    r['pctChg'] < 9.0 and not r['is_up']):
                sig['V3'][d].append(code)
                sig['V4'][d].append(code)
    return sig

def run_backtest(sig, name, max_pos, mode):
    cash = 1.0
    positions = {}
    trades = []
    eq_curve = []
    pend_sell = []
    pend_buy = []
    closed_trades = []
    hwm = 1.0

    for i in range(bt_idx0, len(calendar)):
        d = calendar[i]
        if d < BT_START: continue

        def equity_now(dd):
            mv = 0.0
            for c, p in positions.items():
                rr = row_at(F[c], dd)
                px = rr['close'] if rr is not None else p['last_px']
                p['last_px'] = px
                mv += p['shares']*px
            return cash + mv

        # ---- 开盘：先卖后买 ----
        still_sell = []
        for c in pend_sell:
            if c not in positions: continue
            df = F[c]; r = row_at(df, d)
            if r is None:
                still_sell.append(c); continue
            open_ret = r['open']/r['preclose'] - 1
            thr = r['thr']
            if open_ret <= -(thr - 0.1)/100.0:   # 跌停开盘延卖
                still_sell.append(c); continue
            p = positions[c]
            if d <= p['entry_date']:              # T+1
                still_sell.append(c); continue
            fill = r['open']*(1-SLIP)
            proceeds = p['shares']*fill*(1-SELL_COMM)
            cash += proceeds
            ret = proceeds/(p['shares']*p['cost_price']) - 1
            trades.append({'strategy': name, 'code': c,
                           'open_date': p['entry_date'].date().isoformat(),
                           'close_date': d.date().isoformat(),
                           'days': p['hold_td'],
                           'ret_pct': round(ret*100,3),
                           'reason': p['reason']})
            closed_trades.append(ret)
            del positions[c]
        pend_sell = still_sell

        for c in pend_buy:
            if c in positions: continue
            if len(positions) >= max_pos: continue
            df = F[c]; r = row_at(df, d)
            if r is None: continue
            open_ret = r['open']/r['preclose'] - 1
            thr = r['thr']
            if open_ret >= (thr-0.1)/100.0 - 1e-9:   # 涨停开盘不能买
                continue
            eq = cash + sum(p['shares']*p['last_px'] for p in positions.values())
            fill = r['open']*(1+SLIP)
            if mode == 'V4':
                w = thorp_weight(eq, closed_trades, hwm,
                                 current_exposure=sum(p['shares']*p['last_px'] for p in positions.values()))
            elif mode == 'V1':
                w = min(0.33, 1.0/max_pos)
            elif mode == 'V2':
                w = 1.0/max_pos
            else:
                w = 0.10
            if w <= 0: continue
            budget = min(eq*w, cash*0.999)
            if budget <= 0: continue
            shares = budget/fill
            cost = shares*fill*(1+BUY_COMM)
            if cost > cash:
                shares = cash*0.999/(fill*(1+BUY_COMM))
                cost = shares*fill*(1+BUY_COMM)
            cash -= cost
            positions[c] = {'entry_date': d, 'cost_price': fill, 'shares': shares,
                            'last_px': r['close'], 'high_since': r['high'],
                            'hold_td': 0, 'reason': ''}
        pend_buy = []

        # ---- 收盘盯市 ----
        eq = equity_now(d)
        hwm = max(hwm, eq)
        eq_curve.append((d, eq))

        # ---- 收盘决策次日 ----
        for c, p in list(positions.items()):
            df = F[c]; r = row_at(df, d)
            if r is None: continue
            p['high_since'] = max(p['high_since'], r['high'])
            p['hold_td'] += 1
            held_days = p['hold_td']
            pnl = r['close']/p['cost_price'] - 1
            exit_flag = False
            if mode == 'V1':
                if r['close'] <= p['high_since']*0.90:
                    p['reason']='trail10'; exit_flag=True
                elif held_days >= 3 and r['close'] <= p['cost_price']:
                    p['reason']='noferm3'; exit_flag=True
                elif held_days >= 7:
                    p['reason']='expire7'; exit_flag=True
            elif mode == 'V2':
                if pnl <= -0.04:
                    p['reason']='stop4'; exit_flag=True
                elif held_days >= 5:
                    p['reason']='expire5'; exit_flag=True
            elif mode in ('V3','V4'):
                if pnl <= -0.06:
                    p['reason']='stop6'; exit_flag=True
                elif r['close'] < r['ma5'] and np.isfinite(r['ma5']):
                    p['reason']='breakma5'; exit_flag=True
                elif held_days >= 5:
                    p['reason']='expire5'; exit_flag=True
            if exit_flag and d > p['entry_date']:
                pend_sell.append(c)

        cands = sig[mode][d] if mode in sig else []
        if mode == 'V2' and pool_up.get(d, 999) > 3:
            cands = []
        free = max_pos - len(positions) - len(pend_sell)
        if cands and free > 0:
            todo = [c for c in cands if c not in positions and c not in pend_sell and c not in pend_buy]
            pend_buy.extend(todo[:free])
        pend_buy = [c for c in pend_buy if c not in positions]

    s = pd.Series(dict(eq_curve))
    s.name = name
    return s, trades

def thorp_weight(eq, closed_trades, hwm, current_exposure):
    prior_p, prior_b = 0.45, 1.5          # 弱正期望先验，防小样本过拟合
    recent = closed_trades[-40:] if len(closed_trades) >= 20 else []
    if len(recent) >= 10:
        wins = [x for x in recent if x > 0]; losses = [x for x in recent if x <= 0]
        p = len(wins)/len(recent)
        avgw = np.mean(wins) if wins else prior_b*0.05
        avgl = abs(np.mean(losses)) if losses else 0.05
        b = avgw/avgl if avgl > 0 else prior_b
        p = max(min(p, 0.9), 0.1); b = max(b, 0.2)
    else:
        p, b = prior_p, prior_b
    q = 1 - p
    ev = p*b - q
    if ev <= 0: return 0.0
    f_star = (b*p - q)/b
    alpha = 0.5
    dd = 1 - eq/hwm if hwm > 0 else 0
    if dd >= 0.30: return 0.0
    if dd >= 0.20: alpha *= 0.5
    f_use = alpha*f_star
    stop_pct = 0.06
    by_risk = eq*0.02/stop_pct
    by_kelly = eq*f_use/stop_pct
    by_single = eq*0.20
    by_total = eq*0.80 - current_exposure
    pos_val = min(by_risk, by_kelly, by_single, by_total)
    w = pos_val/eq
    return max(w, 0.0)

if __name__ == '__main__':
    sig = build_signals()
    for v in ['V1','V2','V3','V4']:
        print(f"{v} total signal-days candidates:", sum(len(x) for x in sig[v].values()))
    results = {}
    all_trades = []
    s1, t1 = run_backtest(sig, 'V1_junge', 2, 'V1'); results['V1_junge'] = s1; all_trades += t1
    s2, t2 = run_backtest(sig, 'V2_tongchen', 3, 'V2'); results['V2_tongchen'] = s2; all_trades += t2
    s3, t3 = run_backtest(sig, 'V3_hedan', 3, 'V3'); results['V3_hedan'] = s3; all_trades += t3
    s4, t4 = run_backtest(sig, 'V4_thorp', 3, 'V4'); results['V4_thorp'] = s4; all_trades += t4
    b300 = idx300['close'][idx300.index >= BT_START]; b300 = b300/b300.iloc[0]; b300.name = 'bench_hs300'
    b500 = idx500['close'][idx500.index >= BT_START]; b500 = b500/b500.iloc[0]; b500.name = 'bench_zz500'
    results['bench_hs300'] = b300; results['bench_zz500'] = b500

    nav = pd.DataFrame(results)
    nav.index.name = 'date'
    nav.to_csv(os.path.join(OUT, 'nav_curves.csv'))
    td = pd.DataFrame(all_trades)
    td.to_csv(os.path.join(OUT, 'trades_summary.csv'), index=False)
    print(nav.tail())
    print("trades:", len(td))
    with open(os.path.join(WORK, 'run_result.pkl'), 'wb') as f:
        pickle.dump({'nav': nav, 'trades': td}, f)
