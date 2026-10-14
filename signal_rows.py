# -*- coding: utf-8 -*-
"""信号行统一模型（2026-09-10 结构优化第 1 步）。

背景：同一件事——「某个策略某天对某标的发出了买入/卖出信号」——此前在 app.py 里被
**4 处各写一遍**判定逻辑：
  ① `_intraday_sig_row()`   盘中实时（TODAY_STATUS 单条）
  ② `_intraday_sig_rows()`  盘中实时（SQLite 事件流多条）
  ③ `_candidate_row()`      未决换仓候选（速览表置顶行）
  ④ `_pending_signals_html()` 内联的「换仓候选」判定（首页信号登记块）
结果是改一处漏一处：9/10 修「特  力Ａ（已换仓）」后缀判定时只改了 ③，④ 仍是旧的
`startswith('已换仓')`，首页信号登记块会残留已换仓的伪信号行。

本模块把「判定」收敛为纯函数（不依赖 app 全局状态，避免循环导入），只产出**行对象**，
HTML 渲染仍留在 app.py。所有消费方（首页信号登记 / 总账 / 速览表 / 每日选股 / 今日行动清单）
都从同一函数取行，口径自然一致。

行对象字段（`kind` 为统一字段，其余为兼容既有消费方而保留）：
    kind     '买入信号' | '卖出信号'      —— 记录类型
    status   同 kind（兼容旧代码读 status）
    sid      策略全名（如 'S16_龙头接力'）
    date     信号日 'YYYY-MM-DD'
    name     标的名称（纯文本，渲染时自行 escape/加粗）
    code     标的代码（可空）
    extra    名称后缀（如 '（评分95）'，可空）
    score / price / buy_ref / stop / target   盘中信号附带数值
    tag      短标签（如 '盘中触发·待收盘确认'）
    note     说明文本（纯文本）
    source   'intraday' | 'closed' | 'candidate'
"""
import datetime

from ui_common import _md, _next_td, _dash

# 未决换仓候选可作为「买入信号」登记的最新行状态
CAND_STATUSES = ('建仓', '卖出成交', '卖出信号', '卖出顺延', '持有')


# ============================================================
# 换仓候选文案「标的（评分N）」解析 —— 全系统唯一入口
# ============================================================

def parse_rec_buy(rb):
    """解析最新行 rec_buy 文本 → dict | None

    形如 '百大集团（评分95）'（未决候选）、'特  力Ａ（已换仓）'（已执行）、
    '国芳集团（已废弃）'（已废弃）。返回：
        {'name': 标的, 'extra': '（评分95）', 'done': 是否已换仓, 'dead': 是否已废弃}
    done/dead 任一为真即**不是**待执行的买入信号。
    """
    rb = str(rb or '').strip()
    if not rb or '（' not in rb:
        return None
    name = rb.split('（', 1)[0].strip()
    if not name or name in ('—', '-'):
        return None
    extra = rb[rb.find('（'):]
    return {'name': name, 'extra': extra,
            'done': '已换仓' in rb, 'dead': '已废弃' in rb}


def pending_candidate(last):
    """最新行是否携带「未决换仓候选」→ 解析结果 dict | None

    ★ 唯一判定入口：速览表置顶行与首页信号登记块都必须走这里，
      禁止再各自写 `startswith('已换仓')` 之类判定（9/10 漏改事故的根因）。
    """
    last = last or {}
    info = parse_rec_buy(last.get('rec_buy'))
    if not info or info['done'] or info['dead']:
        return None
    if str(last.get('status') or '') not in CAND_STATUSES:
        return None
    return info


# ============================================================
# 盘中实时信号（SQLite 事件流 → 行对象）
# ============================================================

