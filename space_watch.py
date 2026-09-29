# -*- coding: utf-8 -*-
"""S16 空间龙【盘中盯盘器】（2026-09-24 新增）

========== 为什么要写这个模块 ==========
回测（_daban_fill2.py，全史 2614 只 / 每笔 5000）实测三种下单方式的真实后果：

  E 开盘就挂涨停价 → 开盘价成交   n=1494  净 **-9,947**   炸板率 **73.8%**
  B 盘中触及涨停就挂 → 涨停价成交 n=1119  净 **-63,952**  炸板率 45.2%
  A 等收盘确认封板了才买          n=779   净 **+154,650** 炸板率 0%（daban，现行生产）
  F 同 A 但买入日=连板确认日      n=1195  净 **+200,294** 炸板率 0%（same_day）

结论：**打板不是越早越好，盘中打板=赌封板=亏，必须"等确认"**。
而 same_day（F）之所以 0 炸板是结构性的——买入日=信号日，"空间龙"的定义本身就
要求当日收盘涨停，所以买入当天封板是**已确认的事实**，不是赌。

但 F 要求"当天收盘前就知道谁是空间龙并下单"，盘后跑的 build 做不到
（用户看到信号时已收盘）→ **必须由盘中程序盯**。这就是本模块。

========== 盯什么 / 怎么盯 ==========
1. 盘中实时算"今日潜在空间龙"候选池：当日封板 → 潜在连板数 = 昨日连板数 + 1，
   按（连板数降序, 换手降序）取前 TOPN（默认 5，与回测口径一致）。
2. 全程跟踪每个候选的**封板/炸板轨迹**：谁炸了、炸了几次、什么时候炸的、现在还封着吗。
   （这一步是为了把"盘中打板会炸"这件事从回测数字变成每天可观察的事实。）
3. **14:57 收盘集合竞价**确认：池中仍封板 且 排得到（非一字/秒板）的最高名次 → 买入决策，
   按名次顺延（回测已验证打板场景下顺延有益，与 T+1 开盘买那次否决的顺延不是一回事）。

========== 判定口径（与回测严格对齐）==========
·封板   : 实时 pct >= 9.8（主板 10% 涨停；ST 只有 5%，天然被排除）
·买不进 : open >= 涨停价 × 0.99 —— 与 backtest._s16_is_unbuyable_limit(skip_limit_open=True) 同口径
·涨停价 : round(前收 × 1.1, 2)，前收由 price/(1+pct/100) 反推（腾讯实时没有前收字段）
·昨日连板: 直接取 bt._S16_LEADERS_CACHE['by_date'] 的最后一个交易日，无需重扫 CSV

========== 运行方式 ==========
app.py do_scan 每轮（交易时段）调 tick(realtime, now)；14:57 触发 confirm()；
数据落盘 data/space_watch_{YYYY-MM-DD}.json（跨重启保留）。
本模块**只盯盘 + 出决策 + 通知，不自动改模拟账本**（改账本属上线动作，需用户确认）。
"""

import os
import sys
import json
import threading
import datetime

# backtest.py 在项目根（stock_bot 的上一级）。服务启动时 cwd 通常是项目根所以能直接
# import，但为了任意 CWD 都能用（脚本/自检/其它入口），这里显式锚定（同 DATA_ROOT 坑）。
DATA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DATA_ROOT not in sys.path:
    sys.path.insert(0, DATA_ROOT)

import backtest as bt
WATCH_DIR = os.path.join(DATA_ROOT, 'data')

SNAME = 'S16_空间龙盯盘'    # 通知/信号库用的策略名（独立通道，不污染 S16_龙头接力）

TOPN = 5                    # 盯盘候选池名次（= 回测 S16_DABAN_POOL_TOPN）
SEAL_PCT = 9.8              # 封板判定阈值（主板 10% 涨停）
CONFIRM_HM = 14 * 60 + 57   # 收盘集合竞价：确认并下单
FINAL_HM = 15 * 60 + 1      # 收盘后复核（真实收盘价写回）
MAX_TRACK = 240             # 单票轨迹最大点数（5 分钟一轮，够记一天）

_LOCK = threading.Lock()
_PREV = None                # {'date': 'YYYY-MM-DD', 'lb': {code: (连板数, 量比)}}

STATE = {
    'date': None,           # 盯盘日
    'prev_date': None,      # 昨日（本地数据最后交易日）
    'ready': False,
    'ticks': 0,
    'pool': [],             # 当前候选池
    'seen': {},             # code -> {'name','lb','breaks','sealed_now','first_seal','last_seal'}
    'tracks': {},           # code -> [{'t','pct'}, ...]
    'confirmed': None,      # 收盘确认的买入决策
    'final': None,          # 收盘后复核（真实收盘价）
    'log': [],
}


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except Exception:
        return None


