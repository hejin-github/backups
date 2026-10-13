# -*- coding: utf-8 -*-
"""读取 run_result.pkl，输出指标表 metrics.csv 与净值曲线 nav_curves.png 到 backtest_results/。

用法: python metrics.py
指标: 累计收益、年化收益、最大回撤、胜率、盈亏比、夏普、Calmar、笔数
"""
import os, pickle
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

WORK = os.getcwd()
OUT = os.path.join(WORK, "backtest_results")
os.makedirs(OUT, exist_ok=True)
RF = 0.02

with open(os.path.join(WORK, "run_result.pkl"), "rb") as f:
    R = pickle.load(f)
nav = R["nav"]; trades = R["trades"]

def maxdd(s):
    roll = s.cummax()
    return ((s - roll)/roll).min()

def metrics_for_nav(s):
    s = s.dropna()
    n = len(s)
    cum = s.iloc[-1]/s.iloc[0] - 1
    ann = (s.iloc[-1]/s.iloc[0])**(252.0/n) - 1
    dd = maxdd(s)
    dr = s.pct_change().dropna()
    sharpe = (dr.mean() - RF/252)/dr.std()*np.sqrt(252) if dr.std() > 0 else np.nan
    calmar = ann/abs(dd) if dd < 0 else np.nan
    return cum, ann, dd, sharpe, calmar

rows = []
strat_cols = ["V1_junge","V2_tongchen","V3_hedan","V4_thorp","bench_hs300","bench_zz500"]
for c in strat_cols:
    s = nav[c].dropna()
    cum, ann, dd, sharpe, calmar = metrics_for_nav(s)
    t = trades[trades["strategy"] == c] if len(trades) else pd.DataFrame()
    if len(t) > 0:
        rets = t["ret_pct"].values/100.0
        wins = rets[rets > 0]; losses = rets[rets <= 0]
        wr = len(wins)/len(rets)
        pl = wins.mean()/abs(losses.mean()) if len(losses) > 0 and losses.mean() != 0 else np.nan
        ntr = len(t)
    else:
        wr = np.nan; pl = np.nan; ntr = 0
    rows.append({"strategy": c, "cum_ret": cum, "ann_ret": ann, "max_dd": dd,
                 "win_rate": wr, "pl_ratio": pl, "sharpe": sharpe, "calmar": calmar,
                 "n_trades": ntr})
m = pd.DataFrame(rows)
m.to_csv(os.path.join(OUT, "metrics.csv"), index=False, encoding="utf-8-sig")
print(m.to_string())

fig, ax = plt.subplots(figsize=(10, 6))
for c in strat_cols:
    ax.plot(nav.index, nav[c], label=c, lw=1.5)
ax.set_title("Strategy NAV (start=1.0)")
ax.legend(); ax.grid(alpha=0.3); ax.set_ylabel("NAV")
fig.autofmt_xdate()
fig.tight_layout()
fig.savefig(os.path.join(OUT, "nav_curves.png"), dpi=110)
print("PNG saved:", os.path.join(OUT, "nav_curves.png"))
