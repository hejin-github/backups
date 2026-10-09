# -*- coding: utf-8 -*-
"""收盘复盘 review（2026-10-08 新增）

给「收盘简报」提供一份结构化的当日复盘：
  ① 大盘      上证/深成指/创业板涨跌 + 沪深主板成交（含环比）
  ② 量价宽度  涨跌家数、个股中位涨幅、成交额及其环比
  ③ 情绪      涨停/跌停/炸板/封板率、连板梯队与最高板、昨日涨停晋级率与打板溢价
  ④ 估值      沪深300 PE、中证红利 PB 及近 3 年分位（本地 csv，不联网）
  ⑤ 消息面    data/review_notes/{date}.md（人工维护），缺省留白
  ⑥ 持仓复盘  各策略当前持仓：成本/现价/浮盈/当日表现
  ⑦ 卖出复盘  当日已平仓的每一笔：买卖价、持有天数、盈亏、卖出原因
  ⑧ 明日行动  收盘判定出来的待执行买入/卖出/换仓

设计约束（务必保持）：
  · 纯本地计算为主，只额外拉一次腾讯指数实时（1 个请求），失败静默降级到上证 csv；
  · 任何一步失败都不向外抛异常，缺哪一块就少哪一块，简报其余部分照常出；
  · 只在收盘后（15:00 之后、当日 bar 已回补）调用；
    实时行情的日期 != 目标日时拒绝产出 counters — 休市日腾讯返回的是上一交易日快照
    （详见 2026-09-25 中秋教训），据此统计会得出错误的"今日"结论。
"""
import csv
import datetime
import json
import os
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')

_SEAL_MAIN = 9.8      # 主板涨停阈值（腾讯 pct 为百分数）
_SEAL_ST = 4.8        # ST/*ST 涨停阈值
_NOTES_DIR = os.path.join(DATA, 'review_notes')
_HIST_FILE = os.path.join(DATA, 'review_history.json')


# ---------------- 小工具 ----------------

def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except Exception:
        return None


def _is_st(name):
    n = str(name or '').upper().replace(' ', '')
    return 'ST' in n or n.startswith('*')


def _limit_thr(name):
    return _SEAL_ST if _is_st(name) else _SEAL_MAIN


def _zt_price(px, pct, name):
    """由现价 + 涨幅反推涨停价（腾讯实时无前收字段）"""
    px, pct = _f(px), _f(pct)
    if not px or pct is None or pct <= -100:
        return None
    pre = px / (1.0 + pct / 100.0)
    if pre <= 0:
        return None
    k = 1.05 if _is_st(name) else 1.10
    return round(pre * k, 2)


def _names():
    """{code: name}：与回测同一份股票字典"""
    try:
        import backtest as bt
        return dict(bt.STOCKS or {})
    except Exception:
        return {}


def _md_short(s):
    try:
        _y, mo, d = str(s).split('-')
        return '%s月%s日' % (int(mo), int(d))
    except Exception:
        return str(s or '')


# ---------------- 指数 ----------------

_IDX_SYM = [('上证指数', 'sh000001'), ('深证成指', 'sz399001'), ('创业板指', 'sz399006')]


def fetch_index_realtime():
    """拉一次腾讯指数快照 → [{'name','close','chg_pct','amount_yi'}]

    单请求、零依赖；失败返回 []（调用方自行降级）。
    """
    url = 'https://qt.gtimg.cn/q=' + ','.join('s_' + s for _n, s in _IDX_SYM)
    out = []
    try:
        import requests
        try:
            _sess = requests.Session()
            _sess.trust_env = False          # 同 app.fetch_realtime：绕开本机代理
            r = _sess.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        except Exception:
            r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        r.encoding = 'gbk'
        for line in r.text.strip().split(';'):
            if '="' not in line:
                continue
            head, _, payload = line.partition('="')
            sym = head.split('_')[-1].strip()
            p = payload.rstrip('"').split('~')
            nm = next((n for n, s in _IDX_SYM if s == sym), p[1] if len(p) > 1 else sym)
            out.append({
                'name': nm,
                'close': _f(p[3]) if len(p) > 3 else None,
                'chg': _f(p[5]) if len(p) > 5 else None,
                'amount_yi': round(_f(p[7]) / 10000.0, 1) if len(p) > 7 and _f(p[7]) else None,
            })
    except Exception:
        return []
    return [x for x in out if x.get('close')]