def _today(now=None):
    return (now or datetime.datetime.now()).date().isoformat()


def _file(d=None):
    return os.path.join(WATCH_DIR, 'space_watch_%s.json' % (d or _today()))


def _name(code):
    try:
        return bt.STOCKS.get(code) or code
    except Exception:
        return code


# ---------- 昨日连板底表 ----------
def ensure_prev(force=False):
    """构建"昨日各票连板数"底表。

    直接复用 bt._s16_leaders 的 by_date 缓存（全市场每交易日涨停股 + 连板数），
    不再重扫 2600 份 CSV。首次调用会触发缓存构建（约 80s），应在 boot 阶段跑。
    """
    global _PREV
    if _PREV and not force:
        return _PREV
    try:
        bt._s16_leaders(force=force)
        bd = (bt._S16_LEADERS_CACHE or {}).get('by_date') or {}
    except Exception as e:
        _log('昨日连板底表构建失败: %s' % str(e)[:80])
        return None
    if not bd:
        return None
    try:
        d = max(bd.keys())
        dstr = str(d)[:10]
    except Exception:
        return None
    lb = {}
    for item in bd.get(d, []):
        try:
            code, streak, vr = item[0], int(item[1]), float(item[2])
        except Exception:
            continue
        lb[code] = (streak, vr)
    _PREV = {'date': dstr, 'lb': lb}
    return _PREV


# ---------- 盘中候选池 ----------
def _zt_price(rt):
    """由实时价与涨幅反推涨停价；推不出来返回 None。"""
    px = _f(rt.get('price'))
    pct = _f(rt.get('pct'))
    if not px or pct is None or pct <= -100:
        return None
    pre = px / (1.0 + pct / 100.0)
    return round(pre * 1.1, 2) if pre > 0 else None


# 一字/秒板开盘，但换手达到该阈值(%) → 说明板上有人在涨停价卖出，排队能成交。
# 真正买不进的是"一字开盘 + 换手极低（无人卖）"。
MIN_TURNOVER_YIZI = 3.0


def _buyable(rt):
    """盘中能否排得到。

    ★ 2026-09-28 盘中实测修正：原判据只看开盘价——open >= 涨停价×0.99 就判"一字/秒板买不进"。
      但一字开盘【若换手高】恰恰说明有大量卖单在涨停价成交（有人在卖），是排得到的。
      实测 000850 华茂股份一字开盘、换手 11.38% 却被判"买不进"，会白白错过整天的标的。
      故一字开盘时再用换手兜底：换手 >= MIN_TURNOVER_YIZI 视为排得到。
    """
    op = _f(rt.get('open'))
    zt = _zt_price(rt)
    if not op or not zt:
        return True
    if op < zt * 0.99:
        return True                     # 非一字开盘：盘中有价格变动，买得到
    to = _f(rt.get('turnover')) or 0.0
    return to >= MIN_TURNOVER_YIZI      # 一字开盘但换手够大 → 有人在涨停价卖


def scan_pool(realtime, topn=TOPN):
    """盘中算今日潜在空间龙候选池 → [{code,name,lb,pct,price,turnover,sealed,buyable}]

    潜在连板数 = 昨日连板数 + 1（今日正封板）。
    """
    prev = ensure_prev()
    if not prev:
        return []
    plb = prev['lb']
    out = []
    for code, rt in (realtime or {}).items():
        if not isinstance(rt, dict):
            continue
        pct = _f(rt.get('pct'))
        px = _f(rt.get('price'))
        if pct is None or not px:
            continue
        if pct < SEAL_PCT:          # 当前未封板（或 ST 的 5% 板）→ 不是候选
            continue
        lb0 = plb.get(code, (0, 0.0))[0]
        out.append({
            'code': code,
            'name': _name(code),
            'lb': int(lb0) + 1,
            'pct': round(pct, 2),
            'price': round(px, 2),
            'turnover': round(_f(rt.get('turnover')) or 0.0, 2),
            'open': round(_f(rt.get('open')) or 0.0, 2),
            'sealed': True,
            'buyable': _buyable(rt),
        })
    out.sort(key=lambda x: (-x['lb'], -x['turnover']))
    return out[:topn]


# ---------- 一轮盯盘 ----------
def quote_date(realtime):
    """取实时行情的日期（info['ts'] = YYYYMMDDHHMMSS，取全市场最大值的前 8 位）。

    存在的意义：休市日/行情未更新时，腾讯返回的仍是【上一交易日的收盘快照】，
    而"上一交易日涨停收盘"的票在这份快照里 pct 依然≈10%，看起来就是"封板"。
    用它可以识别出"这份行情不是今天的"，从而拒绝据此下单。
    """
    best = ''
    for info in (realtime or {}).values():
        try:
            t = str(info.get('ts') or '')
        except Exception:
            continue
        if len(t) >= 8 and t[:8] > best:
            best = t[:8]
    return '%s-%s-%s' % (best[:4], best[4:6], best[6:8]) if len(best) == 8 else ''