def _intraday_row_from_ts(ts, sid=None):
    """TODAY_STATUS 单条回退：事件流为空时用最近一轮扫描的实时信号"""
    ts = ts or {}
    sig = ts.get('signal') or {}
    d0 = str(ts.get('date') or '')
    rot = str(ts.get('rotation') or '')
    if not (sig.get('triggered') and sig.get('name') and d0):
        return None
    if sid is not None and rot and str(sid) != rot:
        return None
    nm = str(sig.get('name') or '').strip()
    if not nm:
        return None
    ex = _next_td(d0)
    _cl = datetime.datetime.now().hour >= 15   # ★ 2026-09-10 收盘后改用"已确认"口径
    if _cl:
        _note = (f'{_md(d0)} 收盘已确认（收盘价 {_dash(sig.get("price"), "—")}，'
                 f'评分 {_dash(sig.get("score"), "—")}'
                 + (f'，{sig.get("tag")}' if sig.get('tag') else '')
                 + f'）：于 {_md(ex)} 按本策略时点买入（T+1）')
    else:
        _note = (f'{_md(d0)} 盘中实时触发（现价 {_dash(sig.get("price"), "—")}，'
                 f'评分 {_dash(sig.get("score"), "—")}'
                 + (f'，{sig.get("tag")}' if sig.get('tag') else '')
                 + f'）：收盘价确认后于 {_md(ex)} 按本策略时点买入（T+1）；'
                   '收盘未确认则本行自动消失')
    return {'_intra': True, 'kind': '买入信号', 'status': '买入信号', 'date': d0, 'name': nm,
            'sid': rot, 'code': str(sig.get('code') or ''),
            'score': sig.get('score'), 'price': sig.get('price'),
            'buy_ref': sig.get('buy_ref'), 'stop': sig.get('stop'), 'target': sig.get('target'),
            'tag': ('收盘确认' if _cl else '盘中触发'),
            'source': 'intraday',
            'note': _note}


