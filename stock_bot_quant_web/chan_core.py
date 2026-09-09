# -*- coding: utf-8 -*-
"""缠论核心算法（精简可靠版 v2）
1. K线包含处理  2. 分型识别  3. 笔划分  4. 中枢识别（非重叠）  5. 三类买卖点
输入：df 需含列 日期/开盘/最高/最低/收盘/dif/dea（日线）
"""
import numpy as np


def merge_klines(df):
    """K线包含关系处理，返回合并后的列表 [dict(high,low,idx_start,idx_end)]"""
    kl = []
    n = len(df)
    for i in range(n):
        h = float(df.loc[i, '最高']); l = float(df.loc[i, '最低'])
        kl.append({'high': h, 'low': l, 's': i, 'e': i})
    merged = []
    for k in kl:
        if not merged:
            merged.append(k)
            continue
        prev = merged[-1]
        if (k['high'] >= prev['high'] and k['low'] <= prev['low']) or \
           (k['high'] <= prev['high'] and k['low'] >= prev['low']):
            up = k['high'] >= prev['high'] and k['low'] >= prev['low']
            if up:
                merged[-1] = {'high': max(prev['high'], k['high']),
                              'low': max(prev['low'], k['low']),
                              's': prev['s'], 'e': k['e']}
            else:
                merged[-1] = {'high': min(prev['high'], k['high']),
                              'low': min(prev['low'], k['low']),
                              's': prev['s'], 'e': k['e']}
        else:
            merged.append(k)
    return merged


def find_fractals(merged):
    """在合并后K线上找分型：返回 [dict(i, type, price)] i=合并K线下标"""
    fr = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i-1], merged[i], merged[i+1]
        if b['high'] > a['high'] and b['high'] > c['high'] and \
           b['low'] > a['low'] and b['low'] > c['low']:
            fr.append({'i': i, 'type': 'top', 'price': b['high']})
        elif b['low'] < a['low'] and b['low'] < c['low'] and \
             b['high'] < a['high'] and b['high'] < c['high']:
            fr.append({'i': i, 'type': 'bottom', 'price': b['low']})
    return fr


def build_bi(fr, merged):
    """由分型构成笔：交替顶底分型，分型间隔>=1根合并K线。
    同向分型保留更极端者。返回 [dict(type, i, price)]"""
    bi = []
    for f in fr:
        if not bi:
            bi.append(f)
            continue
        last = bi[-1]
        if last['type'] == f['type']:
            if f['type'] == 'top' and f['price'] >= last['price']:
                bi[-1] = f
            elif f['type'] == 'bottom' and f['price'] <= last['price']:
                bi[-1] = f
        else:
            if f['i'] - last['i'] >= 2:
                bi.append(f)
    return bi


def build_zhongshu(bi, merged, min_bi=3):
    """由笔构建中枢（非重叠）：从 i 起取连续3笔，重叠区间 [ZD,ZG] 有效则记录并跳过3笔。
    ZG=min(段内top.price)，ZD=max(段内bottom.price)
    返回 [dict(start,end,ZG,ZD)]"""
    zs_list = []
    i = 0
    while i <= len(bi) - min_bi:
        seg = bi[i:i+min_bi]
        tops = [x['price'] for x in seg if x['type'] == 'top']
        bottoms = [x['price'] for x in seg if x['type'] == 'bottom']
        if len(tops) >= 2 and len(bottoms) >= 1:
            ZG = min(tops)
            ZD = max(bottoms)
            if ZD < ZG:
                zs_list.append({'start': i, 'end': i + min_bi - 1, 'ZG': ZG, 'ZD': ZD})
                i += min_bi
                continue
        i += 1
    return zs_list


def detect_signals(df, bi, zs_list, merged, cfg):
    """生成买卖点信号（映射回原始K线下标）
    三买：中枢后出现向上突破笔(高点>ZG)，随后的回调笔低点≥ZG → 回调笔末端买入
    一买：当前下跌段创新低(相对前一段) + MACD绿柱面积背驰(当前面积>前段面积×系数) → 该段末端买入
    返回 [(orig_idx, sig_type, price)]"""
    n = len(df)
    dif = np.nan_to_num(np.array(df['dif'], dtype=float))
    dea = np.nan_to_num(np.array(df['dea'], dtype=float))
    macd = (dif - dea) * 2
    sigs = []
    orig = [m['e'] for m in merged]
    tri_dev = cfg.get('tri_buy_dev', 0.03)

    # ---- 三买：中枢后向上突破ZG，随后回调笔低点不破ZG ----
    for zs in zs_list:
        e = zs['end']
        # 中枢结束后最近几笔内寻找向上突破笔
        for k in range(e + 1, min(e + 4, len(bi))):
            up = bi[k]
            if up['type'] != 'top' or up['price'] <= zs['ZG']:
                continue
            # 找到向上突破，其后须跟一笔回调（bottom），回调低点≥ZG（允许小幅偏差）
            if k + 1 < len(bi):
                pull = bi[k+1]
                if pull['type'] == 'bottom' and pull['price'] >= zs['ZG'] * (1 - tri_dev):
                    oi = orig[min(pull['i'], len(orig)-1)]
                    sigs.append((oi, '三买', pull['price']))
            break

    # ---- 一买：下跌段创新低 + MACD底背驰 ----
    for j in range(2, len(bi)):
        b = bi[j]
        if b['type'] != 'bottom':
            continue
        prev_bottoms = [x for x in bi[:j] if x['type'] == 'bottom']
        if not prev_bottoms:
            continue
        prev_b = prev_bottoms[-1]
        if b['price'] >= prev_b['price']:
            continue  # 未创新低
        # 当前段 MACD 绿柱面积（笔起点~笔末端）
        s0 = max(0, orig[bi[j-1]['i']]); e0 = max(0, min(orig[b['i']], n-1))
        seg = macd[s0:e0+1]
        cur_area = float(np.sum(seg[seg < 0]))
        # 前一段（prev_bottom 的笔起点~末端）
        pp = j
        for kk in range(j-1, -1, -1):
            if bi[kk] is prev_b:
                pp = kk
                break
        ps = max(0, orig[bi[pp-1]['i']]); pe = max(0, min(orig[prev_b['i']], n-1))
        pseg = macd[ps:pe+1]
        prev_area = float(np.sum(pseg[pseg < 0]))
        if prev_area < 0 and cur_area > prev_area * (1 - cfg.get('bei_chi_ratio', 0.15)):
            oi = orig[b['i']]
            sigs.append((oi, '一买', b['price']))
    return sigs


def chan_analyze(df, cfg=None):
    """主入口：输入日线df（含 最高/最低/收盘/dif/dea），返回买卖点信号列表"""
    cfg = cfg or {}
    merged = merge_klines(df)
    fr = find_fractals(merged)
    bi = build_bi(fr, merged)
    zs = build_zhongshu(bi, merged, cfg.get('min_bi', 3))
    sigs = detect_signals(df, bi, zs, merged, cfg)
    return {'merged': merged, 'fractals': fr, 'bi': bi, 'zhongshu': zs, 'signals': sigs}