def tick(realtime, now=None, topn=TOPN):
    """每轮扫描调用：更新候选池 + 轨迹 + 炸板统计，到点触发收盘确认。返回 STATE 快照。"""
    now = now or datetime.datetime.now()
    with _LOCK:
        return _tick_impl(realtime, now, topn)


def _tick_impl(realtime, now, topn):
    d = now.date().isoformat()
    if STATE.get('date') != d:
        reset(d)
    prev = ensure_prev()
    STATE['prev_date'] = (prev or {}).get('date')
    STATE['ready'] = bool(prev)
    if not prev:
        return snapshot()

    # ★行情新鲜度校验（2026-09-25 中秋休市暴露）：休市日腾讯返回上一交易日收盘快照，
    #   其中"上一交易日涨停收盘"的票 pct 仍≈10%，scan_pool 会把它当成今日封板，
    #   14:57 就会拿这份陈旧数据误确认买入并推送错误信号。故行情日期必须 == 盯盘日。
    qd = quote_date(realtime)
    STATE['quote_date'] = qd
    if qd and qd != d:
        if not STATE.get('stale_logged'):
            STATE['stale_logged'] = True
            _log('行情日期 %s ≠ 盯盘日 %s（休市或行情未更新）→ 不更新候选池、不做收盘确认' % (qd, d))
        return snapshot()
    STATE['stale_logged'] = False

    hm = now.hour * 60 + now.minute
    pool = scan_pool(realtime, topn)
    STATE['pool'] = pool
    STATE['ticks'] = int(STATE.get('ticks') or 0) + 1
    ts = now.strftime('%H:%M')
    in_pool = set()

    for c in pool:
        code = c['code']
        in_pool.add(code)
        s = STATE['seen'].get(code) or {
            'name': c['name'], 'lb': c['lb'], 'breaks': 0,
            'sealed_now': True, 'first_seal': ts, 'last_seal': ts, 'buyable': c['buyable'],
        }
        s['name'] = c['name']
        s['lb'] = max(int(s.get('lb') or 0), c['lb'])
        s['sealed_now'] = True
        s['last_seal'] = ts
        s['buyable'] = c['buyable']
        STATE['seen'][code] = s
        tr = STATE['tracks'].setdefault(code, [])
        tr.append({'t': ts, 'pct': c['pct']})
        if len(tr) > MAX_TRACK:
            del tr[:len(tr) - MAX_TRACK]

    # 曾进池、当前不在池 = 这一轮没封住 → 记一次炸板
    for code, s in STATE['seen'].items():
        if code in in_pool:
            continue
        rt = (realtime or {}).get(code)
        pct = _f(rt.get('pct')) if isinstance(rt, dict) else None
        if pct is not None and pct >= SEAL_PCT:
            continue          # 仍封板，只是名次掉出 TOPN，不算炸
        if s.get('sealed_now'):
            s['breaks'] = int(s.get('breaks') or 0) + 1
        s['sealed_now'] = False

    # 收盘确认
    if hm >= CONFIRM_HM and not STATE.get('confirmed'):
        conf = _confirm_impl(realtime, now, pool)
        if conf:
            STATE['confirmed'] = conf
            _log('14:57 确认买入 %s(%s) %d板 @%.2f' % (
                conf['name'], conf['code'], conf['lb'], conf['price']))
    # 收盘后复核真实收盘价
    if hm >= FINAL_HM and STATE.get('confirmed') and not STATE.get('final'):
        c = STATE['confirmed']
        rt = (realtime or {}).get(c['code'])
        if isinstance(rt, dict):
            px = _f(rt.get('price'))
            if px:
                STATE['final'] = {
                    'code': c['code'], 'name': c['name'],
                    'close': round(px, 2), 'pct': round(_f(rt.get('pct')) or 0.0, 2),
                    'at': now.strftime('%H:%M'),
                }
    save()
    return snapshot()


def _confirm_impl(realtime, now, pool):
    """收盘确认：取池中"仍封板 且 排得到"的最高名次 → 买入决策（顺延）。"""
    skipped = []
    for c in (pool or []):
        if c.get('sealed') and c.get('buyable'):
            return {
                'code': c['code'], 'name': c['name'], 'lb': c['lb'],
                'price': c['price'], 'pct': c['pct'],
                'at': now.strftime('%H:%M:%S'), 'date': now.date().isoformat(),
                'reason': '收盘集合竞价确认封板（%d板，非一字/秒板）' % c['lb'],
                'skipped': skipped,
            }
        skipped.append('%s(%d板)%s' % (
            c['name'], c['lb'], '一字/秒板买不进' if c.get('sealed') else '已炸板'))
    if pool:
        _log('14:57 无可执行标的：%s' % '；'.join(skipped))
    return None