def _sh_from_csv():
    """上证指数本地 csv 兜底（缺 index 实时时用）"""
    p = os.path.join(DATA, 'csv', 'sh000001.csv')
    try:
        rows = list(csv.DictReader(open(p, encoding='utf-8-sig')))
    except Exception:
        return None
    if len(rows) < 2:
        return None
    c2, c1 = _f(rows[-1]['close']), _f(rows[-2]['close'])
    if not c1:
        return None
    return {'date': rows[-1]['date'], 'close': c2,
            'chg': round((c2 / c1 - 1) * 100, 2), 'amount_yi': None}


# ---------------- 连板底表 ----------------

def _leaders_by_date():
    """{date: [(code, streak, vr), ...]}：复用回测的连板底表缓存（收盘时通常已热）"""
    try:
        import backtest as bt
        bt._s16_leaders()
        return (bt._S16_LEADERS_CACHE or {}).get('by_date') or {}
    except Exception:
        return {}


def _lb_key_map(bd):
    """{日期字符串: 底表原 key}。

    ★必记坑：bt._S16_LEADERS_CACHE['by_date'] 的 key 是 **pd.Timestamp** 而不是字符串，
      直接 `bd.get('2026-10-08')` 恒为 None（与 2026-09-23 踩过的
      `_s16_space_pool.get(d)` 是同一个坑），且**不报错**，表现为"连板/晋级统计全空"。
    """
    out = {}
    for k in (bd or {}).keys():
        try:
            out[str(k)[:10]] = k
        except Exception:
            continue
    return out


def _lb_map(bd, date, strict_before=False):
    """某日（或 date 之前最近一个交易日）的 {code: 连板数}，返回 (map, 该日期)"""
    kmap = _lb_key_map(bd)
    keys = sorted(kmap.keys())
    if strict_before:
        keys = [k for k in keys if k < date]
    else:
        keys = [k for k in keys if k == date]
    if not keys:
        return {}, None
    d = keys[-1]
    out = {}
    for item in (bd.get(kmap[d]) or []):
        try:
            out[str(item[0])] = int(item[1])
        except Exception:
            continue
    return out, d


# ---------------- 估值 ----------------

def _read_series(fname, col, years=3):
    """读乐咕估值 csv，返回 (最新值, 近 years 年分位%)"""
    p = os.path.join(DATA, 'csv', fname)
    try:
        rows = list(csv.DictReader(open(p, encoding='utf-8-sig')))
    except Exception:
        return None, None
    if not rows:
        return None, None
    last_date = rows[-1].get('日期') or ''
    cut = str(int(last_date[:4]) - years) if last_date[:4].isdigit() else ''
    vals = []
    for r in rows:
        if cut and str(r.get('日期') or '') < cut:
            continue
        v = _f(r.get(col))
        if v:
            vals.append(v)
    if not vals:
        return None, None
    cur = vals[-1]
    pct = round(sum(1 for v in vals if v <= cur) / len(vals) * 100, 1)
    return round(cur, 2), pct


def valuation_snapshot():
    pe, pe_pct = _read_series('hs300_pe.csv', '滚动市盈率')
    pb, pb_pct = _read_series('hsly_pb.csv', '市净率')
    return {'pe': pe, 'pe_pct': pe_pct, 'pb': pb, 'pb_pct': pb_pct}


# ---------------- 宽度与情绪 ----------------

