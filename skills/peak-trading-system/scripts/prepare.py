# -*- coding: utf-8 -*-
"""读取 stocks_data.pkl，构建全部策略共用的个股特征帧，保存 prepared.pkl 到当前目录。

用法: python prepare.py [回测开始日期] [回测结束日期]
默认: 2023-10-01 至 2026-10-09（约3年）。数据需从 2022-06 起含预热。
"""
import pickle, os, sys
import pandas as pd
import numpy as np

WORK = os.getcwd()
BT_START = sys.argv[1] if len(sys.argv) > 1 else "2023-10-01"
BT_END = sys.argv[2] if len(sys.argv) > 2 else "2026-10-09"

with open(os.path.join(WORK, "stocks_data.pkl"), "rb") as f:
    raw = pickle.load(f)

def board_of(code):
    c = code.split('.')[1]
    if c.startswith('300') or c.startswith('688') or c.startswith('301'):
        return 20.0
    return 10.0

F = {}
for code, df in raw['stocks'].items():
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    for col in ['open','high','low','close','preclose','volume','amount','turn','pctChg']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df['isST'] = pd.to_numeric(df['isST'], errors='coerce').fillna(0).astype(int)
    df['tradestatus'] = pd.to_numeric(df['tradestatus'], errors='coerce').fillna(1).astype(int)
    df = df.sort_values('date').set_index('date')
    thr = board_of(code)
    df['thr'] = thr
    df['is_up'] = df['pctChg'] >= (thr - 0.1)            # 收盘涨停
    df['is_down'] = df['pctChg'] <= -(thr - 0.1)         # 收盘跌停
    df['ma5'] = df['close'].rolling(5).mean()
    df['roll_low120'] = df['low'].rolling(120).min()
    df['limcnt30'] = df['is_up'].rolling(30).sum()
    df['limcnt60'] = df['is_up'].rolling(60).sum()
    df['limcnt250'] = df['is_up'].rolling(250).sum()
    df['volma15'] = df['volume'].shift(1).rolling(15).mean()
    df['volratio'] = df['volume'] / df['volma15']
    df['float_shares'] = df['volume'] / (df['turn']/100.0)   # 换手率推导流通股本
    df['float_mcap'] = df['close'] * df['float_shares']      # 流通市值（元）
    F[code] = df

idx300 = raw['indices']['sh.000300'].copy()
idx300['date'] = pd.to_datetime(idx300['date'])
for c in ['open','high','low','close','volume']:
    idx300[c] = pd.to_numeric(idx300[c], errors='coerce')
idx300 = idx300.sort_values('date').set_index('date')
calendar = list(idx300.index)

idx500 = raw['indices']['sh.000905'].copy()
idx500['date'] = pd.to_datetime(idx500['date'])
for c in ['open','high','low','close','volume']:
    idx500[c] = pd.to_numeric(idx500[c], errors='coerce')
idx500 = idx500.sort_values('date').set_index('date')

pool_up = pd.Series(0, index=calendar)
for code, df in F.items():
    common = df.index.intersection(calendar)
    pool_up.loc[common] += df.loc[common, 'is_up'].astype(int)

print("stocks:", len(F), "calendar:", len(calendar),
      "range:", calendar[0].date(), calendar[-1].date())
print("pool limit-up/day:", pool_up.describe().round(2).to_dict())

bundle = {"F": F, "calendar": calendar, "idx300": idx300, "idx500": idx500, "pool_up": pool_up}
with open(os.path.join(WORK, "prepared.pkl"), "wb") as f:
    pickle.dump(bundle, f)
print("prepared.pkl saved")