def confirm(realtime, now=None):
    """手动触发收盘确认（幂等：已确认则直接返回）。"""
    now = now or datetime.datetime.now()
    with _LOCK:
        if STATE.get('confirmed'):
            return STATE['confirmed']
        return _confirm_impl(realtime, now, scan_pool(realtime, TOPN))


# ---------- 状态存取 ----------
def reset(d=None):
    STATE.update({
        'date': d or _today(), 'prev_date': None, 'ready': False, 'ticks': 0,
        'pool': [], 'seen': {}, 'tracks': {}, 'confirmed': None, 'final': None, 'log': [],
    })


def snapshot():
    """给页面/通知用的只读快照（含轨迹摘要）。"""
    pool = [dict(c) for c in (STATE.get('pool') or [])]
    seen = {}
    for code, s in (STATE.get('seen') or {}).items():
        seen[code] = dict(s)
    return {
        'date': STATE.get('date'), 'prev_date': STATE.get('prev_date'),
        'quote_date': STATE.get('quote_date'), 'stale': bool(STATE.get('stale_logged')),
        'ready': bool(STATE.get('ready')), 'ticks': STATE.get('ticks'),
        'pool': pool, 'seen': seen,
        'tracks': {k: v[-12:] for k, v in (STATE.get('tracks') or {}).items()},
        'confirmed': STATE.get('confirmed'), 'final': STATE.get('final'),
        'log': (STATE.get('log') or [])[-20:],
    }


def _log(msg):
    st = datetime.datetime.now().strftime('%H:%M:%S')
    (STATE.setdefault('log', [])).append('[%s] %s' % (st, msg))
    try:
        print('[space-watch] %s' % msg)
    except Exception:
        pass


def save(d=None):
    try:
        # ★必须落盘到【盯盘日】而非"今天"：服务重启后 load 的是历史盯盘日，
        #   若用 _today() 会把恢复出来的历史状态写进今天的文件里。
        d = d or STATE.get('date') or _today()
        os.makedirs(WATCH_DIR, exist_ok=True)
        with open(_file(d), 'w', encoding='utf-8') as f:
            json.dump(snapshot(), f, ensure_ascii=False)
    except Exception as e:
        _log('落盘失败: %s' % str(e)[:60])


def load(d=None):
    p = _file(d)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            obj = json.load(f)
        for k in ('date', 'prev_date', 'ready', 'ticks', 'pool', 'seen',
                  'tracks', 'confirmed', 'final', 'log'):
            if k in obj:
                STATE[k] = obj[k]
        return snapshot()
    except Exception as e:
        _log('读取失败: %s' % str(e)[:60])
        return None


def load_today(now=None):
    """服务重启后恢复当日盯盘进度（若有）。"""
    d = _today(now)
    if STATE.get('date') == d and STATE.get('ticks'):
        return snapshot()
    return load(d)


# ---------- 给页面看的文案 ----------
def pool_lines(snap=None, limit=5):
    """候选池渲染行（含"是否排得到"提示）。"""
    snap = snap or snapshot()
    out = []
    for i, c in enumerate((snap.get('pool') or [])[:limit], 1):
        tag = '封板' if c.get('sealed') else '未封'
        if c.get('sealed') and not c.get('buyable'):
            tag = '封板·一字/秒板买不进'
        out.append('%d. %s(%s) %d板 %.2f元 %+.2f%% 换手%.2f%% [%s]' % (
            i, c.get('name'), c.get('code'), c.get('lb', 0),
            c.get('price') or 0, c.get('pct') or 0, c.get('turnover') or 0, tag))
    return out


def confirm_text(snap=None):
    """收盘确认结果文案（供邮件/页面）。"""
    snap = snap or snapshot()
    c = snap.get('confirmed')
    if not c:
        return '今日无可执行标的（候选均未封板或一字/秒板买不进）'
    fin = snap.get('final') or {}
    px = fin.get('close') or c.get('price')
    return ('信号确认 %s(%s)：%d板，今日收盘封板，参考价 %.2f 元%s\n'
            '口径：14:57 收盘集合竞价挂涨停价【排队】——★封板≠买得到，'
            '只有盘中有换手、收盘时有卖单才成交（一字/秒板已排除）。请纸面记录实际成交率。'
            % (c.get('name'), c.get('code'), c.get('lb'), px,
               '（收盘 %.2f 复核）' % fin['close'] if fin.get('close') else ''))