def intraday_rows(ts, sid=None, evs=None, include_gone=False, include_watch=False,
                  ledger=None):
    """当日**全部**盘中触发信号 → 行对象列表。

    数据源：notify 的 SQLite 信号库（data/signals.db，跨重启持久化）；库为空时回退
    TODAY_STATUS 单条（兼容）。sid 传策略全名时只返回该策略的行。
    跨日历史见 /signals 页（notify.intraday_signals_history）。

    ★ 2026-09-17 新增 include_gone，默认 **False**：本函数消费方（首页信号登记 / 总账 /
      速览表 / 每日选股 / 今日行动清单 / 详情页提示条）都是"**待执行信号**"口径，而**已消失**的
      行（盘中曾触发后未持续 / 收盘未确认 / 隔日失效）不是待执行信号，不该再当买入信号登记
      ——此前 9/16 S15 会把"高新发展（已消失·换标的）"置顶，与收盘判定的方大集团互相矛盾。
      含已消失/已停用的完整历史请到 /signals 页（它直读 notify.intraday_signals_history）。

    ★ 2026-09-18 新增 include_watch，默认 **False**：**持仓跟踪**行（watch=1，轮值外策略对
      自己持仓股的盘中窄扫描留痕）不是买入信号——它只是"推荐的下文"，不该进待执行信号口径。
      持仓跟踪只在 /signals 页展示（含 T+1/T+3/T+5 前向表现）。

    ★ 2026-10-08 新增 ledger（策略全名 → 当日账本行 dict）用于**收盘对账**：
      15:00 后本函数原本**无条件**宣称"今日收盘已确认"，但盘中触发用的是实时价拼的伪 bar
      （当日冲高时"创 20 日新高"成立、收盘回落后即不成立），收盘用真实收盘价重判后常常
      **并不触发**；于是出现"首页/邮件说今日收盘触发明日买入、收盘账本却判空仓"的自相矛盾
      （10/08 S9 中国天楹 000035 正是如此）。现在：仅在**当日账本行已落地**（说明收盘
      build 完成、判定权威）时做对账，若该策略当日未触发则行标记 `_confirmed=False`，
      由下游（今日行动清单 / 收盘复盘）降级为"仅留痕、不执行"。
      账本未滚到当日（盘中、或策略本就无账本如空间龙盯盘）→ 不做任何证伪，维持原行为。
    """
    if evs is None:
        try:
            import notify
            evs = notify.intraday_signals_today(sid, include_watch=include_watch)
        except Exception:
            evs = []
    if not evs:
        _one = _intraday_row_from_ts(ts, sid)
        return [_one] if _one else []
    d0 = datetime.date.today().isoformat()
    ex = _next_td(d0)
    _cl = datetime.datetime.now().hour >= 15   # ★ 2026-09-10 收盘后改用"已确认"口径
    # 收盘后被视为"已确认"的账本状态（其余一律算未确认）
    _CONFIRM_ST = ('买入信号', '买入信号(待成交)', '建仓', '换仓买入', '买入成交')
    out = []
    for e in evs:
        nm = str(e.get('name') or '').strip()
        if not nm:
            continue
        if e.get('gone') and not include_gone:
            continue    # ★ 已消失 → 不是待执行信号（历史请到 /signals 页查）
        if e.get('watch') and not include_watch:
            continue    # ★ 持仓跟踪行 → 不是买入信号（只在 /signals 页展示"推荐的下文"）
        _first = str(e.get('first_at') or '')
        _last = str(e.get('last_at') or '')
        _cnt = int(e.get('count') or 1)
        _t = _first[11:16] if len(_first) >= 16 else ''
        _tl = _last[11:16] if len(_last) >= 16 else ''
        # ★ 2026-10-08 收盘对账（见 docstring）：15:00 后且当日账本已落地才判，避免误杀
        _conf, _lg_st = None, ''
        if _cl and ledger:
            _lr = ledger.get(str(e.get('sname') or ''))
            if _lr is not None:
                _lg_st = str(_lr.get('status') or '未触发')
                _conf = bool(_lr.get('triggered')) or _lg_st in _CONFIRM_ST
        if e.get('gone'):
            _nxt = ('盘中曾触发（%s%s）后信号未持续（%s）；若收盘重新确认则以收盘判定为准'
                    % (_t, ('～' + _tl) if (_tl and _tl != _t) else '',
                       str(e.get('gone_reason') or '信号消失')))
        elif _conf is False:
            _nxt = ('盘中实时触发（%s%s），今日收盘用真实收盘价重判未成立（当日账本判定：%s）'
                    '→ 本条不执行，仅留痕'
                    % (_t, ('（共%d次，最近 %s）' % (_cnt, _tl)) if _cnt > 1 else '', _lg_st))
        elif _cl:
            _nxt = ('盘中实时触发（%s%s），今日收盘已确认，于 %s 按本策略时点买入（T+1）'
                    % (_t, ('（共%d次，最近 %s）' % (_cnt, _tl)) if _cnt > 1 else '', _md(ex)))
        else:
            _nxt = ('盘中实时触发（%s%s），收盘价确认后于 %s 按本策略时点买入（T+1）；'
                    '收盘未确认则本行自动消失'
                    % (_t, ('（共%d次，最近 %s）' % (_cnt, _tl)) if _cnt > 1 else '', _md(ex)))
        # ★ 2026-09-10：旧 JSON 迁移进库的事件只有时间、没有 price/score（库里为 NULL）→
        #   同标的时从今日 TODAY_STATUS 补齐，避免页面显示"收盘价 None / 评分 None"
        _px, _sc = e.get('price'), e.get('score')
        if (_px is None or _sc is None):
            # ts 传非 dict（历史调用有传日期字符串的）时不再抛 AttributeError → 整页 500
            _ts_sig = (ts.get('recommend') or {}) if isinstance(ts, dict) else {}
            if str(_ts_sig.get('code') or '') == str(e.get('code') or ''):
                _px = _px if _px is not None else _ts_sig.get('price')
                _sc = _sc if _sc is not None else _ts_sig.get('score')
        _pxs = []
        if _px is not None:
            _pxs.append(f'{"收盘价" if _cl else "现价"} {_px}')
        if _sc is not None:
            _pxs.append(f'评分 {_sc}')
        out.append({'_intra': True, 'kind': '买入信号', 'status': '买入信号', 'date': d0, 'name': nm,
                    'sid': str(e.get('sname') or ''), 'code': str(e.get('code') or ''),
                    'gone': bool(e.get('gone')),   # ★ 2026-09-17 供调用方区分"仍在触发/已消失"
                    # ★ 2026-10-08 收盘对账结论：False=收盘已用真实收盘价重判、本条不成立（不执行）；
                    #   None=无可对账的当日账本行（盘中 / 该策略无账本如空间龙盯盘）→ 维持原行为
                    '_confirmed': _conf,
                    '_ledger_status': _lg_st,
                    'score': _sc, 'price': _px,
                    'buy_ref': e.get('buy_ref'), 'stop': e.get('stop'), 'target': e.get('target'),
                    'tag': ('收盘未确认' if _conf is False else ('收盘确认' if _cl else '盘中触发')),
                    'source': 'intraday',
                    'note': (f'{_md(d0)} ' + _nxt
                             + (('：' + '，'.join(_pxs)) if _pxs else '')
                             + (f'，{e.get("tag")}' if e.get('tag') else ''))})
    return out


# ============================================================
# 未决换仓候选 → 速览表置顶的「买入信号」行
# ============================================================