def breadth(realtime, prev_lb=None):
    """全市场快照统计：涨跌家数/涨停/跌停/炸板/成交额/沪深封板梯队"""
    names = _names()
    up = down = flat = zt = dt = burst = 0
    amount = 0.0
    pcts = []
    zt_items, burst_items = [], []
    for code, rt in (realtime or {}).items():
        if not isinstance(rt, dict):
            continue
        pct = _f(rt.get('pct'))
        if pct is None:
            continue
        nm = names.get(code) or code
        pcts.append(pct)
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        amt = _f(rt.get('amount')) or 0.0
        amount += amt
        thr = _limit_thr(nm)
        ztp = _zt_price(rt.get('price'), pct, nm)
        if pct >= thr:
            zt += 1
            zt_items.append({'code': code, 'name': nm, 'pct': pct,
                             'lb': (prev_lb or {}).get(code, 0) + 1 if prev_lb else 1})
        elif pct <= -thr:
            dt += 1
        # 炸板 = 盘中触及涨停价但**收盘未封**（收盘仍涨停的不计，含回封）
        if pct < thr and ztp and _f(rt.get('high')) and float(rt['high']) >= ztp * 0.995:
            burst += 1
            burst_items.append({'code': code, 'name': nm, 'pct': pct, 'price': _f(rt.get('price')),
                                'lb': (prev_lb or {}).get(code, 0)})
    n = up + down + flat
    return {
        'n': n,
        'up': up, 'down': down, 'flat': flat,
        'up_ratio': round(up / n * 100, 1) if n else None,
        'median': round(statistics.median(pcts), 2) if pcts else None,
        'amount_yi': round(amount / 1e8, 1),
        'zt': zt, 'dt': dt, 'burst': burst,
        'zt_items': zt_items,
        'burst_items': sorted(burst_items, key=lambda x: -(x['lb'] or 0))[:5],
    }


def sentiment(bd, realtime, date):
    """情绪层：封板率、连板梯队、最高板、昨日涨停晋级率与打板溢价

    今日连板优先取底表（当日 bar 已回补时最准）；底表还没有今天（某些日子 bar 未写回）
    就用实时快照推算：今日涨停 = 昨日连板 + 1，首板记 1。
    """
    today_lb, _td = _lb_map(bd, date)
    prev_lb, prev_d = _lb_map(bd, date, strict_before=True)
    res = {'today_map': today_lb, 'prev_map': prev_lb, 'prev_date': prev_d,
           'ladder': {}, 'max_lb': 0, 'advance': 0, 'advance_base': 0,
           'advance_rate': None, 'follow_avg': None, 'follow_win': None, 'follow_n': 0}
    names = _names()
    sealed = set(today_lb.keys())
    if not sealed:
        for code, rt in (realtime or {}).items():
            if isinstance(rt, dict):
                pct = _f(rt.get('pct'))
                if pct is not None and pct >= _limit_thr(names.get(code) or ''):
                    sealed.add(code)
    if not today_lb and prev_lb is not None and sealed:
        today_lb = {c: int(prev_lb.get(c, 0)) + 1 for c in sealed}
        res['today_map'] = today_lb
        res['estimated'] = True
    if today_lb:
        ladder = {}
        for _c, s in today_lb.items():
            if s >= 2:
                ladder[s] = ladder.get(s, 0) + 1
        res['ladder'] = dict(sorted(ladder.items(), reverse=True))
        res['max_lb'] = max(today_lb.values())
    follow = []
    adv = 0
    for code, s in prev_lb.items():
        rt = (realtime or {}).get(code) or {}
        pct = _f(rt.get('pct'))
        if pct is None:
            continue
        follow.append(pct)
        if code in sealed:
            adv += 1
    res['advance'] = adv
    res['advance_base'] = len(prev_lb)
    res['advance_rate'] = round(adv / len(prev_lb) * 100, 1) if prev_lb else None
    if follow:
        res['follow_n'] = len(follow)
        res['follow_avg'] = round(sum(follow) / len(follow), 2)
        res['follow_win'] = round(sum(1 for x in follow if x > 0) / len(follow) * 100, 1)
    return res


def score_sentiment(br, sm):
    """0~100 情绪分：封板率 50% + 上涨占比 30% + 晋级率 20%（缺项按比例让位）"""
    parts = []
    if br.get('zt') or br.get('burst'):
        tot = (br.get('zt') or 0) + (br.get('burst') or 0)
        parts.append((0.5, (br.get('zt') or 0) / tot * 100 if tot else 0))
    if br.get('up_ratio') is not None:
        parts.append((0.3, br['up_ratio']))
    if sm.get('advance_rate') is not None:
        parts.append((0.2, sm['advance_rate']))
    if not parts:
        return None, None
    w = sum(p[0] for p in parts)
    sc = sum(p[0] * p[1] for p in parts) / w
    sc = round(sc, 0)
    if sc < 25:
        lab = '冰点'
    elif sc < 40:
        lab = '低迷'
    elif sc < 55:
        lab = '偏弱'
    elif sc < 70:
        lab = '温和'
    elif sc < 85:
        lab = '活跃'
    else:
        lab = '亢奋'
    return int(sc), lab


