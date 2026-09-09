# -*- coding: utf-8 -*-
"""拉取全市场沪深主板股票列表（剔除 ST/退市/科创板/创业板/北交所），
结果写入 data/stock_universe.csv，供 backtest.STOCKS / advisor 动态加载。"""
import sys, io, os, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import akshare as ak
import pandas as pd

OUT = os.path.join('data', 'stock_universe.csv')


def eligible(code, name):
    """仅沪深主板：600/601/603/605 + 000/001/002/003；
    剔除 创业板300/301/302、科创板688/689、北交所4/8/920、
    ST/*ST/S、退市（名称含退）、暂停上市。"""
    c, n = str(code).zfill(6), str(name).upper()
    if not c.startswith(('600', '601', '603', '605', '000', '001', '002', '003')):
        return False
    if c.startswith(('300', '301', '302', '688', '689', '920')):
        return False
    if 'ST' in n or '退' in n or 'PT' in n or n.startswith('S'):
        return False
    return True


def main():
    # 1) 全市场 A 股列表（代码+名称，5550+ 只）
    df = ak.stock_info_a_code_name()
    print('raw rows:', len(df))
    df.columns = ['code', 'name']
    df['code'] = df['code'].astype(str).str.zfill(6)
    mask = df.apply(lambda r: eligible(r['code'], r['name']), axis=1)
    uni = df[mask].reset_index(drop=True)
    uni.to_csv(OUT, index=False, encoding='utf-8-sig')
    print('universe rows:', len(uni))
    print('head:'); print(uni.head(10).to_string(index=False))
    print('tail:'); print(uni.tail(5).to_string(index=False))
    # 2) 板块统计
    def board(c):
        if c.startswith('600'): return '600'
        if c.startswith('601'): return '601'
        if c.startswith('603'): return '603'
        if c.startswith('605'): return '605'
        if c.startswith('000'): return '000'
        if c.startswith('001'): return '001'
        if c.startswith('002'): return '002'
        if c.startswith('003'): return '003'
        return '?'
    print(uni.groupby(uni['code'].map(board)).size().to_string())


if __name__ == '__main__':
    main()