def candidate_row(last):
    """最新行携带的「未决换仓候选」→ 独立「买入信号」展示行（2026-09-09 用户要求）。

    场景：S16 龙头 9/8 早盘建仓龙版传媒、当日收盘断板扫出换仓候选百大集团——
    此前只在「同日买入推荐」列出现一行小字，买卖时点速览没有独立记录；
    现把该候选作为一条「买入信号」行置顶登记（9/9 开盘卖出持仓成交后换仓买入）。
    候选已废弃/已成交时最新行会演进出新状态（信号废弃/换仓买入…），此候选行自然消失。
    """
    try:
        info = pending_candidate(last)
        if not info:
            return None
        last = last or {}
        nm = info['name']
        d0 = str(last.get('date') or '')
        _tl = str(last.get('pending_sell') or '')
        ex = _next_td(d0)
        exs = f'{_md(ex)} 开盘' if ex else '下一交易日开盘'
        st = str(last.get('status') or '')
        if _tl:
            # ★ 2026-09-10 尾盘策略盘中预览行：卖出挂单今日尾盘才成交，执行日=今天（非 d0 的次日）
            why = (f'{_md(d0)} 盘中预览：今日{_tl}先卖出当前持仓、成交后同笔换仓买入该候选'
                   '（模拟先卖后买；持仓停牌卖不出则顺延，开盘顶板/低开证伪则废弃）')
        elif st == '卖出顺延':
            why = (f'{_md(d0)} 收盘扫出的换仓候选：持仓股停牌致卖出顺延，模拟换仓未执行'
                   f'（信号保留至复牌日）；若你无持仓可自行参考该龙头 {exs}，不追高、开盘顶板买不进即放弃')
        else:
            why = (f'{_md(d0)} 收盘判定：{exs} 先卖出当前持仓、成交后换仓买入该候选'
                   '（模拟先卖后买；若持仓当日卖出受阻则顺延，开盘顶板/低开证伪则废弃）')
        return {'_cand': True, 'kind': '买入信号', 'status': '买入信号', 'date': d0,
                'name': f'{nm}{info["extra"]}', 'note': why,
                'tag': '换仓候选', 'source': 'candidate'}
    except Exception:
        return None


# ============================================================
# 停牌挂账（frozen）：卖不掉也不能耽误换仓，挂账状态须跨日可见
# ============================================================

def frozen_open(daily):
    """跨日追踪「停牌挂账待复牌卖出」的旧仓 → (登记行, frozen_sell) 或 (None, None)。

    背景（2026-09-10 用户要求「修挂账展示」）：`frozen_sell` 只写在**换仓当日那一行**
    （旧仓停牌卖不掉 → 卖出顺延、换仓买入照常执行；见 history_engine）。此后每个交易日
    都会追加新行，展示层若只看 `daily[-1]`，次日（新仓卖出信号行落地）起挂账提示就整条消失
    —— 案例：S16 9/9 停牌换仓买入百大集团、龙版传媒挂账，9/10 收盘新增卖出行后
    首页「另持仓（停牌挂账）」不再出现。

    口径：从最新行往回找最近的 frozen_sell；若该行之后（含该行）出现过同代码 frozen_sold
    （= 复牌已按挂单时点补卖，挂账了结）则不再返回。frozen_sold 可能落在同一行（复牌当日
    同时持有新仓），故用「自新到旧」的 sold 集合判定。
    """
    sold = set()
    for r in reversed(daily or []):
        fsd = r.get('frozen_sold') or {}
        if isinstance(fsd, dict) and fsd.get('code'):
            sold.add(str(fsd['code']).strip())
        fs = r.get('frozen_sell') or {}
        if not isinstance(fs, dict):
            continue
        c = str(fs.get('code') or '').strip()
        if c and c not in sold:
            return r, fs
    return None, None


# ============================================================
# 首页「📌 信号登记」块：待执行信号统一收集
# ============================================================