# ---------------- 账本（持仓 / 卖出 / 明日行动） ----------------

_HOLD_STATUS = ('持有', '建仓', '换仓买入', '买入成交')


def _pos_of(daily, idx=None):
    """从 daily 序列（到 idx 为止）反查当前持仓：返回 {'name','code','buy_date','buy_price'}"""
    for r in reversed(daily[: (idx + 1) if idx is not None else None] if daily else []):
        st = str(r.get('status') or '')
        if st == '建仓' or (r.get('deal_price') is not None and '换仓买入' in str(r.get('note') or '')):
            return {'name': r.get('name'), 'code': r.get('code'),
                    'buy_date': r.get('date'), 'buy_price': r.get('deal_price') or r.get('buy_ref')}
        if st in ('卖出成交', '空仓'):
            if st == '空仓':
                return None
            break
    return None


def account_review(hist, realtime, date, sig_rows=None):
    """持仓复盘 + 当日卖出复盘 + 明日行动。

    明日行动有**两个来源**（缺一不可）：
      ① strategy_daily.json 当日行（有账本策略的收盘判定）；
      ② 今日信号库 data/signals.db（S16_空间龙盯盘等**只盯盘、无独立账本**的策略）。
    """
    pos_list, sold, actions = [], [], []
    for s in ((hist or {}).get('strategies') or []):
        sname = s.get('name') or ''
        daily = s.get('daily') or []
        if not daily:
            continue
        last = daily[-1]
        if str(last.get('date') or '') != date:
            continue        # 账本尚未滚到当日（build 未完成）→ 不猜、不显示
        # 持仓
        if last.get('pos_pnl_pct') is not None and str(last.get('status')) != '卖出成交':
            p = _pos_of(daily) or {}
            rt = (realtime or {}).get(str(last.get('code') or '')) or {}
            buy_date = p.get('buy_date')
            hd = None
            if buy_date:
                try:
                    hd = len([r for r in daily if str(r.get('date')) > str(buy_date)]) + 1
                except Exception:
                    hd = None
            pos_list.append({
                'sname': sname, 'name': last.get('name'), 'code': last.get('code'),
                'cost': _f(last.get('pos_cost')), 'price': _f(last.get('price')),
                'pct': _f(last.get('pos_pnl_pct')), 'pnl': _f(last.get('pos_pnl')),
                'buy_date': buy_date, 'hold_days': hd,
                'day_pct': _f(rt.get('pct')), 'status': last.get('status'),
            })
        # 当日卖出：优先 trades（含卖出原因/净值/持有天数）
        for t in (s.get('trades') or []):
            if str(t.get('sell_date') or '') == date:
                sold.append({
                    'sname': t.get('strategy') or sname, 'name': t.get('stock_name'),
                    'code': t.get('stock_code'), 'buy_date': t.get('buy_date'),
                    'buy_price': _f(t.get('buy_price')), 'sell_price': _f(t.get('sell_price')),
                    'ret_pct': _f(t.get('ret_pct')), 'net_pnl': _f(t.get('net_pnl')),
                    'hold_days': t.get('hold_days'), 'tag': t.get('sell_tag'),
                })
        # 明日行动
        st = str(last.get('status') or '')
        if st in ('买入信号', '买入信号(待成交)'):
            actions.append({'kind': 'buy', 'sname': sname, 'name': last.get('name'),
                            'code': last.get('code'), 'ref': _f(last.get('buy_ref')) or _f(last.get('price')),
                            'stop': _f(last.get('stop')), 'target': _f(last.get('target')),
                            'when': s.get('timing_buy') or s.get('timing') or ''})
        elif st in ('卖出信号', '卖出顺延'):
            actions.append({'kind': 'sell', 'sname': sname, 'name': last.get('name'),
                            'code': last.get('code'), 'ref': None, 'stop': None, 'target': None,
                            'when': s.get('timing_sell') or s.get('timing') or ''})
        if last.get('rec_buy') and st in ('卖出信号', '卖出成交', '买入信号'):
            actions.append({'kind': 'switch', 'sname': sname, 'name': last.get('rec_buy'),
                            'code': None, 'ref': None, 'stop': None, 'target': None, 'when': ''})
    # ★ 2026-10-08 明日行动的**第二个来源**：今日信号库（data/signals.db）。
    #   原因（用户当日实际反馈）：**S16_空间龙盯盘这类策略没有独立账本** —— 它只盯盘 + 出决策
    #   + 落信号库，不写 strategy_daily.json，所以只扫账本会把它"今日收盘确认 → 明日执行"
    #   的决策整条漏掉（当天漏的正是「传艺科技 002866」）。与首页「今日行动清单」同源。
    unconfirmed = []    # ★ 2026-10-08 被收盘价证伪的盘中信号（留痕，不进 actions）
    for r in _pending_signal_rows(date, sig_rows):
        sname = str(r.get('sid') or r.get('sname') or '').strip()
        nm = str(r.get('name') or '').strip()
        code = str(r.get('code') or '').strip()
        if not sname or not nm:
            continue
        rs = next((x for x in ((hist or {}).get('strategies') or [])
                   if str(x.get('name')) == sname), None)
        # 去重①：该策略当日账本行已经是同一标的的买入信号 → 不重复提示（首页同口径）
        if rs and (rs.get('daily') or []):
            rl = rs['daily'][-1]
            if (str(rl.get('date')) == date and rl.get('status') in ('买入信号', '买入信号(待成交)')
                    and str(rl.get('name')) == nm):
                continue
        if any(str(a.get('sname')) == sname and (str(a.get('code') or '') or '') == code
               for a in actions):
            continue        # 去重②：本轮已从账本生成同一条
        # ★ 2026-10-08 收盘对账：盘中触发、但收盘用真实收盘价重判后不成立（当日账本未触发）
        #   → 不是"明日行动"，只留痕。此前这类行会被当成待执行买入写进邮件，与当日账本
        #   "空仓"自相矛盾（10/08 S9 中国天楹）。口径同首页行动清单。
        if r.get('_confirmed') is False:
            unconfirmed.append({'sname': sname, 'name': nm, 'code': code,
                                'ledger': str(r.get('_ledger_status') or '未触发')})
            continue
        # 账本当日对该策略的判定（可能≠信号口径，如实标注，别让人误以为两处矛盾是新信息）
        _lg = ''
        if rs and (rs.get('daily') or []) and str(rs['daily'][-1].get('date') or '') == date:
            _lg = str(rs['daily'][-1].get('status') or '')
        actions.append({'kind': 'buy', 'sname': sname, 'name': nm, 'code': code,
                        'ref': _f(r.get('buy_ref')) or _f(r.get('price')),
                        'stop': _f(r.get('stop')), 'target': _f(r.get('target')),
                        'when': (rs or {}).get('timing_buy') or (rs or {}).get('timing') or '',
                        'ledger': _lg, 'src': 'signal'})
    return {'positions': pos_list, 'sold': sold, 'actions': actions,
            'unconfirmed': unconfirmed}


