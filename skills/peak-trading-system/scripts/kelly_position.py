# -*- coding: utf-8 -*-
"""索普式单笔仓位计算器。

用法:
    python kelly_position.py            # 交互输入
    python kelly_position.py 0.45 2.0 100000 0.06 0.5 0.02 0.2 0.8
参数: 胜率p 盈亏比b 权益E 止损幅度s Kelly折扣α 单笔风险上限 单票上限 总仓上限

输出: EV、盈亏平衡胜率、全Kelly、半Kelly、风险预算市值、Kelly市值、单票上限、总仓上限、最终买入市值与仓位占比。
"""
import sys

def solve(p, b, E, s, alpha, risk_cap=0.02, single_cap=0.20, total_cap=0.80):
    q = 1 - p
    ev = p*b - q
    be = 1.0/(1 + b)
    f_star = (b*p - q)/b
    f_use = alpha*f_star
    by_risk = E*risk_cap/s
    by_kelly = E*f_use/s
    by_single = E*single_cap
    by_total = E*total_cap
    pos = min(by_risk, by_kelly, by_single, by_total)
    return ev, be, f_star, f_use, by_risk, by_kelly, by_single, by_total, pos

def fmt(x):
    return f"{x:,.0f}" if abs(x) >= 1 else f"{x:.4f}"

def main():
    if len(sys.argv) >= 7:
        p, b, E, s, alpha, risk_cap = (float(sys.argv[1]), float(sys.argv[2]),
                                       float(sys.argv[3]), float(sys.argv[4]),
                                       float(sys.argv[5]), float(sys.argv[6]))
        single_cap = float(sys.argv[7]) if len(sys.argv) > 7 else 0.20
        total_cap = float(sys.argv[8]) if len(sys.argv) > 8 else 0.80
    else:
        p = float(input("胜率 p (0~1): "))
        b = float(input("盈亏比 b (平均盈利/平均亏损): "))
        E = float(input("账户权益 E: "))
        s = float(input("止损幅度 s (如0.06): "))
        alpha = float(input("Kelly折扣 α (默认0.5): ") or 0.5)
        risk_cap = float(input("单笔风险上限 (默认0.02): ") or 0.02)
        single_cap = float(input("单票上限 (默认0.20): ") or 0.20)
        total_cap = float(input("总仓上限 (默认0.80): ") or 0.80)

    ev, be, f_star, f_use, by_risk, by_kelly, by_single, by_total, pos = \
        solve(p, b, E, s, alpha, risk_cap, single_cap, total_cap)

    print("\n========== 索普式仓位测算 ==========")
    print(f"EV/R      = {ev:+.4f}  ({'✓ 可做' if ev > 0 else '✗ 不做：期望≤0'})")
    print(f"盈亏平衡胜率 p* = {be*100:.1f}%  (当前胜率 {p*100:.1f}%)")
    print(f"全Kelly f* = {f_star*100:.1f}%   半Kelly f = {f_use*100:.1f}%")
    print(f"风险预算市值   = {fmt(by_risk)}  (权益×{risk_cap:.0%}/止损{s*100:.0%})")
    print(f"Kelly市值      = {fmt(by_kelly)}")
    print(f"单票上限       = {fmt(by_single)}")
    print(f"总仓剩余上限   = {fmt(by_total)}")
    print(f"→ 最终买入市值 = {fmt(pos)}  (占权益 {pos/E*100:.1f}%)")
    if ev <= 0:
        print("\n提示: EV≤0，本信号不应开仓。提高胜率或盈亏比后再测。")
    print("====================================")

if __name__ == "__main__":
    main()