def register_rows(history, ts, intraday=None, susp_hint=None):
    """首页总账顶部「信号登记」块的行数据（2026-09-09 口径：信号触发/判定当日即登记，
    不必等收盘后模拟成交才有记录）。

    ★ 2026-10-14 新增 susp_hint：可调用对象 f(code)->str|None，返回该标的的停牌提示文案
      （今日停牌 / 盘前疑似停牌），None 表示无需提示。只追加到"执行日=今天"的待执行行
      文案里，不改 kind/status（登记口径不变）。
      放这里做参数而不直接引 app 的 RT_SNAPSHOT：本模块不依赖 app 全局状态（见 ui_common
      顶部约定），由 app 侧注入判定能力。
    ★ 同日修复：账本行的 code 此前硬编码为空字符串（页面看不到代码、也无法按代码判停牌），
      现改为取账本行的 code。

    覆盖三类：
      ① 今日盘中实时触发（SQLite 事件流 / TODAY_STATUS 回退）
      ② 各策略最新行仍待执行的买入信号 / 卖出信号
      ③ 未决换仓候选（= 一笔「买入信号」，判定统一走 pending_candidate）
    返回行对象列表，字段见模块 docstring（name/code/extra 已拆开，渲染侧自行转义）。
    """
    rows = []
    _d0 = datetime.date.today().isoformat()
    _ex = _next_td(_d0)
    # ★ 2026-09-10：收盘（15:00）后当日信号已按收盘价确认，不再挂"待收盘确认/未确认则消失"的盘中口径
    _closed = datetime.datetime.now().hour >= 15
    for r in (intraday if intraday is not None else intraday_rows(ts)):
        _rot = str(r.get('sid') or '')
        if not _rot:
            continue
        _note = str(r.get('note') or '')
        if _closed:
            _note = (_note.replace('收盘价确认后于', '今日收盘已确认，于')
                          .replace('；收盘未确认则本行自动消失', ''))
        rows.append({'sid': _rot, 'kind': '买入信号', 'source': 'intraday',
                     'tag': ('收盘已确认·待执行' if _closed else '盘中触发·待收盘确认'),
                     'name': str(r.get('name') or ''), 'code': str(r.get('code') or ''), 'extra': '',
                     'time_txt': (f'{_md(_d0)} 收盘' if _closed else f'{_md(_d0)} 盘中'),
                     'desc': (_note
                              or f'现价 {_dash(r.get("price"), "—")} ｜ '
                                 f'评分 {_dash(r.get("score"), "—")}'
                                 f' —— 收盘确认后于 {_md(_ex)} 按策略时点执行（T+1）')})
    for s in ((history or {}).get('strategies') or []):
        dl = s.get('daily') or []
        if not dl:
            continue
        last = dl[-1]
        st = str(last.get('status') or '')
        nm = str(last.get('name') or '')
        d0 = str(last.get('date') or '')
        ex = _next_td(d0)
        _sid = s.get('name') or ''
        _cd = str(last.get('code') or '')
        # ★ 停牌提示：只在"执行日=今天"时才提示（未来执行日的行说"今日停牌"没有意义）
        _sh = (susp_hint(_cd) if (susp_hint and _cd and ex == _d0) else None)
        _sh_txt = (f'；⚠ {_sh}' if _sh else '')
        if st in ('买入信号', '买入信号(待成交)') and nm:
            rows.append({'sid': _sid, 'kind': '买入信号', 'source': 'closed',
                         'tag': '收盘已判定（待执行）', 'name': nm, 'code': _cd, 'extra': '',
                         'time_txt': f'{_md(d0)} 收盘',
                         'desc': (f'信号确认，{_md(ex)} 按策略时点买入（T+1，不追价）'
                                  + _sh_txt)})
        elif st == '卖出信号' and nm:
            rows.append({'sid': _sid, 'kind': '卖出信号', 'source': 'closed',
                         'tag': '收盘判定（待执行）', 'name': nm, 'code': _cd, 'extra': '',
                         'time_txt': f'{_md(d0)} 收盘',
                         'desc': (f'判定离场，{_md(ex)} 按策略时点卖出' + _sh_txt)})
        # 未决换仓候选 → 独立「买入信号」登记（9/8 建仓龙版传媒→百大集团 场景）
        info = pending_candidate(last)
        if info:
            _tag = '卖出顺延' if st == '卖出顺延' else '换仓候选'
            rows.append({'sid': _sid, 'kind': '买入信号', 'source': 'candidate',
                         'tag': f'{_tag}（收盘判定）', 'name': info['name'], 'code': '',
                         'extra': info['extra'], 'time_txt': f'{_md(d0)} 收盘',
                         'desc': (f'{_md(d0)} 动作扫出的换仓候选：{_md(ex)} 先卖出持仓、成交后换仓买入'
                                  '（卖出受阻则顺延；顶板/低开证伪则废弃）')})
        # ★ 2026-09-10 停牌挂账：挂单顺延中的**卖出信号**必须持续登记（跨日），
        #   否则次日新行落地后「某策略还有一笔卖不掉等复牌」这条信息在信号登记块里消失。
        _fzr, _fz = frozen_open(dl)
        if _fz:
            _fznm = str(_fz.get('name') or '')
            _fzcd = str(_fz.get('code') or '')
            _fzsince = str(_fz.get('since') or (_fzr or {}).get('date') or '')
            if _fznm and not (st == '卖出信号' and nm == _fznm):
                rows.append({'sid': _sid, 'kind': '卖出信号', 'source': 'frozen',
                             'tag': '卖出顺延·停牌挂账', 'name': _fznm, 'code': _fzcd,
                             'extra': '', 'time_txt': f'{_md(_fzsince)} 起停牌',
                             'desc': (f'持仓股 {_fznm} 停牌（无成交），卖出挂单顺延至复牌日'
                                      '（信号不废弃，复牌后按当日时点补卖）；'
                                      '同期换仓买入已照常执行，不因卖不掉而耽误')})
    return rows
