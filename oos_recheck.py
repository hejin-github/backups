# -*- coding: utf-8 -*-
"""样本外(OOS)业绩重算 —— 只读、可重复、不改任何生产逻辑。

背景：stock_bot/history_engine.py:121-134 只有 OOS 钩子(OOS_START / is_oos)，
注释写明「仅做统计隔离、生产口径不变、adopted=[]」，即「有钩子没重算」。
本脚本把钩子真正跑起来：按时间序列把策略逐笔记录切成训练段(IS)/样本外段(OOS)，
分别统计每个策略的 笔数 / 胜率 / 净盈亏 / 期望(每笔)。

数据源（只读，均位于父目录 data/，路径锚定见 backtest.py:20 DATA_ROOT）：
  A. 生产策略账本 strategy_daily.json —— 14 策略逐笔，按「账本中位交易日」切分
  B. 全历史回测账本 backtest_trades.csv —— 按钩子 OOS_START=2024-01-01 切分，样本更充足

用法：
  C:/Users/hejin/AppData/Local/Programs/Python/Python312/python.exe oos_recheck.py
输出：
  父目录 data/oos_report.json（与账本 DATA_ROOT 同目录）。结果确定性，可重复运行。
"""
import json
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
DATA = os.path.join(WORKSPACE, 'data')
LEDGER = os.path.join(DATA, 'strategy_daily.json')
TRADES = os.path.join(DATA, 'backtest_trades.csv')
OUT = os.path.join(DATA, 'oos_report.json')

OOS_START = '2024-01-01'  # 与 history_engine.py:127 钩子口径一致


def stat(sub):
    """一段样本的 笔数 / 胜率 / 净盈亏 / 期望(每笔)。胜率以 net_pnl>0 计。"""
    n = len(sub)
    if n == 0:
        return {'trades': 0, 'win_rate': None, 'net_pnl': 0.0, 'expect_per_trade': None}
    return {
        'trades': int(n),
        'win_rate': round(float((sub['net_pnl'] > 0).mean() * 100), 2),
        'net_pnl': round(float(sub['net_pnl'].sum()), 2),
        'expect_per_trade': round(float(sub['net_pnl'].mean()), 2),
    }


def split_stats(df, split_date, label):
    """buy_date < split_date 为训练段(IS)，>= 为样本外段(OOS)，逐策略统计。"""
    df = df.copy()
    df['buy_date'] = pd.to_datetime(df['buy_date'])
    cut = pd.Timestamp(split_date)
    is_ = df[df['buy_date'] < cut]
    oos = df[df['buy_date'] >= cut]
    per = {}
    for name in sorted(df['strategy'].unique()):
        per[name] = {
            'is': stat(is_[is_['strategy'] == name]),
            'oos': stat(oos[oos['strategy'] == name]),
        }
    neg = [k for k, v in per.items()
           if v['oos']['expect_per_trade'] is not None and v['oos']['expect_per_trade'] < 0]
    pos = [k for k, v in per.items()
           if v['oos']['expect_per_trade'] is not None and v['oos']['expect_per_trade'] > 0]
    nodata = [k for k, v in per.items() if v['oos']['trades'] == 0]
    return {
        'label': label,
        'split_date': str(split_date),
        'rule': 'buy_date < split_date 为训练段(IS)；buy_date >= split_date 为样本外段(OOS)',
        'is_trades': int(len(is_)),
        'oos_trades': int(len(oos)),
        'is_total_net_pnl': round(float(is_['net_pnl'].sum()), 2),
        'oos_total_net_pnl': round(float(oos['net_pnl'].sum()), 2),
        'strategies': per,
        'oos_negative_expectation': neg,
        'oos_positive_expectation': pos,
        'oos_no_trades': nodata,
    }


def main():
    # ---- A. 生产策略账本：按账本中位交易日切分 ----
    led = json.load(open(LEDGER, encoding='utf-8'))
    a = pd.DataFrame([t for s in led['strategies'] for t in s['trades']])
    a = a.dropna(subset=['net_pnl'])
    days = sorted(a['buy_date'].unique())
    median_day = days[len(days) // 2]
    part_a = split_stats(a, median_day,
                         'A 生产策略账本 strategy_daily.json（中位交易日切分）')
    part_a['ledger_generated'] = led.get('generated')
    part_a['ledger_start'] = led.get('start')
    part_a['ledger_end'] = led.get('end')
    part_a['trading_days_total'] = len(days)
    part_a['split_basis'] = ('全部逐笔 buy_date 去重后共 %d 个交易日，取第 %d 个（中位）%s'
                             % (len(days), len(days) // 2, median_day))

    # ---- B. 全历史回测账本：复用 OOS 钩子口径 ----
    b = pd.read_csv(TRADES, encoding='utf-8-sig', low_memory=False)
    b = b.dropna(subset=['net_pnl'])
    part_b = split_stats(b, OOS_START,
                         'B 全历史回测账本 backtest_trades.csv（钩子 OOS_START 切分）')
    part_b['hook'] = 'history_engine.py:127 OOS_START=2024-01-01 / is_oos(d)'
    part_b['date_range'] = [str(b['buy_date'].min()), str(b['buy_date'].max())]

    result = {
        'purpose': '样本外(OOS)业绩重算：只读账本，不修改任何策略参数/生产选股逻辑',
        'data_sources': {'A_ledger': LEDGER, 'B_trades': TRADES},
        'part_a': part_a,
        'part_b': part_b,
        'disclaimer': ('结论仅为统计观察；负期望策略只做处置建议，'
                       '未自动禁用/删除任何策略，也未改动生产代码'),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')

    # ---- stdout 摘要 ----
    print('已写出:', OUT)
    for tag, part in (('A', part_a), ('B', part_b)):
        print('\n=== %s %s ===' % (tag, part['label']))
        print('切分点 %s | IS %d 笔 / OOS %d 笔 | IS净盈亏 %.2f / OOS净盈亏 %.2f'
              % (part['split_date'], part['is_trades'], part['oos_trades'],
                 part['is_total_net_pnl'], part['oos_total_net_pnl']))
        print('%-26s %8s %10s %12s %12s'
              % ('策略', 'OOS笔数', 'OOS胜率%', 'OOS净盈亏', 'OOS期望/笔'))
        for k, v in part['strategies'].items():
            o = v['oos']
            print('%-26s %8d %10s %12s %12s'
                  % (k, o['trades'], o['win_rate'], o['net_pnl'], o['expect_per_trade']))
        print('OOS 负期望策略(%d): %s'
              % (len(part['oos_negative_expectation']),
                 ', '.join(part['oos_negative_expectation']) or '无'))
        if part['oos_no_trades']:
            print('OOS 无交易(无法判定): %s' % ', '.join(part['oos_no_trades']))
    return result


if __name__ == '__main__':
    main()