def _pending_signal_rows(date, sig_rows=None):
    """当日"待执行"信号行 —— 排除已消失(gone)与持仓跟踪(watch)，口径同首页行动清单。"""
    if sig_rows is not None:
        rows = sig_rows
    else:
        rows = []
        try:
            import signal_store
            rows = signal_store.query(date=date, include_gone=False, include_watch=False)
        except Exception:
            try:
                import notify
                rows = notify.intraday_signals_today(include_gone=False, include_watch=False)
            except Exception:
                rows = []
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if r.get('gone') or r.get('watch') or r.get('deleted'):
            continue
        if str(r.get('trade_date') or r.get('date') or date)[:10] != date:
            continue
        if not str(r.get('name') or '').strip():
            continue
        out.append(r)
    return out


# ---------------- 消息面（人工维护） ----------------

def load_notes(date):
    """读 data/review_notes/{date}.md / _default.md，每行去空白后返回列表"""
    for fn in (os.path.join(_NOTES_DIR, date + '.md'), os.path.join(_NOTES_DIR, '_default.md')):
        try:
            txt = open(fn, encoding='utf-8').read()
        except Exception:
            continue
        lines = [x.strip() for x in txt.splitlines()]
        lines = [x for x in lines if x and not x.startswith('#')]
        if lines:
            return lines
    return []


