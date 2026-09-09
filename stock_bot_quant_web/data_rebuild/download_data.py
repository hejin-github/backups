# -*- coding: utf-8 -*-
"""下载 A 股行情数据（股票池 + 指数）并缓存 CSV"""
import sys, io, os, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import akshare as ak
import pandas as pd

os.makedirs('data/csv', exist_ok=True)

# 中低价、流动性好的代表股（确保 5000 元能买一手）
STOCKS = {
    '000725': '京东方A',
    '600010': '包钢股份',
    '601288': '农业银行',
    '600028': '中国石化',
    '601668': '中国建筑',
    '600030': '中信证券',
    '000651': '格力电器',
    '600887': '伊利股份',
    '601012': '隆基绿能',
    '002415': '海康威视',
    '600276': '恒瑞医药',
    '000002': '万科A',
    '600036': '招商银行',
    '600900': '长江电力',
    '601398': '工商银行',
}

START = '20180101'   # 预留指标计算窗口
END = '20261231'

for code, name in STOCKS.items():
    fp = f'data/csv/{code}.csv'
    if os.path.exists(fp):
        print(f'{code} {name}: cached')
        continue
    for attempt in range(4):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=START, end_date=END)
            if df is None or df.empty:
                raise ValueError('empty')
            df.to_csv(fp, index=False, encoding='utf-8-sig')
            print(f'{code} {name}: OK {len(df)} rows')
            break
        except Exception as e:
            print(f'{code} {name}: attempt {attempt} ERR {type(e).__name__} {str(e)[:80]}')
            time.sleep(4)
    time.sleep(1)

# 沪深300 指数 PE（含指数点位）
fp = 'data/csv/hs300_pe.csv'
if not os.path.exists(fp):
    for attempt in range(4):
        try:
            df = ak.stock_index_pe_lg(symbol='沪深300')
            df.to_csv(fp, index=False, encoding='utf-8-sig')
            print(f'hs300_pe: OK {len(df)} rows')
            break
        except Exception as e:
            print(f'hs300_pe: attempt {attempt} ERR {str(e)[:100]}')
            time.sleep(4)

# 上证红利指数 PB（含指数点位）
fp = 'data/csv/hsly_pb.csv'
if not os.path.exists(fp):
    for attempt in range(4):
        try:
            df = ak.stock_index_pb_lg(symbol='上证红利')
            df.to_csv(fp, index=False, encoding='utf-8-sig')
            print(f'hsly_pb: OK {len(df)} rows')
            break
        except Exception as e:
            print(f'hsly_pb: attempt {attempt} ERR {str(e)[:100]}')
            time.sleep(4)

print('DONE')
