# -*- coding: utf-8 -*-
"""全市场主板选股池历史日线拉取（新浪源，多进程每进程串行，避开 mini_racer 线程不安全）。"""
import sys, io, os, time, json, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)
import requests
_orig_init = requests.Session.__init__
def _np(self, *a, **kw):
    _orig_init(self, *a, **kw)
    self.trust_env = False
requests.Session.__init__ = _np

import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

os.makedirs('data/csv', exist_ok=True)
uni = pd.read_csv('data/stock_universe.csv', encoding='utf-8-sig')
items = [(str(r['code']).zfill(6), str(r['name'])) for _, r in uni.iterrows()]
END = datetime.date.today().strftime('%Y%m%d')
PROG = os.path.join('data', '_fetch_progress.json')
NWORKERS = 6


def _fetch_one(code, name):
    fp = f'data/csv/{code}.csv'
    sym = ('sh' if code.startswith(('6', '9')) else 'sz') + code
    for attempt in range(3):
        try:
            import akshare as ak
            df = ak.stock_zh_a_daily(symbol=sym, start_date='20180101', end_date=END)
            if df is None or df.empty:
                return code, 'empty'
            d = pd.DataFrame({
                '日期': df['date'].astype(str),
                '股票代码': code,
                '开盘': df['open'], '收盘': df['close'],
                '最高': df['high'], '最低': df['low'],
                '成交量': (df['volume'] / 100).round(0),   # 股 -> 手
                '成交额': df['amount'],
            })
            d['昨收'] = d['收盘'].shift(1)
            d['振幅'] = ((d['最高'] - d['最低']) / d['昨收'] * 100).round(2)
            d['涨跌幅'] = ((d['收盘'] / d['昨收'] - 1) * 100).round(2)
            d['涨跌额'] = (d['收盘'] - d['昨收']).round(3)
            d['换手率'] = (df['turnover'] * 100).round(4) if 'turnover' in df.columns else None
            cols = ['日期', '股票代码', '开盘', '收盘', '最高', '最低', '成交量',
                    '成交额', '振幅', '涨跌幅', '涨跌额', '换手率']
            d[cols].to_csv(fp, index=False, encoding='utf-8-sig')
            return code, 'ok'
        except Exception:
            time.sleep(2 * (attempt + 1))
    return code, 'err'


def process_chunk(chunk):
    out = []
    for code, name in chunk:
        out.append(_fetch_one(code, name))
    return out


def main():
    total = len(items)
    chunks = [items[i::NWORKERS] for i in range(NWORKERS)]
    done = ok = err = empty = 0
    err_list = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=NWORKERS) as ex:
        futs = [ex.submit(process_chunk, ch) for ch in chunks]
        for f in as_completed(futs):
            for code, st in f.result():
                done += 1
                if st == 'ok':
                    ok += 1
                elif st == 'empty':
                    empty += 1
                else:
                    err += 1
                    err_list.append(code)
            if done % 200 == 0 or done == total:
                el = time.time() - t0
                print(f'[{done}/{total}] ok={ok} empty={empty} err={err} '
                      f'elapsed={el/60:.1f}min eta={(total-done)*el/max(done,1)/60:.1f}min', flush=True)
                json.dump({'done': done, 'total': total, 'ok': ok, 'err': err_list[-50:]},
                          open(PROG, 'w'))
    print('DONE ok=%d empty=%d err=%d err_list=%s' % (ok, empty, err, err_list[:30]))


if __name__ == '__main__':
    main()