def _hist_load():
    try:
        with open(_HIST_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _hist_save(date, br, idx):
    """累积每日核心指标（只留最近 120 天），用于次日复盘做环比"""
    d = _hist_load()
    prev = None
    keys = sorted(k for k in d if k < date)
    if keys:
        prev = d[keys[-1]]
    d[date] = {'amount_yi': br.get('amount_yi'), 'zt': br.get('zt'), 'dt': br.get('dt'),
               'up': br.get('up'), 'down': br.get('down'),
               'sh_close': (idx or [{}])[0].get('close') if idx else None}
    for k in sorted(d.keys())[:-120]:
        d.pop(k, None)
    try:
        with open(_HIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass
    return prev


# ---------------- 汇总 ----------------

def build_review(date=None, realtime=None, hist=None, market=None, sig_rows=None):
    """产出复盘 dict（任何一块失败都只缺失该块，不抛异常）。

    sig_rows：可选的当日待执行信号行（一般传 app._intraday_sig_rows()，保证与首页
    同源）；不传则本模块自行读 data/signals.db。
    """
    date = date or datetime.date.today().isoformat()
    rv = {'date': date, 'ok': False, 'index': [], 'source_date': None}

    # ★ 行情新鲜度闸门：快照不是当日的（休市/未更新）→ 不产出复盘
    try:
        qdates = [str(v.get('date') or '') for v in (realtime or {}).values()]
        qdates = [q for q in qdates if len(q) == 10]
        if qdates:
            rv['source_date'] = max(qdates)
            if max(qdates) != date:
                rv['reason'] = '行情快照日期 %s ≠ 复盘日 %s（休市或行情未更新），复盘已跳过' % (
                    max(qdates), date)
                return rv
    except Exception:
        pass

    try:
        idx = fetch_index_realtime()
    except Exception:
        idx = []
    if not idx:
        sh = _sh_from_csv()
        if sh:
            idx = [{'name': '上证指数', 'close': sh['close'], 'chg': sh['chg'],
                    'amount_yi': sh['amount_yi']}]
    rv['index'] = idx

    try:
        bd = _leaders_by_date()
    except Exception:
        bd = {}
    prev_lb = None
    try:
        prev_lb, _pd = _lb_map(bd, date, strict_before=True)
    except Exception:
        prev_lb = None

    try:
        rv['breadth'] = breadth(realtime, prev_lb)
    except Exception:
        rv['breadth'] = {}
    br = rv['breadth'] or {}
    try:
        rv['sentiment'] = sentiment(bd, realtime, date)
    except Exception:
        rv['sentiment'] = {}
    sm = rv['sentiment'] or {}
    try:
        sc, lab = score_sentiment(br, sm)
        rv['score'], rv['label'] = sc, lab
    except Exception:
        rv['score'] = rv['label'] = None
    # 最高板 / 龙头（今日封板且连板最高）
    try:
        tops = sorted((sm.get('today_map') or {}).items(), key=lambda kv: -kv[1])[:3]
        rv['tops'] = [{'code': c, 'name': (_names().get(c) or c), 'lb': s} for c, s in tops]
    except Exception:
        rv['tops'] = []
    try:
        rv['prev_amount_yi'] = (_hist_save(date, br, idx) or {}).get('amount_yi')
    except Exception:
        rv['prev_amount_yi'] = None
    try:
        rv['valuation'] = valuation_snapshot()
    except Exception:
        rv['valuation'] = {}
    try:
        rv['notes'] = load_notes(date)
    except Exception:
        rv['notes'] = []
    try:
        rv.update(account_review(hist, realtime, date, sig_rows=sig_rows))
    except Exception:
        rv.setdefault('positions', [])
        rv.setdefault('sold', [])
        rv.setdefault('actions', [])
        rv.setdefault('unconfirmed', [])
    rv['ok'] = bool(br)
    return rv


# ---------------- 渲染（Markdown，供收盘简报直接使用） ----------------

def _arrow(x):
    try:
        return '▲' if float(x) >= 0 else '▼'
    except Exception:
        return ''


# 账本 timing 字段 → 中文（open=开盘、close=尾盘）
_WHEN = {'open': '开盘', 'close': '尾盘'}


def render_md(rv):
    """复盘 → Markdown 文本（简明：每块 1~4 行）"""
    if not rv:
        return ''
    out = []
    date = rv.get('date')
    if not rv.get('ok'):
        out.append('**复盘**：%s' % (rv.get('reason') or '数据不足，今日复盘略过'))
        return '\n'.join(out)

    # ① 大盘 + ② 量价
    idx = rv.get('index') or []
    if idx:
        seg = ' ｜ '.join('%s %s %s%s%%' % (i['name'], '%.2f' % i['close'],
                                            _arrow(i.get('chg')), abs(i.get('chg') or 0))
                          for i in idx if i.get('close') and i.get('chg') is not None)
        out.append('**大盘** ' + seg)
    br = rv.get('breadth') or {}
    amt = br.get('amount_yi')
    prev_amt = rv.get('prev_amount_yi')
    amt_txt = '%.0f 亿' % amt if amt else '—'
    if amt and prev_amt:
        amt_txt += '（环比 %+.0f%%）' % ((amt / prev_amt - 1) * 100)
    out.append('**量价** 沪深主板成交 %s ｜ 涨 %d / 跌 %d ｜ 个股中位 %s%%'
               % (amt_txt, br.get('up') or 0, br.get('down') or 0,
                  '—' if br.get('median') is None else '%+.2f' % br['median']))

    # ③ 情绪
    sm = rv.get('sentiment') or {}
    seal_tot = (br.get('zt') or 0) + (br.get('burst') or 0)
    seal_rate = (br.get('zt') or 0) / seal_tot * 100 if seal_tot else None
    lines = ['**情绪** 涨停 %d（跌停 %d）｜炸板 %d｜封板率 %s'
             % (br.get('zt') or 0, br.get('dt') or 0, br.get('burst') or 0,
                '—' if seal_rate is None else '%.0f%%' % seal_rate)]
    if rv.get('tops'):
        lines[-1] += ' ｜ 最高 %d板 %s' % (rv['tops'][0]['lb'], rv['tops'][0]['name'])
    lad = sm.get('ladder') or {}
    if lad:
        lines[-1] += ' ｜ 梯队 ' + ' · '.join('%d板%d' % (k, v) for k, v in sorted(lad.items(), reverse=True))
    if sm.get('advance_base'):
        lines.append('**晋级** 昨日涨停 %d 只 → 今日连板 %d 只（%s%%）｜昨日涨停股今日均值 %s%%（%s%%上涨）'
                     % (sm['advance_base'], sm.get('advance') or 0,
                        '—' if sm.get('advance_rate') is None else '%.1f' % sm['advance_rate'],
                        '—' if sm.get('follow_avg') is None else '%+.2f' % sm['follow_avg'],
                        '—' if sm.get('follow_win') is None else '%.0f' % sm['follow_win']))
    if rv.get('score') is not None:
        lines.append('→ 情绪评分 **%d（%s）**' % (rv['score'], rv['label']))
    out.extend(lines)
    bul = br.get('burst_items') or []
    if bul:
        out.append('　炸板：' + '、'.join('%s%s%%' % (b['name'], '%+.1f' % b['pct']) for b in bul[:3]))

    # ④ 估值 + ⑤ 消息面
    val = rv.get('valuation') or {}
    if val.get('pe') or val.get('pb'):
        out.append('**估值** 沪深300 PE %s（近3年 %s 分位）｜中证红利 PB %s（%s 分位）'
                   % (val.get('pe') or '—', '—' if val.get('pe_pct') is None else '%.0f%%' % val['pe_pct'],
                      val.get('pb') or '—', '—' if val.get('pb_pct') is None else '%.0f%%' % val['pb_pct']))
    notes = rv.get('notes') or []
    if notes:
        out.append('**消息/基本面** ' + '；'.join(notes[:4]))
    else:
        out.append('**消息面** —（无内置新闻源；把要带上的人工要点写进 '
                   'data/review_notes/%s.md 即会出现在这里）' % date)

    # ⑥ 持仓复盘
    ps = rv.get('positions') or []
    if ps:
        tot = sum(p['pnl'] or 0 for p in ps)
        out.append('**持仓复盘**（%d 只，合计浮盈 %+.0f 元）' % (len(ps), tot))
        for p in ps[:8]:
            day = '' if p.get('day_pct') is None else ' 今日%+.2f%%' % p['day_pct']
            hd = ('%d日' % p['hold_days']) if p.get('hold_days') else ''
            out.append('- %s｜%s(%s) 成本%s 现价%s **%+.1f%%**%s %s'
                       % (p['sname'], p.get('name') or '—', p.get('code') or '',
                          '%.2f' % p['cost'] if p.get('cost') else '—',
                          '%.2f' % p['price'] if p.get('price') else '—',
                          p.get('pct') or 0, day, hd))
    # ⑦ 当日卖出
    sd = rv.get('sold') or []
    if sd:
        tot = sum(x['net_pnl'] or 0 for x in sd)
        out.append('**今日卖出**（%d 笔，合计 %+.0f 元）' % (len(sd), tot))
        for x in sd[:8]:
            out.append('- %s｜%s %s@%.2f → %.2f **%+.2f%%**(%+.0f元) %s｜%s'
                       % (x['sname'], x.get('name') or '—',
                          _md_short(x.get('buy_date')), x.get('buy_price') or 0,
                          x.get('sell_price') or 0, x.get('ret_pct') or 0,
                          x.get('net_pnl') or 0,
                          ('%d日' % x['hold_days']) if x.get('hold_days') else '',
                          x.get('tag') or ''))
    # ⑧ 明日行动
    ac = rv.get('actions') or []
    if ac:
        out.append('**明日行动**（%d 项）' % len(ac))
        for a in ac[:10]:
            _when = _WHEN.get(str(a.get('when') or '').strip(), '') or ''
            _wtxt = '（%s执行）' % _when if _when else ''
            if a['kind'] == 'buy':
                # src='signal' = 来自今日信号库（空间龙盯盘等无独立账本的策略），
                # 这类是"今日收盘价确认"的决策，措辞与时点都和账本粗细口径不同，必须标注。
                if a.get('src') == 'signal':
                    _txt = '（今日收盘确认 → 明日执行）'
                    _lg = str(a.get('ledger') or '')
                    if _lg and _lg not in ('买入信号', '买入信号(待成交)'):
                        _txt += '；注：本条出自今日信号，收盘账本当日判定为「%s」' % _lg
                else:
                    _txt = _wtxt
                out.append('- 买入 %s｜%s(%s) 参考价 %s%s'
                           % (a['sname'], a.get('name') or '—', a.get('code') or '',
                              '%.2f' % a['ref'] if a.get('ref') else '—', _txt))
            elif a['kind'] == 'sell':
                out.append('- 卖出 %s｜%s(%s)%s'
                           % (a['sname'], a.get('name') or '—', a.get('code') or '', _wtxt))
            else:
                out.append('- 备选（卖出后换仓）%s｜%s' % (a['sname'], a.get('name') or '—'))
    elif rv.get('ok'):
        out.append('**明日行动** 无（各策略收盘后无待执行买卖）')
    # ★ 2026-10-08 被收盘价证伪的盘中信号：不进「明日行动」，但必须留痕说明它为何消失
    _unc = rv.get('unconfirmed') or []
    if _unc:
        out.append('**盘中触发·收盘未成立（不执行，仅留痕）**：' + '；'.join(
            '%s｜%s(%s)（当日账本：%s）' % (x.get('sname'), x.get('name') or '—',
                                    x.get('code') or '', x.get('ledger') or '未触发')
            for x in _unc[:6]))
    return '\n'.join(out)


def preview(date=None, realtime=None, hist=None, sig_rows=None):
    """本地预览用：build + render"""
    return render_md(build_review(date=date, realtime=realtime, hist=hist, sig_rows=sig_rows))


if __name__ == '__main__':
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    try:
        hist = json.load(open(os.path.join(DATA, 'strategy_daily.json'), encoding='utf-8'))
    except Exception:
        hist = None
    print(preview(date=d, realtime={}, hist=hist))
