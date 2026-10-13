# -*- coding: utf-8 -*-
"""下载沪深300∪中证500 日线数据（baostock，前复权），缓存到当前目录 stocks_data.pkl。

用法:
    python download_data.py [起始日期] [结束日期]
默认: 起始 2022-06-01（留一年预热），结束为最近交易日。回测正式区间见 prepare.py。
依赖: pip install baostock pandas
"""
import baostock as bs
import pandas as pd
import numpy as np
import pickle, time, sys, os

WORK = os.getcwd()
START = sys.argv[1] if len(sys.argv) > 1 else "2022-06-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2026-10-09"

lg = bs.login()
print("login:", lg.error_code, lg.error_msg)

def get_constituents():
    hs = bs.query_hs300_stocks()
    hs_df = []
    while (hs.error_code == '0') & hs.next():
        hs_df.append(hs.get_row_data())
    hs = pd.DataFrame(hs_df, columns=hs.fields)
    zz = bs.query_zz500_stocks()
    zz_df = []
    while (zz.error_code == '0') & zz.next():
        zz_df.append(zz.get_row_data())
    zz = pd.DataFrame(zz_df, columns=zz.fields)
    codes = list(dict.fromkeys(list(hs['code']) + list(zz['code'])))
    return hs, zz, codes

hs, zz, codes = get_constituents()
print("HS300:", len(hs), "ZZ500:", len(zz), "union:", len(codes))

fields = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,isST,tradestatus"

def fetch(code):
    rs = bs.query_history_k_data_plus(code, fields, start_date=START, end_date=END,
                                      frequency="d", adjustflag="2")  # qfq
    rows = []
    while (rs.error_code == '0') & rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return None
    return pd.DataFrame(rows, columns=rs.fields)

data = {}
fails = []
t0 = time.time()
for i, code in enumerate(codes):
    df = None
    for attempt in range(3):
        try:
            df = fetch(code)
            break
        except Exception:
            time.sleep(1)
    if df is None or len(df) < 60:
        fails.append(code)
    else:
        data[code] = df
    if (i + 1) % 50 == 0:
        print(f"{i+1}/{len(codes)} done, {len(data)} kept, {time.time()-t0:.0f}s", flush=True)

idx = {}
for ic in ["sh.000300", "sh.000905"]:
    rs = bs.query_history_k_data_plus(ic, "date,open,high,low,close,volume",
                                      start_date=START, end_date=END, frequency="d", adjustflag="3")
    rows = []
    while (rs.error_code == '0') & rs.next():
        rows.append(rs.get_row_data())
    idx[ic] = pd.DataFrame(rows, columns=rs.fields)
    print("index", ic, len(idx[ic]))

bs.logout()

out = {"stocks": data, "indices": idx, "hs300": hs, "zz500": zz, "fails": fails}
with open(os.path.join(WORK, "stocks_data.pkl"), "wb") as f:
    pickle.dump(out, f)
print("SAVED stocks_data.pkl. stocks:", len(data), "fails:", fails[:20], "n_fails:", len(fails))
