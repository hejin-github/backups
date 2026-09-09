# -*- coding: utf-8 -*-
"""财报问题股过滤：基于 2025 年报 + 2026Q1 业绩报表，剔除
   - 最近一期（2026Q1）净利润 < 0（亏损）
   - 最近一期每股净资产 < 0（资不抵债）
   - 2025 年报与 2026Q1 连续两期净利润 < 0（持续亏损、ST 前兆）
覆盖写回 data/stock_universe.csv，剔除名单存 data/fin_problem_codes.csv，
并删除 dynamic_pool_index.json / dynamic_pool_bt.json 强制全量重建。"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import akshare as ak
import pandas as pd

UNI = os.path.join('data', 'stock_universe.csv')
PROB = os.path.join('data', 'fin_problem_codes.csv')


def main():
    uni = pd.read_csv(UNI, dtype={'code': str})
    uni['code'] = uni['code'].str.zfill(6)
    print('universe before:', len(uni))

    q1 = ak.stock_yjbb_em(date='20260331')[['股票代码', '股票简称', '净利润-净利润', '每股净资产']]
    a5 = ak.stock_yjbb_em(date='20251231')[['股票代码', '股票简称', '净利润-净利润']]
    q1.columns = ['code', 'name', 'np_q1', 'bps']
    a5.columns = ['code', 'name', 'np_a5']
    q1['code'] = q1['code'].astype(str).str.zfill(6)
    a5['code'] = a5['code'].astype(str).str.zfill(6)

    m = uni.merge(q1[['code', 'np_q1', 'bps']], on='code', how='left') \
           .merge(a5[['code', 'np_a5']], on='code', how='left')
    # 财报问题 = 资不抵债（每股净资产<0） 或 2025年报与2026Q1连续两期亏损；
    # 单季亏损但全年盈利（季节性，如养殖/面板）不属于"财报有问题"，保留。
    bad = m[(m['bps'].notna() & (m['bps'] < 0)) |
            (m['np_a5'].notna() & (m['np_a5'] < 0) &
             m['np_q1'].notna() & (m['np_q1'] < 0))].copy()
    print('problem rows:', len(bad))
    print(bad[['code', 'name', 'np_a5', 'np_q1', 'bps']].to_string(index=False))

    keep = uni[~uni['code'].isin(bad['code'])].reset_index(drop=True)
    keep.to_csv(UNI, index=False, encoding='utf-8-sig')
    bad[['code', 'name', 'np_a5', 'np_q1', 'bps']].to_csv(PROB, index=False, encoding='utf-8-sig')
    print('universe after:', len(keep))

    for f in ('data/dynamic_pool_index.json', 'data/dynamic_pool_bt.json'):
        if os.path.exists(f):
            os.remove(f)
            print('removed', f, '-> force full rebuild')


if __name__ == '__main__':
    main()
