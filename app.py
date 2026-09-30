# -*- coding: utf-8 -*-
"""策略选股回放 + 每日推荐 Web 服务
- 后台引擎（history_engine）生成每个策略的每日选股记录与历史交易列表（本金5000）
- 页面按策略展示：买卖时点速览 + 每日选股（含T+1涨跌、持仓/空仓状态）+ 历史交易明细
- 交易日每5分钟扫描一次行情并刷新页面；信号收盘后判定、T+1开盘执行
- 页面极简：纯 HTML + 少量 JS（折叠/过滤/锚点导航）
启动：start.bat 或 python app.py
"""
import sys, os, re, json, time, threading, datetime, html, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WORKSPACE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKSPACE)

import advisor
import notify
import space_watch      # ★ 2026-09-24 S16 空间龙盘中盯盘器（候选池/炸板轨迹/收盘确认）
# ★ 2026-09-10 结构优化（第 1 步）：页面样式/文本工具/SVG 图表等**纯展示基础件**移到 ui_common，
#   "信号行"判定统一到 signal_rows，消除同一逻辑在 4 处各写一遍（改一处漏一处）的问题。
#   注意：ui_common / signal_rows 内不得反向 import app —— app.py 以 __main__ 运行，
#   被二次 import 会产生两份全局状态（TODAY_STATUS/HISTORY 不共享）。
from ui_common import (CSS, JS, _md, _next_td, _num2, _dash, _esc_note, svg_line_chart,
                       MARKET_HOLIDAYS)
import signal_rows

PORT = 8081
SCAN_SECONDS = 300
HISTORY_FILE = 'data/strategy_daily.json'

STATE = {
    'last_scan': None,
    'scan_log': [],
    'recommend': None,
    'rank': [],
    'strategy': None,
    'date': None,
    'realtime_ok': False,
    'scans_today': 0,
    'data_asof': None,   # 判断所依据的数据时点（今日收盘价/盘中快照/本地数据）
    'idx_asof': {},      # 指数估值数据截至日
}
# ★ 统一「今日状态」（2026-09-03）：页面「今日盘中提醒」卡与微信推送共用同一数据源
TODAY_STATUS = None
# ★ 最近一次扫描的实时行情快照 {code: {price/open/volume/date...}}（2026-09-10）：
#   供「今日行动清单」判定标的当日是否停牌（停牌 → 买卖挂单顺延，不能照常显示"今日需执行"）
RT_SNAPSHOT = {}
# 收盘后每日一次后台 build 的状态（running=正在跑；last_ok=最近一次完成时间）
BUILD_STATE = {'running': False, 'last_ok': None}
LAST_BUILD_FILE = 'data/last_build_date.json'   # 每日 build 去重标记（跨启动）
LAST_INTRA_FILE = 'data/last_intra_build_date.json'  # ★盘中即时记录槽去重（am/pm 每槽每日至多一次）
_MKT_CACHE = {'date': None, 'data': None}       # analyze_market 按自然日缓存
HISTORY = None          # strategy_daily.json 数据
HISTORY_READY = False   # 引擎是否已生成
PAGE_CACHE = {}         # 页面 HTML 缓存 {path: bytes}
POOL_INDEX = None       # 选股池指数数据（data/pool_index.json）
POOL_INDEX_FILE = 'data/pool_index.json'
DYNAMIC_POOL = None     # 动态选股池指数数据（data/dynamic_pool_index.json）
DYNAMIC_POOL_FILE = 'data/dynamic_pool_index.json'
DYNAMIC_POOL_BT = None  # 动态选股池回测报告（data/dynamic_pool_bt.json）
DYNAMIC_POOL_BT_FILE = 'data/dynamic_pool_bt.json'
SH_INDEX_CSV = 'data/csv/sh000001.csv'


def log(msg):
    t = datetime.datetime.now().strftime('%H:%M:%S')
    STATE['scan_log'].append(f'[{t}] {msg}')
    STATE['scan_log'] = STATE['scan_log'][-20:]


def _is_trading_day(dt):
    """是否【交易日】= 非周末 且 不在交易所休市日（MARKET_HOLIDAYS）。

    ★2026-09-30 收口：此前代码里四处交易日判断都写 `weekday() >= 5`，只跳周末、
      不看休市日 —— 于是国庆 10/1(周四)~10/7 会被当成正常交易日：照样拉行情、
      跑盘中/收盘重建，并可能把休市日写进 strategy_daily（污染账本）。
      现统一经由此函数，所有日历判断共用 ui_common.MARKET_HOLIDAYS。
      该表按上交所休市公告维护，**每年必须人工补充下一年度**（见 ui_common 注释）。"""
    try:
        d = dt.date() if isinstance(dt, datetime.datetime) else dt
        return d.weekday() < 5 and d.isoformat() not in MARKET_HOLIDAYS
    except Exception:
        return True      # 取不到日历时保守放行，不改变原有调度行为


def is_trading_time(dt=None):
    dt = dt or datetime.datetime.now()
    if not _is_trading_day(dt):
        return False
    hm = dt.hour * 60 + dt.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def _to_float(parts, i):
    try:
        return float(parts[i]) if i < len(parts) and parts[i] else None
    except (ValueError, IndexError):
        return None


def fetch_realtime():
    """拉取个股实时行情，返回 {code: {price/open/high/low/volume/amount/turnover/amp/pct/chg/ts/date}}
    - amount 统一转为元（与本地 CSV 一致）；ts 为行情时间戳（YYYYMMDDHHMMSS）
    - 全市场 2600+ 只必须分批拉取（每批 100），单 URL 拼全部会被腾讯接口拒绝返回空"""
    try:
        import requests
        symbols = []
        for code in advisor.STOCKS:
            prefix = 'sh' if code.startswith('6') else 'sz'
            symbols.append(f'{prefix}{code}')
        out = {}
        CHUNK = 100
        # ★ 2026-09-28：环境里的 HTTP(S)_PROXY 会让连续 26 批请求全部 ProxyError
        #   （实测盘中 fetch_realtime 返回 0 只 → 盯盘全程无行情）。
        #   用 trust_env=False 的 Session 直连腾讯；直连不通再退回默认（走代理）。
        _sess = requests.Session()
        _sess.trust_env = False
        for i in range(0, len(symbols), CHUNK):
            part = symbols[i:i + CHUNK]
            try:
                url = 'https://qt.gtimg.cn/q=' + ','.join(part)
                try:
                    r = _sess.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                except Exception:
                    r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                r.encoding = 'gbk'
                for line in r.text.strip().split(';'):
                    if '="' not in line:
                        continue
                    # ★ 2026-09-28 修复：单行解析失败绝不能丢弃整批。
                    #   实测盘中 26 批全部失败 → fetch_realtime 返回 0 只 → 盯盘全程瞎跑。
                    #   只要一批里有一只停牌/字段数不足的个股抛异常，整批 100 只全废。
                    try:
                        head, _, payload = line.partition('="')
                        sym = head.split('_')[-1].strip()
                        code = sym[2:]
                        parts = payload.rstrip('"').split('~')
                        if len(parts) <= 3 or not parts[3]:
                            continue
                        ts = parts[30] if len(parts) > 30 else ''
                        d = ts[:8] if len(ts) >= 8 else ''
                        amount_wan = _to_float(parts, 37)
                        out[code] = {
                            'price': _to_float(parts, 3),
                            'open': _to_float(parts, 5),
                            'high': _to_float(parts, 33),
                            'low': _to_float(parts, 34),
                            'volume': _to_float(parts, 36),
                            'amount': round(amount_wan * 10000, 2) if amount_wan else None,
                            'turnover': _to_float(parts, 38),
                            'amp': _to_float(parts, 43),
                            'pct': _to_float(parts, 32),
                            'chg': _to_float(parts, 31),
                            'ts': ts,
                            'date': f'{d[:4]}-{d[4:6]}-{d[6:8]}' if len(d) == 8 else '',
                        }
                    except Exception:
                        continue          # 跳过这一只，保留同批其余股票
            except Exception as e:
                log(f'实时行情第 {i // CHUNK + 1} 批失败({type(e).__name__})，继续下一批')
        if out:
            log(f'实时行情获取成功 {len(out)} 只')
            return out
        log('实时行情为空，使用本地数据')
        return {}
    except Exception as e:
        log(f'实时行情不可用: {str(e)[:40]}，使用本地数据')
        return {}


_SCAN_LOCK = threading.Lock()


def _in_close_window(now):
    """★ 2026-09-17 新增：收盘判定窗口 = 交易日 15:00~16:30。

    背景：`is_trading_time` 只覆盖 13:00~15:00（含 15:00 那一分钟），而 do_scan 开头是
    `if not force and not is_trading_time(now): return` → 15:01 起每轮扫描直接返回，收盘段
    （收盘判定/收盘简报/当日记录重算/**信号对账**）只有"碰巧在 15:00:00~15:00:59 那一轮命中"
    才会执行，否则要等下次重启 boot_scan(force=True) 才补。表现为"收盘记录/简报时有时无"。
    """
    if not _is_trading_day(now):
        return False
    hm = now.hour * 60 + now.minute
    return 15 * 60 <= hm <= 16 * 60 + 30


def _scan_holdings_intraday(rot_sname='', realtime=None, today=None):
    """★ 2026-09-18：盘中**持仓策略**留痕（用户反馈"推荐的下文跟踪断了"）。

    背景：盘中扫描只覆盖**当日轮值策略**（一天 1 个，约 12 个交易日轮一圈），有持仓的非轮值
    策略在轮值外既不被扫描、也没有任何盘中记录，看似"跟踪断了"。
    成本取舍：全市场扫 N 个策略不可接受（S16 类尤其贵），故**只扫它自己的持仓股**（1 只/策略）。

    落地口径（与轮值触发行严格区分）：watch=1 的**持仓跟踪**行 —— 不推送邮件、不算待执行
    买入信号（signal_rows.intraday_rows 默认过滤 watch），只在 /signals 页展示"下文"，
    并参与同一套 T+1/T+3/T+5 前向收益回填。
    返回留痕的策略数。
    """
    global HISTORY
    ensure_history()
    if not HISTORY:
        return 0
    _d0 = str(today or datetime.date.today().isoformat())
    n = 0
    for s in (HISTORY.get('strategies') or []):
        _sname = str(s.get('name') or '')
        if not _sname or _sname == str(rot_sname):
            continue          # 轮值策略已全市场扫描并走正常信号口径，不重复留痕
        if _sname in ('S7_沪深300PE分批低买高卖', 'S8_红利低估值分批买入'):
            continue          # 指数策略没有个股持仓股可扫（analyze_index 不看个股）
        try:
            pos = _open_position(s.get('daily') or [])
        except Exception:
            pos = None
        if not pos:
            continue          # 空仓策略不需要盘中跟踪
        code = str(pos.get('code') or '').strip() or _code_of_name(pos.get('name'))
        if not code or code not in advisor.STOCKS:
            continue
        try:
            rank = advisor.scan_stock_strategy(_sname, realtime=realtime, codes=[code])
        except Exception as e:
            log(f'持仓跟踪扫描失败 [{_sname} {code}]: {str(e)[:60]}')
            continue
        _nm = str(pos.get('name') or '')
        r = None
        for x in (rank or []):
            if str(x.get('code') or '') == code:
                r = x
                break
        if r is None:
            # ★ 多数策略的分析器"不满足条件即不返回结果"（rank 里根本没有这只票）。
            #   这正是"轮值外无踪迹"最典型的场景 —— 此时仍要留痕一行（无评分），
            #   否则持仓股在非轮值日依旧查不到任何盘中下文。现价取实时行情快照。
            _rt0 = (realtime or {}).get(code) if isinstance(realtime, dict) else None
            _px0 = _rt0.get('price') if isinstance(_rt0, dict) else None
            r = {'code': code, 'name': _nm, 'triggered': False, 'price': _px0}
        r = dict(r)
        r['trigger_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _bits = [f'建仓 {str(pos.get("buy_date") or "")[:10]} @ {pos.get("buy_price")}']
        _pp = pos.get('ret')                       # _open_position：ret=pos_pnl_pct（浮盈%）
        if isinstance(_pp, (int, float)):
            _bits.append(f'浮动 {_pp:+.2f}%')
        if r.get('price') is not None:
            _bits.append(f'现价 {r.get("price")}')
        if r.get('score') is not None:
            _bits.append(f'评分 {r.get("score")}')
        _bits.append('触发买入信号' if r.get('triggered') else '未再触发（继续持有观察）')
        _note = '持仓跟踪（轮值外策略盘中留痕，非买入信号）：' + ' ｜ '.join(_bits)
        if notify.intraday_watch_record(r, _sname, code=code,
                                        name=str(pos.get('name') or r.get('name') or ''),
                                        trade_date=_d0, note=_note):
            n += 1
    return n


def _space_watch_tick(rt, now):
    """★ 2026-09-24 S16 空间龙【盘中盯盘】驱动。

    每轮扫描调用 space_watch.tick()：更新盘中候选池与封板/炸板轨迹；
    到 14:57 集合竞价自动确认"仍封板且排得到"的最高名次 → 推送一次（当日去重）。

    口径依据（_daban_fill2.py 全史 2614 只 / 每笔 5000）：
      开盘就挂涨停价   n=1494 净  -9,947  炸板率 73.8%
      盘中触及就挂     n=1119 净 -63,952  炸板率 45.2%
      等收盘确认封板买 n= 779 净+154,650  炸板率 0%
    → 打板不是越早越好，盘中赌封板必亏，必须"等确认"。故本盯盘器只盯不出手，
      唯一出手点是 14:57 确认那一笔。
    """
    try:
        snap = space_watch.tick(rt or {}, now)
    except Exception as e:
        log(f'空间龙盯盘失败: {str(e)[:80]}')
        return
    c = snap.get('confirmed')
    if not c:
        return
    _key = 'sw_pushed_%s_%s' % (snap.get('date'), c.get('code'))
    if STATE.get(_key):
        return
    STATE[_key] = True
    try:
        _sk = c.get('skipped') or []
        _px = c.get('price') or 0
        rec = {
            'triggered': True, 'code': c.get('code'), 'name': c.get('name'),
            'price': _px, 'score': 95, 'buy_ref': _px,
            'stop': round(_px * 0.92, 2), 'target': round(_px * 1.20, 2),
            'tag': '空间龙打板',
            'note': ('14:57 集合竞价确认封板（%d板，非一字/秒板）→ 按涨停价排队买入%s'
                     % (c.get('lb') or 0,
                        '' if not _sk else '；跳过：' + '；'.join(_sk))),
            'trigger_time': '%s %s' % (c.get('date'), c.get('at')),
        }
        notify.send_intraday_signal(rec, space_watch.SNAME)
        log(f"空间龙盯盘确认买入 {c.get('name')}({c.get('code')}) "
            f"{c.get('lb')}板 @{_px}")
    except Exception as e:
        log(f'空间龙盯盘推送失败: {str(e)[:80]}')


def do_scan(force=False):
    global PAGE_CACHE
    # 非阻塞互斥：boot_scan(force) 与 scan_thread 定时扫描并发时只放行一个，
    # 避免重复拉行情/重复跑全量选股导致耗时翻倍、STATE 互相覆盖
    if not _SCAN_LOCK.acquire(blocking=False):
        log('扫描进行中，跳过本次')
        return
    try:
        log('[scan-step] do_scan enter force=%s' % force)
        now = datetime.datetime.now()
        _today_s = now.strftime('%Y-%m-%d')
        # ★ 2026-09-17 跨日滚动失效：把**昨日及更早**仍标"触发中"的盘中信号统一标消失。
        #   根因：mark_gone 只作用于"当日轮值策略"（下面每轮只调 intraday_signal_clear(sname)），
        #   轮值切换后旧策略的行再没人收尾 → 9/15 S10 和泰机电在 9/17 仍显示"触发中"（僵尸）。
        #   口径：盘中信号只在触发当日有效。当日只清一次（幂等，非交易时段也执行）。
        if STATE.get('sig_expired_date') != _today_s:
            try:
                _n = notify.intraday_expire_stale()
                STATE['sig_expired_date'] = _today_s
                if _n:
                    log(f'盘中信号跨日失效：{_n} 条历史"触发中"已标记消失')
            except Exception as e:
                log(f'盘中信号跨日清理失败: {str(e)[:60]}')
        trading = is_trading_time(now)
        if not force and not trading and not _in_close_window(now):
            return
        if not force and not trading and STATE.get('close_scan_date') == _today_s:
            return      # 当日收盘判定已完成，避免 15:00~16:30 期间每 5 分钟重复全量扫描
        STATE['last_scan'] = now.strftime('%Y-%m-%d %H:%M:%S')
        STATE['scans_today'] += 1
        sname = advisor.today_strategy(now.date())
        STATE['strategy'] = sname
        STATE['date'] = now.strftime('%Y-%m-%d')
        log('[scan-step] fetch_realtime start')
        rt = fetch_realtime()
        log('[scan-step] fetch_realtime done %d 只' % len(rt or {}))
        STATE['realtime_ok'] = bool(rt)
        RT_SNAPSHOT.clear()
        RT_SNAPSHOT.update(rt or {})   # ★ 供行动清单判定"标的今日停牌"（2026-09-10）
        # 启动太晚时先补齐今日数据：已收盘用实时行情修正/补全个股当日 bar，盘中只做内存叠加
        STATE['data_asof'] = advisor.backfill_today(rt)
        log('[scan-step] backfill_today done asof=%s' % STATE['data_asof'])
        # 指数估值：收盘后或启动时尝试拉取最新数据（乐咕有发布时滞，拉不到则标注截至日）
        if now.hour >= 15 or force:
            STATE['idx_asof'] = advisor.backfill_index(force=force)
            log('[scan-step] backfill_index done %s' % STATE['idx_asof'])
        # 指数持久化补齐：历史指数已固定、只补新增交易日（错过日期时在此自动重跑补齐）。
        # 收盘后（个股当日 bar 已写回）增量补选股池指数；上证指数按缺失日期从接口补齐。
        if (now.hour >= 15 and now.weekday() < 5) or force:
            try:
                import history_engine
                global POOL_INDEX
                history_engine.refresh_sh_index()      # 上证指数补齐缺失日期（幂等）
                log('[scan-step] refresh_sh_index done')
                # build_pool_index 已停用（选股池指数 2026-08-27 移除）
                if os.path.exists(POOL_INDEX_FILE):
                    POOL_INDEX = json.load(open(POOL_INDEX_FILE, encoding='utf-8'))
            except Exception as e:
                log(f'指数补齐失败: {str(e)[:60]}')
        log('[scan-step] scan_stock_strategy start')
        rank = advisor.scan_stock_strategy(sname, realtime=rt or None)
        log('[scan-step] scan_stock_strategy done %d 条' % len(rank or []))
        STATE['rank'] = rank
        rec = None
        for r in rank:
            if r.get('triggered'):
                rec = r
                break
        if rec is None and rank:
            rec = rank[0]
        if rec:
            trading = is_trading_time(now)
            if rec.get('triggered'):
                # 盘中按实时价预估触发，收盘价确认后（收盘后扫描）自动更新为收盘判定
                rec['status'] = ('盘中触发买入信号（收盘价确认，收盘后扫描自动更新）'
                                 if trading else '今日收盘触发买入信号，下一交易日(T+1)按本策略时点执行')
            else:
                rec['status'] = '接近买入（未触发）——按规则不买入，继续观察'
            rec['market'] = 'trading' if trading else 'closed'
            # ★ 扫描触发时刻（2026-09-09）：邮件/页面需展示"具体几点几分扫描出来"，不再只有日期
            rec['trigger_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            rec['data_asof'] = STATE['data_asof']
            STATE['recommend'] = rec
            log(f"推荐 [{sname}] {rec.get('name')} 现价{rec.get('price')} 评分{rec.get('score')}")
            if trading:
                if rec.get('triggered'):
                    notify.send_intraday_signal(rec, sname)   # 盘中触发信号实时推送（当日同标去重，持久化跨重启）
                else:
                    # 信号消失/未触发：**改标志位** gone=1（行保留在 signals.db，历史可查），
                    # 是否重推由 pushed 标志位决定（当日同策略同标的只推一次）
                    notify.intraday_signal_clear(sname)
        else:
            STATE['recommend'] = None
            log(f'[{sname}] 今日无候选')
            if is_trading_time(now):
                notify.intraday_signal_clear(sname)
        # ★ 2026-09-18 盘中持仓跟踪：轮值外、但**有持仓**的策略各扫自己的持仓股（1 只/策略），
        #   留痕为 watch 行（不推送、不算待执行买入信号），解决"推荐的下文跟踪断了"。
        #   只在交易时段做：收盘后账本会正式落当日行，无需重复。
        if is_trading_time(now):
            try:
                _nw = _scan_holdings_intraday(sname, rt or None, today=_today_s)
                if _nw:
                    log(f'盘中持仓跟踪：{_nw} 个非轮值持仓策略已留痕（watch）')
            except Exception as e:
                log(f'盘中持仓跟踪失败: {str(e)[:60]}')
        # ★ 2026-09-24 S16 空间龙盘中盯盘（交易时段每轮；14:57 确认买入并推送一次）
        _space_watch_tick(rt or {}, now)
        # 收盘推送（每个交易日收盘后一次）：收盘简报 + 行情异常告警（notify 内部按日去重）
        if now.hour >= 15 and now.weekday() < 5:
            try:
                import history_engine
                _m = history_engine.analyze_market()
                # ★ 收盘后刷新统一今日状态（页面提醒卡 / 微信推送同一数据源）
                update_today_status(rec=STATE['recommend'], sname=sname, market=_m)
                notify.send_close_brief(market=_m, rec=STATE['recommend'],
                                        sname=sname, hist=HISTORY)
                notify.send_market_alert(_m)
                # ★ 2026-09-17 收盘对账：把当日盘中信号统一到**收盘判定**口径。
                #   9/16 S15 盘中最后一刻换成高新发展，但收盘判定（daily 9/16 行）是方大集团、
                #   9/17 实际买的也是方大集团 → 库里却显示"高新发展触发中/方大已消失（换标的）"，
                #   与首页/账本完全相反。原因是收盘判定此前从不回写信号库（只有盘中扫描在写）。
                _rc = notify.intraday_reconcile_close(sname, STATE['recommend'])
                if _rc:
                    log(f'收盘信号对账：[{sname}] {_rc} 条按收盘判定更新状态')
                # ★ 收盘后每日一次后台重建当日记录（完成后重载数据 + 页面自动刷新）
                maybe_daily_build()
                STATE['close_scan_date'] = _today_s
            except Exception as e:
                log(f'收盘推送失败: {str(e)[:60]}')
        else:
            # ★ 盘中同样刷新统一今日状态，保证页面提示与微信推送口径一致
            update_today_status(rec=STATE['recommend'], sname=sname)
            # ★盘中即时记录：早盘/午后把当日已开盘成交滚入记录——
            #   早盘买入/卖出/换仓当天就出现在记录里，不用等收盘再补；
            #   收盘后正式 build 会以当日收盘价复核并覆盖当日行。
            try:
                maybe_intraday_build(realtime=rt or None, now=now)
            except Exception as e:
                log(f'盘中即时记录调度失败: {str(e)[:60]}')
        PAGE_CACHE.clear()
    finally:
        _SCAN_LOCK.release()


def scan_thread():
    while True:
        try:
            do_scan()
        except Exception as e:
            log(f'扫描异常: {str(e)[:60]}')
            notify.send_error_alert(f'扫描异常: {str(e)[:200]}')
        time.sleep(SCAN_SECONDS)


def ensure_history():
    """若历史数据文件不存在，后台构建（首次约30-60秒）"""
    global HISTORY, HISTORY_READY, PAGE_CACHE, POOL_INDEX
    if HISTORY is not None:
        return
    if os.path.exists(HISTORY_FILE):
        HISTORY = json.load(open(HISTORY_FILE, encoding='utf-8'))
        HISTORY_READY = True
        log(f'历史数据已加载（{HISTORY["generated"]}，{len(HISTORY["strategies"])}个策略）')
        if os.path.exists(POOL_INDEX_FILE):
            POOL_INDEX = json.load(open(POOL_INDEX_FILE, encoding='utf-8'))
        return
    HISTORY_READY = False
    log('历史数据未生成，后台开始计算（约30-60秒）...')

    def _build():
        global HISTORY, HISTORY_READY, PAGE_CACHE, POOL_INDEX
        try:
            import history_engine
            history_engine.build()
            HISTORY = json.load(open(HISTORY_FILE, encoding='utf-8'))
            HISTORY_READY = True
            if os.path.exists(POOL_INDEX_FILE):
                POOL_INDEX = json.load(open(POOL_INDEX_FILE, encoding='utf-8'))
            PAGE_CACHE.clear()
            log('历史数据计算完成')
        except Exception as e:
            log(f'历史数据计算失败: {str(e)[:60]}')
            notify.send_error_alert(f'历史数据计算失败: {str(e)[:200]}')
    threading.Thread(target=_build, daemon=True).start()


# ============ 统一「今日状态」（页面提醒卡与微信推送同一数据源） ============

# _md / _next_td / _num2 / _esc_note / _nice_ticks / svg_line_chart 已移至 ui_common（见文件头 import）


def _is_suspended_today(code, today):
    """★ 标的今日是否停牌（2026-09-10）：优先用最近一次扫描的实时快照（成交量与今开均为 0），
    无当日快照时退回本地 CSV 的今日行（成交量=0）；两处都无今日数据则返回 False（无法判定，按正常处理）。
    用途：行动清单里"今日需卖出/买入"的标的若停牌，实际挂单会顺延到复牌日，不能照常显示。"""
    code = str(code or '')
    if not code:
        return False
    info = (RT_SNAPSHOT or {}).get(code)
    if isinstance(info, dict) and str(info.get('date') or '') == today:
        return (info.get('volume') or 0) <= 0 and (info.get('open') or 0) <= 0
    try:
        with open(os.path.join('data', 'csv', f'{code}.csv'), encoding='utf-8') as f:
            lines = [x for x in f.read().strip().splitlines() if x.strip()]
        if len(lines) >= 2:
            p = lines[-1].split(',')
            if p and p[0].strip() == today and len(p) > 6:
                return (float(p[6]) if p[6] else 0.0) <= 0
    except Exception:
        pass
    return False


def daban_realtime_note(code, today=None):
    """★ 2026-09-28：龙头接力类推荐的【实盘证伪】提示。

    首页推荐是 T+1 静态信号（昨日收盘生成，例 9/24 收盘确认 → 9/28 买入），
    到执行日实盘可能已把它证伪：
      ·炸板：曾封板但当前未封（案例 601811 新华文轩 9/28 触及涨停 22.39 后砸到
        19.81、翻绿 -2.65%）→ 接力已失败，照推荐追进去就是接刀（浮亏 -10%）；
      ·买不进：一字/秒板封死且换手极低 → 挂单是假的，排不到；
      ·未封板：今日压根没涨停 → 接力未成立。
    判定口径与 space_watch 严格一致（SEAL_PCT / _buyable），避免两处结论打架。
    ★仅在持有"今日"行情时才下结论（休市/行情陈旧返回 None，防误判——9/25 教训）。
    返回 (level, text)，level ∈ 'bad' | 'warn' | 'ok'；None = 无法判定。
    """
    code = str(code or '')
    if not code:
        return None
    today = today or datetime.date.today().isoformat()
    try:
        import space_watch
    except Exception:
        return None
    rt = (RT_SNAPSHOT or {}).get(code)
    if not isinstance(rt, dict) or str(rt.get('date') or '') != today:
        return None                      # 无今日实时行情（休市/未刷新）→ 不妄下结论
    try:
        pct = float(rt.get('pct'))
    except Exception:
        return None
    px = rt.get('price') or 0
    try:
        to = float(rt.get('turnover') or 0)
    except Exception:
        to = 0.0
    seen = (space_watch.STATE.get('seen') or {}).get(code) or {}
    breaks = int(seen.get('breaks') or 0)
    to_txt = '%.2f%%' % to
    if pct >= space_watch.SEAL_PCT:
        try:
            buyable = space_watch._buyable(rt)
        except Exception:
            buyable = True
        if buyable:
            return ('ok', '实盘：当前封板 %.2f 元 %+.2f%%，换手 %s → 可排队'
                         % (px, pct, to_txt))
        return ('warn', '实盘：一字/秒板封死 %.2f 元 %+.2f%%，换手仅 %s → 排不进，'
                        '照推荐挂单大概率不成交' % (px, pct, to_txt))
    # 曾触板判定：seen 里有炸板计数，或今日最高价到过涨停（触及涨停后回落同样是炸板）。
    # 不能只看"此刻是否封板"——601811 盘中触及 22.39 涨停后砸下来，此刻未封板但当日确实炸过。
    try:
        zt = space_watch._zt_price(rt)
        hi = float(rt.get('high') or 0)
    except Exception:
        zt, hi = None, 0
    touched = bool(zt and hi and hi >= zt * 0.995)
    if breaks or touched:
        _why = (' %d 次' % breaks) if breaks else ('（最高 %.2f 触及涨停后回落）' % hi)
        return ('bad', '实盘：今日炸板%s，现价 %.2f 元 %+.2f%% → 接力已失败，'
                       '本条推荐已被实盘证伪，切勿追入' % (_why, px, pct))
    return ('bad', '实盘：今日未封板，现价 %.2f 元 %+.2f%% → 龙头接力未成立，'
                   '本条推荐已被实盘证伪' % (px, pct))


SPACE_REFRESH = {'running': False, 'at': None, 'msg': ''}


def _space_refresh_run():
    """★ 盯盘页「单独刷新」（2026-09-28 用户要求）：只跑一轮 space_watch.tick。

    与 /api/refresh（= do_scan(force)）的区别：do_scan 会拉指数估值、可能触发当日
    历史重建，跑一次数分钟；这里只拉一次全市场实时行情 + 一轮 tick，几十秒就能看到
    最新候选池/炸板轨迹。**只更新盯盘数据，不改模拟账本**；若已到 14:57 会照常触发
    收盘确认（confirm 幂等，重复调用不会重复下单）。
    """
    if SPACE_REFRESH['running']:
        return
    SPACE_REFRESH['running'] = True
    SPACE_REFRESH['msg'] = '拉取实时行情中…'
    try:
        rt = fetch_realtime() or {}
        if rt:
            RT_SNAPSHOT.clear()
            RT_SNAPSHOT.update(rt)      # 同步给首页"实盘证伪"用，避免两页行情不一致
            STATE['realtime_ok'] = True
        _space_watch_tick(rt, datetime.datetime.now())
        SPACE_REFRESH['msg'] = '完成（行情 %d 只，已盯第 %s 轮）' % (
            len(rt), space_watch.STATE.get('ticks'))
    except Exception as e:
        SPACE_REFRESH['msg'] = '失败：%s' % str(e)[:80]
    finally:
        SPACE_REFRESH['at'] = datetime.datetime.now().strftime('%H:%M:%S')
        SPACE_REFRESH['running'] = False


def analyze_market_cached():
    """analyze_market 按自然日缓存（纯本地秒级计算，避免每次扫描/每个卡片重复算）"""
    today = datetime.date.today().isoformat()
    if _MKT_CACHE['date'] == today and _MKT_CACHE['data'] is not None:
        return _MKT_CACHE['data']
    try:
        import history_engine
        _MKT_CACHE['data'] = history_engine.analyze_market()
    except Exception:
        _MKT_CACHE['data'] = _MKT_CACHE.get('data') or {}
    _MKT_CACHE['date'] = today
    return _MKT_CACHE['data']


def _market_alerts(m):
    """从行情判断结果提取风险告警文案（判定口径与 notify.send_market_alert 一致）"""
    out = []
    sh = (m or {}).get('sh') or {}
    n = (m or {}).get('next') or {}
    chg = sh.get('chg')
    if isinstance(chg, (int, float)):
        if chg <= -2.0:
            out.append('上证大跌 %.2f%%' % chg)
        if chg >= 3.0:
            out.append('上证大涨 %.2f%%' % chg)
    if (m or {}).get('label') in ('放量长阴', '超跌'):
        out.append('形态：%s' % m.get('label'))
    if n.get('verdict') in ('调整/回落', '偏弱/调整') and (n.get('prob') or 0) >= 60:
        out.append('%s 预判 %s（%.0f%%）' % (_md(m.get('target')), n['verdict'],
                                              n.get('prob') or 0))
    return out


def update_today_status(rec=None, sname='', market=None):
    """★ 更新统一「今日状态」TODAY_STATUS（2026-09-03）：
    页面「今日盘中提醒」卡 / 策略详情页提示条 / 微信推送（盘中信号、收盘简报）
    全部读这一份数据，避免各处各算一套导致展示与推送不一致。
    结构：date / rotation(今日轮换策略) / env(行情环境) / market(大盘判断)
          / alerts(风险告警) / signal(触发状态) / asof(数据时点) / updated"""
    global TODAY_STATUS
    try:
        import history_engine
        m = market if market is not None else analyze_market_cached()
        m = m or {}
        sh = m.get('sh') or {}
        n = m.get('next') or {}
        env = m.get('env')
        if not env and m.get('asof'):
            env = history_engine.market_env_on(m['asof'])
        sig = None
        if rec:
            sig = {k: rec.get(k) for k in (
                'triggered', 'name', 'code', 'price', 'score', 'buy_ref',
                'stop', 'target', 'status', 'note', 'tag', 'market',
                'trigger_time')}
        TODAY_STATUS = {
            'date': datetime.date.today().isoformat(),
            'rotation': sname,
            'env': env or '',
            'market': {'asof': m.get('asof'), 'target': m.get('target'),
                       'label': m.get('label'),
                       'close': sh.get('close'), 'chg': sh.get('chg'),
                       'verdict': n.get('verdict'), 'prob': n.get('prob')},
            'alerts': _market_alerts(m),
            'signal': sig,
            'asof': STATE.get('data_asof') or {},
            'updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        log('今日状态更新失败: %s' % str(e)[:60])


# ============ 收盘后每日一次后台重建当日记录 ============

def _last_build_date():
    try:
        with open(LAST_BUILD_FILE, encoding='utf-8') as f:
            return json.load(f).get('date')
    except Exception:
        return None


def _mark_built(date):
    try:
        with open(LAST_BUILD_FILE, 'w', encoding='utf-8') as f:
            json.dump({'date': date,
                       'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f)
    except Exception:
        pass


def maybe_daily_build(force=False, now=None):
    """★ 收盘后每日一次后台重建当日记录（2026-09-03）：
    每个交易日 15:00 后（当日 bar 已回补）自动跑一次 history_engine.build()，
    把当日信号/成交滚入策略记录；完成后重载 HISTORY 并清页面缓存，
    页面通过 build_ver 检测变化后自动 reload。
    同一天只跑一次（标记写 data/last_build_date.json，跨启动去重）。
    force=True 仅用于人工/联调触发，跳过时段与去重判定。"""
    global BUILD_STATE
    now = now or datetime.datetime.now()
    if not force and (not _is_trading_day(now) or now.hour < 15):
        return
    today = now.date().isoformat()
    if not force and (BUILD_STATE['running'] or _last_build_date() == today):
        return

    def _do():
        global HISTORY, PAGE_CACHE
        BUILD_STATE['running'] = True
        try:
            log('收盘后自动重建当日记录：后台开始计算（约30-60秒）...')
            import history_engine
            history_engine.build()
            HISTORY = json.load(open(HISTORY_FILE, encoding='utf-8'))
            _mark_built(today)
            BUILD_STATE['last_ok'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            PAGE_CACHE.clear()
            log('收盘后自动重建完成（当日记录已滚入，页面将自动刷新）')
        except Exception as e:
            log('收盘后自动重建失败: %s' % str(e)[:60])
            notify.send_error_alert('收盘后自动重建失败: %s' % str(e)[:200])
        finally:
            BUILD_STATE['running'] = False

    threading.Thread(target=_do, daemon=True).start()


def _last_intra_slot():
    """上次盘中即时记录槽位：{'date': 'YYYY-MM-DD', 'slot': 'am'/'pm'}（跨启动去重）"""
    try:
        with open(LAST_INTRA_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _mark_intra_slot(date, slot):
    try:
        with open(LAST_INTRA_FILE, 'w', encoding='utf-8') as f:
            json.dump({'date': date, 'slot': slot,
                       'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, f)
    except Exception:
        pass


def maybe_intraday_build(realtime=None, now=None):
    """★盘中即时记录（2026-09-03 新增）：
    目标：早盘(开盘)已买入/卖出的动作，当天盘中就应出现在记录里，不必等收盘再补。
    机制：交易日开盘后首轮扫描(≥9:30)与午后(≥13:00)各一次，用实时行情把当日 bar 叠加为虚拟
    收盘帧，调用 history_engine.build(realtime=..., intraday=True) 后台重建——
    盘中模式只落「当日开盘成交(建仓/卖出成交/换仓买入)+持仓预览」，不做当日收盘信号判定；
    15:00 后由 maybe_daily_build 以当日收盘数据正式复核并覆盖当日行（盘中行会被顶掉）。
    am/pm 每槽每日至多一次（文件去重，跨启动生效）；与收盘 build 共用 running 锁。"""
    global BUILD_STATE
    now = now or datetime.datetime.now()
    if not _is_trading_day(now):
        return
    hm = now.hour * 60 + now.minute
    if hm >= 15 * 60:
        return
    slot = 'am' if hm < 11 * 60 + 30 else 'pm'
    if slot == 'am' and hm < 9 * 60 + 30:      # 早盘槽：9:30 开盘后首个扫描 tick 即记（开盘价 9:25 集合竞价已定格）
        return
    if slot == 'pm' and hm < 13 * 60:          # 午后槽：午后再推进一次（晚启动也能补上早盘成交）
        return
    if not realtime:
        return
    today = now.date().isoformat()
    last = _last_intra_slot()
    if last and last.get('date') == today and last.get('slot') == slot:
        return
    if BUILD_STATE['running']:
        return

    def _do():
        global HISTORY, PAGE_CACHE
        BUILD_STATE['running'] = True
        try:
            log(f'盘中即时记录（{slot}槽）：把当日已开盘成交滚入记录，后台计算约30-60秒...')
            import history_engine
            history_engine.build(realtime=realtime, intraday=True)
            HISTORY = json.load(open(HISTORY_FILE, encoding='utf-8'))
            _mark_intra_slot(today, slot)
            BUILD_STATE['last_ok'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            PAGE_CACHE.clear()
            log('盘中即时记录完成：当日早盘买/卖已进记录（收盘后按收盘价复核并覆盖当日行）')
        except Exception as e:
            log(f'盘中即时记录失败: {str(e)[:60]}')
            try:
                notify.send_error_alert('盘中即时记录失败: %s' % str(e)[:200])
            except Exception:
                pass
        finally:
            BUILD_STATE['running'] = False

    threading.Thread(target=_do, daemon=True).start()


# ---------------- 渲染 ----------------

STATUS_CLASS = {
    '买入信号': 'ok', '建仓': 'ok', '换仓买入': 'ok', '持有': 'hold',
    '卖出信号': 'sell', '卖出成交': 'sell', '卖出顺延': 'warn',  # 持仓停牌致卖出挂单顺延（信号不废弃，2026-09-09）
    '资金不足': 'warn', '信号废弃': 'warn',
    '观望': 'gray', '空仓': 'gray', '买入信号(待成交)': 'gray',
}
BUY_STATES = ('买入信号', '建仓', '买入信号(待成交)', '换仓买入')
SELL_STATES = ('卖出信号', '卖出成交')
KEY_STATES = BUY_STATES + SELL_STATES
# _esc_note（股票简称加粗）、CSS / JS 页面常量均已移至 ui_common（见文件头 import）


YEAR = str(datetime.datetime.now().year)  # 页面仅展示今年的数据
MONTH = datetime.datetime.now().strftime('%Y-%m')  # 本月，用于本月收益
CAPITAL = 5000.0  # 每策略本金
SIM_START = '2026-07-01'  # 模拟交易起点：此日期之前的交易仅为历史回测，不计入收益栏位
SIM_LABEL = '7月1日'      # 模拟起点的展示文案
TRADING_DAYS = 244        # A股年化交易日数（夏普/年化波动率/年化收益折算用）
RF_RATE = 0.02            # 无风险利率：与引擎空仓理财口径一致（年化2%货基/逆回购）


def recalc_summary(trades):
    """按过滤后的交易重算策略汇总（与 _trade_stats 同一口径）"""
    st = _trade_stats(trades)
    return {'trades': st['trades'], 'total_net_pnl': st['pnl'],
            'avg_pnl_per_trade': st['avg'], 'win_rate': st['win_rate'],
            'avg_hold_days': st['hold']}


def _t1_html(r):
    t1 = r.get('t1_pct')
    if t1 is None:
        return '-'
    # ★ 2026-09-10：A股无单日 -100%，出现即"次日停牌/无行情"导致（如 S16 9/8 龙版传媒 →
    #   9/9 停牌），原样显示成 -100.00% 会被误读为暴跌，改标"停牌"。
    if t1 <= -99.9:
        return '<span class="muted" title="次日停牌或无行情，非真实涨跌">停牌</span>'
    cls = 'up' if t1 > 0 else 'down'
    return f'<span class="{cls}">{t1:+.2f}%</span>'


def _pos_pnl_html(r):
    """持仓浮盈：有持仓显示浮盈%与浮盈额；空仓显示 -"""
    pnl_pct = r.get('pos_pnl_pct')
    if pnl_pct is None:
        return '-'
    cls = 'up' if pnl_pct >= 0 else 'down'
    s = f'{pnl_pct:+.2f}%'
    if r.get('pos_pnl') is not None:
        s += f'<br><span class="muted">{r["pos_pnl"]:+.0f}元</span>'
    return f'<span class="{cls}">{s}</span>'


# _nice_ticks / svg_line_chart 已移至 ui_common

def pool_index_card(rng='ytd'):
    """首页「选股池指数」卡片：按市值加权指数算法（流通股本近似）合成的 15 只股票池指数
    rng: 区间范围 ytd(今年,默认)/1y/3y/all，决定曲线图与「区间最高/最低/涨幅」的展示范围"""
    global POOL_INDEX
    if POOL_INDEX is None and os.path.exists(POOL_INDEX_FILE):
        try:
            POOL_INDEX = json.load(open(POOL_INDEX_FILE, encoding='utf-8'))
        except Exception:
            POOL_INDEX = None
    d = POOL_INDEX
    if not d or not d.get('series') or len(d['series']) < 2:
        return ''
    s = d['series']
    last = s[-1]['close']
    prev = s[-2]['close'] if len(s) >= 2 else last
    day_chg = (last / prev - 1) * 100 if prev else 0.0
    dcls = 'up' if day_chg >= 0 else 'down'
    darr = '▲' if day_chg >= 0 else '▼'
    # ---- 区间过滤：曲线图 + 区间最高/最低 + 区间涨幅跟随所选范围（默认今年）----
    last_date = s[-1]['date']
    if rng == 'ytd':
        start_date = f'{YEAR}-01-01'
    elif rng == '1y':
        start_date = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=365)).isoformat()
    elif rng == '3y':
        start_date = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=365 * 3)).isoformat()
    else:  # all
        start_date = s[0]['date']
    sub = [x for x in s if x['date'] >= start_date] or s[-1:]
    rng_first = sub[0]['close']
    rng_chg = (last / rng_first - 1) * 100
    hi = max(sub, key=lambda x: x['close'])
    lo = min(sub, key=lambda x: x['close'])
    # ---- 全局指标（不随范围变化）----
    first26 = next((x['close'] for x in s if x['date'].startswith(str(YEAR))), None)
    ytd = (last / first26 - 1) * 100 if first26 else None
    base_chg = (last / d['base'] - 1) * 100
    ytd_html = f'<b class="{"up" if ytd >= 0 else "down"}">{ytd:+.2f}%</b>' if ytd is not None else '-'
    bcls = 'up' if base_chg >= 0 else 'down'
    rcls = 'up' if rng_chg >= 0 else 'down'
    # 权重 Top3（HISTORY 提供名称映射）
    names = {p['code']: p['name'] for p in (HISTORY['pool'] if HISTORY else [])}
    w = sorted(d.get('weights', {}).items(), key=lambda kv: kv[1], reverse=True)[:3]
    topw = '、'.join(f'{names.get(c, c)}({v / 1e8:.0f}亿股)' for c, v in w)
    totw = sum(d.get('weights', {}).values())
    top3pct = sum(v for _, v in w) / totw * 100 if totw else 0
    curve = svg_line_chart([(x['date'], x['close']) for x in sub], base=rng_first,
                           base_label=f'{sub[0]["date"]}=100', tip_label='选股池指数', h=180, ma=[20, 60])
    rngs = [('ytd', '今年'), ('1y', '近1年'), ('3y', '近3年'), ('all', '全部')]
    rng_html = ' '.join(f'<a class="rng-link{" on" if k == rng else ""}" href="/?idx={k}">{v}</a>' for k, v in rngs)
    return (f'<div class="card"><h3>📊 {html.escape(d.get("name", "选股池指数"))}'
            f'<span class="muted">共 {_dash(d.get("n_stocks"), 0)} 只 · 基日 {_dash(d.get("base_date"))} = {_dash(d.get("base"), 100)} 点</span></h3>'
            f'<div class="rng-switch">区间：{rng_html}</div>'
            '<div class="idx-sum">'
            f'<span>最新点位 <b>{last:,.2f}</b> <b class="{dcls}">{darr} {day_chg:+.2f}%</b>'
            f'<span class="muted">较前一交易日</span></span>'
            f'<span>今年 {ytd_html}</span>'
            f'<span>基日以来 <b class="{bcls}">{base_chg:+.2f}%</b></span>'
            f'<span>区间最高 <b class="up">{hi["close"]:,.2f}</b>（{hi["date"]}）</span>'
            f'<span>区间最低 <b class="down">{lo["close"]:,.2f}</b>（{lo["date"]}）</span>'
            f'<span>区间涨幅 <b class="{rcls}">{rng_chg:+.2f}%</b>'
            f'<span class="muted">自{sub[0]["date"]}</span></span>'
            '</div>'
            f'{curve}'
            f'<p class="note">口径：市值加权指数（权重为换手率估算的流通股本，非上证综指"发行总股本"口径），'
            f'指数=Σ收盘价×权重股本 / 基期总市值 ×100；成分固定 15 只、无调仓，故不需要除数修正法；'
            f'停牌沿用前收盘价。当前权重最大：{html.escape(topw)}（Top3 占比约 {top3pct:.0f}%）。'
            '本指数仅用于观察选股池整体走势，公式与上证综指同为市值加权综合指数，但权重口径/成分不同，不可直接对比点位。</p>'
            '</div>')

def sh_aid_card():
    """首页「上证指数辅助选股」卡片：上证综指点位/MA20 趋势（大盘风向标）+
    今日轮换策略买入过滤器的实时判定状态（哪些策略以上证当日上涨为辅助过滤器）。"""
    try:
        import pandas as pd
        import history_engine
        df = pd.read_csv(SH_INDEX_CSV, encoding='utf-8')
        df['date'] = df['date'].astype(str)
        df = df.sort_values('date').reset_index(drop=True)
        if len(df) < 21:
            return ''
        last = float(df.iloc[-1]['close'])
        prev = float(df.iloc[-2]['close'])
        day_chg = (last / prev - 1) * 100
        ma20 = float(df['close'].rolling(20).mean().iloc[-1])
        above = last >= ma20
        fy = df[df['date'] >= f'{YEAR}-01-01']['close']
        ytd = (last / float(fy.iloc[0]) - 1) * 100 if len(fy) else None
        last_date = str(df.iloc[-1]['date'])
    except Exception:
        return ''
    dcls = 'up' if day_chg >= 0 else 'down'
    darr = '▲' if day_chg >= 0 else '▼'
    tcls = 'up' if above else 'down'
    ttxt = '站上MA20（大盘偏强）' if above else '跌破MA20（大盘偏弱）'
    ytd_html = f'<b class="{"up" if ytd >= 0 else "down"}">{ytd:+.2f}%</b>' if ytd is not None else '-'
    sname = STATE['strategy'] or ''
    try:
        flt = history_engine.BUY_FILTERS.get(sname)
        if flt:
            kind, label = flt
            state, _ = history_engine._filter_pass(sname, last_date)
            flt_html = (f'今日轮换策略「{html.escape(sname)}」使用过滤器：<b>{html.escape(label)}</b>'
                        f' → 最近收盘判定 <b class="st {"ok" if state else "warn"}">'
                        f'{"放行（当日上涨）" if state else "拦截（当日未涨）"}</b>')
        else:
            flt_html = f'今日轮换策略「{html.escape(sname) or "-"}」未配置过滤器（恒放行）'
    except Exception:
        flt_html = ''
    return (f'<div class="card"><h3>📈 上证指数辅助选股'
            f'<span class="muted">大盘风向标 · 截至最近收盘 {last_date}</span></h3>'
            '<div class="idx-sum">'
            f'<span>最新点位 <b>{last:,.2f}</b> <b class="{dcls}">{darr} {day_chg:+.2f}%</b>'
            f'<span class="muted">较前一交易日</span></span>'
            f'<span>今年 {ytd_html}</span>'
            f'<span>MA20 状态 <b class="{tcls}">{ttxt}</b></span>'
            '</div>'
            f'<p class="note">辅助规则：<b>上证当日上涨</b>视为大盘环境偏多，作为部分策略的买入过滤器'
            '（S6 布林做T、S7 沪深300PE 采用上证当日上涨过滤，均经回测落地验证提升胜率）；'
            f'上证跌破 MA20 提示大盘走弱、入场谨慎。{flt_html}</p>'
            '</div>')


def market_card():
    """首页「当前行情判断 · 明日行情预判」卡片：基于上证指数量价形态 + 近10年相似历史
    统计次日「强修复/弱修复/回落」概率，结合超买超卖、MA20 位置、动态池相对强弱修正。"""
    try:
        import history_engine
        m = history_engine.analyze_market()
    except Exception:
        return ''
    if not m.get('asof'):
        return ''
    sh = m['sh']
    dcls = 'up' if sh['chg'] >= 0 else 'down'
    darr = '▲' if sh['chg'] >= 0 else '▼'
    label_style = {'缩量回调': 'warn', '放量长阴': 'down', '超跌': 'warn',
                   '缩量横盘': 'flat', '普通波动': 'flat',
                   '缩量反弹': 'ok', '放量上涨': 'up', '放量长阳': 'up'}.get(m['label'], 'flat')
    vr_txt = ('缩量' if sh['vr5'] < 0.8 else '放量' if sh['vr5'] >= 1.5
              else '量能正常') + f'（量/5日均量 {sh["vr5"]:.2f}）'
    rsi_txt = '超卖' if sh['rsi14'] is not None and sh['rsi14'] < 25 else (
        '超买' if sh['rsi14'] is not None and sh['rsi14'] > 75 else
        f'中性（RSI {sh["rsi14"]}）' if sh['rsi14'] is not None else '—')
    # ★ 行情环境 → 适配策略（2026-08-27：行情驱动的策略选择）
    env = m.get('env') or history_engine.market_env_on(m['asof'])
    env_style = {'trend_up': 'up', 'range': 'flat', 'weak': 'down'}.get(env, 'flat')
    env_name = history_engine.ENV_NAMES.get(env, env)
    fitted = history_engine.strategies_for_env(env)
    fitted_short = '、'.join(x.split('_', 1)[1] for x in fitted)
    env_html = (f'<span>行情环境 <b class="st {env_style}">{html.escape(env_name)}</b>'
                f'<span class="muted">不适配的策略当天空仓（不建仓）</span></span>')
    # 动态池已停用（全市场选股池替代）后 dyn_chg 可能为 None，缺失时不渲染该区块
    dyn_span = ''
    if m['dyn_chg'] is not None:
        dcls2 = 'up' if m['dyn_chg'] >= 0 else 'down'
        dyn_span = f'<span>动态池今日 <b class="{dcls2}">{m["dyn_chg"]:+.2f}%</b></span>'
    rel_html = ''
    if m['rel'] is not None:
        rcls = 'up' if m['rel'] >= 0 else 'down'
        rel_html = (f'<span>动态池相对强弱 <b class="{rcls}">{m["rel"]:+.2f}pp</b>'
                    '<span class="muted">动态池今日-上证今日，>0 结构性偏强</span></span>')
    n = m['next']
    bars = (f'<div class="prob-bar"><i style="width:{n["strong"]:.0f}%" class="pb-up"></i>'
            f'<i style="width:{n["weak"]:.0f}%" class="pb-flat"></i>'
            f'<i style="width:{n["down"]:.0f}%" class="pb-down"></i></div>'
            f'<div class="prob-legend">'
            f'<span class="lg-up">强修复 {n["strong"]:.0f}%</span>'
            f'<span class="lg-flat">弱修复 {n["weak"]:.0f}%</span>'
            f'<span class="lg-down">回落 {n["down"]:.0f}%</span></div>')
    if n['n_sim']:
        similar = '、'.join(f'{d}:{nxt:+.1f}%' for d, _, nxt in n['recent'][:3])
        sim_html = (f'<span class="muted">近10年相似形态 {n["n_sim"]} 例，平均次日 {n["avg_next"]:+.2f}%；'
                    f'最近 3 例次日：{similar}</span>')
    else:
        sim_html = ''
    verdict_cls = 'up' if n['verdict'] in ('强修复', '偏强修复') else (
        'down' if n['verdict'] in ('调整/回落', '偏弱/调整') else 'flat')
    verdict_html = (f'<b class="{verdict_cls}">{html.escape(n["verdict"])}</b>'
                    + (f'（概率 {n["prob"]:.0f}%）' if n['prob'] is not None else ''))
    streak_txt = (f'<span>连{("涨" if sh["streak"] > 0 else "跌")}{abs(sh["streak"])}日</span>'
                  if sh['streak'] else '')
    tgt_txt = f'{_md(m.get("target"))} 预判'
    return (f'<div class="card"><h3>🧭 当前行情判断 · {tgt_txt}'
            f'<span class="muted">基于最近收盘 {m["asof"]} 的量价与近10年相似历史'
            f'（统计最近收盘后首个交易日的涨跌）</span></h3>'
            '<div class="idx-sum">'
            f'<span>上证 <b>{sh["close"]:,.2f}</b> <b class="{dcls}">{darr} {sh["chg"]:+.2f}%</b>'
            f'<span class="muted">较前一交易日</span></span>'
            f'<span>形态 <b class="st {label_style}">{html.escape(m["label"])}</b> {vr_txt}</span>'
            f'<span>MA20 乖离 <b class="{"up" if sh["dist20"] >= 0 else "down"}">{sh["dist20"]:+.1f}%</b></span>'
            f'<span>{rsi_txt}</span>{streak_txt}'
            f'{dyn_span}{rel_html}{env_html}'
            '</div>'
            f'<p class="note"><b>行情驱动的策略选择</b>：当前行情[<b class="st {env_style}">{html.escape(env_name)}</b>]'
            f'适配策略：<b>{html.escape(fitted_short)}</b>；不适配的策略当天空仓（不建仓）。</p>'
            f'<p class="note"><b>{tgt_txt}</b>：{verdict_html}　{bars}{n and f"强修复=收盘≥+0.5%，弱修复=0~+0.5%，回落<0。" or ""}{sim_html}</p>'
            '<p class="note">判读提示：<b>缩量回调不破 MA20</b> → 弱修复/震荡修复概率高，若 RSI 超卖则强修复上修；'
            '<b>跌破 MA20 的放量长阴</b> → 反弹以弱修复对待、不追高；<b>超跌（RSI&lt;25 或乖离&lt;-5%）</b> → 次日强修复概率显著；'
            '<b>放量长阳</b> → 趋势延续概率高。动态池强于上证（相对强弱>0）说明行情有结构性主线，修复概率上修。</p>'
            '</div>')


def dynamic_pool_card(rng='ytd'):
    """首页「动态选股池指数辅助」卡片：按策略（趋势+动量得分）每 N 个交易日从沪深主板
    候选重新选入最强势成分、链式市值加权合成的指数（行情轮动自适应），展示当前成分、
    最近调仓与「动态池当日上涨」过滤器的回测采用状态。"""
    global DYNAMIC_POOL, DYNAMIC_POOL_BT
    if DYNAMIC_POOL is None and os.path.exists(DYNAMIC_POOL_FILE):
        try:
            DYNAMIC_POOL = json.load(open(DYNAMIC_POOL_FILE, encoding='utf-8'))
        except Exception:
            DYNAMIC_POOL = None
    d = DYNAMIC_POOL
    if not d or not d.get('series') or len(d['series']) < 2:
        return ''
    if DYNAMIC_POOL_BT is None and os.path.exists(DYNAMIC_POOL_BT_FILE):
        try:
            DYNAMIC_POOL_BT = json.load(open(DYNAMIC_POOL_BT_FILE, encoding='utf-8'))
        except Exception:
            DYNAMIC_POOL_BT = None
    s = d['series']
    last = s[-1]['close']
    prev = s[-2]['close'] if len(s) >= 2 else last
    day_chg = (last / prev - 1) * 100 if prev else 0.0
    dcls = 'up' if day_chg >= 0 else 'down'
    darr = '▲' if day_chg >= 0 else '▼'
    last_date = s[-1]['date']
    if rng == 'ytd':
        start_date = f'{YEAR}-01-01'
    elif rng == '1y':
        start_date = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=365)).isoformat()
    elif rng == '3y':
        start_date = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=365 * 3)).isoformat()
    else:
        start_date = s[0]['date']
    sub = [x for x in s if x['date'] >= start_date] or s[-1:]
    rng_chg = (last / sub[0]['close'] - 1) * 100
    hi = max(sub, key=lambda x: x['close'])
    lo = min(sub, key=lambda x: x['close'])
    first26 = next((x['close'] for x in s if x['date'].startswith(str(YEAR))), None)
    ytd = (last / first26 - 1) * 100 if first26 else None
    base_chg = (last / d['base'] - 1) * 100
    rebs = d.get('rebalances') or []
    cur_codes = rebs[-1]['codes'] if rebs else list(d.get('weights', {}).keys())
    last_rb = rebs[-1]['date'] if rebs else d.get('base_date')
    names = {p['code']: p['name'] for p in (HISTORY['pool'] if HISTORY else [])}
    cur_names = '、'.join(names.get(c, c) for c in cur_codes[:6])
    if len(cur_codes) > 6:
        cur_names += f' 等{len(cur_codes)}只'
    bt = DYNAMIC_POOL_BT or {}
    adopted = bt.get('adopted') or []
    if adopted:
        parts = []
        for a in adopted:
            lw = a.get('landed_win_rate')
            parts.append(f'{html.escape(a.get("strategy", ""))}'
                         + (f'（落地胜率{lw}%）' if lw is not None else ''))
        adopt_html = (f'<b class="up">已采用</b>「动态池当日上涨」过滤器：{"、".join(parts)}'
                      '（回测胜率≥当前+3pp 且 A/B 落地重放验证通过后自动启用，见 data/dynamic_pool_bt.json）')
    else:
        adopt_html = ('回测对比后暂无可采用策略：「动态池当日上涨」未达「胜率≥当前过滤器+3pp 且'
                      'A/B 落地验证通过」标准，现有过滤器保持不变（全量对比见 data/dynamic_pool_bt.json）')
    curve = svg_line_chart([(x['date'], x['close']) for x in sub], base=sub[0]['close'],
                           base_label=f'{sub[0]["date"]}=100', tip_label='动态选股池指数', h=180, ma=[20, 60])
    rngs = [('ytd', '今年'), ('1y', '近1年'), ('3y', '近3年'), ('all', '全部')]
    rng_html = ' '.join(f'<a class="rng-link{" on" if k == rng else ""}" href="/?idx={k}">{v}</a>' for k, v in rngs)
    return (f'<div class="card"><h3>📊 动态选股池指数辅助'
            f'<span class="muted">行情轮动自适应 · 最近调仓 {last_rb}</span></h3>'
            f'<div class="rng-switch">区间：{rng_html}</div>'
            '<div class="idx-sum">'
            f'<span>最新点位 <b>{last:,.2f}</b> <b class="{dcls}">{darr} {day_chg:+.2f}%</b>'
            f'<span class="muted">较前一交易日</span></span>'
            f'<span>今年 <b class="{"up" if (ytd or 0) >= 0 else "down"}">{ytd:+.2f}%</b></span>'
            f'<span>基日以来 <b class="{"up" if base_chg >= 0 else "down"}">{base_chg:+.2f}%</b></span>'
            f'<span>区间涨幅 <b class="{"up" if rng_chg >= 0 else "down"}">{rng_chg:+.2f}%</b>'
            f'<span class="muted">自{sub[0]["date"]}</span></span>'
            f'<span>当前成分 <b>{html.escape(cur_names)}</b></span>'
            '</div>'
            f'{curve}'
            f'<p class="note">口径：候选为沪深主板（剔除创业板/科创板/北交所/ST/退市，共 {d.get("n_candidates", 0)} 只）；'
            f'每个调仓日按「趋势+动量」得分（0.5·近20日涨幅 + 0.3·站上MA20 + 0.2·MA20斜率）选出'
            f'Top {d.get("cfg", {}).get("top_n", 8)} 只最强势标的，每 {d.get("cfg", {}).get("rebalance_days", 10)} 个交易日重选，'
            '行情轮动时成分自动换血；指数=链式市值加权（调仓日自动衔接、连续），权重口径与选股池指数一致'
            f'（换手率估算流通股本）。{adopt_html}</p>'
            '</div>')


def equity_chart(all_daily, rng='sim'):
    """资金曲线（本金5000起，含往年累计已实现盈亏）
    rng: 区间范围 sim(模拟以来7.1起,默认)/ytd/1y/3y/all，决定曲线与「区间权益变动/最大回撤」的展示范围"""
    pts = [(r['date'], r['equity']) for r in all_daily if r.get('equity') is not None]
    if len(pts) < 2:
        return ''
    last_v = pts[-1][1]
    last_date = pts[-1][0]
    if rng == 'sim':
        start_date = SIM_START
    elif rng == 'ytd':
        start_date = f'{YEAR}-01-01'
    elif rng == '1y':
        start_date = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=365)).isoformat()
    elif rng == '3y':
        start_date = (datetime.date.fromisoformat(last_date) - datetime.timedelta(days=365 * 3)).isoformat()
    else:  # all
        start_date = pts[0][0]
    sub = [x for x in pts if x[0] >= start_date] or pts[-1:]
    rng_start = sub[0][1]
    chg_all = last_v - CAPITAL
    chg_rng = last_v - rng_start
    peak, mdd = rng_start, 0.0
    for _, v in sub:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0)
    c_all = 'up' if chg_all >= 0 else 'down'
    c_rng = 'up' if chg_rng >= 0 else 'down'
    rng_label = {'sim': f'模拟以来（{SIM_LABEL}起）', 'ytd': f'{YEAR}年', '1y': '近1年', '3y': '近3年', 'all': '全历史'}[rng]
    note = (f'最新权益 <b>{last_v:,.2f}</b>'
            f'（<span class="{c_all}">全历史累计 {chg_all:+.2f}</span>）'
            f' ｜ {rng_label}权益变动 <span class="{c_rng}">{chg_rng:+.2f}</span>'
            f'<span class="muted">（{sub[0][0]} 起 {rng_start:,.2f}）</span>'
            f' ｜ {rng_label}最大回撤 <span class="down">-{mdd * 100:.1f}%</span>')
    rngs = [('sim', f'模拟以来（{SIM_LABEL}起）'), ('ytd', f'{YEAR}年'), ('1y', '近1年'), ('3y', '近3年'), ('all', '全历史')]
    rng_html = ' '.join(f'<a class="rng-link{" on" if k == rng else ""}" href="?eq={k}">{v}</a>' for k, v in rngs)
    return (f'<div class="card"><h3>资金曲线（{rng_label}，本金5000起）'
            f'<span class="muted">悬停曲线查看每日权益；{note}</span></h3>'
            f'<div class="rng-switch">区间：{rng_html}</div>'
            f'{svg_line_chart(sub, base=rng_start, base_label=f"区间起始{rng_start:,.0f}", ma=[5, 20])}'
            f'<p class="note">默认展示<b>模拟以来（{SIM_START} 起）</b>的模拟交易记录；'
            f'切换「{YEAR}年/近1年/近3年/全历史」可查看历史回测曲线。'
            f'口径：权益 = 本金5000 + <b>全部历史</b>已实现净盈亏 + 当前持仓浮盈'
            f' + 空仓资金理财收益（年化2%：空仓的钱按货基/逆回购计息，复利滚动，平台段呈缓坡）；'
            f'橙色=MA5、蓝色=MA20 均线。'
            f'上方「▸ {SIM_LABEL}以来落袋交易 总盈亏」为模拟起点（{SIM_START}）后卖出成交交易的净盈亏'
            f'（真实账本口径，不含理财），另附「固定5000口径」对照（每笔固定投入≤5000、收益不滚入）；'
            f'往年数据仅用于回测（见「回测答卷」），不计入收益栏位。</p></div>')


def compound_html(s):
    """复利收益模拟：按历史每轮卖出后净盈亏滚入本金继续下一轮买入，按年/总额列出 + 复利曲线"""
    from history_engine import compound_series
    series = compound_series(s['trades'])
    if not series:
        return ''
    years, order = {}, []
    for it in series:
        y = it['sell_date'][:4]
        if y not in years:
            years[y] = {'rounds': 0, 'gain': 0.0, 'pool': None}
            order.append(y)
        years[y]['rounds'] += 1
        years[y]['gain'] += it['gain']
        years[y]['pool'] = it['pool']
    prev_pool = CAPITAL
    rows = ''
    for y in order:
        st = years[y]
        ry = st['gain'] / prev_pool * 100 if prev_pool else 0
        cls = 'up' if st['gain'] >= 0 else 'down'
        rows += (f'<tr><td>{y}</td><td>{st["rounds"]}</td>'
                 f'<td class="{cls}">{st["gain"]:+.2f}</td>'
                 f'<td class="{cls}">{ry:+.2f}%</td>'
                 f'<td>{st["pool"]:,.2f}</td></tr>')
        prev_pool = st['pool']
    total_gain = prev_pool - CAPITAL
    tot_ret = total_gain / CAPITAL * 100
    tcls = 'up' if total_gain >= 0 else 'down'
    table = (f'<div class="tbl-wrap"><table><thead><tr><th>年份</th><th>轮次</th>'
             f'<th>当年净盈亏</th><th>当年收益率</th><th>年末资金(复利)</th></tr></thead>'
             f'<tbody>{rows}'
             f'<tr class="tot"><td><b>合计（{len(order)}年 / {len(series)}轮）</b></td><td><b>{len(series)}</b></td>'
             f'<td class="{tcls}"><b>{total_gain:+.2f}</b></td>'
             f'<td class="{tcls}"><b>{tot_ret:+.2f}%</b></td>'
             f'<td><b>{prev_pool:,.2f}</b></td></tr></tbody></table></div>')
    curve = svg_line_chart([(it['sell_date'], it['pool']) for it in series],
                           base=CAPITAL, base_label='本金5000', tip_label='复利资金', h=150, ma=[5, 20])
    return ('<div class="card"><h3>复利收益模拟'
            '<span class="muted">本金5000起，每轮卖出后把净盈亏并入本金，下一轮按新本金买入（滚动复利）</span></h3>'
            f'{table}{curve}'
            '<p class="note">口径：将历史买卖按「每轮卖出后净盈亏并入本金、下轮全额复投」滚动测算；'
            '当年收益率 = 当年净盈亏 / 年初本金；期末资金 = 5000 + 累计净盈亏。'
            '个股按整手（100股）买入、指数按 5000/批 等比例缩放。'
            '对本金全额投入的策略该测算与真实账本一致；S16 龙头两档自2026-09-03起真实账本为'
            '单笔≤max(本金5000,账户/2)、不会把全部资金复投进单笔，本表仅作「全额复投」的上限演示。'
            '上方「固定5000口径」为对照：每笔固定投入≤5000元、收益不滚入。</p></div>')


def _year_trades(s, pref=None):
    """策略某期（默认今年 YEAR；传入 'YYYY-MM' 为本月）卖出落袋的交易；
    无卖出记录时退回「该期建仓」交易。
    跨年持仓今年卖出时，盈亏计入今年落袋——与资金曲线权益变动一致，
    避免「今年建仓0笔→概况0盈亏」但权益曲线今年大涨的矛盾。"""
    pref = pref or YEAR
    sold = [t for t in s['trades'] if t.get('sell_date', '').startswith(pref)]
    return sold or [t for t in s['trades'] if t['buy_date'].startswith(pref)]


def _sim_trades(s):
    """模拟交易起点（SIM_START，2026-07-01）以来的交易：仅含起点后买入的交易；
    起点之前的历史回测交易仅用于回测，不计入收益栏位（买入时间不可能早于7.1）。"""
    return [t for t in s['trades'] if str(t.get('buy_date', ''))[:10] >= SIM_START]


def _sim_pnl(s):
    """策略模拟交易起点（SIM_START）以来落袋交易净盈亏合计（真实账本/复利投入口径）"""
    return round(sum(t['net_pnl'] for t in _sim_trades(s)), 2)


def _sim_float_pnl(s):
    """策略当前持仓（未平仓）浮盈金额：取每日记录最新一条的 pos_pnl；
    无持仓或数据缺失返回 None。"""
    daily = s.get('daily') or []
    if not daily:
        return None
    v = daily[-1].get('pos_pnl')
    return v if isinstance(v, (int, float)) else None


def _sim_total_pnl(s):
    """策略模拟起点以来总盈亏（含浮盈）= 已卖出落袋净盈亏 + 当前持仓浮盈。
    持仓未卖出并不代表没有收益/亏损，浮盈浮亏同样计入策略总盈利，用于收益排序。"""
    base = _sim_pnl(s)
    f = _sim_float_pnl(s)
    return round(base + f, 2) if f is not None else base


def _sim_ledger_rows():
    """全策略「模拟以来」成交总账：买入即记账（含持仓），卖出补记卖出价/日期/盈亏。
    数据源：HISTORY 各策略 trades（buy_date≥SIM_START）+ 当前持仓(_open_position)。"""
    rows = []
    if not HISTORY:
        return rows
    for s in HISTORY['strategies']:
        sid = s['name']
        for t in _sim_trades(s):
            rows.append({
                'open': False, 'sid': sid,
                'name': t.get('stock_name') or t.get('name') or '',
                'code': str(t.get('stock_code') or t.get('code') or '').strip(),
                'buy_date': str(t.get('buy_date', ''))[:10], 'buy_price': t.get('buy_price'),
                'invest': t.get('invest'),
                'sell_date': str(t.get('sell_date', ''))[:10], 'sell_price': t.get('sell_price'),
                'net': t.get('net_pnl'), 'ret': t.get('ret_pct'),
                'state': '卖出成交',
            })
        pos = _open_position(s.get('daily') or [])
        if pos:
            rows.append({
                'open': True, 'sid': sid,
                'name': pos.get('name') or '',
                'code': str(pos.get('code') or '').strip(),
                'buy_date': str(pos.get('buy_date', ''))[:10], 'buy_price': pos.get('buy_price'),
                'invest': pos.get('invest'),
                'sell_date': '', 'sell_price': None,
                'net': pos.get('pos_pnl'), 'ret': pos.get('pos_pnl_pct'),
                'state': '买入成交（持仓中）',
            })
        # ★ 2026-09-10 停牌挂账：旧仓停牌卖不掉（卖出挂单顺延、换仓买入照常）时，这笔买入既不是
        #   当前持仓（已被换仓新仓取代）也还没卖出 → 原实现在成交流水里整条消失。补一行挂账，
        #   浮盈按"停牌前最后一个交易日"的估值显示（停牌期间价格不变，即 frozen 的估值口径）。
        _fzr, _fz = signal_rows.frozen_open(s.get('daily') or [])
        if _fz:
            _fzcd = str(_fz.get('code') or '').strip()
            _fznm = str(_fz.get('name') or '')
            _fzsince = str(_fz.get('since') or (_fzr or {}).get('date') or '')[:10]
            _already = bool(_fzcd) and any(
                r.get('code') == _fzcd and r.get('sid') == sid for r in rows)
            if not _already:
                _buy, _val = None, None
                for _r in (s.get('daily') or []):
                    _hit = (str(_r.get('code') or '').strip() == _fzcd
                            or (_fznm and _sl_norm(_r.get('name') or '') == _sl_norm(_fznm)))
                    if not _hit:
                        continue
                    if str(_r.get('status') or '') in ('建仓', '换仓买入') and _buy is None:
                        _buy = _r
                    if (_val is None and _r.get('pos_pnl') is not None
                            and str(_r.get('date') or '') < _fzsince):
                        _val = _r
                rows.append({
                    'open': True, 'sid': sid, 'name': _fznm, 'code': _fzcd,
                    'buy_date': str((_buy or {}).get('date') or _fzsince)[:10],
                    'buy_price': (_buy or {}).get('deal_price'),
                    'invest': (_buy or {}).get('invest'),
                    'sell_date': '', 'sell_price': None,
                    'net': (_val or {}).get('pos_pnl'), 'ret': (_val or {}).get('pos_pnl_pct'),
                    'state': f'买入成交（停牌挂账：{_md(_fzsince)}起停牌，复牌日按时点补卖）',
                })
    rows.sort(key=lambda r: (r['buy_date'] or '', r['sid']), reverse=True)
    return rows


# _num2 已移至 ui_common

def _ledger_row(r):
    nm = html.escape(str(r.get('name') or '—'))
    cd = str(r.get('code') or '').strip()
    sym = f'<b>{nm}</b>' if not cd else f'<b>{nm}</b> <span class="muted">{cd}</span>'
    st = r.get('state', '')
    st_cls = ('warn' if '挂账' in st else
              ('hold' if '持仓' in st else 'gray'))
    net, ret = r.get('net'), r.get('ret')
    if net is None:
        net_cls, net_s = 'gray', '—'
    else:
        net_cls = 'up' if net >= 0 else 'down'
        net_s = '%+.2f' % net
    if ret is None:
        ret_cls, ret_s = 'gray', '—'
    else:
        ret_cls = 'up' if ret >= 0 else 'down'
        ret_s = '%+.2f%%' % ret
    return (f'<tr><td class="note">{html.escape(str(r["sid"]))}</td>'
            f'<td>{sym}</td>'
            f'<td class="d-key">{r.get("buy_date") or "—"}</td>'
            f'<td>{_num2(r.get("buy_price"))}</td>'
            f'<td>{_num2(r.get("invest"))}</td>'
            f'<td class="d-key">{r.get("sell_date") or "—"}</td>'
            f'<td>{_num2(r.get("sell_price"))}</td>'
            f'<td class="{net_cls}">{net_s}</td>'
            f'<td class="{ret_cls}">{ret_s}</td>'
            f'<td><b class="st {st_cls}">{st}</b></td></tr>')


def _pending_signals_html():
    """★ 首页总账顶部「信号登记」块（2026-09-09）：买入/卖出信号在触发/判定当日即登记，
    不必等收盘后模拟成交才有记录（用户口径：收盘再记录没有意义）。
    覆盖：①今日盘中实时触发；②各策略最新行仍待执行的买入信号/卖出信号/未决换仓候选。
    买入成交/卖出成交由下方成交流水在成交后自动记录。

    ★ 2026-09-10 结构优化：行数据统一由 signal_rows.register_rows() 产出（与速览表、
      总账、今日行动清单共用同一套判定），本函数只负责把行渲染成 HTML —— 顺带修掉旧实现里
      「未决换仓候选」判定用 startswith('已换仓') 漏判后缀式「特  力Ａ（已换仓）」的问题。"""
    try:
        rows = signal_rows.register_rows(HISTORY, TODAY_STATUS)
        if not rows:
            # ★ 2026-09-17：**空态也渲染本块**。此前 return '' 会让首页整块消失，用户无法区分
            #   "今天没有待执行信号"与"页面漏了 / 信号凭空没了"（本次"记录查不到"的困惑之一）；
            #   同时 _autotest 的「首页区块 信号登记」断言在收盘后（各策略均已建仓/持有、
            #   无待执行信号）会误报红。
            return ('<details style="margin:2px 0 10px"><summary><b>📌 信号登记</b>'
                    '<span class="muted">（盘中触发/收盘判定的买入、卖出信号在<b>当日即登记</b>，'
                    '不用等收盘模拟成交；已消失的信号不再登记）</span></summary>'
                    '<p class="muted">当前<b>无待执行</b>的买入 / 卖出信号'
                    '（各策略最新判定为持有 / 空仓 / 已成交）。'
                    '已消失、已停用的历史信号见 '
                    '<a href="/signals">📡 盘中信号历史</a>。</p></details>')
        body = ''
        for r in rows:
            _sname = str(r.get('sid') or '')
            _sid = _sname.split('_')[0]
            _short = _sname.split('_', 1)[1] if '_' in _sname else ''
            _slink = (f'<a href="/s/{urllib.parse.quote(_sname)}">'
                      f'{html.escape(_sid + " " + _short)}</a>')
            _kind = str(r.get('kind') or '')
            _cls = {'买入信号': 'ok', '卖出信号': 'sell'}.get(_kind, '')
            _nm = f'<b>{html.escape(str(r.get("name") or ""))}</b>'
            if r.get('code'):
                _nm += f'<span class="muted">（{html.escape(str(r.get("code")))}）</span>'
            if r.get('extra'):
                _nm += f'<span class="muted">{html.escape(str(r.get("extra")))}</span>'
            body += (f'<tr><td class="note">{_slink}</td>'
                     f'<td><b class="st {_cls}">{_kind}</b>'
                     f'<span class="muted">（{html.escape(str(r.get("tag") or ""))}）</span></td>'
                     f'<td>{_nm}</td><td>{html.escape(str(r.get("time_txt") or ""))}</td>'
                     f'<td class="note">{html.escape(str(r.get("desc") or ""))}</td></tr>')
        return ('<details open style="margin:2px 0 10px"><summary><b>📌 信号登记</b>'
                '<span class="muted">（盘中触发/收盘判定的买入、卖出信号在<b>当日即登记</b>，不用等收盘'
                '模拟成交；下方成交流水继续记录买入成交/卖出成交）</span></summary>'
                '<div class="tbl-wrap"><table><thead><tr><th>策略</th><th>记录类型</th>'
                '<th>标的</th><th>登记时点</th><th>说明 / 下一步</th></tr></thead><tbody>'
                + body + '</tbody></table></div>'
                '<p class="muted" style="margin:4px 0 0">'
                '<a href="/signals">📡 查看全部历史信号（含已消失 / 已停用记录，可调取任意历史日）→</a>'
                '</p></details>')
    except Exception:
        return ''


# ============================================================
# 全策略模拟信号记录（总账）
# 口径（2026-09-09 用户）：买入信号/换仓候选/卖出信号各自登记一行（同日一事件可分两行）；
#   “记录信号日就买入”——任一买入类信号在其信号日当天即按当日收盘价虚拟买入，不等 T+1 成交；
#   卖出统一比两种卖法的模拟收益：①卖出信号日收盘卖（卖出信号日成交收益）
#   ②实际卖出成交日卖（卖出成交日收益，卖价=真实平仓价）。
# ============================================================
_SL_IDX = {'沪深300指数', '红利指数', '中证500指数', '上证50指数'}
_SL_BAD = ('已换仓', '已废弃', '废弃', '过滤', '不可买', '顶板', '买不进', '低开')
_SL_STOP = re.compile(r'收盘触发.{0,8}(止损|卖出|平仓)信号')
_SL_UNIV = None
_SL_UNIV_LOAD = False
_SL_CSV = {}


def _sl_norm(x):
    return re.sub(r'\s+', '', str(x or ''))


def _sl_num(x):
    try:
        float(x)
        return True
    except Exception:
        return False


def _sl_code(name):
    """名称→代码（全市场股票池 stock_universe.csv）"""
    global _SL_UNIV, _SL_UNIV_LOAD
    if not _SL_UNIV_LOAD:
        _SL_UNIV_LOAD = True
        _SL_UNIV = {}
        try:
            for line in open('data/stock_universe.csv', encoding='utf-8-sig'):
                line = line.strip()
                if not line or line.startswith('code'):
                    continue
                p = line.split(',')
                if len(p) >= 2:
                    _SL_UNIV[_sl_norm(p[1])] = str(p[0]).strip()
        except Exception:
            pass
    return (_SL_UNIV or {}).get(_sl_norm(name))


def _sl_close_of(name, date):
    """目标在信号日收盘价（=“记录信号日就买入”的虚拟买入价）"""
    code = _sl_code(name)
    if not code:
        return None
    if code not in _SL_CSV:
        import pandas as pd
        try:
            df = pd.read_csv(f'data/csv/{code}.csv', dtype={'股票代码': str})
            df['日期'] = df['日期'].astype(str)
            _SL_CSV[code] = df
        except Exception:
            _SL_CSV[code] = None
    df = _SL_CSV.get(code)
    if df is None or df.empty:
        return None
    m = df.loc[df['日期'] == str(date), '收盘']
    if m.empty:
        return None
    try:
        return float(m.iloc[0])
    except Exception:
        return None


def _sl_close(r):
    """行内信号日参考价（判定日收盘）"""
    return float(r['price']) if _sl_num(r.get('price')) else None


def _sl_sellpx(r):
    """实际卖出成交价（换仓卖出行 sell_price 保留原卖出价）"""
    for k in ('sell_price', 'deal_price'):
        if _sl_num(r.get(k)):
            return float(r[k])
    return None


def _sl_pend_cand(rb):
    """返回 rec_buy 里的待执行换仓候选名（已换仓/废弃/过滤等不算），无则 None"""
    rb = str(rb or '')
    if '（评分' in rb and not any(w in rb for w in _SL_BAD):
        n = _sl_norm(rb.split('（')[0])
        return n or None
    return None


def _sl_is_sell_sig(r):
    st = str(r.get('status') or '')
    if st in ('卖出信号', '卖出顺延'):
        return True
    return bool(_SL_STOP.search(str(r.get('note') or '')))


def _sl_ledger_rows():
    """全策略模拟信号记录（总账）行。最新在前；每行=一次信号事件。
    行字段：date登记日, type(买入信号/换仓候选/卖出信号), name, code,
    b_date/b_px 虚拟买入基准(信号日收盘), a_date/a_px 卖出信号日, pA,
    b2_date/b2_px 实际卖出成交日, pB, state, remark"""
    out = []
    try:
        for s in (HISTORY or {}).get('strategies', []):
            daily = sorted([r for r in s.get('daily', [])
                            if str(r.get('date', '')) >= SIM_START],
                           key=lambda r: str(r['date']))
            if not daily:
                continue
            buy_ev, sell_ev = [], []
            cand_runs, buy_runs = {}, {}      # name -> i（连续候选/待成交只记最早）
            for i, r in enumerate(daily):
                date = str(r['date']); st = str(r.get('status') or '')
                name = _sl_norm(r.get('name') or ''); rb = str(r.get('rec_buy') or '')
                if st == '信号废弃':
                    cand_runs, buy_runs = {}, {}
                    continue
                if st in ('卖出成交', '建仓'):
                    buy_runs.pop(name, None)
                    cand_runs.pop(name, None)
                c = _sl_pend_cand(rb)
                if c:
                    if c not in cand_runs:
                        cand_runs[c] = i
                        buy_ev.append({'type': '换仓候选', 'date': date, 'name': c,
                                       'close': None, 'i': i,
                                       # ★ 2026-09-10：区分两种"候选"——
                                       #   flat=True  本行已空仓（pos_pnl 缺失）→ 平仓/未成交后重扫出的新标的，
                                       #              标签「空仓后买入」（不是接旧仓的换仓）
                                       #   flat=False 仍持仓 → 卖旧买新的真换仓，标签「买入·换仓候选」
                                       'flat': r.get('pos_pnl') is None})
                elif rb:
                    cand_runs = {}
                else:
                    cand_runs = {}
                if st in ('买入信号', '买入信号(待成交)') and name and name not in _SL_IDX:
                    if st == '买入信号' or name not in buy_runs:
                        buy_runs[name] = i
                        buy_ev.append({'type': '买入信号', 'date': date, 'name': name,
                                       'close': _sl_close(r), 'i': i})
                if st == '卖出信号' and name and name not in _SL_IDX:
                    sell_ev.append({'date': str(r['date']), 'name': name,
                                    'close': _sl_close(r), 'i': i})

            def _exit_of(i0, nm):
                """真实盘该信号后续：(是否买入, 卖出信号日(date,close), 卖出成交日(date,卖价))"""
                acq = False; sig = None; ex = None
                for k in range(i0 + 1, len(daily)):
                    r = daily[k]
                    st = str(r.get('status') or '')
                    nm2 = _sl_norm(r.get('name') or '')
                    note = _sl_norm(str(r.get('note') or ''))
                    hit = (nm2 == nm)
                    if not acq:
                        if hit and st in ('持有', '建仓'):
                            acq = True
                        elif (st in ('卖出成交', '建仓') and ('换仓买入' + nm) in note):
                            acq = True
                        elif hit and st in ('卖出信号', '卖出成交', '卖出顺延'):
                            break          # 无建仓先遇卖出 → 非本周期
                        else:
                            continue
                    if sig is None and hit and _sl_is_sell_sig(r):
                        sig = (str(r['date']), _sl_close(r))
                    if st == '卖出成交' and hit:
                        ex = (str(r['date']), _sl_sellpx(r))
                        break
                return acq, sig, ex

            # ★ 2026-09-10 停牌挂账（跨日）：本策略若有未了结的 frozen_sell（旧仓停牌卖不掉、
            #   卖出挂单顺延中），其买入周期的"待卖出成交"要标明是挂账，否则总账里同样一行
            #   看起来像"信号已发但成交迟迟不回填"，看不出原因。
            _fzr, _fz = signal_rows.frozen_open(daily)
            _fznm = _sl_norm((_fz or {}).get('name') or '')
            _fzsince = str((_fz or {}).get('since') or (_fzr or {}).get('date') or '')[:10]

            cyc = []
            for e in buy_ev:
                acq, sig, ex = _exit_of(e['i'], e['name'])
                if e['close'] is None:
                    e['close'] = _sl_close_of(e['name'], e['date'])
                e.update(acq=acq, sig=sig, ex=ex)
                cyc.append(e)

            # 卖出信号行（对应其周期买入基准，两卖法收益一并给出）
            for e in sell_ev:
                anch = None
                for c in cyc:
                    if c['name'] == e['name'] and c['date'] <= e['date'] and c['acq']:
                        anch = c
                if anch is None:
                    continue
                out.append({
                    'sid': s['name'], 'type': '卖出信号', 'name': e['name'],
                    'code': _sl_code(e['name']), 'date': e['date'],
                    'b_date': anch['date'], 'b_px': anch['close'],
                    'a_date': e['date'], 'a_px': e['close'],
                    'b2_date': anch['ex'][0] if anch['ex'] else None,
                    'b2_px': anch['ex'][1] if anch['ex'] else None,
                    'cycle': anch, 'entry_type': anch['type'],
                    'active': bool(cand_runs.get(anch['name']) == anch['i']
                                   or buy_runs.get(anch['name']) == anch['i']),
                })

            # 买入信号 / 换仓候选 行
            for c in cyc:
                if c['acq']:
                    state = ('已卖出' if c['ex'] else
                             ('待卖出成交' if c['sig'] else '持仓中'))
                else:
                    active = bool(cand_runs.get(c['name']) == c['i']
                                  or buy_runs.get(c['name']) == c['i'])
                    state = '待执行' if active else '未买成'
                out.append({
                    'sid': s['name'], 'type': c['type'], 'name': c['name'],
                    'code': _sl_code(c['name']), 'date': c['date'],
                    'b_date': c['date'], 'b_px': c['close'],
                    'a_date': c['sig'][0] if c['sig'] else None,
                    'a_px': c['sig'][1] if c['sig'] else None,
                    'b2_date': c['ex'][0] if c['ex'] else None,
                    'b2_px': c['ex'][1] if c['ex'] else None,
                    'cycle': c, 'entry_type': c['type'],
                    'frozen_since': (_fzsince if (_fznm and c['name'] == _fznm
                                                 and not c['ex']) else None),
                    'active': bool(cand_runs.get(c['name']) == c['i']
                                   or buy_runs.get(c['name']) == c['i']),
                    'state': state, 'flat': c.get('flat'),
                })
    except Exception:
        return []
    # ★ 2026-09-10 用户要求：今日盘中实时触发的买入信号（TODAY_STATUS，与提醒卡/微信同源）
    #   也要在总账当日登记（不必等收盘确认才有记录）；收盘后引擎落当日行，本临时行自动被真实行取代，
    #   收盘未确认则随扫描更新自动消失。
    try:
        _d0 = datetime.date.today().isoformat()
        for _r in _intraday_sig_rows():
            _nm0 = _sl_norm(_r.get('name'))
            _rot = str(_r.get('sid') or '')
            if not (_nm0 and _rot):
                continue
            _has = any(str(r.get('sid')) == _rot and _sl_norm(r.get('name')) == _nm0
                       and str(r.get('date')) == _d0 for r in out)
            if _has:
                continue
            out.append({
                'sid': _rot, 'type': '买入信号', 'name': _nm0,
                'code': str(_r.get('code') or _sl_code(_nm0)),
                'date': _d0, 'b_date': _d0, 'b_px': _r.get('price'),
                'a_date': None, 'a_px': None, 'b2_date': None, 'b2_px': None,
                'cycle': None, 'entry_type': '买入信号', 'active': True,
                'state': '待执行', 'intraday': True,
                'sig_price': _r.get('price'), 'sig_score': _r.get('score'),
                'sig_tag': None, 'intraday_note': _r.get('note'),
            })
    except Exception:
        pass
    # 最新在前
    out.sort(key=lambda x: (x.get('date') or '', x.get('sid') or ''), reverse=True)
    return out


def _sl_retpct(a, b):
    """b/a - 1 的百分比；任一缺失返回 None"""
    if a is None or b is None or not _sl_num(a) or not _sl_num(b):
        return None
    return (float(b) / float(a) - 1.0) * 100.0


def _sl_pct_html(v):
    if v is None:
        return '<span class="muted">—</span>'
    cls = 'up' if v > 0 else ('down' if v < 0 else '')
    return f'<span class="{cls}">{v:+.2f}%</span>'


def _signal_ledger_card():
    """首页「全策略模拟信号记录（总账）」，置于今日行动清单之后、成交流水之前"""
    rows = _sl_ledger_rows()
    if not rows:
        return ''
    shown, more = rows[:15], rows[15:]
    h2 = ('💡 全策略模拟信号记录（总账）'
          '<span class="muted">买入信号/换仓候选/卖出信号<b>在信号日当天即登记</b>'
          '（不等 T+1 成交）；买入类信号按<b>“记录信号日就买入”=信号日收盘价</b>虚拟买入，'
          '卖出给两种模拟口径：卖出信号日收盘卖、实际卖出成交日卖。最新在前</span>')
    note = ('<p class="note">口径：<b>买入信号/换仓候选</b>信号日即虚拟买入（虚拟买入价=信号日收盘价，'
            '不依赖次日 T+1 真实成交）；<b>卖出信号</b>登记行给出“若信号出现当日收盘即卖”的收益，'
            '对照列再给“真实卖出成交日”（真实平仓价）收益，二者均相对该笔信号日收盘的虚拟买入价计算。'
            '信号确认后按 T+1 执行的交易会在成交流水（下表）同步记录；因顶板/低开证伪、资金不足等未买成的'
            '买入类信号本表照实登记、两卖出列留空（状态=未买成），不再生成成交流水。</p>')
    body = ''
    for r in shown:
        body += _sl_ledger_row(r)
    head = ('<table><thead><tr><th>策略</th><th>记录类型</th><th>标的</th>'
            '<th>买入信号日<br><span class="muted">(记录信号日就买入)</span></th>'
            '<th>信号日收盘<br><span class="muted">(虚拟买入价)</span></th>'
            '<th>卖出信号日</th><th>卖出信号日成交收益</th>'
            '<th>卖出成交日</th><th>卖出成交日收益</th><th>状态</th><th>说明</th></tr></thead><tbody>')
    out = (f'<div class="card"><h2>{h2}</h2>{note}'
           f'<div class="tbl-wrap">{head}{body}</tbody></table></div>')
    if more:
        out += (f'<details><summary>展开全部（共 {len(rows)} 条，还有 {len(more)} 条）</summary>'
                f'<div class="tbl-wrap">{head}'
                + ''.join(_sl_ledger_row(r) for r in more) + '</tbody></table></div></details>')
    out += '</div>'
    return out


def _sl_ledger_row(r):
    b_date, b_px = r.get('b_date'), r.get('b_px')
    a_date, a_px = r.get('a_date'), r.get('a_px')
    b2_date, b2_px = r.get('b2_date'), r.get('b2_px')
    pA = _sl_retpct(b_px, a_px)
    pB = _sl_retpct(b_px, b2_px)
    # 记录类型徽标
    tcls = {'买入信号': 'ok', '换仓候选': 'warn', '卖出信号': 'sell'}.get(r['type'], '')
    tlab = {'买入信号': '买入信号', '换仓候选': '买入·换仓候选'}.get(r['type'], r['type'])
    if r['type'] == '换仓候选' and r.get('flat'):
        tlab = '空仓后买入'   # 平仓/未成交后重扫出的新标的（非接旧仓的换仓）
    _sid = str(r['sid']).split('_')[0]
    _short = str(r['sid']).split('_', 1)[1] if '_' in str(r['sid']) else ''
    slink = f'<a href="/s/{urllib.parse.quote(str(r["sid"]))}">{html.escape(_sid + " " + _short)}</a>'
    code_html = f'<span class="muted">（{html.escape(str(r.get("code") or ""))}）</span>'
    state, scls = r.get('state', ''), 'gray'
    # ★ 2026-09-10 停牌挂账：卖出信号已判、旧仓却停牌卖不掉 → 状态直接标明"挂账"+停牌起始日，
    #   否则同一行看起来像"信号已发而成交迟迟不回填"（原因不可见）
    if state == '待卖出成交' and r.get('frozen_since'):
        state = '待卖出成交（停牌挂账）'
    state_cls = {'待执行': 'warn', '未买成': 'gray', '持仓中': 'hold',
                 '待卖出成交': 'sell', '已卖出': 'ok',
                 '待卖出成交（停牌挂账）': 'warn'}.get(state, 'gray')
    remark = ''
    _extra_html = ''      # ★ 已自行转义过的补充 HTML：不参与 html.escape，否则标签会变文本
    if r.get('intraday'):
        # ★ 2026-09-10 盘中实时触发行：价格=盘中现价，收盘确认后按信号日收盘价更新
        _sp, _ss = r.get('sig_price'), r.get('sig_score')
        _cl = datetime.datetime.now().hour >= 15   # ★ 2026-09-10：收盘后不再用"盘中价/待确认"口径
        if _cl:
            remark = (f"今日收盘已确认（{r.get('date')} 收盘价 {_sp if _sp is not None else '—'}，"
                      f"评分 {_ss if _ss is not None else '—'}"
                      + (f"，{r.get('sig_tag')}" if r.get('sig_tag') else '')
                      + f"）：按信号日收盘价登记，于 {_next_td(r.get('date'))} 按策略时点买入（T+1）")
        else:
            remark = (f"盘中实时触发（{r.get('date')} 现价 {_sp if _sp is not None else '—'}，"
                      f"评分 {_ss if _ss is not None else '—'}"
                      + (f"，{r.get('sig_tag')}" if r.get('sig_tag') else '')
                      + f"）；本行价格=盘中价，收盘后按信号日收盘价更新；收盘确认后于 "
                        f"{_next_td(r.get('date'))} 按策略时点买入（T+1），收盘未确认则本行自动消失")
    elif state == '持仓中' and r['type'] != '卖出信号':
        remark = '真实盘仍持仓；卖出列待其后卖出信号/成交后回填'
    elif state == '待卖出成交（停牌挂账）':
        remark = (f'{a_date} 卖出信号（收盘判定）；持仓股自 '
                  f'{r.get("frozen_since") or "—"} 起停牌（无成交），卖出挂单顺延至复牌日、'
                  '复牌当日按策略时点补卖（信号不废弃；换仓买入已照常执行，未因卖不掉而耽误）')
    elif state == '待卖出成交':
        remark = f'{a_date} 卖出信号（收盘判定），实际卖出成交待 T+1 后回填'
    elif state == '待执行':
        remark = '信号已确认，T+1 按策略时点执行；顶板/低开证伪则废弃不买'
        # ★ 实盘证伪（2026-09-28）：执行日就是今天时，静态推荐必须由实盘数据修正——
        #   昨日收盘的"龙头"到今天可能已经炸板（接力失败）或一字封死（排不进），
        #   照静态推荐追进去就是接刀。案例：601811 新华文轩 9/24 五连板信号 →
        #   9/28 触及涨停 22.39 后砸到 19.81(-2.65%)，而首页仍写着"今日买入 评分95"。
        _exd = _next_td(r.get('date'))
        if (_exd and _exd == datetime.date.today().isoformat()
                and r['type'] != '卖出信号'):
            _dn = daban_realtime_note(str(r.get('code') or ''), _exd)
            if _dn:
                _col = {'bad': '#c62828', 'warn': '#e67e00'}.get(_dn[0], '#2e7d32')
                _ico = '⚠' if _dn[0] in ('bad', 'warn') else '●'
                _extra_html += ('<br><span style="color:%s;font-weight:600">%s %s</span>'
                                % (_col, _ico, html.escape(_dn[1])))
    elif state == '未买成':
        remark = '真实盘未成交（顶板/低开证伪、资金不足或候选被替换），本表仅登记信号'
    elif state == '已卖出':
        remark = f'真实盘于 {b2_date} 平仓（若 b2 列缺失为换仓行）'
    else:
        remark = r.get('entry_type', '')
    remark = remark if remark and remark != 'None' else ''
    return (
        f'<tr><td class="note">{slink}</td>'
        f'<td><b class="st {tcls}">{html.escape(tlab)}</b>'
        f'{("<span class=muted>（登记）</span>") if r["type"] == "卖出信号" else ""}'
        f'{("<span class=muted>（" + ("收盘确认" if datetime.datetime.now().hour >= 15 else "盘中触发") + "）</span>") if r.get("intraday") else ""}</td>'
        f'<td><b>{html.escape(str(r["name"]))}</b>{code_html}</td>'
        f'<td>{html.escape(str(b_date or "—"))}</td>'
        f'<td>{b_px if b_px is not None else "—"}</td>'
        f'<td>{html.escape(str(a_date or "—"))}</td><td>{_sl_pct_html(pA)}</td>'
        f'<td>{html.escape(str(b2_date or "—"))}</td><td>{_sl_pct_html(pB)}</td>'
        f'<td><b class="st {state_cls}">{html.escape(state or "登记")}</b></td>'
        f'<td class="note">{html.escape(remark)}{_extra_html}</td></tr>')


def _ledger_card(rows):
    """首页「策略选股回放与每日推荐」下方全策略模拟成交总账：默认10条，其余折叠展开"""
    if not rows:
        return ''
    shown, more = rows[:10], rows[10:]
    _thead = ('<table><thead><tr><th>策略</th><th>标的</th><th>买入日期</th><th>买入价</th>'
              '<th>投入(元)</th><th>卖出日期</th><th>卖出价</th><th>净盈亏(元)</th>'
              '<th>收益率</th><th>状态</th></tr></thead><tbody>')
    out = (f'<div class="card"><h2>📋 全策略模拟成交记录（总账）'
           f'<span class="muted">自 {SIM_START} 模拟以来，各策略<b>买入即记账</b>、卖出后补记卖出价/日期/盈亏；'
           f'持仓行净盈亏=当前浮动盈亏。最新在前，默认显示 {len(shown)} 条</span></h2>'
           f'{_pending_signals_html()}'
           f'<p class="note">口径：上方「信号登记」= 盘中/收盘判定的<b>买入信号、卖出信号当日即登记</b>'
           f'（不等成交）；下表=四类记录中的<b>买入成交 / 卖出成交</b>——信号收盘确认后于下一交易日（T+1）'
           f'按策略时点成交即自动转正为成交流水；未成交前仅登记、若信号收盘未确认则自动消失。</p>'
           f'<div class="tbl-wrap">{_thead}{"".join(_ledger_row(r) for r in shown)}</tbody></table></div>')
    if more:
        out += (f'<details><summary>展开全部（共 {len(rows)} 条，还有 {len(more)} 条）</summary>'
                f'<div class="tbl-wrap">{_thead}{"".join(_ledger_row(r) for r in more)}'
                '</tbody></table></div></details>')
    out += '</div>'
    return out


def _fixed_pnl(trades):
    """固定5000口径：每笔按5000元本金、实际买卖价、整手、真实费用重算，收益不滚入。
    返回 (合计, 有效笔数)；个股5000买不起1手或指数微批次的笔跳过。"""
    from history_engine import fixed5000_pnl
    return fixed5000_pnl(trades)


def _sim_pnl_fixed(s):
    """策略模拟交易起点（SIM_START）以来固定5000口径净盈亏合计（按起点后落袋交易）"""
    from history_engine import fixed5000_pnl
    return fixed5000_pnl(_sim_trades(s))[0]


def _monthly_pnl(s):
    """策略本月权益变动（含持仓浮盈结算）：
    本月落袋净盈亏 + (月末持仓浮盈 − 月初持仓浮盈)。
    持仓中的浮盈/浮亏也计入本月，避免「本月无卖出→显示0」掩盖真实盈亏。"""
    realized = sum(t['net_pnl'] for t in s['trades']
                   if (t.get('sell_date') or '').startswith(MONTH))
    daily = s.get('daily', [])
    if not daily:
        return round(realized, 2)
    first_m = next((i for i, r in enumerate(daily)
                    if (r.get('date') or '').startswith(MONTH)), None)
    if first_m is None:
        return round(realized, 2)
    open_pnl = daily[first_m - 1].get('pos_pnl') if first_m > 0 else None
    close_pnl = daily[-1].get('pos_pnl')

    def _f(v):
        return v if isinstance(v, (int, float)) else 0.0
    return round(realized + (_f(close_pnl) - _f(open_pnl)), 2)


def invest_advice(s):
    """投资方式建议：推荐复投 vs 建议固定投资（基于全历史数据）。
    依据：样本量、全历史累计净盈亏、近3年(不含今年)表现、累计收益率。
    返回 (标签, 样式key[ok/warn/bad/gray], 说明)。"""
    tr = s['trades']
    total = sum(float(t['net_pnl']) for t in tr)
    n = len(tr)
    yrs = {}
    for t in tr:
        y = t['buy_date'][:4]
        yrs[y] = yrs.get(y, 0.0) + float(t['net_pnl'])
    n_years = len(yrs)
    last3 = sum(v for y, v in yrs.items() if str(int(YEAR) - 3) <= y < YEAR)
    ret = total / 5000.0 * 100
    if n < 20 or n_years <= 2:
        return ('数据不足', 'gray',
                f'全历史仅 {n_years} 年 {n} 笔交易，样本过少，不足以判断长期稳定性；'
                '建议暂按固定投入（每笔≤5000、收益不滚入）控制风险，积累样本后再评估复投。')
    if total >= 1000 and last3 >= 0:
        return ('推荐复投', 'ok',
                f'全历史累计净盈亏 {total:+.0f} 元（本金5000起，累计收益率 {ret:.0f}%），'
                f'近3年（{int(YEAR)-3}–{int(YEAR)-1}）{last3:+.0f} 元保持盈利，'
                '收益滚入本金可放大复利；页面「复利口径」通常高于「固定5000口径」，适合复投。')
    if total > 0:
        return ('复投需谨慎', 'warn',
                f'全历史虽累计 {total:+.0f} 元，但本金5000起累计收益率仅 {ret:.0f}%，'
                '年度盈亏波动大、正收益年份不连续；复投会同时放大盈亏波动，'
                '建议固定投入为主，待盈利稳健后再复投。')
    if last3 > 0:
        return ('建议固定投资', 'warn',
                f'全历史累计 {total:+.0f} 元为负，历史亏损已滚入本金（复利自缚，可用资金缩水到约 '
                f'{5000+total:.0f} 元），今年绝对收益因此被压缩；近3年 {last3:+.0f} 元虽转正，'
                '仍建议固定投入（每笔≤5000、收益不滚入）控制敞口，连续盈利多年后再评估复投。')
    return ('建议固定投资', 'bad',
            f'全历史累计 {total:+.0f} 元为负（本金5000起），若复投，亏损会滚入本金、可用资金持续缩水'
            f'（当前约 {5000+total:.0f} 元），同样收益率下绝对收益越滚越小（复利自缚）；'
            '固定投入（每笔≤5000、收益不滚入）可隔离历史亏损、控制风险敞口。')


def _trade_stats(trades):
    """trades → 统计 dict（笔数/胜场/胜率/总盈亏/平均单笔/最大盈/最大亏/平均持有）"""
    if not trades:
        return {'trades': 0, 'wins': 0, 'win_rate': 0.0, 'pnl': 0.0, 'avg': 0.0,
                'best': 0.0, 'worst': 0.0, 'hold': 0.0}
    pnl = [t['net_pnl'] for t in trades]
    wins = sum(1 for p in pnl if p > 0)
    return {
        'trades': len(pnl),
        'wins': wins,
        'win_rate': round(wins / len(pnl) * 100, 1),
        'pnl': round(sum(pnl), 2),
        'avg': round(sum(pnl) / len(pnl), 2),
        'best': round(max(pnl), 2),
        'worst': round(min(pnl), 2),
        'hold': round(sum(t['hold_days'] for t in trades) / len(trades), 1),
    }


def _summary_table(rows):
    """rows: [(期间, stats)] → 收益汇总表 HTML（期间为年/月）"""
    body = ''
    for key, st in rows:
        cls = 'up' if st['pnl'] >= 0 else 'down'
        body += (f'<tr><td>{key}</td><td>{st["trades"]}</td><td>{st["win_rate"]}%</td>'
                 f'<td class="{cls}">{st["pnl"]:+.2f}</td><td>{st["avg"]:+.2f}</td>'
                 f'<td class="up">{st["best"]:+.2f}</td><td class="down">{st["worst"]:+.2f}</td>'
                 f'<td>{st["hold"]}天</td></tr>')
    return ('<table><thead><tr><th>期间</th><th>笔数</th><th>胜率</th><th>总盈亏</th>'
            '<th>平均单笔</th><th>最大盈利</th><th>最大亏损</th><th>平均持有</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def _year_report(strategies):
    """7.1以来回测答卷：按收益降序列出每策略交易统计，含合计行（与 _trade_stats 同口径）"""
    rows_html = []
    totals = {'trades': 0, 'wins': 0, 'pnl': 0.0, 'pnl_f': 0.0, 'hold': 0.0, 'best': None, 'worst': None}
    for s in strategies:
        trades = _sim_trades(s)
        if not trades:
            continue
        st = _trade_stats(trades)
        pnl_f = _fixed_pnl(trades)[0]
        totals['trades'] += st['trades']
        totals['wins'] += st['wins']
        totals['pnl'] += st['pnl']
        totals['pnl_f'] += pnl_f
        totals['hold'] += st['hold'] * st['trades']
        if totals['best'] is None or st['best'] > totals['best']:
            totals['best'] = st['best']
        if totals['worst'] is None or st['worst'] < totals['worst']:
            totals['worst'] = st['worst']
        pfcls = 'up' if pnl_f >= 0 else 'down'
        rows_html.append(
            f'<tr><td><a href="/s/{urllib.parse.quote(s["name"])}">{html.escape(s["name"])}</a></td>'
            f'<td>{st["trades"]}</td>'
            f'<td>{st["win_rate"]}%</td>'
            f'<td class="{"up" if st["pnl"] >= 0 else "down"}">{st["pnl"]:+.2f}</td>'
            f'<td class="{pfcls}">{pnl_f:+.2f}</td>'
            f'<td>{st["avg"]:+.2f}</td>'
            f'<td class="up">{st["best"]:+.2f}</td>'
            f'<td class="down">{st["worst"]:+.2f}</td>'
            f'<td>{st["hold"]}天</td></tr>')
    if not rows_html:
        return ''
    t = totals
    wr = round(t['wins'] / t['trades'] * 100, 1) if t['trades'] else 0.0
    avg_pnl = round(t['pnl'] / t['trades'], 2) if t['trades'] else 0.0
    avg_hold = round(t['hold'] / t['trades'], 1) if t['trades'] else 0.0
    cls = 'up' if t['pnl'] >= 0 else 'down'
    pfc = 'up' if t['pnl_f'] >= 0 else 'down'
    rows_html.append(
        f'<tr class="tot"><td><b>合计（{len([s for s in strategies if _sim_trades(s)])}个策略有交易）</b></td>'
        f'<td><b>{t["trades"]}</b></td><td><b>{wr}%</b></td>'
        f'<td class="{cls}"><b>{t["pnl"]:+.2f}</b></td>'
        f'<td class="{pfc}"><b>{t["pnl_f"]:+.2f}</b></td>'
        f'<td><b>{avg_pnl:+.2f}</b></td>'
        f'<td class="up"><b>{t["best"]:+.2f}</b></td>'
        f'<td class="down"><b>{t["worst"]:+.2f}</b></td>'
        f'<td><b>{avg_hold}天</b></td></tr>')
    return ('<div class="card"><h2>7.1以来回测答卷</h2>'
            '<p class="note"><b>以下为回测结果</b>（2026年7月1日以来的回测数据）；'
            f'收益栏位（上方卡片/详情页「7.1以来」）为模拟交易记录，自 {SIM_START} 起，两者口径相同。'
            '回测口径：基于今年全部已发生行情回放；<b>真实账本口径</b>：本金5000起、收益滚入本金，可用现金 = 本金+已实现盈亏+理财收益，含费整手买入'
            '（S16龙头两档自2026-09-03起单笔≤max(本金5000,账户/2)、收益不全额复投；其余策略按可用现金全额投入）；<b>固定5000口径</b>：每笔固定投入≤5000元、收益不滚入，'
            '按实际买卖价与真实费用重算。信号收盘判定、按各策略标注时点成交，决策只用当日及之前数据，无未来数据参与。</p>'
            '<div class="tbl-wrap"><table><thead><tr><th>策略</th><th>笔数</th><th>胜率</th>'
            '<th>总盈亏(复利)</th><th>总盈亏(固定5000)</th><th>平均单笔</th><th>最大单笔盈利</th>'
            f'<th>最大单笔亏损</th><th>平均持有</th></tr></thead><tbody>{"".join(rows_html)}</tbody></table></div></div>')


# ★ 换仓日展示拆分（2026-09-02）：引擎同日先卖后买换仓时，单条 daily 记录混装两笔操作——
#   status='卖出成交'、name=卖出标的，但 deal_price 被覆盖为"换仓买入价"，真实卖出价只在 note 文本中。
#   展示层按 note 拆成两条：卖出成交行（价格=实际卖出价）、换仓买入行（新标的），
#   时间倒序下"换仓买入"在上、"卖出成交"在下（先卖后买）。
_SW_SELL_RE = re.compile(r'(?:开盘止损|开盘|收盘)(\d+\.\d+)平仓')
# ★ 2026-09-10 修复：标的名称含空格（如"特  力Ａ""深 赛 格"）时旧式 [^\s（(]+ 会在空格处截断
#   → 整条换仓 note 匹配失败 → 展示层拆不出「换仓买入」行、卖出行成交价退回被覆盖的买入价
#   （案例：S15 9/10 卖深粮 8.28 + 换仓买特力Ａ 15.59，页面曾显示"已卖出 深粮控股（成交价 15.59）"）。
#   改用非贪婪 (.+?) 并由后缀「\s*N股（投入X元）」锚定，名称内部空格照常保留。
_SW_BUY_RE = re.compile(r'；同日(开盘|收盘)(\d+\.\d+)换仓买入(.+?)\s*(\d+)股[（(]投入([\d.]+)元[)）]')

_NAME2CODE_CACHE = None
_NAME2CODE_SQ_CACHE = None


def _name2code(name):
    """按股票名称反查代码（全市场懒加载一次并缓存）。换仓买入行里新标的的
    代码不在结构化字段中（引擎记录的 name/code 是卖出标的），据此补正确代码。
    ★ 2026-09-10：名称含空格（"特  力Ａ"）时精确匹配失败 → 去空格兜底再匹配一次。"""
    global _NAME2CODE_CACHE, _NAME2CODE_SQ_CACHE
    if _NAME2CODE_CACHE is None:
        try:
            import history_engine as _he
            _NAME2CODE_CACHE = {n: c for c, n in _he.STOCKS.items()}
        except Exception:
            _NAME2CODE_CACHE = {}
        _NAME2CODE_SQ_CACHE = {str(k).replace(' ', ''): v for k, v in _NAME2CODE_CACHE.items()}
    nm = str(name or '').strip()
    if nm in _NAME2CODE_CACHE:
        return _NAME2CODE_CACHE[nm]
    return (_NAME2CODE_SQ_CACHE or {}).get(nm.replace(' ', ''))


def _switch_view(r):
    """换仓日还原成"已换入新标的"的展示视图（供最新交易日卡片/首页卡片用）；
    非换仓日原样返回原 dict。附带 _sell_px/_sell_name 记录原卖出标的。"""
    if r.get('status') != '卖出成交' or '换仓买入' not in str(r.get('note', '')):
        return r
    note = str(r.get('note', ''))
    ms, mb = _SW_SELL_RE.search(note), _SW_BUY_RE.search(note)
    if not mb:
        return r
    sell_px = r.get('sell_price')          # 优先结构化字段（重建后数据自带）
    if sell_px is None and ms:
        sell_px = float(ms.group(1))
    if sell_px is None:
        return r
    v = dict(r)
    buy_px = float(mb.group(2))
    v['status'] = '换仓买入'
    v['name'] = str(mb.group(3)).strip()
    _c2 = _name2code(v['name'])
    if _c2:
        v['code'] = _c2
    v['deal_price'] = buy_px
    v['_sell_px'] = sell_px
    v['_sell_name'] = str(r.get('name') or '')
    # 现价=换入标的当日收盘估算（由持仓浮盈额反推），无浮盈数据时退回买入价
    if r.get('pos_pnl') is not None:
        try:
            sh = int(mb.group(4))
            invest = float(mb.group(5))
            v['price'] = round((r['pos_pnl'] + invest) / sh, 2)
        except Exception:
            pass
    return v


def _candidate_row(last):
    """未决换仓候选 → 速览表置顶的「买入信号」行（原实现见 signal_rows.candidate_row）。

    ★ 2026-09-10 结构优化：判定逻辑收敛到 signal_rows，与首页「信号登记」块共用同一入口
      （旧实现两处各写一份，修了速览表却漏了首页）；此处保留薄封装，调用点无需改动。"""
    return signal_rows.candidate_row(last)


def _intraday_sig_row(sid=None):
    """今日盘中实时触发的买入信号（TODAY_STATUS 单条）→ 详情页置顶的独立「买入信号」行。

    ★ 2026-09-10 结构优化：实现移至 signal_rows._intraday_row_from_ts，此处为薄封装
      （仅在信号事件流为空时作为回退路径，正式路径见 _intraday_sig_rows）。"""
    return signal_rows._intraday_row_from_ts(TODAY_STATUS, sid)


def _intraday_sig_rows(sid=None, include_gone=False):
    """当日**全部**盘中触发信号（SQLite 信号库 data/signals.db，跨重启持久化）→ 页面伪行列表。

    此前只取 TODAY_STATUS 里"最后一轮"那一条，导致当天更早触发的标的（用户反馈的"华金资本"）
    在首页总账/详情页完全查不到。库为空时回退 TODAY_STATUS 单条（兼容）。
    跨日历史见 /signals 页（notify.intraday_signals_history）。
    ★ 2026-09-10 结构优化：实现移至 signal_rows.intraday_rows，此处为薄封装。
    ★ 2026-09-17：默认 include_gone=False —— 本函数的消费方（首页信号登记 / 总账 / 速览表 /
      每日选股 / 今日行动清单 / 详情页提示条）都是"**待执行信号**"口径，已消失的行不属于
      待执行信号（9/16 S15 曾把"高新发展（已消失）"当买入信号置顶，与收盘判定方大集团矛盾）。
      /signals 页直读 notify.intraday_signals_history，不受此参数影响。"""
    return signal_rows.intraday_rows(TODAY_STATUS, sid, include_gone=include_gone)


def render_signals(query=None):
    """★ 2026-09-10「盘中信号历史」页：SQLite 信号库（data/signals.db）可调取**任意历史日**的全部
    盘中信号——含"已消失（信号未持续）"与"已停用（人工隐藏）"记录，并支持停用/恢复。

    背景（用户反馈）：旧实现把当日信号写 data/intraday_signals.json、跨日重建，且信号消失时
    **直接删除记录**，导致"华金资本明明推荐过却哪里都查不到"。现在一条信号一行、按交易日分区，
    消失/停用只改标志位（gone / deleted=1，行保留），本页就是"事后调取"的入口。"""
    q = query or {}

    def _g(k, d=''):
        return str((q.get(k) or [d])[0])

    try:
        days = int(_g('days', '30') or 30)
    except Exception:
        days = 30
    days = max(1, min(days, 365))
    show_del = _g('deleted') in ('1', 'true', 'on')
    kw = _g('q').strip()
    try:
        evs = notify.intraday_signals_history(days=days, include_deleted=show_del, limit=3000)
    except Exception as e:
        return (f'<div class="card"><h2>盘中信号历史</h2>'
                f'<p class="muted">读取失败：{html.escape(str(e))}</p></div>')
    if kw:
        _k = kw.lower()
        evs = [e for e in evs
               if _k in str(e.get('code') or '').lower()
               or _k in str(e.get('name') or '').lower()
               or _k in str(e.get('sname') or '').lower()]
    n_all = len(evs)
    n_live = sum(1 for e in evs if not e.get('gone') and not e.get('deleted'))
    n_gone = sum(1 for e in evs if e.get('gone') and not e.get('deleted'))
    n_del = sum(1 for e in evs if e.get('deleted'))

    # ★ 2026-09-18 前向收益回填（T+1/T+3/T+5 + 收盘确认），幂等：只处理 evaluated_at 为空的行
    #   （数据没到齐的下次再算），故每次访问成本极低；每天自动跑一次，?backfill=1 强制全量重算。
    _force_bf = _g('backfill') in ('1', 'true')
    if _force_bf or STATE.get('fwd_bf_date') != datetime.date.today().isoformat():
        try:
            _bf = notify.intraday_backfill_fwd(days=max(days, 90), force=_force_bf)
            STATE['fwd_bf_date'] = datetime.date.today().isoformat()
            if (_bf or {}).get('filled'):
                log(f'盘中信号前向收益回填: {_bf}')
        except Exception as e:
            log(f'前向收益回填失败: {str(e)[:60]}')
    _fst = None
    try:
        import signal_store as _ss
        _fst = _ss.fwd_stats(days=days)
    except Exception as e:
        log(f'前向表现统计失败: {str(e)[:60]}')

    def _fwd_cell(v):
        """前向收益单元格：涨红跌绿（class up/down），无数据显示 —（数据未到齐）"""
        try:
            fv = float(v)
        except Exception:
            return '<span class="muted">—</span>'
        _c = 'up' if fv > 0 else ('down' if fv < 0 else 'muted')
        return f'<span class="{_c}">{fv:+.2f}%</span>'

    def _agg_txt(k):
        d = (_fst or {}).get(k) or {}
        if not d.get('n'):
            return '—'
        return f"{d.get('mean')}%（n={d['n']}，上涨 {d.get('win')}%）"

    _fwd_bar = ''
    if (_fst or {}).get('n'):
        _cr = _fst.get('confirm_rate')
        _fwd_bar = (
            f'<p class="note">★ <b>区间表现</b>（近 {days} 天，基准 = <b>触发日收盘</b>／qfq；'
            f'样本 {_fst["n"]} 条，不含持仓跟踪行）：'
            f'<b>T+1</b> {_agg_txt("t1")} ｜ <b>T+3</b> {_agg_txt("t3")} ｜ '
            f'<b>T+5</b> {_agg_txt("t5")} ｜ <b>收盘确认</b> {_fst.get("confirmed")}/{_fst["n"]}'
            + (f'（{_cr:.1f}%）' if isinstance(_cr, (int, float)) else '')
            + '　<span class="muted">口径同 _signal_health.py：T+1/3/5 = 自触发日收盘起第 1/3/5 个'
              '<b>交易日</b>收盘的涨跌幅；显示 — 表示该期限未到期或日线未入库，下次访问自动补算。'
              '「收盘确认」= 收盘判定最终采纳了本行标的（含 T+1 实际成交价）。</span></p>')

    def _url(d=days, deleted=show_del, q=kw):
        _p = ['days=%d' % int(d)]
        if deleted:
            _p.append('deleted=1')
        if q:
            _p.append('q=' + urllib.parse.quote(str(q)))
        return '/signals?' + '&'.join(_p)

    _range = []
    for _d in (7, 30, 90, 365):
        _range.append(f'<b>{_d}天</b>' if _d == days else f'<a href="{_url(d=_d)}">{_d}天</a>')
    _cnt_line = f'触发中 <b class="st ok">{n_live}</b> ｜ 已消失 <b class="st warn">{n_gone}</b>'
    if show_del:
        _cnt_line += f' ｜ 已停用 <b class="st gray">{n_del}</b>'
    # ★ 2026-09-17：当日概况行 —— 直接回答"今天为什么没有新记录"：
    #   扫描只覆盖**当日轮值策略**，别的策略当天根本不扫，自然不会新增行（非故障）。
    _d0 = datetime.date.today().isoformat()
    try:
        _t_evs = notify.intraday_signals_today()
    except Exception:
        _t_evs = []
    _n_today = len(_t_evs)
    _n_today_live = sum(1 for e in _t_evs if not e.get('gone') and not e.get('deleted'))
    _rot = str(STATE.get('strategy') or '')
    _rot_short = _rot.split('_')[0] if _rot else '—'
    _auto_js = 'true' if is_trading_time() else 'false'
    bar = ('<div class="card"><h2>📡 盘中信号历史'
           '<span class="muted">（SQLite 全量留痕：data/signals.db · 表 intraday_signal）</span></h2>'
           f'<p>今日（{_md(_d0)}）当日轮值策略 <b>{html.escape(_rot_short)}</b>'
           f'<span class="muted">（{html.escape(_rot)}）</span>：盘中信号 <b>{_n_today}</b> 条'
           f'，其中触发中 <b>{_n_today_live}</b>'
           '<span class="muted">　—— 扫描只覆盖<b>当日轮值策略</b>：其它策略当天不扫描、'
           '不会产生新记录（这就是"某策略今天没有记录"的正常原因，不是丢数据）。</span></p>'
           f'<p>最近 <b>{days}</b> 天共 <b>{n_all}</b> 条：{_cnt_line}'
           + (f' ｜ 关键词 “{html.escape(kw)}”' if kw else '')
           + '</p><p>区间：' + ' ｜ '.join(_range)
           + f' ｜ <a href="{_url(deleted=(not show_del))}">'
           + ('隐藏已停用记录' if show_del else '显示已停用记录')
           + '</a> ｜ <a href="' + _url() + '&backfill=1">重算前向收益</a></p>'
           # ★ 2026-09-17 刷新（用户要求）：本页不走页面缓存（每次访问直读 signals.db），
           #   并提供手动刷新 + 交易时段自动刷新（60s，勾选状态记忆在浏览器本地）
           + _fwd_bar
           + '<p><button type="button" onclick="location.reload()" '
           'style="padding:4px 12px;cursor:pointer">🔄 刷新</button>'
           '<label class="muted" style="margin-left:12px">'
           '<input type="checkbox" id="sigAuto" onchange="sigAutoToggle()"> '
           '自动刷新（交易时段每 60 秒，非交易时段不自动）</label>'
           '<span class="muted" id="sigAutoTip"></span></p>'
           '<form method="get" action="/signals" style="margin:8px 0">'
           f'<input type="hidden" name="days" value="{days}">'
           + ('<input type="hidden" name="deleted" value="1">' if show_del else '')
           + f'<input name="q" value="{html.escape(kw)}" placeholder="按代码/名称/策略搜索，如 000532 / 华金" '
             'style="padding:4px 8px;width:240px"> '
             '<button type="submit" style="padding:4px 10px">搜索</button></form>'
           '<p class="note">★ 状态口径（2026-09-17 修订）：'
           '① <b>触发中</b> = 该标的在<b>触发当日</b>最后一轮扫描时仍在触发（盘中口径）；'
           '是否算数看策略页「每日选股与状态」的收盘判定。'
           '② <b>已消失</b> 三类原因：<b>换标的</b>（同策略当日改触发别的标的——谁最后触发谁触发中）、'
           '<b>收盘未持续</b>（收盘判定采纳的是别的标的 / 未判定，收盘后自动对账回写）、'
           '<b>隔日失效</b>（跨交易日后不再跟踪：盘中信号只在触发当日有效）。'
           '③ 本页<b>不走页面缓存</b>，每次访问直读 signals.db，盘中可随时点「刷新」。</p>'
           '<p class="note">★ <b>持仓跟踪</b>（2026-09-18 新增，状态列标「持仓跟踪」）：有持仓的'
           '<b>非轮值策略</b>盘中只扫自己的持仓股（1 只/策略，成本可忽略）并留痕，解决"轮值外'
           '推荐的下文跟踪断了"；这类行<b>不推送邮件、不算待执行买入信号</b>（首页信号登记／'
           '总账／行动清单均已过滤），只在本页展示，且不计入上面的区间表现统计。</p>'
           '<p class="note">★ 删除语义（2026-09-10 改造）：本系统<b>不做物理删除</b>——信号盘中短暂消失只把 '
           '<code>gone</code> 置 1（行保留，本页仍可查"曾触发过"）；误报可"停用"（<code>deleted</code> 置 1，'
           '默认隐藏、可一键恢复）。此前旧 JSON 机制会直接删除记录，导致当天更早推送过的标的（如 9/10 的'
           '华金资本）事后无法调取。</p>'
           '<script>function sigFlag(id,act){fetch("/api/signal_flag",{method:"POST",'
           'headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id,action:act})})'
           '.then(function(r){return r.json();}).then(function(d){if(d&&d.ok){location.reload();}})'
           '.catch(function(){});}'
           'function sigAutoToggle(){var cb=document.getElementById("sigAuto");'
           'try{localStorage.setItem("sig_auto",cb.checked?"1":"0");}catch(e){}'
           'if(cb.checked){location.reload();}}'
           '(function(){var cb=document.getElementById("sigAuto");var v=null;'
           'try{v=localStorage.getItem("sig_auto");}catch(e){}'
           'var on=(v===null)?' + _auto_js + ':(v==="1");'
           'if(cb){cb.checked=on;document.getElementById("sigAutoTip").innerHTML='
           'on?"（已开启：本页每 60 秒自动刷新）":"（已关闭）";}'
           'if(on){setTimeout(function(){location.reload();},60000);}})();</script></div>')
    if not evs:
        return bar + '<div class="card"><p class="muted">该区间暂无信号记录。</p></div>'
    groups = {}
    for e in evs:
        groups.setdefault(str(e.get('trade_date') or ''), []).append(e)
    out = [bar]
    for _d in sorted(groups.keys(), reverse=True):
        rows = groups[_d]
        _live = sum(1 for e in rows if not e.get('gone') and not e.get('deleted'))
        _del = sum(1 for e in rows if e.get('deleted'))
        _psh = sum(1 for e in rows if e.get('pushed'))
        _sub = (f'{len(rows)} 条：触发中 {_live} ｜ 已推送 {_psh}'
                + (f' ｜ 已停用 {_del}' if _del else ''))
        trs = []
        for e in rows:
            _sname = str(e.get('sname') or '')
            _sid = _sname.split('_')[0]
            _short = _sname.split('_', 1)[1] if '_' in _sname else ''
            _f = str(e.get('first_at') or '')
            _l = str(e.get('last_at') or '')
            _tm = _f[11:19] if len(_f) >= 19 else _f
            if len(_l) >= 19 and _l[11:19] != _tm:
                _tm += '～' + _l[11:19]
            if e.get('watch') and not e.get('deleted'):
                # ★ 持仓跟踪行（轮值外策略对自己持仓股的盘中留痕）：不是买入信号
                _stc = '<b class="st hold">持仓跟踪</b>'
            elif e.get('deleted'):
                _stc = ('<b class="st gray">已停用</b><span class="muted">（%s）</span>'
                        % html.escape(str(e.get('deleted_reason') or '人工停用')))
            elif e.get('gone'):
                _stc = ('<b class="st warn">已消失</b><span class="muted">（%s）</span>'
                        % html.escape(str(e.get('gone_reason') or '信号消失')))
            else:
                _stc = '<b class="st ok">触发中</b>'
            # ★ 2026-09-18 收盘确认：收盘判定是否采纳了本行标的（含 T+1 实际成交价）
            if e.get('confirmed'):
                _cfc = ('<b class="st ok">✓ 已确认</b>'
                        + (f'<span class="muted"> {e.get("confirm_px")}</span>'
                           if e.get('confirm_px') is not None else ''))
            elif e.get('confirm_name'):
                _cfc = ('<span class="muted">否（收盘采纳 '
                        f'{html.escape(str(e.get("confirm_name")))}）</span>')
            elif e.get('evaluated_at'):
                _cfc = '<span class="muted">未判定买入</span>'
            else:
                _cfc = '<span class="muted">—</span>'
            _push = ('<span class="muted">已推送 %s</span>' % html.escape(str(e.get('pushed_at') or '')[:19])
                     if e.get('pushed') else '<span class="muted">未推送</span>')
            _px = []
            if e.get('price') is not None:
                _px.append(f'现价 {e.get("price")}')
            if e.get('score') is not None:
                _px.append(f'评分 {e.get("score")}')
            _note = str(e.get('tag') or '') or str(e.get('note') or '')
            _eid = int(e.get('id') or 0)
            _btn = ''
            if _eid:
                _btn = (f'<button onclick="sigFlag({_eid},\'restore\')" '
                        'style="padding:2px 8px;font-size:12px;cursor:pointer">恢复</button>'
                        if e.get('deleted') else
                        f'<button onclick="sigFlag({_eid},\'hide\')" '
                        'style="padding:2px 8px;font-size:12px;cursor:pointer">停用</button>')
            trs.append(
                f'<tr><td><a href="/s/{urllib.parse.quote(_sname)}">{html.escape(_sid + " " + _short)}</a></td>'
                f'<td><b>{html.escape(str(e.get("name") or ""))}</b>'
                f'<span class="muted">（{html.escape(str(e.get("code") or ""))}）</span></td>'
                f'<td>{html.escape(_tm)}</td><td>{int(e.get("count") or 0)}</td>'
                f'<td>{html.escape(" ｜ ".join(_px)) or "—"}</td>'
                f'<td>{_stc}</td><td>{_cfc}</td>'
                f'<td>{_fwd_cell(e.get("t1_ret"))}</td>'
                f'<td>{_fwd_cell(e.get("t3_ret"))}</td>'
                f'<td>{_fwd_cell(e.get("t5_ret"))}</td>'
                f'<td>{_push}</td>'
                f'<td class="note">{html.escape(_note[:60])}</td><td>{_btn}</td></tr>')
        out.append(f'<div class="card"><h3>{_md(_d)} 盘中信号'
                   f'<span class="muted">（{_sub}）</span></h3>'
                   '<div class="tbl-wrap"><table><thead><tr><th>策略</th><th>标的</th><th>首次触发</th>'
                   '<th>次数</th><th>价格/评分</th><th>状态</th><th>收盘确认</th>'
                   '<th>T+1</th><th>T+3</th><th>T+5</th><th>推送</th><th>备注</th><th>操作</th>'
                   '</tr></thead><tbody>' + ''.join(trs) + '</tbody></table></div></div>')
    return ''.join(out)


def _split_switch(r):
    """把『卖出成交 + 同日自动换仓买入』的单条每日记录拆成两条展示记录。
    返回 [换仓买入行, 卖出成交行]（按时间倒序展示时买入行在上、卖出行在下）；
    非换仓日返回 [r] 原样。"""
    if r.get('status') != '卖出成交' or '换仓买入' not in str(r.get('note', '')):
        return [r]
    note = str(r.get('note', ''))
    ms, mb = _SW_SELL_RE.search(note), _SW_BUY_RE.search(note)
    if not mb:
        return [r]
    sell_px = r.get('sell_price')          # 优先结构化字段（重建后数据自带）
    if sell_px is None and ms:
        sell_px = float(ms.group(1))
    if sell_px is None:
        return [r]
    buy_px = float(mb.group(2))
    buy_name = str(mb.group(3)).strip()
    buy_shares = int(mb.group(4))
    buy_invest = float(mb.group(5))
    # 卖出成交行：价格列还原为实际卖出价；换仓细节由买入行承载
    sell = dict(r)
    sell['deal_price'] = sell_px
    sell['rec_buy'] = None
    sell['note'] = note.split('；同日')[0]
    # 换仓买入行：新标的；现价=新标的当日收盘估算（无浮盈数据时退回买入价）
    est_close = buy_px
    if r.get('pos_pnl') is not None:
        try:
            est_close = round((r['pos_pnl'] + buy_invest) / buy_shares, 2)
        except Exception:
            pass
    buy = dict(r)
    _c2 = _name2code(buy_name)
    buy.update({'status': '换仓买入', 'name': buy_name,
                'deal_price': buy_px, 'price': est_close,
                'invest': buy_invest, 'shares': buy_shares,
                'rec_buy': None, 'trade_pnl': None, 'trade_pnl_pct': None,
                't1_pct': None, 'note': mb.group(0).lstrip('；')})
    if _c2:
        buy['code'] = _c2
    return [buy, sell]


def _parse_rec_buy(rb):
    """解析每日行 rec_buy 文本「标的（评分N[，被xx过滤]）」→ (名称, 括号内展示文本, 代码)。
    用于行动清单里把「换仓候选」渲染成一条可执行的买入动作（含代码反查）。"""
    txt = str(rb or '').strip()
    if not txt or '（' not in txt:
        return '', '', ''
    nm = txt.split('（', 1)[0].strip()
    return nm, txt[txt.find('（'):], (_name2code(nm) or '')


# 详情页每日明细折叠：连续"空仓/观望"与连续"持有（同标的）"段各合并为一条区间行
# （仅展示层，不改数据；持有段还附带期间浮盈范围/停牌天数摘要）
_FOLD_IDLE_STATES = ('空仓', '观望')


def _fold_rows(daily):
    """daily 时间升序 → 把『连续空仓/观望』段与『连续持有（同标的、无触发/推荐/当日盈亏）』
    段各合成一条区间行：区间行附加 _fold0/_fold1/_fold_n（起/止日期、天数）与
    _fold_kind('idle'|'hold')、_fold_extra（持有段摘要文本），其余行原样保留。
    单日段前后若紧邻非折叠状态则保留逐日显示，避免孤立日丢日期可读性。"""
    out = []
    seg = None  # [kind, 最后一条行副本, 段首日期, 段尾日期, 天数, 段内行列表]
    for r in daily:
        st = r.get('status')
        is_idle = st in _FOLD_IDLE_STATES
        is_hold = (st == '持有' and not r.get('triggered') and not r.get('rec_buy')
                   and r.get('trade_pnl') is None)
        if is_idle or is_hold:
            kind = 'idle' if is_idle else 'hold'
            if seg is None or seg[0] != kind or (kind == 'hold' and seg[5][0].get('name') != r.get('name')):
                if seg is not None:
                    out.append(_fold_summary_row(seg))
                seg = [kind, r, r['date'], r['date'], 1, [r]]
            else:
                seg[1], seg[3], seg[4] = r, r['date'], seg[4] + 1
                seg[5].append(r)
            continue
        if seg is not None:
            out.append(_fold_summary_row(seg))
            seg = None
        out.append(r)
    if seg is not None:
        out.append(_fold_summary_row(seg))
    return out


def _fold_summary_row(seg):
    """由折叠段生成展示行：n=1 原样返回；n>1 以段末行为代表附加折叠标记与摘要。"""
    kind, last, d0, d1, n, rows = seg
    if n <= 1:
        return last
    r = dict(last)
    r['_fold0'], r['_fold1'], r['_fold_n'], r['_fold_kind'] = d0, d1, n, kind
    if kind == 'hold':
        pcts = [x.get('pos_pnl_pct') for x in rows
                if isinstance(x.get('pos_pnl_pct'), (int, float))]
        susp = sum(1 for x in rows if '停牌' in str(x.get('note', '')))
        parts = []
        if pcts:
            parts.append('期间浮盈 %+.1f%% ~ %+.1f%%' % (min(pcts), max(pcts)))
        if susp:
            parts.append('停牌 %d 天' % susp)
        r['_fold_extra'] = '，'.join(parts)
    return r


def daily_row(r):
    status = r['status']
    cls = STATUS_CLASS.get(status, '')
    if status in BUY_STATES:
        rowcls = 'row-buy'
    elif status in SELL_STATES:
        rowcls = 'row-sell'
    elif status == '持有':
        rowcls = 'row-hold'
    elif status in ('观望', '空仓'):
        rowcls = 'row-plain'
    elif status in ('资金不足', '信号废弃', '卖出顺延'):
        rowcls = 'row-warn'
    else:
        rowcls = ''
    dcls = ' class="d-key"' if (status in KEY_STATES or r.get('_fold_n')) else ''
    trig = '是' if r.get('triggered') else ''
    note = str(r.get('note', ''))
    if r.get('_fold_n'):
        if r.get('_fold_kind') == 'hold':
            _ex = r.get('_fold_extra')
            note += (f'；连续持有 {r["_fold_n"]} 个交易日'
                     f'（{r["_fold0"]} ~ {r["_fold1"]}'
                     + (f'，{_ex}' if _ex else '')
                     + '，每日明细已合并为一行）')
        else:
            note += (f'；连续{status} {r["_fold_n"]} 个交易日'
                     f'（{r["_fold0"]} ~ {r["_fold1"]}，每日状态一致，明细已合并为一行）')
    if r.get('rec_buy'):
        _ptl = str(r.get('pending_sell') or '')
        if '已换仓' in str(r['rec_buy']):
            note += f"；同日已自动换仓买入 {r['rec_buy']}"
        elif _ptl:
            # ★ 2026-09-10 盘中预览行：卖出挂单今日尾盘才成交，候选执行时点同样是今天
            note += f"⚠ 同日买入推荐：{r['rec_buy']}（今日{_ptl}先卖出当前持仓、同笔换仓买入，待执行）"
        else:
            note += f"⚠ 同日买入推荐：{r['rec_buy']}（次日开盘可买）"
    _dtxt = r['date']
    if r.get('_fold_n'):
        _dtxt = f'{r["_fold0"]} ~ {r["_fold1"]}'
    # 买入参考列：策略信号参考价(buy_ref)优先；已成交买入行(buy_ref 为空)回退为实际买入价，
    # 便于核对成本（如换仓买入行 deal_price=买入价，price=当日收盘）。卖出行不受影响。
    _br = r.get('buy_ref')
    if _br is None and status in ('建仓', '换仓买入') and r.get('deal_price') is not None:
        _br = r['deal_price']
    pnl = r.get('trade_pnl')
    if pnl is not None:
        pcls = 'up' if pnl > 0 else ('down' if pnl < 0 else '')
        pnl_td = f'<td class="{pcls}">{pnl:+.2f}</td>'
    elif status in ('建仓', '换仓买入') and r.get('invest') is not None:
        pnl_td = f'<td class="muted">投入 {r["invest"]:,.2f}</td>'
    else:
        pnl_td = '<td>-</td>'
    return (f'<tr class="{rowcls}"><td{dcls}>{html.escape(_dtxt)}</td>'
            f'<td class="st {cls}">{html.escape(status)}</td>'
            f'<td>{html.escape(r["name"]) or "-"}</td>'
            f'<td>{r.get("score") if r.get("score") is not None else "-"}</td>'
            f'<td>{r.get("price") if r.get("price") is not None else "-"}</td>'
            f'<td>{_br if _br is not None else "-"}</td>'
            f'<td>{r.get("stop") if r.get("stop") is not None else "-"}</td>'
            f'<td>{r.get("target") if r.get("target") is not None else "-"}</td>'
            f'<td>{trig}</td><td>{_t1_html(r)}</td>{pnl_td}'
            f'<td class="note">{_esc_note(note)}</td></tr>')


_NAME_TO_CODES = None


def _code_of_name(name):
    """持仓名称 → 股票代码（全市场名称反查）；重名时返回空串，避免误导。"""
    global _NAME_TO_CODES
    if _NAME_TO_CODES is None:
        rev = {}
        for c, n in advisor.STOCKS.items():
            rev.setdefault(n, []).append(c)
        _NAME_TO_CODES = {n: cs[0] if len(cs) == 1 else '' for n, cs in rev.items()}
    return _NAME_TO_CODES.get(str(name), '')


def _open_position(daily):
    """定位当前持仓（未平仓）：从每日记录末尾反向找最近一次买入成交（建仓/换仓买入）。
    返回供 trade_row 展示的 dict（含 open=True），无持仓时返回 None。"""
    if not daily:
        return None
    last = daily[-1]
    if last.get('pos_pnl') is None:
        return None
    buy_date = buy_price = invest = shares = None
    pos_name = last.get('name', '')
    for r in reversed(daily):
        if r.get('status') == '建仓':
            buy_date, buy_price = r['date'], r.get('deal_price') or r.get('buy_ref')
            invest, shares = r.get('invest'), r.get('shares')
            pos_name = r.get('name', pos_name)
            if shares is None:
                # 老行无 shares 字段（引擎 2026-09-10 起才写）→ 从 note 兜底："开盘8.28买入700股（投入…元）"
                _m0 = re.search(r'(\d+)股', str(r.get('note') or ''))
                if _m0:
                    shares = int(_m0.group(1))
            break
        if '换仓买入' in str(r.get('note', '')) and r.get('deal_price') is not None:
            # 换仓行 name 是卖出标的，当前持仓名须从 note 解析（如"换仓买入万向德农 600股"）
            # ★ 2026-09-10 修复：旧实现 split('换仓买入')[-1].split(' ')[0] 被两件事同时击穿——
            #   ①停牌换仓 note 尾部追加"——换仓买入不因卖不掉而耽误"，[-1] 取到第二个"换仓买入"，
            #     持仓名变成"不因卖不掉而耽误；成本14.10"（S16/S16V2 9/9 案例）；
            #   ②标的含空格（"特  力Ａ"）时 split(' ')[0] 只取到"特"（S15 9/10 案例）。
            #   现改为：结构化字段 switch_buy['name'] 优先 → 与展示层同源的 _SW_BUY_RE → 宽松兜底。
            note = str(r.get('note', ''))
            buy_date, buy_price = r['date'], r.get('deal_price')
            invest = r.get('invest')
            _sb = r.get('switch_buy') if isinstance(r.get('switch_buy'), dict) else {}
            _n = str(_sb.get('name') or '').strip()
            _sh = None
            _m = _SW_BUY_RE.search(note)
            if _m:
                _n = _n or str(_m.group(3)).strip()
                _sh = int(_m.group(4))
            else:
                _m2 = re.search(r'换仓买入(.+?)\s*(\d+)股', note)
                if _m2:
                    _n = _n or str(_m2.group(1)).strip()
                    _sh = int(_m2.group(2))
            if shares is None:
                # 老行（2026-09-10 前的 build）未写 shares 字段，从 note 兜底：
                #   建仓 "开盘8.28买入700股（投入5381.05元）" / 换仓 "…换仓买入百大集团 300股（投入4235.04元）"
                _m3 = re.search(r'(\d+)股', note)
                if _m3:
                    shares = int(_m3.group(1))
            if _n:
                pos_name = _n
            break
    return {
        'open': True,
        'buy_date': buy_date or last.get('date'),
        'buy_price': buy_price if buy_price is not None else '-',
        'shares': shares,
        'invest': invest,
        'name': pos_name,
        'code': _code_of_name(pos_name),
        'pos_pnl_pct': last.get('pos_pnl_pct'),
        'pos_pnl': last.get('pos_pnl'),
        'last_date': last['date'],
    }


def trade_row(t):
    if t.get('open'):
        p = t.get('pos_pnl_pct')
        pc = 'up' if p is not None and p >= 0 else 'down'
        pct_s = f'{p:+.2f}%' if p is not None else '-'
        nm = html.escape(str(t.get('name') or ''))
        cd = str(t.get('code') or '').strip()
        sym = f'<b>{nm}</b>' if not cd else f'<b>{nm}</b> <span class="muted">{cd}</span>'
        return (f'<tr class="row-hold"><td>{sym}</td><td class="d-key"><b>{t["buy_date"]}</b></td>'
                f'<td>{_dash(t.get("buy_price"))}</td>'
                f'<td><b class="st ok">持仓中</b></td><td>-</td>'
                f'<td>{_dash(t.get("shares"))}</td><td>{_dash(t.get("invest"))}</td>'
                f'<td>-</td><td class="{pc}">{pct_s}</td><td>-</td>'
                f'<td>当前持仓（截至 {t["last_date"]}，未实现浮盈随行情更新）</td></tr>')
    pnl = t.get('net_pnl') or 0
    cls = 'up' if pnl > 0 else 'down'
    # ★ ret_pct 为 None 时 '{:+.2f}'.format(None) 会抛 TypeError → 整页 500，故先兜底为 0
    rp = t.get('ret_pct')
    rp = 0 if rp is None else rp
    nm = html.escape(str(t.get('stock_name') or ''))
    cd = str(t.get('stock_code') or '').strip()
    sym = f'<b>{nm}</b>' if not cd else f'<b>{nm}</b> <span class="muted">{cd}</span>'
    return (f'<tr><td>{sym}</td><td class="d-key">{t["buy_date"]}</td><td>{_dash(t["buy_price"])}</td>'
            f'<td class="d-key">{t["sell_date"]}</td><td>{_dash(t["sell_price"])}</td>'
            f'<td>{_dash(t["shares"])}</td><td>{_dash(t["invest"])}</td>'
            f'<td class="{cls}">{pnl:+.2f}</td>'
            f'<td class="{cls}">{rp:+.2f}%</td>'
            f'<td>{_dash(t["hold_days"])}天</td>'
            f'<td>{html.escape(str(t.get("sell_tag") or ""))}</td></tr>')


def daily_table(rows):
    # 换仓日拆成两行展示（rows 为时间倒序：换仓买入在上、卖出成交在下）
    body = ''.join(daily_row(r) for rr in rows for r in _split_switch(rr))
    return ('<table class="daily"><thead><tr><th>日期</th><th>状态</th><th>标的</th><th>评分</th>'
            '<th>现价</th><th>买入参考</th><th>止损</th><th>目标</th><th>触发</th>'
            f'<th>T+1涨跌</th><th>模拟收益</th><th>说明</th></tr></thead><tbody>{body}</tbody></table>')


def trade_table(rows):
    body = ''.join(trade_row(t) for t in rows)
    return ('<table><thead><tr><th>标的</th><th>买入日期</th><th>买入价</th><th>卖出日期</th><th>卖出价</th>'
            '<th>股数</th><th>投入(元)</th><th>净盈亏</th><th>收益率</th><th>持有天数</th>'
            f'<th>卖出原因</th></tr></thead><tbody>{body}</tbody></table>')


def _timing_op(timing):
    """时点标注（如'早盘买/尾盘卖'）→ 具体操作时点描述"""
    if not timing:
        timing = '早盘买/尾盘卖'
    parts = str(timing).split('/')
    if len(parts) != 2:
        return '指数估值收盘后判定，按估值价成交'
    b, s = parts
    bd = ('买入：次日 09:30 开盘价委托' if '早盘' in b
          else ('买入：次日 14:45-14:57 收盘集合竞价' if '尾盘' in b else '买入：估值收盘判定成交'))
    sd = ('卖出：次日 09:30 开盘价委托' if '早盘' in s
          else ('卖出：次日 14:45-14:57 收盘集合竞价' if '尾盘' in s else '卖出：估值收盘判定成交'))
    return f'{bd}；{sd}'


def keypoints_table(daily, max_shown=5, timing=None, sid=None):
    """买卖时点速览：从每日记录中筛出买入/卖出信号与成交日，按时间倒序；
    换仓日（卖出成交+同日换仓买入）拆成两行：换仓买入行在上、卖出成交行在下。"""
    rows = [r for r in daily if r['status'] in KEY_STATES]
    # ★ 2026-09-10 停牌日换仓买入（旧仓挂账、卖出顺延）：该日行 status 被新仓状态覆盖
    #   （持有/卖出信号），速览表按 switch_buy 补一条"换仓买入"行，避免漏记当日换仓买入。
    for _r in daily:
        _sb = _r.get('switch_buy') if isinstance(_r, dict) else None
        if _sb and _r['status'] != '换仓买入':
            _buy = dict(_r)
            _buy.update({'status': '换仓买入',
                         'name': _sb.get('name') or _r.get('name'),
                         'code': _sb.get('code') or _r.get('code'),
                         'deal_price': _sb.get('price'), 'price': _sb.get('price'),
                         'invest': _sb.get('invest'), 'rec_buy': None,
                         'trade_pnl': None, 'trade_pnl_pct': None,
                         'note': str(_sb.get('note') or '')})
            rows.append(_buy)
    if not rows:
        return ''
    # ★ 2026-09-10：上面 switch_buy 补行是 append 到末尾的（不在原日期位），直接 reversed
    #   会让补的旧行压到最新行之上（9/9 补行排在 9/10 前面）→ 统一按日期升序后再倒序。
    rows.sort(key=lambda r: str(r.get('date') or ''))
    rows = list(reversed(rows))  # 最新在上
    # 换仓日拆成两笔展示（卖出成交+换仓买入），计数/切片按拆后操作计，与表内行数一致
    rows_all = [rr for r in rows for rr in _split_switch(r)]
    # ★ 换仓候选独立「买入信号」行（2026-09-09）：最新行 rec_buy 为未决候选时
    # 置顶登记（如 9/8 建仓龙版传媒收盘扫出百大集团 → 表内新增 买入信号：百大集团）
    _cand = _candidate_row(daily[-1]) if daily else None
    if _cand:
        rows_all.insert(0, _cand)
    # ★ 2026-09-10 今日盘中实时触发的买入信号（事件流，含当日**全部**）→ 置顶独立「买入信号」行，
    #   与首页提醒卡/微信同源；收盘确认后引擎落当日行，这些临时行自动被真实行取代。
    for _r in reversed(_intraday_sig_rows(sid)):
        rows_all.insert(0, _r)
    rows = rows_all
    nb = sum(1 for r in rows if r['status'] in BUY_STATES)
    ns = len(rows) - nb
    # ★ T+1 涨跌列：次日停牌（策略下一行 note 提到停牌）时无有效涨跌值，
    #   直接标"停牌"而不是留"-"（如 S16 9/8 龙版传媒 → 9/9 停牌）
    _susp_dates = {str(x.get('date')) for x in daily if '停牌' in str(x.get('note') or '')}

    def _next_susp(r):
        d = str(r.get('date') or '')
        for x in daily:
            xd = str(x.get('date') or '')
            if xd > d:
                return xd in _susp_dates
        return False

    def _t1c_disp(r):
        h = _t1_html(r)
        if h == '-' and _next_susp(r):
            return '<span class="muted" title="次日停牌/无行情，无有效涨跌">停牌</span>'
        return h

    def _kp_row(r):
        if r['status'] == '买入信号':
            op = '买入信号'
        elif r['status'] == '建仓':
            op = '买入成交'
        elif r['status'] == '换仓买入':
            op = '换仓买入'
        elif r['status'] == '买入信号(待成交)':
            op = '买入待成交'
        elif r['status'] == '卖出信号':
            op = '卖出信号'
        else:
            op = '卖出成交'
        actcls = 'ok' if (op.startswith('买入') or op == '换仓买入') else 'sell'
        rec_buy = r.get('rec_buy')
        if rec_buy:
            _ptl = str(r.get('pending_sell') or '')
            if '已换仓' in str(rec_buy):
                rec_td = (f'<td class="rec-buy">🔄 已换仓买入：{_esc_note(str(rec_buy))}'
                          f'<br><span class="muted">同日收盘先卖后买（自动）</span></td>')
            elif _ptl:
                # ★ 2026-09-10 盘中预览行：卖出挂单今日{_ptl}才成交，候选同为今日执行
                rec_td = (f'<td class="rec-buy">⚠ 同日买入推荐：{html.escape(str(rec_buy))}'
                          f'<br><span class="muted">今日{_ptl}先卖后买（待执行）</span></td>')
            else:
                rec_td = (f'<td class="rec-buy">⚠ 同日买入推荐：{html.escape(str(rec_buy))}'
                          f'<br><span class="muted">次日收盘先卖后买</span></td>')
        else:
            rec_td = '<td>-</td>'
        # 价格列：成交行优先显示实际成交价（deal_price；换仓卖出成交行已由 _split_switch 还原为卖出价）
        cell_px = r.get('deal_price')
        if cell_px is None:
            cell_px = r.get('price')
        pnl = r.get('trade_pnl')
        if pnl is not None:
            pcls = 'up' if pnl > 0 else ('down' if pnl < 0 else '')
            pnl_td = f'<td class="{pcls}">{pnl:+.2f}</td>'
        elif op in ('买入成交', '换仓买入') and r.get('invest') is not None:
            pnl_td = f'<td class="muted">投入 {r["invest"]:,.2f}</td>'
        else:
            pnl_td = '<td>-</td>'
        return (f'<tr><td class="st {actcls}">{op}</td>'
                f'<td class="d-key">{r["date"]}</td>'
                f'<td>{html.escape(r["name"]) or "-"}</td>'
                f'<td>{cell_px if cell_px is not None else "-"}</td>'
                f'<td>{_t1c_disp(r)}</td>{pnl_td}{rec_td}'
                f'<td class="note">{_esc_note(r.get("note", ""))}</td></tr>')

    def _parts(rows_):
        # 换仓日拆成两行（rows_ 为时间倒序：换仓买入行在上、卖出成交行在下）
        return ''.join(_kp_row(r) for rr in rows_ for r in _split_switch(rr))

    _theader = ('<table><thead><tr><th>操作</th><th>日期</th><th>标的</th><th>价格**</th>'
                '<th>T+1涨跌*</th><th>模拟收益</th><th>同日买入推荐</th><th>说明</th></tr></thead><tbody>')
    _tlab = timing or '早盘买/尾盘卖'
    head = (f'<div class="card kp"><h3>买卖时点速览（共{len(rows)}次：买入{nb} / 卖出{ns}）'
            f'<span class="muted">信号=当日收盘判定，本策略时点 <b>{html.escape(_tlab)}</b>；成交行价格=实际成交价；默认显示最近{max_shown}条</span></h3>'
            + _theader + _parts(rows[:max_shown]) + '</tbody></table>')
    if len(rows) > max_shown:
        head += (f'<details><summary>查看更多（还有 {len(rows) - max_shown} 条）</summary>'
                 '<div class="tbl-wrap">' + _theader
                 + _parts(rows[max_shown:]) + '</tbody></table></div></details>')
    _pxnote = '*** 价格列：信号行=信号日<b>收盘价</b>，买入/卖出成交行=实际成交价（历史明细中的买入/卖出价即成交价）。'
    if '早盘' in _tlab or '尾盘' in _tlab:
        _bw = '次日<b>开盘</b>' if '早盘' in _tlab.split('/')[0] else '次日<b>收盘</b>'
        _sw = '次日<b>收盘</b>' if '尾盘' in _tlab.split('/')[-1] else '次日<b>开盘</b>'
        _pxnote = (f'*** 价格列：信号行=信号日<b>收盘价</b>，买入成交行={_bw}成交价，卖出成交行={_sw}成交价'
                   '（历史明细中的买入/卖出价即成交价）。')
    return (head + '<p class="muted">* T+1涨跌为<b>事后统计</b>（信号次日<b>开盘</b>相对信号日收盘），'
            '仅用于复盘验证，不参与买卖决策，不构成未来数据依据。'
            '<br>** 模拟收益列：<b>买入成交行</b>=实际投入金额（含费、按各策略单笔预算上限整手买入；S16龙头两档≤max(本金5000,账户/2)），'
            '<b>卖出成交行</b>=本次操作净盈亏（含手续费/印花税）。'
            + _pxnote + '</p></div>')


def strategy_body(s, eq_rng='sim'):
    """单个策略详情页正文（不折叠，全部卡片直接展示）"""
    name = s['name']
    sid = name.split('_')[0]
    # 仅保留模拟交易起点（SIM_START）以来的每日记录与交易；all_daily 用于全历史回测资金曲线
    all_daily = s['daily']
    daily = [r for r in all_daily if r['date'] >= SIM_START]
    trades = _sim_trades(s)
    _tm = str(s.get('timing', '早盘买/尾盘卖'))
    if not daily:
        return (f'<div class="card"><h2>{html.escape(name)} '
                f'<span class="timing-badge">{html.escape(_tm)}</span>'
                f'<span class="muted">▸ {SIM_LABEL}（{SIM_START}）以来暂无数据</span></h2></div>')
    ssum = recalc_summary(trades)
    ssum['cur_status'] = daily[-1]['status']
    last = daily[-1]
    pnl = ssum['total_net_pnl']
    pnl_f = _fixed_pnl(trades)[0]
    pfc = 'up' if pnl_f >= 0 else 'down'
    # 总盈亏 = 已卖出落袋 + 当前持仓浮盈（2026-09-03：浮盈浮亏同样计入策略总盈利）
    _fp = last.get('pos_pnl')
    _fp = _fp if isinstance(_fp, (int, float)) else None
    tot = round(pnl + _fp, 2) if _fp is not None else pnl
    pnl_txt = (f'<span class="{"up" if tot >= 0 else "down"}"><b>{tot:+.2f}</b></span>'
               f'<span class="muted">（含当前持仓浮盈{"%+.2f" % _fp}）</span>' if _fp is not None else
               f'<span class="{"up" if pnl >= 0 else "down"}">{pnl:+.2f}</span>')
    head = (f'<h1>{html.escape(name)} '
            f'<span class="timing-badge">{html.escape(_tm)}</span></h1>'
            f'<p class="muted">▸ {SIM_LABEL}以来已平仓 {ssum["trades"]}笔，'
            f'总盈亏(含浮盈) {pnl_txt}'
            f'<span class="muted">；落袋{ssum["trades"]}笔合计 <span class="{"up" if pnl >= 0 else "down"}">{pnl:+.2f}</span>'
            f'（真实账本口径：收益滚入本金，S16龙头两档单笔≤max(本金5000,账户/2)）</span>'
            f' ｜ 固定5000口径(落袋) <span class="{pfc}">{pnl_f:+.2f}</span>'
            f' 胜率{ssum["win_rate"]}% 均持{ssum["avg_hold_days"]}天 '
            f'当前[{ssum["cur_status"]}]'
            f'<a href="/" style="margin-left:14px">← 返回首页</a></p>')
    lab, acls, why = invest_advice(s)
    head += (f'<div class="inv-advice"><b>投资方式建议：'
             f'<span class="rec-{acls}">{html.escape(lab)}</span></b> '
             f'<span class="muted">{html.escape(why)}</span></div>')
    # 资金口径标注（2026-09-03：S16 龙头接力两档单笔投入由「可用现金全额复利」改为 half-cap 封顶）
    if name in ('S16_龙头接力', 'S16_龙头接力V2'):
        _eq = daily[-1].get('equity')
        if isinstance(_eq, (int, float)):
            _cap = max(CAPITAL, _eq / 2.0)
            head += (f'<div class="fund-note"><b>资金口径（本策略）</b>：初始本金 {CAPITAL:,.0f} 元'
                     f' ｜ 当前账户权益 <b>{_eq:,.2f}</b>'
                     f" ｜ 累计总盈利(含浮盈) <b class=\"{'up' if tot >= 0 else 'down'}\">{tot:+.2f}</b>"
                     f' ｜ 单笔投入本钱上限 = max(本金5000, 账户/2) = <b>{_cap:,.0f}</b>'
                     f'<span class="muted">（盈利未超5000→每笔≤5000；盈利超5000后按账户一半，如账户11000→单笔≤5500；'
                     f'预算未用完现金按年化2%计息，收益不再全额复投）</span></div>')
        else:
            head += (f'<div class="fund-note"><b>资金口径（本策略）</b>：初始本金 {CAPITAL:,.0f} 元，'
                     f'单笔投入 ≤ max(本金5000, 账户/2)，收益不全部滚入复投。</div>')
        # 官方回测口径汇总（真实成交校验：开盘顶板买不进/低开≥0.5%证伪即废弃；2026-09-07 全史重放确认维持放弃式）
        head += ('<div class="official-note"><b>官方回测口径（2026-09-04 起与落地引擎同规则）</b>：'
                 '信号次日若开盘顶板(≥+9.8%)买不进、或低开≥0.5%证伪，该信号<b>废弃、不计成交</b>并记录原因。'
                 '全史重放（2019-01 起·沪深主板 2614 只，数据至 2026-09-15；'
                 '2026-09-15 重放：已修复 8/31 全市场涨跌幅列污染、低开阈值 -3%→-0.5%）同底统计：'
                 '<b>base 537笔 净 +2,464 胜率39.9%</b>（顶板买不进废弃752次/低开317次）；'
                 '<b>V2 471笔 净 +4,256 胜率41.4%</b>（废弃747次/288次）。'
                 '★2026-09-16 口径/护栏修订：全库『振幅/涨跌幅/涨跌额』统一为<b>前复权</b>自算'
                 '（3050 只已改 2792 只，余下为新浪源无足够历史的次新股），除权除息日不再被算成下跌；'
                 '同时实测发现旧"偏差>2pp 且≈次日倒算"污染护栏在全库 qfq 后会在 <b>317 行上巧合误伤</b>'
                 '（真正的错列污染是<b>单日上千只批量</b>发生——8/31 事故 2188 只，而这些误伤行实测单日最多 6 只）'
                 '→ 已改为<b>仅在同日≥50 只批量命中时</b>才纠正，全库实测批量污染日为 0 天。'
                 '★低开阈值由 3% 收紧到 0.5% 的依据：低开是明确负期望子集，收紧后额外剔除的 116/102 笔'
                 '（gap∈(-3%,-0.5%]）合计 -19,200/-18,267 元、均 -166 元，2018~2026 逐年净额全为负，'
                 '而 top5 盈利笔一笔未动；据此 9/15 口径全史由 -16,718 转正为 +1,665（V2 由 -14,554 转正为 +3,070），'
                 '逐年 9/9 改善；9/16 全库 qfq 统一后为上方 +2,464 / +4,256。'
                 '此前按"开盘必成交"回测的 +16.5万/+16.8万，恰是买进了真实世界买不到的顶板龙头，已作废。'
                 '★2026-09-07 曾试点"最强龙头买不进→自动顺延队内第2~5强开盘可买者"：'
                 '2614 只全史重放证明顺延多成交的 562/293 笔净亏约 -26.0万/-23.6万'
                 '（接盘的是开盘即弱于龙头的备选）→ 维持<b>每日只买最强龙头、命中不了即放弃</b>。'
                 '<br><b>每日候选机制</b>：每策略每交易日只选全市场涨停池<b>最强龙头 1 只</b>，命中不了就放弃：'
                 '次日集合竞价(9:15-9:25)以参考价/涨停价限价挂单，开盘未顶板即按开盘价成交；'
                 '开盘顶板买不进或低开≥0.5%则该【昨日信号】判死、记录废弃原因；'
                 '★2026-09-15 起废弃当日仍照常收盘重扫（废弃行与新信号行同日并存，'
                 '避免"昨天的信号判死"连带白丢今天的机会），收盘后按当日涨停池重选次日候选'
                 '（V2 还需连板≥3，无候选日即空仓）。逐日候选/成交/废弃见下方「每日选股与状态」。</div>')
    if last.get('pos_pnl_pct') is not None:
        pc = 'up' if last['pos_pnl_pct'] >= 0 else 'down'
        head += (f'<p class="muted">持仓浮盈 '
                 f'<span class="{pc}">{last["pos_pnl_pct"]:+.2f}%</span></p>')
    card = ''
    if last:
        lc = STATUS_CLASS.get(last['status'], '')
        pos_extra = ''
        if last.get('pos_pnl_pct') is not None:
            pc = 'up' if last['pos_pnl_pct'] >= 0 else 'down'
            pos_extra = (f' 持仓浮盈 <span class="{pc}">{last["pos_pnl_pct"]:+.2f}%</span>'
                         f'<span class="muted">（{last.get("pos_pnl", 0):+.0f}元）</span>')
        lv = _switch_view(last)
        if lv is last:
            # 普通交易日
            card = (f'<div class="card">'
                    f'<p>最新交易日 <b>{last["date"]}</b>：'
                    f'<span class="st {lc}">{html.escape(last["status"])}</span>'
                    f' 标的 <b>{html.escape(last["name"]) or "（指数）"}</b>'
                    f' 现价 {last["price"] if last["price"] is not None else "-"}'
                    f' 买入参考 {last["buy_ref"] if last["buy_ref"] is not None else "-"}'
                    f' 止损 {last["stop"] if last["stop"] is not None else "-"}'
                    f' 目标 {last["target"] if last["target"] is not None else "-"}'
                    f' T+1涨跌 {_t1_html(last)}{pos_extra}</p>'
                    f'<p class="note">{_esc_note(last.get("note", ""))}</p>'
                    f'</div>')
        else:
            # 换仓日（卖出成交+同日换仓买入）：先卖后买两笔分开展示
            spx = lv.get('_sell_px')
            spx_s = (f'<span class="muted">（成交价 {spx:.2f}）</span>'
                     if spx is not None else '')
            card = (f'<div class="card">'
                    f'<p>最新交易日 <b>{last["date"]}</b>：'
                    f'<span class="st sell">卖出成交 {html.escape(str(lv.get("_sell_name") or ""))}</span> {spx_s}'
                    f' → <span class="st ok">换仓买入 {html.escape(lv["name"])}</span>'
                    f'<span class="muted">（买入价 {lv["deal_price"]:.2f}）</span>'
                    f' 现约 {lv["price"] if lv["price"] is not None else "-"}'
                    f'{pos_extra}</p>'
                    f'<p class="note">{_esc_note(last.get("note", ""))}</p>'
                    f'</div>')
    # ★ 换仓候选 / 买入信号提示条（2026-09-09）：引擎在卖出信号日 / 卖出顺延行写入 rec_buy
    # 换仓候选，页面把它从表格角落文案提升为醒目提示——模拟账户受持仓停牌约束未动，真实无持仓者可自行参考。
    try:
        _rb = last.get('rec_buy') if last and isinstance(last, dict) else None
        if _rb and '被' not in str(_rb) and ('已换仓' not in str(_rb) or last.get('switch_buy')):
            _st = str(last.get('status') or '')
            _cn, _, _cs = str(_rb).partition('（')
            _cs = _cs.rstrip('）')
            if _cs.startswith('评分'):
                _cs = _cs[2:]
            _tt = f'<b>{html.escape(str(_cn).strip())}</b>'
            if _cs:
                _tt += f'<span class="muted">（评分 {html.escape(_cs)}）</span>'
            _ptl = str(last.get('pending_sell') or '')
            if last.get('switch_buy'):
                # ★ 2026-09-10 停牌日已照常换仓买入（旧仓挂账 frozen，复牌后按当日时点补卖）
                _sb2 = last['switch_buy'] or {}
                _tt = f'<b>{html.escape(str(_sb2.get("name") or ""))}</b>'
                if _sb2.get('code'):
                    _tt += f'<span class="muted">（{html.escape(str(_sb2.get("code")))}）</span>'
                _hint = ('模拟账本已按信号<b>照常换仓买入</b>该候选：持仓股停牌卖不掉 → 卖出挂单顺延至'
                         '复牌日（信号不废弃），复牌后按当日时点补卖，'
                         '<b>换仓买入不因卖不掉而耽误</b>')
                _hc = '#2fa84f'
            elif _st == '卖出顺延':
                _hint = ('模拟持仓股停牌/无行情致卖出顺延，换仓未执行（挂单保留至复牌日，信号不废弃）；'
                         '该候选<b>今日实际可买</b>——若你未持有该持仓股，可自行参考：开盘顶板(≥+9.8%)买不进'
                         '或低开&gt;3%证伪即放弃，不追高')
                _hc = '#e8a13c'
            elif _ptl:
                # ★ 2026-09-10 尾盘策略盘中预览行：执行时点=今日尾盘（不是"次日开盘"）
                _hint = (f'模拟账本将在<b>今日{_ptl}</b>（本策略时点）先卖出当前持仓、'
                         '再同笔换仓买入该候选（盘中预览，收盘后按正式数据复核；'
                         '开盘顶板买不进或低开&gt;3%证伪则废弃）')
                _hc = '#2b7ae2'
            else:
                _ex = _next_td(last.get('date'))
                _hint = (f'模拟账本将在 {_md(_ex)} 开盘先卖出当前持仓、再换仓买入该候选'
                         '（若届时持仓可正常卖出；开盘顶板买不进或低开&gt;3%证伪则废弃）')
                _hc = '#2b7ae2'
            card += (f'<div style="margin:8px 0 2px;padding:8px 12px;border-left:4px solid {_hc};'
                     f'background:#f3f7ff;border-radius:4px">'
                     f'🎯 <b>换仓候选 / 买入信号提示</b>：{_tt}'
                     f'<span class="muted">（{html.escape(str(last.get("date") or ""))} '
                     f'{"盘中预览" if _ptl else "收盘"}）</span><br>'
                     f'<span class="muted" style="font-size:12px">{_hint}</span></div>')
    except Exception:
        pass
    kp = keypoints_table(daily, timing=_tm, sid=s.get('name'))
    if not kp:
        kp = ('<div class="card kp"><h3>买卖时点速览</h3>'
              f'<p class="muted">{SIM_LABEL}（{SIM_START}）以来暂无买卖时点'
              f'（当前状态：{html.escape(daily[-1]["status"])}）</p></div>')
    # 每日选股：先合并连续空仓/观望段与连续持有（同标的）段为区间行（仅展示层），再默认展示最近10条
    daily_disp = _fold_rows(daily)
    # ★ 2026-09-10 今日盘中实时触发信号 → 「每日选股与状态」置顶（当日全部；收盘确认后由真实行取代）
    _intr_ds = _intraday_sig_rows(s.get('name'))
    if _intr_ds:
        daily_disp = daily_disp + _intr_ds
    shown = list(reversed(daily_disp[-10:]))
    hidden = list(reversed(daily_disp[:-10]))
    daily_html = (f'<div class="card"><h3>每日选股与状态（最近{len(daily)}天）'
                  f'<span class="muted">连续空仓/观望与连续持有（同标的）日已合并为区间行（共{len(shown)}行）；模拟收益列：买入成交行=实际投入金额（含费、按各策略单笔预算上限整手买入；S16龙头两档≤max(本金5000,账户/2)），卖出成交行=本次净盈亏（含手续费/印花税）</span></h3>'
                  f'<div class="tbl-wrap">{daily_table(shown)}</div>')
    if hidden:
        daily_html += (f'<details><summary>查看更早 {len(hidden)} 个交易日</summary>'
                       f'<div class="tbl-wrap">{daily_table(hidden)}</div></details>')
    daily_html += '</div>'
    # 历史交易：按时间倒序（最新在前），默认展开5条，其余藏"查看更多"
    trades_desc = list(reversed(trades))
    shown_trades = trades_desc[:5]
    _open = _open_position(s.get('daily', []))
    if _open:
        # 当前持仓（未平仓）置于明细最上方，避免"平仓后才看得到"的误解
        shown_trades.insert(0, _open)
    more_trades = trades_desc[5:]
    t_extra = ''
    if more_trades:
        t_extra = (f'<details><summary>查看更多（还有 {len(more_trades)} 笔）</summary>'
                   f'<div class="tbl-wrap">{trade_table(more_trades)}</div></details>')
    # 模拟以来收益汇总：仅统计模拟交易起点后的卖出落袋（往年回测不计入收益栏位）
    sim_trades = [t for t in s['trades'] if t.get('sell_date', '') >= SIM_START]
    if sim_trades:
        year_keys = sorted({t['sell_date'][:4] for t in sim_trades})
        year_rows = [(y, _trade_stats([t for t in sim_trades if t['sell_date'].startswith(y)]))
                     for y in year_keys]
        month_keys = sorted({t['sell_date'][:7] for t in sim_trades})
        month_html = []
        for y in sorted({m[:4] for m in month_keys}, reverse=True):
            rows = [(m, _trade_stats([t for t in sim_trades if t['sell_date'].startswith(m)]))
                    for m in month_keys if m.startswith(y)]
            month_html.append(f'<h4>按月（{y}）</h4><div class="tbl-wrap">{_summary_table(rows)}</div>')
        summary_html = ('<div class="card sub"><h3>模拟以来收益汇总'
                        f'<span class="muted">自 {SIM_START}（模拟交易起点）卖出落袋，按卖出月分组；'
                        '往年数据仅用于回测、不计入收益栏位；对照「固定5000口径」见上方总盈亏</span></h3>'
                        f'<div class="tbl-wrap">{_summary_table(year_rows)}</div>'
                        + ''.join(month_html) + '</div>')
    else:
        summary_html = ('<div class="card sub"><h3>模拟以来收益汇总</h3>'
                        f'<p class="muted">{SIM_LABEL}（{SIM_START}）以来暂无落袋交易；'
                        '往年回测数据见「回测答卷」。</p></div>')
    if trades or _open:
        _cnt = f'已平仓{len(trades)}笔'
        if _open:
            _cnt += ' + 持仓中1笔'
        trades_html = (f'<div class="card"><h3>模拟交易明细（{SIM_LABEL}以来，真实账本口径，按时间倒序）'
                       f'<span class="muted">{_cnt}，默认显示最近5条；<b>持仓中</b>=当前未平仓持仓（未实现盈亏随行情更新）；'
                       f'实时跟随请看上方「每日选股与状态」最新一行（买入信号/卖出信号=次日开盘操作）</span></h3>'
                       f'<div class="tbl-wrap">{trade_table(shown_trades)}</div>{t_extra}</div>')
    else:
        trades_html = (f'<div class="card"><h3>模拟交易明细（{SIM_LABEL}以来）</h3>'
                       f'<p class="muted">{SIM_LABEL}（{SIM_START}）以来暂无交易记录。</p></div>')
    return (f'{head}{card}{kp}{daily_html}'
            f'{utilization_html(s)}{risk_html(s)}{equity_chart(all_daily, eq_rng)}{compound_html(s)}{summary_html}{trades_html}')


def sidebar_html(active):
    """左侧菜单：首页 + 回测对照入口 + 各策略 + 手动刷新按钮"""
    strat_items = []
    if HISTORY is not None:
        # 左侧菜单按总盈亏（含当前持仓浮盈，真实账本/复利口径）降序排列
        for _s in sorted(HISTORY['strategies'], key=_sim_total_pnl, reverse=True):
            _sid = _s['name'].split('_')[0]
            _short = _s['name'].split('_', 1)[1] if '_' in _s['name'] else ''
            # 用完整策略名做 URL（quote 编码），避免 S16_龙头接力 / S16_龙头接力V2 前缀冲突
            strat_items.append((f'/s/{urllib.parse.quote(_s["name"])}', f'{_sid} {_short}'))

    def _a(url, label, cls_extra=''):
        cls = ' class="m-item%s%s"' % (cls_extra, ' active' if url == active else '')
        return f'<a href="{url}"{cls}>{html.escape(label)}</a>'

    # ★ 回测对照固定项（S15 可做窗口 gtma20 变体 vs 原版 长期对照）：置于策略列表前便于直达
    menu = _a('/', '🏠 首页')
    menu += _a('/signals', '📡 盘中信号历史', ' lane')
    menu += _a('/s15ab', '🔬 S15 回测对照', ' lane')
    menu += _a('/watch', '👁 空间龙盯盘', ' lane')
    if strat_items:
        menu += '<div class="s-group">策略 · 按累计盈亏↓</div>'
        for url, label in strat_items:
            menu += _a(url, label)
    return ('<aside class="sidebar"><div class="brand">📈 策略选股</div>'
            f'{menu}'
            '<div class="refresh-box"><button onclick="refreshNow(this)">🔄 手动刷新</button></div>'
            '</aside>')


def page_frame(title, active, body, trading):
    """页面外壳：左侧菜单 + 内容区 + 自动刷新（停留当前URL）
    2026-09-03：新增「数据版本」检测——收盘后后台自动重建当日记录完成
    （/api 的 build_ver 变化）时页面自动 reload，无需手动刷新。"""
    refresh_ms = 60000 if trading else 600000
    refresh_js = 'true' if trading else 'false'
    build_ver = BUILD_STATE.get('last_ok') or (HISTORY['generated'] if HISTORY else '')
    watch_js = (
        'var __bv=%s;'
        'setInterval(function(){fetch("/api").then(function(r){return r.json();})'
        '.then(function(d){var v=d.build_ver||"";if(v&&v!==__bv){location.reload();}})'
        '.catch(function(){});},90000);' % json.dumps(build_ver)
    )
    # ★ 2026-09-10 详情页(/s/…)的 <main> 加 .sdetail 类：仅该范围内无 class 的加粗标的走纯黑
    _main_cls = 'content sdetail' if str(active).startswith('/s/') else 'content'
    return ('<!DOCTYPE html>\n<html lang="zh"><head><meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f'<title>{html.escape(title)}</title>\n'
            f'<style>{CSS}</style></head><body>\n'
            f'<div class="layout">{sidebar_html(active)}<main class="{_main_cls}">{body}</main></div>\n'
            f'<script>{JS}</script>\n'
            f'<script>{watch_js}</script>\n'
            f'<script>setTimeout(function(){{if({refresh_js})location.reload();}},{refresh_ms});</script>\n'
            '</body></html>')


def utilization_html(s):
    """资金利用率统计卡片：持仓/空仓/信号天数占比 + 平均持仓时长 + 当前状态"""
    daily = s.get('daily', [])
    trades = s.get('trades', [])
    if not daily:
        return ''
    n = len(daily)
    hold = sum(1 for r in daily if r.get('pos_pnl') is not None)   # 持仓日
    empty = sum(1 for r in daily if r.get('status') == '空仓')     # 空仓日
    signal = n - hold - empty                                      # 信号/待成交等
    hd = []
    for t in trades:
        if t.get('buy_date') and t.get('sell_date'):
            try:
                from datetime import datetime as _dt
                hd.append((_dt.strptime(t['sell_date'], '%Y-%m-%d')
                           - _dt.strptime(t['buy_date'], '%Y-%m-%d')).days)
            except Exception:
                pass
    avg = sum(hd) / len(hd) if hd else 0.0
    mx = max(hd) if hd else 0
    last = daily[-1]
    cur = ('持仓中' if last.get('pos_pnl') is not None
           else '空仓' if last.get('status') == '空仓' else '信号')
    pct = lambda x: f'{x / n * 100:.1f}%'
    rows = (
        f'<tr><td>持仓（有仓位）</td><td>{hold} 天</td><td>{pct(hold)}</td></tr>'
        f'<tr><td>空仓（无仓位）</td><td>{empty} 天</td><td>{pct(empty)}</td></tr>'
        f'<tr><td>信号/待成交</td><td>{signal} 天</td><td>{pct(signal)}</td></tr>'
        f'<tr><td>平均持仓时长</td><td colspan="2">{avg:.1f} 天（最长 {mx} 天）</td></tr>'
        f'<tr><td>当前状态</td><td colspan="2">{cur}</td></tr>'
    )
    return (f'<div class="card"><h3>资金利用率'
            f'<span class="muted">资金闲置程度一目了然；空仓资金按年化2%计理财收益（复利滚动）</span></h3>'
            f'<div class="tbl-wrap"><table><thead><tr><th>指标</th><th>天数</th><th>占交易日比</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>')


def risk_stats(records):
    """风险收益指标（基于每日权益序列）：
    最大回撤 / 年化收益 / 年化波动率 / 年化夏普 / 收益回撤比(Calmar)。
    返回 dict 或 None（样本不足）。无风险利率取 RF_RATE（空仓理财2%口径）。"""
    pts = [(r['date'], r['equity']) for r in records if r.get('equity') is not None]
    if len(pts) < 3:
        return None
    vals = [v for _, v in pts]
    # 最大回撤：从历史峰值到谷底的最大跌幅
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        if peak:
            mdd = max(mdd, (peak - v) / peak)
    # 日收益率序列
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
    n = len(rets)
    start_v, end_v = vals[0], vals[-1]
    ann_ret = (end_v / start_v) ** (TRADING_DAYS / n) - 1 if start_v > 0 else 0.0
    mean_r = sum(rets) / n
    var = sum((x - mean_r) ** 2 for x in rets) / (n - 1) if n > 1 else 0.0
    ann_vol = var ** 0.5 * (TRADING_DAYS ** 0.5)
    # 波动率过低（如长期空仓只吃货基理财，日收益恒定）时夏普无统计意义，标记为 None
    sharpe = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0.005 else None
    calmar = ann_ret / mdd if mdd > 0 else 0.0
    return {'n': len(pts), 'start': pts[0][0], 'end': pts[-1][0],
            'mdd': mdd, 'ann_ret': ann_ret, 'ann_vol': ann_vol,
            'sharpe': sharpe, 'calmar': calmar}


def _sharpe_grade(sh):
    """夏普比率通俗评级：>1 优秀 / 0.5~1 可接受 / 0~0.5 一般 / <0 跑输无风险；
    None=波动率过低（长期空仓吃货基理财）无法评估"""
    if sh is None:
        return 'muted', '几乎无波动（长期空仓/持有货基理财，无评估意义）'
    if sh >= 1.0:
        return 'up', '优秀（>1）'
    if sh >= 0.5:
        return 'up', '可接受（0.5~1）'
    if sh >= 0:
        return 'down', '一般（0~0.5）'
    return 'down', '跑输无风险（<0）'


def risk_html(s):
    """风险收益卡片：最大回撤 / 年化收益 / 夏普，直观回答「风险换收益」"""
    stats = risk_stats(s.get('daily', []))
    if stats is None:
        return ''
    pct = lambda x: f'{x * 100:.2f}%'
    mdd = stats['mdd']
    ann = stats['ann_ret']
    vol = stats['ann_vol']
    sh = stats['sharpe']
    sh_cls, sh_txt = _sharpe_grade(sh)
    sh_disp = '—' if sh is None else f'{sh:.2f}'
    cal = stats['calmar']
    days_n = stats['n']
    # 通俗解读：回撤 X% 意味着本金最高从峰值缩水 X%；年化收益是「如果全年都像这 N 天一样」的折算
    rows = (
        f'<tr><td>年化收益率</td><td class="{"up" if ann >= 0 else "down"}">{pct(ann)}</td>'
        f'<td class="muted">按最近 {days_n} 个交易日（{stats["start"]}~{stats["end"]}）折算</td></tr>'
        f'<tr><td>年化波动率</td><td>{pct(vol)}</td>'
        f'<td class="muted">日收益率的年化标准差，衡量净值上下颠簸程度</td></tr>'
        f'<tr><td>年化夏普比率</td><td class="{sh_cls}"><b>{sh_disp}</b></td>'
        f'<td class="muted">{sh_txt}；每单位波动换来的超额收益（相对无风险2%）</td></tr>'
        f'<tr><td>最大回撤</td><td class="down">{pct(mdd)}</td>'
        f'<td class="muted">期间从最高点跌到谷底的最大亏损幅度（峰值~谷底）</td></tr>'
        f'<tr><td>收益回撤比（Calmar）</td><td class="{"up" if cal >= 0 else "down"}">{cal:.2f}</td>'
        f'<td class="muted">年化收益 ÷ 最大回撤；>1 表示赚得动也扛得住</td></tr>'
    )
    note = ('<p class="note">口径：基于 <b>模拟以来（{sim}起）</b>每日权益序列（含持仓浮盈与空仓理财）。'
            '<b>样本仅 {n} 个交易日，年化数字是把最近这么短一段「全年折算」的参考值，不代表未来能持续——'
            '高年化往往伴随高波动/大回撤，盈亏同源；</b>'
            '夏普&gt;1 才意味着承担的风险换来了对得起它的收益。'
            '解读示例：年化收益 30% + 最大回撤 8%，意味着想拿 30% 就得先扛住 8% 的账面浮亏，'
            '不能只盯着收益不看回撤。</p>').format(sim=SIM_LABEL, n=days_n)
    return (f'<div class="card"><h3>风险收益（风险换收益是否划算）'
            f'<span class="muted">最大回撤 + 夏普比率，判断策略赚的是「实力」还是「胆量」</span></h3>'
            f'<div class="tbl-wrap"><table><thead><tr><th>指标</th><th>数值</th><th>说明</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>{note}</div>')


def _risk_grid(rk):
    """首页策略卡片里的迷你风险指标（年化折算/夏普/最大回撤）"""
    if not rk:
        return '<span>风险 <b>-</b></span>'
    ann_cls = 'up' if rk['ann_ret'] >= 0 else 'down'
    sh = rk['sharpe']
    sh_disp = '—' if sh is None else f'{sh:.2f}'
    sh_cls = 'up' if (sh is not None and sh >= 0.5) else 'down'
    sh_tip = ('每单位波动换来的超额收益（相对无风险2%）；&gt;1 优秀 / 0.5~1 可接受'
              if sh is not None else '波动率过低（长期空仓吃货基理财）无法评估')
    return (f'<span>年化(折算) <b class="{ann_cls}" '
            f'title="把模拟以来 {rk["n"]} 个交易日收益按年折算，样本小仅参考">'
            f'{rk["ann_ret"] * 100:+.1f}%</b></span>'
            f'<span>夏普 <b class="{sh_cls}" title="{sh_tip}">'
            f'{sh_disp}</b></span>'
            f'<span>最大回撤 <b class="down" title="期间峰值到谷底最大跌幅">'
            f'{rk["mdd"] * 100:.1f}%</b></span>')


def _sid_short(s):
    s = str(s or '')
    return s.split('_')[0] + ((' ' + s.split('_', 1)[1]) if '_' in s else '')


def _cross_conflict_html(today):
    """★ 2026-09-10 跨策略冲突提示：同一标的同一天被不同策略给出**相反**操作（一买一卖）。

    各策略是独立账本（每策略本金 5,000）、独立信号，互不覆盖——模拟盘可以同时"某策略买、
    另一策略卖"，但实盘只有一个账户，必须二选一，故在此显式提示，避免照单全做。
    """
    try:
        acts = {}
        for s in (HISTORY or {}).get('strategies', []):
            dl = s.get('daily') or []
            if not dl:
                continue
            last = dl[-1]
            if str(last.get('date') or '') != today:
                continue
            sid = s.get('name') or ''
            nm = str(last.get('name') or '')
            st = str(last.get('status') or '')
            # 换仓买入的标的两个来源：
            #   ① 结构化 switch_buy 字段（仅停牌换仓 from_frozen 分支写）
            #   ② note 里的"；同日X换仓买入Y N股（投入Z元）"（普通换仓，与展示层同源的 _SW_BUY_RE）
            _sb = last.get('switch_buy') if isinstance(last.get('switch_buy'), dict) else None
            if _sb and _sb.get('name'):
                acts.setdefault(str(_sb['name']), {}).setdefault('buy', []).append(
                    (_sid_short(sid), _sb.get('price'), '换仓买入'))
            _sw = _SW_BUY_RE.search(str(last.get('note') or ''))
            if _sw:
                acts.setdefault(str(_sw.group(3)).strip(), {}).setdefault('buy', []).append(
                    (_sid_short(sid), _sw.group(5), '投入'))
            if nm:
                if st in ('建仓', '换仓买入'):
                    acts.setdefault(nm, {}).setdefault('buy', []).append(
                        (_sid_short(sid), last.get('invest'), '投入'))
                elif st in ('卖出成交', '卖出信号', '卖出顺延'):
                    # ★ 换仓日原始行的 deal_price 存的是**新仓买入价**（如 S15 9/10 是 15.59=特力Ａ），
                    #   卖出价须按展示层同源方式还原（_split_switch：sell_price 字段 → note 里的平仓价）
                    _sp = last.get('deal_price')
                    if st == '卖出成交':
                        _sl = [x for x in _split_switch(last) if x.get('status') == '卖出成交']
                        if _sl:
                            _sp = _sl[0].get('deal_price')
                    acts.setdefault(nm, {}).setdefault('sell', []).append(
                        (_sid_short(sid), _sp, '成交价'))
        out = ''
        for nm, d in acts.items():
            if not (d.get('buy') and d.get('sell')):
                continue

            def _fmt(items, kind):
                parts = []
                for sid, v, lab in items:
                    tail = ''
                    if v is not None:
                        tail = ('（%s %s 元）' % (lab, ('%s' % v))) if kind == 'buy' else ('（%s %s 元）' % (lab, v))
                    parts.append(html.escape(sid) + tail)
                return '、'.join(parts)

            out += (f'<p class="st warn">⚠️ <b>跨策略冲突</b>：同一标的 <b>{html.escape(nm)}</b> '
                    f'今日被反向操作 —— <b class="st ok">买入：{_fmt(d["buy"], "buy")}</b>'
                    f' ／ <b class="st sell">卖出：{_fmt(d["sell"], "sell")}</b>。'
                    '各策略为<b>独立账本</b>（每策略本金 5,000 元）、信号互不覆盖，'
                    '模拟盘可同时成立；实盘只有一个账户，需<b>自行二选一</b>'
                    '（建议：跟随你主跟的那条策略，或按"本策略时点先到先执行"处理）。</p>')
        return out
    except Exception:
        return ''


def today_action_card():
    """★ 首页「今日行动清单」卡（2026-09-03）：每天（尤其早盘）/每次刷新先看这一张卡——
    今天到底要做什么，一屏说清：
    · 待执行（昨日收盘确认的信号 → 今日 T+1 按策略时点买入/卖出）
    · 今日已发生（收盘后模拟回放已滚入的行，如已建仓/已卖出）
    · 明日预告（今日盘中新触发/今日收盘才确认的信号，避免和"今天"混淆）
    · 其余策略当前状态（持仓 XX / 空仓等信号）汇总
    数据源：HISTORY 各策略 daily 最新行 + 统一 TODAY_STATUS（与首页提醒卡/详情页提示条同源）。"""
    if HISTORY is None or not HISTORY.get('strategies'):
        return ''
    try:
        today = datetime.date.today().isoformat()
        _act, _done, _fut, _hold, _idle = [], [], [], [], []  # (kind, html)
        _ref = ''

        def _hb(nm_html, cd_html=''):
            """★ 2026-09-17 用户要求：行动清单「持仓」区标的名加粗，与策略名/说明文字区分。
            入参须已 html.escape（本函数只负责加 <b>，不做转义）。"""
            return f'<b>{nm_html}</b>' + (f'（{cd_html}）' if cd_html else '')

        def _fp(r):
            """★ 2026-09-22：持仓浮盈片段（数据层 row['pos_pnl_pct']，无则空串）。

            此前只有 status='持有' 这一支拼了浮盈，'换仓买入'（显示成"持仓（换仓后）"）、
            '建仓'（非当日）、停牌顺延等持仓分支都不带 → 换仓当天及之后的周末/节假日期间
            首页看不到浮盈，用户误以为"这条没数据"。数据其实一直有（引擎按当日持仓计算）。

            ★ frozen（旧仓停牌卖不掉、挂账待复牌）时 row 的 pos_pnl* 只反映**新仓**，
            旧仓浮盈在 frozen 里单独估值，此处不显示以免误导。"""
            if r.get('frozen_sell') or r.get('frozen_sold'):
                return ''
            p = r.get('pos_pnl_pct')
            if not isinstance(p, (int, float)):
                return ''
            return f' 浮盈{p:+.1f}%'

        for s in HISTORY['strategies']:
            daily = s.get('daily') or []
            if not daily:
                continue
            last = daily[-1]
            L = str(last.get('date') or '')
            _ref = L if L > _ref else _ref
            lv = _switch_view(last)
            st = str((lv or last).get('status') or '')
            _cd_raw = str((lv or last).get('code') or '')
            nm = html.escape(str((lv or last).get('name') or ''))
            cd = html.escape(_cd_raw)
            sc = last.get('score')
            sc_txt = f' 评分{sc}' if sc is not None else ''
            sid = s['name'].split('_')[0]
            short = html.escape(s['name'].split('_', 1)[1])
            link = f'<a href="/s/{urllib.parse.quote(s["name"])}">{sid} {short}</a>'
            tm = html.escape(str(s.get('timing') or ''))
            head = f'{link}<span class="muted">（{tm}）</span>'
            # ★ 2026-09-10 停牌挂账（跨日）：本策略持仓区统一兜底 —— 只要还有
            #   未了结的 frozen_sell（停牌卖不掉、卖出挂单顺延中），就一直提示旧仓挂账，
            #   不因次日新行（新仓持有/卖出信号）落地而消失。
            _h0 = len(_hold)

            def _fz_line():
                _fzr, _fs2 = signal_rows.frozen_open(daily)
                if not _fs2:
                    return
                _fznm = str(_fs2.get('name') or '')
                if any(_fznm and _fznm in str(_ln) for _ln in _hold[_h0:]):
                    return          # 本策略已有持仓行提到该标的（如"卖出顺延"行）→ 不重复
                _hold.append(f'{head} 另持仓（停牌挂账）'
                             f'{_hb(html.escape(_fznm), html.escape(str(_fs2.get("code") or "")))}'
                             f'——自 '
                             f'{_md(str(_fs2.get("since") or _fzr.get("date") or ""))} 起停牌，'
                             '卖出挂单顺延至复牌日（信号不废弃，复牌后按当日时点补卖）')
            # ★ 今日(L==today)回放行记录的是"当日已发生的成交"：卖出成交/建仓/换仓买入
            # 必须按动作列出（换仓日拆出『已买入新标的 + 已卖出旧标的』两条），
            # 不能沿用 _switch_view 的"持仓(换仓后)"口径——否则当天换仓买入会被
            # 归入持仓组，首页误报"今日无需买卖操作"（2026-09-03 新赛股份换仓案例）。
            if L == today and (str(last.get('status') or '') in ('卖出成交', '建仓', '换仓买入')
                               or last.get('switch_buy')):
                # 先卖后买：引擎真实顺序是先平仓旧标的、再按推荐换仓买入新标的
                for _p in reversed(_split_switch(last)):
                    _ps = str(_p.get('status') or '')
                    if _ps not in ('卖出成交', '建仓', '换仓买入'):
                        continue  # 停牌日换仓：原行状态被新仓覆盖（持有/卖出信号），动作由 switch_buy 补
                    _pnm = html.escape(str(_p.get('name') or ''))
                    _pcd = html.escape(str(_p.get('code') or ''))
                    if _ps == '卖出成交':
                        _sp = _p.get('deal_price')
                        _ab = _p.get('abandon_note')
                        _done.append(('sell', f'{head} → <b class="st sell">{_md(L)}已卖出 {_pnm}（{_pcd}）</b>'
                                            + (f'（成交价 {_sp} 元）' if _sp is not None else '')
                                            + (f'<br><span class="muted">⚠ {html.escape(_ab)}</span>' if _ab else '')))
                    else:  # 建仓 / 换仓买入 → 当日买入动作
                        _inv = _p.get('invest')
                        _tag = '（换仓买入）' if _ps == '换仓买入' else ''
                        _done.append(('buy', f'{head} → <b class="st ok">{_md(L)}已买入 {_pnm}（{_pcd}）{_tag}</b>'
                                           + (f'（投入 {_inv:,.2f} 元）' if _inv is not None else '')))
                # ★ 2026-09-10 停牌日换仓：旧仓卖不掉（挂账待复牌补卖），换仓买入照常执行——
                #   当日行 status 会被新仓状态覆盖（持有/卖出信号），故按 switch_buy 补一条"已买入"。
                _sb = last.get('switch_buy') or {}
                if _sb and str(last.get('status') or '') not in ('卖出成交', '建仓', '换仓买入'):
                    _sb_nm = html.escape(str(_sb.get('name') or ''))
                    _sb_cd = html.escape(str(_sb.get('code') or ''))
                    _done.append(('buy', f'{head} → <b class="st ok">{_md(L)}已买入 {_sb_nm}（{_sb_cd}）'
                                         '（换仓买入）</b>'
                                         + (f'（投入 {_sb["invest"]:,.2f} 元）'
                                            if _sb.get('invest') is not None else '')
                                         + '<span class="muted">（持仓股停牌卖不掉，卖出顺延至复牌日，'
                                           '换仓买入照常执行）</span>'))
                _fz_line()          # 挂账旧仓提示（跨日兜底，覆盖当日与更早的 frozen_sell）
                continue
            # ★ 2026-09-10 停牌顺延后的复牌补卖：当日已完成卖出（当日可能同时持有换仓买来的新仓）
            _fsd = last.get('frozen_sold')
            if _fsd and L == today:
                _done.append(('sell', f'{head} → <b class="st sell">{_md(L)}已卖出 '
                                      f'{html.escape(str(_fsd.get("name") or ""))}'
                                      f'（{html.escape(str(_fsd.get("code") or ""))}）</b>'
                                      + (f'（成交价 {_fsd.get("sell_price")} 元）'
                                         if _fsd.get('sell_price') is not None else '')
                                      + '<span class="muted">（停牌顺延后复牌补卖，未影响换仓）</span>'))
            # ★ 2026-09-10 尾盘策略盘中预览行（status=持有 + pending_sell）：今日尾盘才成交，
            #   必须列入「今日需执行」并带上同笔换仓买入——否则首页只显示"持有(不动)"，
            #   今日待执行的卖出+买入全丢失（案例：S1 9/10 尾盘卖爱丽家居 + 换仓买深粮控股）。
            if L == today and st == '持有' and last.get('pending_sell'):
                _tl = str(last.get('pending_sell') or '收盘')
                _cn, _ct, _cc = _parse_rec_buy(last.get('rec_buy'))
                _act.append(('sell', f'{head} → <b class="st sell">{_md(today)}{_tl}卖出 {nm}（{cd}）</b>'
                                    f'：{_md(L)} 收盘判定卖出，{_md(today)}{_tl}按本策略时点离场'
                                    + (f'；卖出成交后同笔资金换仓买入 {_cn}' if _cn else '')))
                if _cn:
                    _act.append(('buy', f'{head} → <b class="st ok">同笔换仓买入 {_cn}'
                                        f'{("（" + _cc + "）") if _cc else ""}{_ct}</b>'
                                        f'：卖出成交后同一{_tl}时段买入（模拟先卖后买、资金接续）'))
                continue
            if st in ('买入信号', '买入信号(待成交)'):
                ex = _next_td(L)
                if ex == today and _is_suspended_today(_cd_raw, today):
                    _fut.append(('buy', f'{head} → <b class="st ok">{_md(ex)}买入 {nm}（{cd}）{sc_txt}</b>'
                                        f'：⚠ 标的今日停牌（无成交），买入挂单顺延至复牌日，信号不废弃'))
                elif ex == today:
                    # ★ 实盘证伪：静态推荐在执行日必须由实盘修正（炸板/买不进）
                    _dn = daban_realtime_note(_cd_raw, today)
                    _dn_html = ''
                    if _dn:
                        _col = {'bad': '#c62828', 'warn': '#e67e00'}.get(_dn[0], '#2e7d32')
                        _ico = '⚠' if _dn[0] in ('bad', 'warn') else '●'
                        _dn_html = ('<br><span style="color:%s;font-weight:600">%s %s</span>'
                                    % (_col, _ico, html.escape(str(_dn[1]))))
                    _act.append(('buy', f'{head} → <b class="st ok">{_md(ex)}买入 {nm}（{cd}）{sc_txt}</b>'
                                        f'：{_md(L)} 收盘信号确认，{_md(ex)}按本策略时点成交，不追价' + _dn_html))
                else:
                    _fut.append(('buy', f'{head} → <b class="st ok">{_md(ex)}买入 {nm}（{cd}）{sc_txt}</b>'
                                        f'：{_md(L)} 收盘确认，{_md(ex)}按本策略时点执行'))
            elif st == '卖出信号':
                ex = _next_td(L)
                if ex == today and _is_suspended_today(_cd_raw, today):
                    _cn, _ct, _cc = _parse_rec_buy(last.get('rec_buy'))
                    if _cn:
                        # ★ 2026-09-10 用户要求：卖不掉也不能耽误换仓、耽误信号 →
                        #   持仓股今日停牌卖不掉：卖出挂单顺延至复牌日，但换仓买入照常列入今日执行
                        _act.append(('buy', f'{head} → <b class="st ok">{_md(today)}换仓买入 {_cn}'
                                            f'{("（" + _cc + "）") if _cc else ""}{_ct}</b>'
                                            '：持仓股今日停牌卖不掉，卖出挂单顺延至复牌日（信号不废弃），'
                                            '但换仓买入照常按本策略时点执行——不因卖不掉而耽误信号'))
                        _hold.append(f'{head} 持仓 {_hb(nm, cd)}——标的今日停牌（无成交），'
                                     '卖出挂单顺延至复牌日；换仓买入照常执行' + _fp(last))
                    else:
                        _hold.append(f'{head} 持仓 {_hb(nm, cd)}——标的今日停牌（无成交），'
                                     f'{_md(ex)}卖出挂单顺延至复牌日（信号不废弃，详见详情）' + _fp(last))
                elif ex == today:
                    _act.append(('sell', f'{head} → <b class="st sell">{_md(ex)}卖出 {nm}（{cd}）</b>'
                                          f'：{_md(L)} 收盘判定卖出，{_md(ex)}按本策略时点离场'))
                else:
                    _fut.append(('sell', f'{head} → <b class="st sell">{_md(ex)}卖出 {nm}（{cd}）</b>'
                                          f'：{_md(L)} 收盘判定卖出，{_md(ex)}按本策略时点离场'))
            elif st == '建仓':
                if L == today:
                    inv = last.get('invest')
                    _done.append(('buy', f'{head} → <b class="st ok">{_md(L)}已买入 {nm}（{cd}）</b>'
                                         + (f'（投入 {inv:,.2f} 元）' if inv is not None else '')))
                else:
                    _hold.append(f'{head} 持仓 {_hb(nm)}' + _fp(last))
            elif st == '换仓买入':
                _hold.append(f'{head} 持仓（换仓后）{_hb(nm, cd)}' + _fp(lv or last))
            elif st == '卖出成交':
                if L == today:
                    _done.append(('sell', f'{head} → <b class="st sell">{_md(L)}已卖出 {nm}</b>'))
                else:
                    _idle.append(f'{head} 空仓（{_md(L)} 已卖出）')
            elif st == '卖出顺延':
                if last.get('frozen_sell'):
                    _hold.append(f'{head} 持仓 {_hb(nm, cd)}——持仓股停牌、卖出挂单顺延至复牌日；'
                                 '换仓买入已照常执行（详见详情）')
                else:
                    _hold.append(f'{head} 持仓 {_hb(nm, cd)}——持仓股停牌、卖出信号顺延至复牌日'
                                 '（详见详情）' + _fp(last))
            elif st == '持有':
                _hold.append(f'{head} 持仓 {_hb(nm)}' + _fp(last))
            else:
                _idle.append(f'{head} ' + {'空仓': '空仓等信号', '观望': '观望',
                                           '资金不足': '资金不足',
                                           '信号废弃': '昨日买入信号已废弃（未买入）'}.get(st, '空仓等信号'))
            _fz_line()          # ★ 跨日兜底：旧仓仍挂账（停牌卖不掉）就持续在持仓区提示
        # 今日盘中新触发（★ 2026-09-10 起取当日**全部**盘中信号事件流，收盘确认后明日才执行，
        #   与"今日要买什么"区分开；此前只取最后一条，当天更早触发的标的会被漏掉）
        rot_sig = ''
        ex = _next_td(today)
        _closed_td = datetime.datetime.now().hour >= 15   # ★ 收盘后不再称"盘中触发"
        for _r in _intraday_sig_rows():
            _rot = str(_r.get('sid') or '')
            if not _rot:
                continue
            dup = False
            rs = next((x for x in HISTORY['strategies'] if x['name'] == _rot), None)
            if rs and rs.get('daily'):
                rl = rs['daily'][-1]
                if (str(rl.get('date')) == today and rl.get('status') in ('买入信号', '买入信号(待成交)')
                        and str(rl.get('name')) == str(_r.get('name'))):
                    dup = True  # 收盘后今日行已体现同标的买入信号，不重复提示
            if dup:
                continue
            _rsid = _rot.split('_')[0]
            _rshort = html.escape(_rot.split('_', 1)[1]) if '_' in _rot else ''
            rot_sig += (f'<p class="st warn">⚠️ {_md(today)}'
                        f'{"收盘触发" if _closed_td else "盘中触发"}（{_md(ex)}执行，勿提前操作）：'
                        f'<b>{_rsid} {_rshort}</b> {html.escape(str(_r.get("name") or ""))}'
                        f'（{html.escape(str(_r.get("code") or ""))}）'
                        f'{"收盘价" if _closed_td else "现价"} {_r.get("price", "—")} 评分 {_r.get("score", "—")} —— '
                        f'{"已按收盘价确认" if _closed_td else "收盘确认后"}按该策略时点在 {_md(ex)} 执行（T+1）。</p>')
        h = (f'<div class="card" style="border-left:4px solid #2b7ae2">'
             f'<h2>📋 今日行动清单 · {_md(today)}'
             '<span class="muted">——早盘/每次刷新先看这里：按日期核对买/卖/持仓动作</span></h2>')
        if rot_sig:
            h += rot_sig
        # ★ 2026-09-10 跨策略冲突：同一标的今日一买一卖（模拟盘各自成立，实盘须二选一）
        _cf = _cross_conflict_html(today)
        if _cf:
            h += _cf
        if not _act and not _done:
            h += f'<p class="st ok">✅ {_md(today)}无需买卖操作'
            if _hold:
                h += f'：持仓中 {len(_hold)} 项按兵不动'
            if _idle:
                h += f'，空仓 {len(_idle)} 项继续等信号'
            h += '。</p>'
        else:
            if _act:
                n_buy = sum(1 for k, _ in _act if k == 'buy')
                n_sell = sum(1 for k, _ in _act if k == 'sell')
                h += (f'<p class="st warn">🔔 {_md(today)}需执行 {len(_act)} 项'
                      f'（买入 {n_buy} / 卖出 {n_sell}）：</p>')
                for _, x in _act:
                    h += f'<p style="margin:2px 0">{x}</p>'
            if _done:
                n_buy = sum(1 for k, _ in _done if k == 'buy')
                n_sell = sum(1 for k, _ in _done if k == 'sell')
                h += (f'<p class="st ok">✅ {_md(today)}今日已完成 {len(_done)} 项'
                      f'（买入 {n_buy} / 卖出 {n_sell}）：</p>')
                for _, x in _done:
                    h += f'<p style="margin:2px 0">{x}</p>'
        if _fut:
            h += '<p class="note">🕘 以下为信号已确认、<b>执行日在未来交易日</b>（各行动行已标执行日期，勿提前操作）：</p>'
            for _, x in _fut:
                h += f'<p style="margin:2px 0" class="muted">{x}</p>'
        if _hold:
            h += '<p class="muted" style="margin-top:8px"><b>持仓 ' + str(len(_hold)) + '</b>：</p>'
            for _x in _hold:
                h += f'<p class="muted" style="margin:1px 0 1px 14px">{_x}</p>'
        if _idle:
            h += '<p class="muted"><b>空仓 ' + str(len(_idle)) + '</b>：</p>'
            for _x in _idle:
                h += f'<p class="muted" style="margin:1px 0 1px 14px">{_x}</p>'
        h += ('<p class="note">口径：收盘确认的买卖信号 → 按各策略 T+1 时点在执行日成交（各行动行已标日期）；'
              '各策略状态取最近已收盘的每日记录'
              + (f'（{_md(_ref)}）' if _ref else '')
              + '，盘中刷新时该日成交行尚未生成，收盘后自动回放补齐并刷新本页。'
              '点击策略名进入详情查看买卖时点与明细。</p></div>')
        return h
    except Exception:
        return ''


def yesterday_signal_card():
    """昨日轮换策略信号待执行提醒卡：昨日收盘触发买入信号、今日(T+1)应执行时，
    在首页置顶提醒（点击直达策略详情页）。若昨日未触发或无记录则返回空。"""
    if HISTORY is None or not STATE.get('strategy'):
        return ''
    try:
        today = datetime.date.today()
        y_date = today - datetime.timedelta(days=1)
        y_str = y_date.strftime('%Y-%m-%d')
        y_sname = advisor.today_strategy(y_date)
        if y_sname == STATE.get('strategy'):
            return ''  # 今日仍是同一策略，今日卡片已覆盖
        s = next((x for x in HISTORY['strategies'] if x['name'] == y_sname), None)
        if s is None:
            return ''
        # ★2026-09-15 同一天可能出现两行（「信号废弃」行 + 当日收盘扫出的新信号行）→
        #   取当日**最后**一行 = 最新决策，避免命中前一条废弃行而漏掉新信号
        yrec = next((r for r in reversed(s['daily']) if r['date'] == y_str), None)
        if yrec is None or not yrec.get('triggered'):
            return ''
        # 今日(T+1)是否已按昨日信号买入成交（引擎已模拟：状态为建仓/买入成交 或 有 invest 字段）
        t_str = today.strftime('%Y-%m-%d')
        trec = next((r for r in reversed(s['daily']) if r['date'] == t_str), None)
        done = bool(trec and (trec.get('status') in ('建仓', '买入成交') or trec.get('invest') is not None))
        _tm = str(s.get('timing', ''))
        _sid = s['name'].split('_')[0]
        _short = s['name'].split('_', 1)[1]
        if done:
            st_cls, st_txt = 'ok', f'今日已按昨日信号买入成交（模拟，投入 {trec.get("invest"):,.2f} 元）'
        else:
            st_cls, st_txt = 'warn', '今日待执行：按本策略时点买入（T+1，勿手动追价，等策略时点）'
        st_txt_html = html.escape(st_txt)
        return (f'<div class="card" style="border-left:4px solid #e8a13c;cursor:pointer" '
                f'onclick="location.href=\'/s/{urllib.parse.quote(s["name"])}\'">'
                f'<h2>昨日信号提醒（{y_str} {html.escape(_sid)} {html.escape(_short)}）'
                f'<span class="muted">（时点：{html.escape(_tm) or "次日按策略时点执行"}）</span></h2>'
                f'<p class="st {st_cls}">{st_txt_html}</p>'
                f'<p class="note">昨日收盘触发买入信号：{html.escape(str(yrec.get("name") or ""))}'
                f'（评分 {_dash(yrec.get("score"))}）；{html.escape(str(yrec.get("note", "")))}'
                f'。点击进入策略详情页查看昨日信号与今日成交。</p></div>')
    except Exception:
        return ''


def today_alert_card():
    """★ 首页顶部「今日盘中提醒」卡（2026-09-03）：
    数据源 = 统一 TODAY_STATUS（与微信推送同一份），展示：
    今日轮换策略 / 大盘风险（形态·涨跌·明日预判）/ 触发状态 / 判断数据时点。"""
    ts = TODAY_STATUS
    if not ts:
        return ''
    try:
        m = ts.get('market') or {}
        sig = ts.get('signal') or {}
        alerts = ts.get('alerts') or []
        asof = ts.get('asof') or {}
        # 大盘：上证点位/涨跌 + 形态 + 明日预判
        chg = m.get('chg')
        chg_cls = 'up' if (chg or 0) >= 0 else 'down'
        chg_txt = '—' if chg is None else '%+.2f%%' % chg
        mkt_bits = ['上证 %s <b class="%s">%s</b>' % (m.get('close') or '—', chg_cls, chg_txt),
                    '形态 %s' % html.escape(str(m.get('label') or '—'))]
        if m.get('verdict'):
            prob = '' if m.get('prob') is None else '（%.0f%%）' % m['prob']
            mkt_bits.append('%s 预判 <b>%s</b>%s' % (_md(m.get('target')), html.escape(str(m['verdict'])), prob))
        # 触发状态
        if not sig:
            sig_cls, sig_txt = 'gray', '今日暂无候选（未扫描/无信号）'
        else:
            nm = html.escape(str(sig.get('name') or ''))
            cd = html.escape(str(sig.get('code') or ''))
            px = sig.get('price') if sig.get('price') is not None else '—'
            sc = sig.get('score') if sig.get('score') is not None else '—'
            head = '<b>%s</b>（%s） 现价 %s 评分 %s' % (nm, cd, px, sc)
            if sig.get('triggered'):
                sig_cls = 'ok'
                sig_txt = head + ' —— <b>已触发买入信号</b>（收盘确认后，下一交易日 T+1 按策略时点执行）'
            else:
                sig_cls = 'warn'
                sig_txt = head + ' —— 接近买入（未触发），按规则不买入、继续观察'
        alert_html = ''
        if alerts:
            alert_html = '<p class="st sell">⚠ 风险提示：%s</p>' % html.escape('；'.join(alerts))
        env = ts.get('env') or ''
        env_badge = ('<span class="timing-badge">行情环境 %s</span>'
                     % html.escape(env)) if env else ''
        return ('<div class="card" style="border-left:4px solid #3d7eff">'
                '<h2>📢 今日盘中提醒'
                '<span class="muted">（%s ｜ 更新 %s ｜ 与微信推送同一数据源）</span></h2>'
                '<div class="strat-grid">'
                '<span>今日轮换 <b>%s</b>%s</span>'
                '<span>大盘 %s</span>'
                '<span>触发状态 <span class="st %s">%s</span></span>'
                '<span>判断数据 <b>%s</b></span>'
                '</div>%s'
                '<p class="note">本卡与微信推送（盘中信号 / 收盘简报）读同一份今日状态；'
                '信号收盘后判定、T+1 按策略时点执行，盘中不临时改单、不追价。</p></div>'
                % (html.escape(str(ts.get('date') or '')),
                   html.escape(str(ts.get('updated') or '—')),
                   html.escape(str(ts.get('rotation') or '—')), env_badge,
                   ' ｜ '.join(mkt_bits), sig_cls, sig_txt,
                   html.escape(str(asof.get('label') or '本地数据')), alert_html))
    except Exception:
        return ''


def strategy_today_bar(s):
    """★ 策略详情页顶部「今日状态与操作注意」提示条（2026-09-03）：
    数据源与首页「今日盘中提醒」卡一致（TODAY_STATUS + 本策略最新 daily 记录）。"""
    if s is None:
        return ''
    try:
        import history_engine
        daily = s.get('daily') or []
        last = daily[-1] if daily else {}
        lv = _switch_view(last) if last else {}
        _st = str((lv or {}).get('status') or last.get('status') or '—')
        lc = STATUS_CLASS.get(_st, 'gray')
        _d0 = str(last.get('date') or '')
        _dcn = _md(_d0) if _d0 else '最新'
        _nd = _next_td(_d0) if _d0 else None
        if _st == '买入信号':
            act = (f'{_md(_nd)} 开盘买入' if _nd else '下一交易日开盘买入')
            act_cls = 'ok'
        elif _st == '卖出信号':
            act = (f'{_md(_nd)} 开盘卖出' if _nd else '下一交易日开盘卖出')
            act_cls = 'sell'
        elif _st == '持有':
            act, act_cls = '持有（不动）', 'hold'
        elif _st == '换仓买入':
            act, act_cls = '已换仓买入（持仓中）', 'ok'
        elif _st in ('空仓', '观望'):
            act, act_cls = '空仓等待信号', 'gray'
        else:
            act, act_cls = '按最新状态处理', 'gray'
        ts = TODAY_STATUS or {}
        is_rot = ts.get('rotation') == s['name']
        rot_txt = ('<b class="ok">是（今日轮换策略）</b>' if is_rot
                   else '<span class="muted">否</span>')
        env_fit = ''
        _fit = history_engine.STRATEGY_ENV_FIT.get(s['name'])
        if _fit is not None:
            _env = ts.get('env') or 'range'
            _ok = _env in _fit
            env_fit = ('<span>行情适配 <b class="%s">%s（%s）</b></span>'
                       % ('ok' if _ok else 'sell', '适配' if _ok else '不适配',
                          html.escape(str(_env))))
        nm = html.escape(str((lv or {}).get('name') or '（指数）'))
        tm = html.escape(str(s.get('timing') or '早盘买/尾盘卖'))
        # ★ 今日盘中信号（2026-09-10 起取本策略当日**全部**事件，与首页提醒卡/微信同源）：
        #   避免"首页/邮件提醒过、详情页却看不到"（用户反馈的华金资本即属此类）
        sig_html = ''
        # ★ 2026-09-10：收盘（15:00）后当日信号已按收盘价确认，不再挂"待收盘确认/未确认则消失"口径
        _closed_now = datetime.datetime.now().hour >= 15
        for _r in _intraday_sig_rows(s['name']):
            _nm = html.escape(str(_r.get('name') or '—'))
            _cd = html.escape(str(_r.get('code') or ''))
            _px = _r.get('price') if _r.get('price') is not None else '—'
            _sc = _r.get('score') if _r.get('score') is not None else '—'
            _note = str(_r.get('note') or '')
            if _closed_now:
                _note = (_note.replace('收盘价确认后于', '今日收盘已确认，于')
                              .replace('；收盘未确认则本行自动消失', ''))
            # ★ 详情页模板已单独给出「收盘价/现价 + 评分」，去掉 note 尾部重复的明细段
            _m = re.search(r'：(?:现价|收盘价)[^：]*$', _note)
            if _m:
                _note = _note[:_m.start()]
            sig_html += (f'<p class="note">📌 今日{"收盘" if _closed_now else "盘中"}信号'
                         f'（{_md(str(_r.get("date") or ""))}）：'
                         f'<b>{_nm}（{_cd}）</b> {"收盘价" if _closed_now else "现价"} {_px} 评分 {_sc} —— '
                         f'{html.escape(_note)}；'
                         '上方各卡片为<b>模拟记录</b>（截止 '
                         f'{html.escape(str(last.get("date") or "—"))} 最近收盘），'
                         + ('今日信号已按收盘价确认，下一交易日（T+1）执行后计入「每日选股与状态」。'
                            if _closed_now else '今日信号待收盘确认后计入「每日选股与状态」。') + '</p>')
        # ★ S15 可做窗口标注（2026-09-08）：上证收盘站上20日线=可做；口径与 gtma20 变体一致
        win_html = ''
        if s['name'] == 'S15_缠论三买':
            try:
                _w = history_engine.sh_window(datetime.date.today().isoformat())
                win_html = ('<span>三买可做窗口 <b class="%s">%s</b>'
                            '<span class="muted">（%s）</span></span>'
                            % ('ok' if _w.get('ok') else 'sell',
                               '可做 ✓' if _w.get('ok') else '禁做 ✗',
                               html.escape(str(_w.get('label') or '无上证序列'))))
            except Exception:
                win_html = ''
        # ★ 2026-09-10 停牌挂账（跨日）：详情页顶部也要能看到"旧仓卖不掉、正在挂账"，
        #   否则换仓当日之后（新仓持有/卖出信号行落地）这层信息只留在历史行里。
        fz_html = ''
        try:
            _fzr, _fz = signal_rows.frozen_open(daily)
            if _fz:
                fz_html = ('<p class="note">⚠ 另持仓（停牌挂账）'
                           f'<b>{html.escape(str(_fz.get("name") or ""))}</b>'
                           f'（{html.escape(str(_fz.get("code") or ""))}）：自 '
                           f'{_md(str(_fz.get("since") or _fzr.get("date") or ""))} 起停牌（无成交），'
                           '卖出挂单顺延至复牌日——信号不废弃，复牌当日按本策略时点补卖；'
                           '同期换仓买入已照常执行，未因卖不掉而耽误。</p>')
        except Exception:
            fz_html = ''
        return ('<div class="card" style="border-left:4px solid #e8a13c">'
                f'<h2>{_dcn} 状态与操作注意'
                '<span class="muted">（模拟记录截止 %s ｜ 与首页提醒卡/微信推送同一数据源）</span></h2>'
                '<div class="strat-grid">'
                '<span>状态 <b class="st %s">%s</b></span>'
                '<span>标的 <b>%s</b></span>'
                '<span>操作注意 <b class="st %s">%s</b></span>'
                '<span>今日轮换 %s</span>'
                '%s'
                '%s'
                '</div>'
                '%s'
                '%s'
                '<p class="note">执行时点：%s；信号收盘后判定、于下一交易日（T+1）按此时点执行，'
                '不追价、盘中不改单。</p></div>'
                % (html.escape(str(last.get('date') or '—')), lc, html.escape(_st),
                   nm, act_cls, act, rot_txt, env_fit, win_html, sig_html, fz_html, tm))
    except Exception:
        return ''


def render_page(path='/', query=None):
    global PAGE_CACHE
    today = datetime.date.today().isoformat()
    key = (path, json.dumps(query or {}, sort_keys=True, ensure_ascii=False))
    # ★ 2026-09-17：/signals（盘中信号历史）**不走页面缓存**。PAGE_CACHE 的有效期是"当天"，
    #   该页当天首次打开后内容就固定了 → 用户刷新也看不到信号的最新状态（gone/推送/新增），
    #   这正是"历史一直显示触发中、页面刷不出来"的直接原因之一。该页只查 SQLite，渲染很轻。
    _no_cache = path in ('/signals', '/watch')   # 盘中盯盘页必须实时（候选池每轮都在变）
    hit = PAGE_CACHE.get(key)
    if (not _no_cache) and hit is not None and hit[0] == today:
        return hit[1]
    if path == '/watch':                      # ★ S16 空间龙盘中盯盘面板（2026-09-24）
        body = page_frame('空间龙盘中盯盘', '/watch', render_watch(), is_trading_time())
    elif path == '/s15ab':                    # ★ S15 可做窗口(gtma20)变体对照回测报告页
        body = page_frame('S15 回测对照（可做窗口 gtma20）', '/s15ab',
                          render_bt_s15(), is_trading_time())
    elif path == '/signals':                  # ★ 盘中信号历史（SQLite 全量留痕，可调取任意历史日）
        body = page_frame('盘中信号历史', '/signals', render_signals(query), is_trading_time())
    elif path.startswith('/s/'):
        body = render_strategy(path, query)
    else:
        body = render_index(query or {})
    data = body.encode('utf-8')
    if not _no_cache:
        PAGE_CACHE[key] = (today, data)
    return data
def render_watch():
    """★ 2026-09-24 S16 空间龙【盘中盯盘面板】。

    展示：候选池（今日潜在空间龙前 5）、全天盯过的票的封板/炸板轨迹、14:57 收盘确认结果。
    数据源 = app.do_scan 每轮驱动的 space_watch.tick()（跨重启从 json 恢复）。
    """
    try:
        if not (space_watch.STATE.get('date') and space_watch.STATE.get('ticks')):
            space_watch.load_today()
        # ★ ready / prev_date 只在 tick() 里赋值，盘前或非交易时段一次都还没 tick，
        #   页面会误报"底表尚未就绪"（实测：日志已打印构建完成 68s，页面却说未就绪）。
        #   这里主动探测一次——底表已由启动线程预热，命中缓存立即返回，不会重跑 68s。
        if not space_watch.STATE.get('ready'):
            _pv = space_watch.ensure_prev()
            if _pv:
                space_watch.STATE['ready'] = True
                space_watch.STATE['prev_date'] = _pv.get('date')
        if not space_watch.STATE.get('date'):
            space_watch.STATE['date'] = datetime.date.today().isoformat()
        snap = space_watch.snapshot()
    except Exception as e:
        return (f'<div class="card"><h2>空间龙盘中盯盘</h2>'
                f'<p class="muted">状态读取失败：{html.escape(str(e))}</p></div>')

    def esc(x):
        return '' if x is None else html.escape(str(x))

    def _pf(v, fmt='{:.2f}'):
        try:
            return fmt.format(float(v))
        except Exception:
            return '-'

    h = ('<div class="card"><h2>S16 空间龙 · 盘中盯盘</h2>'
         '<p class="muted">盯盘逻辑：实时算出"今日潜在空间龙"候选池（当日封板 → 潜在连板数 = 昨日连板数+1），'
         '按（连板降序、换手降序）取前 ' + str(space_watch.TOPN) + ' 名，全程跟踪封板/炸板轨迹，'
         '<b>只在 14:57 集合竞价确认仍封板且排得到时才出手</b>，龙一买不进则顺延次强。</p>'
         '<p class="note">为什么必须"等确认"（全史 2614 只 / 每笔 5000 实测）：'
         '开盘就挂涨停价 → n=1494 净 <b class="down">-9,947</b>（炸板率 73.8%）；'
         '盘中触及涨停就挂 → n=1119 净 <b class="down">-63,952</b>（炸板率 45.2%）；'
         '等收盘确认封板才买 → n=779 净 <b class="up">+154,650</b>（炸板率 0%）。'
         '<br>→ 打板不是越早越好：<b>盘中赌封板必亏</b>，本面板全天只盯不出手。'
         '<br>★ <b>但"确认封板" ≠ "买得到"</b>：14:57 挂涨停价本质是<b>排队</b>，'
         '只有盘中有换手、收盘时有卖单才排得进（一字/秒板已自动过滤）。'
         '上面 +154,650 是<b>假设能成交</b>的乐观上界，实盘务必先纸面记录成交率再上仓位。</p></div>')

    # ★ 2026-09-28：盯盘页「单独刷新」按钮——只跑一轮 tick，不等 5 分钟扫描轮次
    _rr = SPACE_REFRESH
    _swr_state = ('<span style="color:#2b7ae2">刷新进行中…（%s）</span>' % esc(_rr.get('msg'))
                  if _rr.get('running') else
                  ('上次刷新 %s %s' % (esc(_rr.get('at') or '—'), esc(_rr.get('msg') or ''))
                   if _rr.get('at') else ''))
    _swr_html = (
        '<p><button id="swrBtn" onclick="swrGo()" '
        'style="padding:6px 16px;cursor:pointer;font-size:14px">刷新盯盘数据</button> '
        '<span id="swrMsg" class="muted">%s</span></p>'
        '<script>function swrGo(){var b=document.getElementById("swrBtn"),'
        'm=document.getElementById("swrMsg");if(b.disabled)return;b.disabled=true;'
        'b.innerText="刷新中…";m.innerText="正在拉取全市场行情，约 30 秒后自动刷新页面";'
        'fetch("/api/space_watch_refresh",{method:"POST"}).then(function(r){return r.json()})'
        '.then(function(j){setTimeout(function(){location.reload();},30000)})'
        '.catch(function(e){m.innerText="请求失败，请重试";b.disabled=false;'
        'b.innerText="刷新盯盘数据"});}</script>' % _swr_state)
    _stale = snap.get('stale')
    _qd = snap.get('quote_date')
    st = ('<div class="card"><h3>盯盘状态</h3>'
          f'<p>盯盘日：<b>{esc(snap.get("date") or "-")}</b> ｜ '
          f'昨日连板底表截止：<b>{esc(snap.get("prev_date") or "-")}</b> ｜ '
          f'行情日期：<b>{esc(_qd or "-")}</b> ｜ '
          f'已盯 <b>{int(snap.get("ticks") or 0)}</b> 轮 ｜ '
          f'轨迹票 <b>{len(snap.get("seen") or {})}</b> 只</p>'
          + _swr_html
          + ('<p class="note"><b>今日休市（或无实时行情）</b>：行情日期为 '
             f'{esc(_qd or "-")}，与盯盘日不一致 → 已拒绝用陈旧快照更新候选池和收盘确认，'
             '避免把上一交易日的涨停票误判成今日封板。</p>' if _stale else '')
          + ('' if snap.get('ready')
             else '<p class="muted">昨日连板底表尚未就绪（首次构建约 80 秒），稍后刷新。</p>')
          + '</div>')
    h += st                       # ★状态卡任何情况下都要显示（此前只在未就绪分支输出，正常路径丢了）
    if not snap.get('ready'):
        return h

    pool = snap.get('pool') or []
    if pool:
        rows = ''
        for i, c in enumerate(pool, 1):
            if not c.get('sealed'):
                tag, cls = '未封', ''
            elif not c.get('buyable'):
                tag, cls = '封板·一字/秒板<br>买不进', 'muted'
            else:
                tag, cls = '封板·可排队', 'up'
            rows += (f'<tr><td>{i}</td>'
                     f'<td><b>{esc(c.get("name"))}</b><br><span class="muted">{esc(c.get("code"))}</span></td>'
                     f'<td>{int(c.get("lb") or 0)}板</td>'
                     f'<td>{_pf(c.get("price"))}</td>'
                     f'<td class="up">{_pf(c.get("pct"), "{:+.2f}")}%</td>'
                     f'<td>{_pf(c.get("turnover"))}%</td>'
                     f'<td class="{cls}">{tag}</td></tr>')
        h += ('<div class="card"><h3>当前候选池</h3><table>'
              '<tr><th>名次</th><th>标的</th><th>潜在连板</th><th>现价</th>'
              '<th>涨幅</th><th>换手</th><th>能否买到</th></tr>'
              + rows + '</table></div>')
    else:
        h += ('<div class="card"><h3>当前候选池</h3>'
              '<p class="muted">暂无封板标的（尚未开盘 / 盘中无票封板 / 非交易日）。</p></div>')

    seen = snap.get('seen') or {}
    if seen:
        rows = ''
        for code, s in sorted(seen.items(),
                              key=lambda kv: (-int(kv[1].get('lb') or 0),
                                              -int(kv[1].get('breaks') or 0))):
            state = ('<b class="up">封板中</b>' if s.get('sealed_now')
                     else '<b class="down">已炸板</b>')
            able = '可排队' if (s.get('sealed_now') and s.get('buyable')) else '—'
            rows += (f'<tr><td><b>{esc(s.get("name"))}</b> '
                     f'<span class="muted">{esc(code)}</span></td>'
                     f'<td>{int(s.get("lb") or 0)}板</td>'
                     f'<td>{esc(s.get("first_seal"))}</td>'
                     f'<td>{int(s.get("breaks") or 0)}</td>'
                     f'<td>{state}</td><td>{able}</td></tr>')
        h += ('<div class="card"><h3>全天盯盘轨迹</h3><table>'
              '<tr><th>标的</th><th>最高连板</th><th>首次封板</th>'
              '<th>炸板次数</th><th>当前</th><th>能否买到</th></tr>'
              + rows + '</table>'
              '<p class="muted">炸板次数 = 曾封板后盘中回落至未封板的次数'
              '（仅因名次掉出前 ' + str(space_watch.TOPN) + ' 名不计），回封后继续跟踪。</p></div>')

    c = snap.get('confirmed')
    if c:
        fin = snap.get('final') or {}
        _sk = c.get('skipped') or []
        _fin = (f'，收盘复核 <b>{_pf(fin.get("close"))}</b>'
                f'（{_pf(fin.get("pct"), "{:+.2f}")}%，{esc(fin.get("at"))}）'
                if fin.get('close') else '')
        h += ('<div class="card"><h3>14:57 收盘确认</h3>'
              f'<p>确认买入 <b>{esc(c.get("name"))}</b>（{esc(c.get("code"))}）：'
              f'<b>{int(c.get("lb") or 0)}板</b>，买入参考价 <b>{_pf(c.get("price"))}</b> 元{_fin}'
              f'<br><span class="muted">确认时刻 {esc(c.get("at"))} ｜ {esc(c.get("reason"))}</span></p>'
              + (f'<p class="note">顺延跳过：{esc("；".join(_sk))}</p>' if _sk else '')
              + '</div>')
    else:
        import datetime as _dt
        _hm = _dt.datetime.now().hour * 60 + _dt.datetime.now().minute
        if _hm >= space_watch.CONFIRM_HM:
            h += ('<div class="card"><h3>14:57 收盘确认</h3>'
                  f'<p class="muted">{esc(space_watch.confirm_text(snap))}</p></div>')

    h += ('<div class="card"><h3>口径说明</h3>'
          '<p class="muted">封板判定：实时涨幅 ≥ 9.8%（主板 10%，ST 的 5% 板天然排除）；'
          '买不进判定：开盘价 ≥ 涨停价×0.99（一字/秒板），与回测 '
          '<code>backtest._s16_is_unbuyable_limit(skip_limit_open=True)</code> 同口径；'
          '昨日连板数取自 <code>_s16_leaders</code> 缓存的 by_date，不重扫 CSV。<br>'
          '<b>本页只盯盘 + 出决策 + 推送，不自动写入模拟账本</b>（改账本属上线动作，需单独确认）。</p></div>')
    return h


def render_bt_s15():
    """S15 可做窗口(gtma20)变体 vs 原版 对照回测报告页（读 data/bt_s15_gtma20.json，
    由 D:/codebuddy_workspace/_s15_ab.py 全市场重放生成）"""
    fp = 'data/bt_s15_gtma20.json'
    if not os.path.exists(fp):
        return ('<div class="card"><h2>S15 回测对照（可做窗口 gtma20）</h2>'
                '<p class="muted">报告尚未生成。生成方式（任意 CWD 均可，全市场重放约 10 分钟）：</p>'
                '<p class="note"><code>python -X utf8 -u _s15_ab.py</code>'
                '<br>产出 <code>data/bt_s15_gtma20.json</code>（本页）与 '
                '<code>data/bt_s15_gtma20_trades.csv</code>（逐笔明细）。</p></div>')
    try:
        with open(fp, encoding='utf-8') as f:
            r = json.load(f)
    except Exception as e:
        return f'<div class="card"><p class="muted">报告读取失败：{html.escape(str(e))}</p></div>'

    def esc(x):
        return '' if x is None else html.escape(str(x))

    def _pnl(v):
        v = float(v or 0)
        cls = 'up' if v >= 0 else 'down'
        return f'<b class="{cls}">{v:+,.0f}</b>'

    b, g = r.get('base') or {}, r.get('gt') or {}
    w = r.get('window') or {}
    h1 = (f'<div class="card"><h2>S15 回测对照：三买「可做窗口(gtma20)」变体 vs 原版</h2>'
          f'<p class="muted">生成 {esc(r.get("gen"))} ｜ 区间 {esc(r.get("frm"))} ~ {esc(r.get("to"))}'
          f'（数据至 {esc(r.get("data_last"))}）｜ 全市场主板 {esc(r.get("universe_n"))} 只'
          f'｜ 口径：每笔5000含费、信号次日开盘成交、止盈8%/止损5%（与官方 S15 同）</p>')
    if w:
        h1 += (f'<p>上证站上20日线（可做）天数：<b>{w.get("ok_days")}</b> / {w.get("total_days")}'
               f'（{w.get("ok_pct")}%）；跌破20日线（禁做）：{w.get("bad_days")} 天'
               ' <span class="muted">——「可做窗口」判定于三买信号确认日。</span></p>')
    h1 += '</div>'
    # 结论
    con = r.get('conclusion') or []
    concl = ('<div class="card"><h3>结论</h3>' +
             ''.join(f'<p>{"·" if i else ""}{esc(x)}</p>' for i, x in enumerate(con)) +
             '</div>') if con else ''
    # 汇总对比表
    def _srow(label, kb, kg, fmt='{:,.0f}', cls=None):
        return (f'<tr><td>{label}</td>'
                f'<td class="{cls or ("" if float(kb or 0) >= 0 else "down")}">{fmt.format(kb or 0)}</td>'
                f'<td class="{cls or ("" if float(kg or 0) >= 0 else "down")}">{fmt.format(kg or 0)}</td></tr>')
    rows = []
    rows.append(f'<tr><th>指标</th><th>原版（全时段）</th><th>gtma20（仅可做窗口）</th></tr>')
    rows.append(_srow('成交笔数', b.get('trades'), g.get('trades'), '{:,.0f}'))
    rows.append(_srow('净利（含费，元）', b.get('net'), g.get('net'), '{:+,.0f}'))
    rows.append(_srow('胜率', b.get('win_rate'), g.get('win_rate'), '{:.1f}%'))
    rows.append(_srow('平均每笔收益', b.get('avg_ret_pct'), g.get('avg_ret_pct'), '{:+.2f}%'))
    rows.append(_srow('平均持有(交易日)', b.get('avg_hold_days'), g.get('avg_hold_days'), '{:.1f}'))
    rows.append(_srow('累计净利最大回撤%', b.get('max_dd'), g.get('max_dd'), '{:.2f}'))
    rows.append(_srow('开在禁做窗口的笔数', b.get('bad_window_buys'), g.get('bad_window_buys'), '{:,.0f}'))
    rows.append(_srow('其中合计净利', b.get('bad_window_net'), g.get('bad_window_net'), '{:+,.0f}'))
    tbl = ('<div class="card"><h3>全史汇总对比</h3><div class="tbl-wrap"><table><tbody>'
           + ''.join(rows) + '</tbody></table></div></div>')
    # 逐年表
    def _yr_cell(d):
        if not d:
            return '-'
        return (f'<td>{d.get("trades", 0)}<span class="muted">笔</span> '
                f'{_pnl(d.get("net"))} <span class="muted">胜率{d.get("win_rate")}%</span></td>')
    yr = ''
    if (b.get('annual') or g.get('annual')):
        years = sorted(set((b.get('annual') or {}).keys()) | set((g.get('annual') or {}).keys()))
        body = ''.join(
            f'<tr><td>{y}</td>{_yr_cell((b.get("annual") or {}).get(y))}'
            f'{_yr_cell((g.get("annual") or {}).get(y))}</tr>' for y in years)
        yr = ('<div class="card"><h3>逐年对照（净利为每笔5000含费口径）</h3>'
              '<div class="tbl-wrap"><table><thead><tr><th>年度</th>'
              '<th>原版（全时段）</th><th>gtma20（仅可做窗口）</th></tr></thead><tbody>'
              + body + '</tbody></table></div></div>')
    # 落地同段
    def _rc(d):
        if not d or not d.get('trades'):
            return '<td class="muted">无成交</td>'
        return (f'<td>{d["trades"]}笔 {_pnl(d.get("net"))}'
                f' 胜率{d.get("win_rate")}%<span class="muted"> 窗口外{d.get("bad_window_buys")}笔/'
                f'净{_pnl(d.get("bad_window_net"))}</span></td>')
    rec = ('<div class="card"><h3>落地同段对照（2026-07-01 起，模拟盘真实口径）</h3>'
           '<div class="tbl-wrap"><table><thead><tr><th>版本</th><th>统计</th></tr></thead><tbody>'
           f'<tr><td>原版</td>{_rc(b.get("recent"))}</tr>'
           f'<tr><td>gtma20</td>{_rc(g.get("recent"))}</tr>'
           '</tbody></table></div>'
           '<p class="muted">* 落地段为每笔5000、随真实行情逐笔重放；原版窗口外开仓=当日跌破20日线仍照买。</p></div>')
    foot = ('<p class="muted">明细见 <code>data/bt_s15_gtma20_trades.csv</code>（含每笔 sig_date/sig_win 字段，'
            'sig_win=False 即开在禁做窗口）。重新生成：<code>python -X utf8 -u _s15_ab.py</code></p>')
    return h1 + concl + tbl + yr + rec + foot


def render_index(query=None):
    """首页：今日推荐 + 各策略概况卡片（点击直达）+ 时点指引 + 回测答卷 + 日志"""
    idx_rng = 'ytd'
    if query:
        v = (query.get('idx') or ['ytd'])[0]
        if v in ('all', '3y', '1y', 'ytd'):
            idx_rng = v
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    trading = is_trading_time()
    sname = STATE['strategy'] or ''
    # ★ 当前行情环境（供策略卡片标注适配/不适配，2026-08-27 行情驱动的策略选择）
    global history_engine
    import history_engine
    _mkt_env = 'range'
    try:
        _ma = analyze_market_cached()      # 按自然日缓存，避免每次渲染重复计算
        if _ma.get('asof'):
            _mkt_env = _ma.get('env') or history_engine.market_env_on(_ma['asof'])
    except Exception:
        pass
    _cur_tm = ''
    if HISTORY is not None:
        for _s in HISTORY['strategies']:
            if _s['name'] == sname:
                _cur_tm = str(_s.get('timing', ''))
                break
    rec = STATE['recommend']
    logs = ''.join(f'<li>{html.escape(x)}</li>' for x in STATE['scan_log'])
    strategies = []
    if HISTORY is not None:
        # 排序口径（2026-09-03）：总盈亏=落袋净盈亏+当前持仓浮盈，避免"深中华A浮盈大但未卖出"被排到后面
        strategies = sorted(HISTORY['strategies'], key=_sim_total_pnl, reverse=True)

    info = (f'<div id="info">状态：<b>{"交易中（每5分钟扫描）" if trading else "已收盘（扫描已暂停）"}</b> ｜ '
            f'今日轮换策略：<b>{html.escape(sname)}</b> ｜ 最后扫描：{STATE["last_scan"] or "-"} ｜ '
            f'行情：{"实时" if STATE["realtime_ok"] else "本地"} ｜ '
            f'判断数据：{html.escape(str((STATE.get("data_asof") or {}).get("label", "-")))}'
            + (f' {html.escape(str((STATE.get("data_asof") or {}).get("detail", "")))}'
               if (STATE.get("data_asof") or {}).get("detail") else '')
            + f' ｜ 页面时间：{now}</div>')

    guide = (f'<div class="card guide"><b>左侧菜单进入各策略详情，本页为总览。</b>'
             '点击下方策略卡片直达对应详情页（含买卖时点速览、每日选股、资金曲线、历史收益）；'
             '各策略成交时点为<b>策略级独立配置</b>（见下方「盘中操作时点指引」时点表）。<br>'
             '<span class="muted">S15 回测对照：</span>'
             '<a href="/s15ab"><b>三买「可做窗口(gtma20)」变体 vs 原版 长期表现</b></a>'
             '<span class="muted">（每笔5000官方口径 · 上证>20日线才开仓，全史逐年对照）</span></div>')

    rec_html = '<p class="muted">暂无信号（非交易时间或今日无候选；下方为策略历史模拟结果）</p>'
    if rec:
        is_index = '指数' in rec.get('name', '') or rec.get('code', '') == ''
        if is_index:
            label = '指数'
            price_label = '指数点位'
            ref_label = '买入估值线'
            tgt_label = '卖出估值线'
            etf_note = ('<p class="note"><b>说明：</b>这是指数 ETF 策略，不直接买股票；'
                        f'可通过场内基金跟踪该指数（如沪深300 ETF 510300、红利 ETF 510880）。'
                        f'表中「{price_label}」为指数价格，「{ref_label}/{tgt_label}」为估值指标。</p>')
            data_line = (f'<p class="note"><b>判断数据：</b>估值截至 {_dash(rec.get("date"))}'
                         '（中证指数估值通常次日发布；服务启动时会尝试自动补齐今日数据，'
                         '拉不到则按最近已发布估值判断，不会用未来数据）</p>')
        else:
            label = '个股'
            price_label = '现价'
            ref_label = '买入参考'
            tgt_label = '目标'
            etf_note = ''
            a = STATE.get('data_asof') or {}
            data_line = (f'<p class="note"><b>判断数据：</b>{html.escape(a.get("label", "本地数据"))}'
                         + (f' {html.escape(str(a.get("detail", "")))}' if a.get('detail') else '')
                         + (f'；<span class="muted">{html.escape(str(a.get("backfilled", "")))}</span>' if a.get('backfilled') else '')
                         + '；决策只用当日及之前数据，不用未来数据</p>')
        _tm_part = (f'<span class="timing-badge">{html.escape(_cur_tm)}</span>'
                    if _cur_tm else '信号次日按策略时点执行')
        # 盘中扫描为实时价预估（收盘价确认），收盘后扫描为收盘判定 → 标题区分，避免误导
        _sig_lab = '盘中信号' if rec.get('market') == 'trading' else '收盘信号'
        _sname_lab = html.escape(sname) if sname else '今日策略'
        rec_html = (f'<div class="card"><h2>今日轮换策略{_sig_lab}'
                    f'（{_sname_lab} · {_tm_part}）：'
                    f'{html.escape(rec.get("name", ""))} '
                    f'<span class="code">{rec.get("code", "")}</span>'
                    f'<span class="muted">（{label}）</span></h2>'
                    f'<p class="st {"ok" if rec.get("triggered") else "warn"}">{html.escape(rec.get("status", ""))}</p>'
                    f'<table class="bp"><tr><th>{price_label}</th><th>{ref_label}</th><th>止损</th><th>{tgt_label}</th><th>评分</th></tr>'
                    f'<tr><td>{_dash(rec.get("price"))}</td><td>{_dash(rec.get("buy_ref"))}</td>'
                    f'<td>{rec.get("stop") if rec.get("stop") is not None else "-"}</td>'
                    f'<td>{rec.get("target") if rec.get("target") is not None else "动态"}</td>'
                    f'<td>{_dash(rec.get("score"))}</td></tr></table>'
                    f'<p class="note">{html.escape(str(rec.get("tag", "")))}</p>'
                    f'<p class="note">{_esc_note(rec.get("note", ""))}</p>{data_line}{etf_note}</div>')

    # 各策略概况卡片（点击直达策略详情页）
    cards = ''
    if HISTORY is not None:
        for s in strategies:
            df_ = [r for r in s['daily'] if r['date'].startswith(YEAR)]
            if not df_:
                continue
            r = df_[-1]
            lc = STATUS_CLASS.get(r['status'], '')
            # ★ 换仓日展示口径（2026-09-02）：卖出成交+同日换仓买入 → 状态/标的/现价按新持仓展示
            lv = _switch_view(r)
            if lv is not r:
                lc = 'ok'
            _st = lv['status']
            _nm = html.escape(str(lv.get('name') or '（指数）'))
            _tt = ''
            if lv is not r:
                _tt = (f' title="当日卖出成交 {lv.get("_sell_name") or ""}（成交价 {lv.get("_sell_px")}）'
                       f'→ 换仓买入 {lv["name"]}（买入价 {lv["deal_price"]}）"')
            # ★ 操作提示：跟随模拟盘=看最新状态日 r["date"] 的下一交易日动作
            _ndx = _next_td(str(r.get('date') or ''))
            _ndx_t = f'{_md(_ndx)}' if _ndx else '下一交易日'
            if _st == '买入信号':
                _act = f'<span class="st ok">{_ndx_t} 开盘买入</span> {_nm}'
            elif _st == '卖出信号':
                _act = f'<span class="st sell">{_ndx_t} 开盘卖出</span> {_nm}'
            elif _st == '持有':
                _act = f'<span class="st hold">持有</span> {_nm}'
            elif _st == '换仓买入':
                _act = (f'<span class="st ok">已换仓买入</span> {_nm}'
                        f'<span class="muted">（持仓中）</span>')
            elif _st in ('空仓', '观望'):
                _act = '<span class="st gray">空仓等待信号</span>'
            else:
                _act = f'<span class="st {lc or "gray"}">{html.escape(_st)}</span>'
            yp = _sim_pnl(s)          # 已卖出落袋净盈亏
            tot = _sim_total_pnl(s)   # 总盈亏 = 落袋 + 当前持仓浮盈
            yp_f = _sim_pnl_fixed(s)
            mp = _monthly_pnl(s)
            _ytrades = _sim_trades(s)
            st = recalc_summary(_ytrades)
            _rk = risk_stats(s['daily'])  # 风险收益：最大回撤/年化/夏普（模拟以来）
            _t = str(s.get('timing', '早盘买/尾盘卖'))
            _sid = s['name'].split('_')[0]
            _short = s['name'].split('_', 1)[1]
            ycls = 'up' if tot >= 0 else 'down'   # 主数字用含浮盈口径
            ylcls = 'up' if yp >= 0 else 'down'   # 落袋数字用落袋口径
            yfcls = 'up' if yp_f >= 0 else 'down'
            mcls = 'up' if mp >= 0 else 'down'
            lab2, acls2, _ = invest_advice(s)
            # ★ 行情适配标注（2026-08-27）：当前行情不适配时该策略空仓（不建仓）
            _fit = history_engine.STRATEGY_ENV_FIT.get(s['name'])
            if _fit is None:
                _env_badge = ''
            else:
                _env_ok = _mkt_env in _fit
                _env_badge = (f'<span class="sc-env {"ok" if _env_ok else "no"}" '
                              f'title="当前行情[{_mkt_env}]，本策略适配：{"、".join(sorted(_fit))}；'
                              f'不适配时当天空仓（不建仓）">行情{"适配" if _env_ok else "不适配"}</span>')
            cards += (f'<div class="strat-card" onclick="location.href=\'/s/{urllib.parse.quote(s["name"])}\'">'
                      f'<div class="sc-head"><b>{html.escape(_sid)} {html.escape(_short)}</b>'
                      f'<span class="timing-badge">{html.escape(_t)}</span>'
                      f'{_env_badge}'
                      f'<span class="sc-pnl {ycls}" title="模拟交易起点(2026-07-01)以来总盈亏=已卖出落袋净盈亏+当前持仓浮盈；真实账本口径：收益滚入本金、按各策略单笔资金规则买入（S16龙头两档单笔≤max(本金5000,账户/2)）">7.1以来(含浮盈) {tot:+.2f}</span></div>'
                      f'<div class="strat-grid">'
                      f'<span>7.1以来落袋 <b class="{ylcls}" title="已卖出交易的净盈亏合计（不含当前持仓浮盈）；持有中未卖出的浮盈另计">{yp:+.2f}</b></span>'
                      f'<span>7.1以来固定5000 <b class="{yfcls}" title="每笔固定投入不超过5000元、收益不滚入；按已平仓落袋统计">{yp_f:+.2f}</b></span>'
                      f'<span>本月(含浮盈) <b class="{mcls}" title="本月权益变动=本月落袋净盈亏+持仓浮盈变动">{mp:+.2f}</b></span>'
                      f'<span>持仓浮盈 <b>{_pos_pnl_html(r)}</b></span>'
                      f'<span>交易 <b>{st["trades"]}笔</b> 胜率 <b>{st["win_rate"]}%</b></span>'
                      f'{_risk_grid(_rk)}'
                      f'<span>投资方式 <b class="rec-{acls2}" title="基于全历史表现，详情页有完整说明">{html.escape(lab2)}</b></span>'
                      f'<span>状态 <b class="st {lc}"{_tt}>{html.escape(_st)}</b></span>'
                      f'<span>操作 <b>{_act}</b></span>'
                      f'<span>标的 <b>{_nm}</b></span>'
                      f'<span>最新 <b>{r["date"]}</b> 现价 <b>{lv.get("price") or "-"}</b></span>'
                      f'</div></div>')
        if not cards:
            cards = '<p class="muted">暂无数据。</p>'
    elif HISTORY_READY:
        cards = '<p class="muted">历史数据加载中...</p>'
    else:
        cards = ('<div class="card"><h2>后台计算中</h2>'
                 '<p>首次运行需生成全部策略的历史选股与交易列表（约30-60秒），'
                 '页面将自动刷新。</p></div>')

    # 各策略成交时点表（显眼标注）
    timing_rows = ''
    if HISTORY is not None:
        for _s in strategies:
            _t = str(_s.get('timing', '早盘买/尾盘卖'))
            timing_rows += (f'<tr><td>{html.escape(_s["name"])}</td>'
                            f'<td class="timing-tag">{html.escape(_t)}</td>'
                            f'<td class="note">{html.escape(_timing_op(_t))}</td></tr>')

    body = (f'<h1>📊 策略选股回放与每日推荐</h1>\n'
            f'{today_action_card()}\n'
            f'{_signal_ledger_card()}\n'
            f'{_ledger_card(_sim_ledger_rows())}\n'
            f'{info}\n{today_alert_card()}\n{yesterday_signal_card()}\n{guide}\n{rec_html}\n'
            f'{sh_aid_card()}\n'
            f'{market_card()}\n'
            f'{dynamic_pool_card(idx_rng)}\n'
            '<h2>策略总览（点击卡片进入详情）</h2>\n'
            f'<div class="strat-list">{cards}</div>\n'
            '<h2>盘中操作时点指引（什么时候买、什么时候卖）</h2>\n'
            '<div class="card timing">\n'
            '<p><b>① 信号判定时点：每个交易日 15:00 收盘后</b>，用当日收盘数据计算指标并给出信号；'
            '判定时当日已收盘、不能再买卖，<b>买入在信号次日（T+1）开盘、卖出在信号次日（T+1）收盘执行</b>；'
            '盘中不临时触发，不会用到未来数据。</p>\n'
            '<p><b>② 个股策略（S1~S6、S9、S10）执行时点：按策略各自最适合的时点配置</b>'
            '（基于 timing_backtest 四组合回测 + 策略逻辑综合选定；早盘=次日开盘价约 09:30 委托，'
            '尾盘=次日 14:45 后至 14:57 收盘集合竞价）：</p>\n'
            f'<table><thead><tr><th>策略</th><th>成交时点</th><th>具体操作</th></tr></thead><tbody>{timing_rows}</tbody></table>\n'
            '<p><b>③ 指数策略（S7、S8）执行时点：估值收盘判定、按估值价成交</b>'
            '（估值数据收盘后确认，中证/中证红利每日收盘发布；有发布时滞时按最新已发布估值判定）。</p>\n'
            '<p><b>④ 动态换仓（自动）</b>：卖出信号日收盘若扫出触发买入候选，'
            '换仓买入跟随卖出时段<b>先卖后买</b>（A股卖出资金当日可用），'
            '避免资金闲置；候选显示为「已换仓」。</p>\n'
            '<p><b>⑤ 不追价</b>：信号依据收盘数据，盘中大幅波动不临时改单；成交价与信号日收盘价'
            '可能有跳空，属正常滑点，已计入回测。</p>\n'
            '</div>\n'
            '<h2>模拟规则说明</h2>\n'
            '<p class="note">收益栏位统计自模拟交易起点（2026-07-01）起的模拟交易记录；'
            '此前历史数据仅用于回测（见「7.1以来回测答卷」与资金曲线历史区间）。'
            '每策略本金 5000 元/笔；信号收盘后确定，各策略按各自最适合的成交时点执行'
            '（见上方「盘中操作时点指引」各策略时点表：早盘=次日开盘价约 09:30、尾盘=次日 14:45-14:57 收盘集合竞价；A股T+1）；'
            '卖出后若当日有触发候选，换仓买入跟随卖出时段自动先卖后买；指数策略（S7/S8）含浮亏8%止损与最长持有期；'
            'T+1涨跌* = 标的下一交易日<b>开盘</b> / 信号日收盘 - 1，为<b>事后统计仅用于复盘</b>，不参与决策；'
            '股票池已剔除创业板/科创板/ST/退市，仅保留沪深主板 15 只。状态含义：买入信号(收盘判定、按策略时点买入)、'
            '建仓(已成交)、持有(T+1可保留)、卖出信号(收盘判定、按策略时点卖出)、卖出成交、'
            '空仓(无信号/有候选未触发/当前行情不适配本策略)、资金不足(5000买不起1手)。</p>\n'
            f'{_year_report(HISTORY["strategies"] if HISTORY else [])}\n'
            '<h2>扫描日志</h2>\n'
            f'<ul>{logs}</ul>\n')
    return page_frame('策略选股 - 首页', '/', body, trading)


def render_strategy(path, query=None):
    """策略详情页：/s/S1 ~ /s/S10"""
    eq_rng = 'sim'
    if query:
        v = (query.get('eq') or ['sim'])[0]
        if v in ('all', '3y', '1y', 'ytd', 'sim'):
            eq_rng = v
    sid = urllib.parse.unquote(path.split('/')[-1].strip())
    s = None
    if HISTORY is not None:
        # 精确匹配完整策略名优先（S16_龙头接力 / S16_龙头接力V2 需区分）
        for _s in HISTORY['strategies']:
            if _s['name'].lower() == sid.lower():
                s = _s
                break
        # 兼容旧式前缀链接 /s/S1 ~ /s/S16
        if s is None:
            for _s in HISTORY['strategies']:
                if _s['name'].split('_')[0].lower() == sid.lower():
                    s = _s
                    break
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    trading = is_trading_time()
    sname = STATE['strategy'] or ''
    logs = ''.join(f'<li>{html.escape(x)}</li>' for x in STATE['scan_log'])
    info = (f'<div id="info">状态：<b>{"交易中（每5分钟扫描）" if trading else "已收盘（扫描已暂停）"}</b> ｜ '
            f'今日轮换策略：<b>{html.escape(sname)}</b> ｜ 最后扫描：{STATE["last_scan"] or "-"} ｜ '
            f'行情：{"实时" if STATE["realtime_ok"] else "本地"} ｜ 页面时间：{now}</div>')
    if s is None:
        title = '策略不存在'
        body = (f'<h1>未找到策略 {html.escape(sid)}</h1>{info}'
                '<p class="muted"><a href="/">返回首页</a></p>')
    else:
        title = s['name'] + ' - 策略详情'
        body = (f'{info}\n{strategy_today_bar(s)}\n{strategy_body(s, eq_rng)}\n'
                f'<h2>扫描日志</h2>\n<ul>{logs}</ul>\n')
    # active 与侧边菜单 URL 保持一致（quote 编码的完整策略名），确保仅命中一个菜单项
    return page_frame(title, f'/s/{urllib.parse.quote(s["name"])}' if s else f'/s/{sid}', body, trading)


class Handler(BaseHTTPRequestHandler):
    # 客户端中途断开连接（浏览器自动刷新/长轮询超时）是常态错误，静默处理避免刷屏
    _CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError)

    def _respond(self, ctype, body, status=200):
        try:
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except self._CLIENT_GONE:
            try:
                self.connection.close()
            except Exception:
                pass

    def do_GET(self):
        try:
            if self.path.startswith('/api'):
                body = json.dumps({
                    'strategy': STATE['strategy'], 'date': STATE['date'],
                    'last_scan': STATE['last_scan'], 'scans_today': STATE['scans_today'],
                    'realtime_ok': STATE['realtime_ok'], 'recommend': STATE['recommend'],
                    'rank': STATE['rank'][:50], 'log': STATE['scan_log'][-20:],
                    'data_asof': STATE['data_asof'], 'idx_asof': STATE['idx_asof'],
                    'history_ready': HISTORY_READY,
                    'history_generated': HISTORY['generated'] if HISTORY else None,
                    'today_status': TODAY_STATUS,
                    'build': {'running': BUILD_STATE['running'],
                              'last_built': BUILD_STATE['last_ok'],
                              'built_today': _last_build_date() == datetime.date.today().isoformat()},
                    'build_ver': BUILD_STATE['last_ok'] or (HISTORY['generated'] if HISTORY else ''),
                }, ensure_ascii=False).encode('utf-8')
                self._respond('application/json; charset=utf-8', body)
                return
            raw = self.path
            qs = urllib.parse.parse_qs(raw.split('?', 1)[1]) if '?' in raw else {}
            path = raw.split('?')[0]
            if path in ('/', '/index.html'):
                path = '/'
            elif path == '/watch':           # ★ S16 空间龙盘中盯盘面板（2026-09-24）
                pass
            elif path == '/s15ab':           # ★ S15 回测对照报告页（统一外壳）
                pass
            elif path == '/signals':         # ★ 盘中信号历史（SQLite 全量留痕）
                pass
            elif path.startswith('/s/') and path[3:].strip():
                pass  # 策略详情页 /s/S1 ~ /s/S10
            else:
                path = '/'
            self._respond('text/html; charset=utf-8', render_page(path, qs))
        except self._CLIENT_GONE:
            try:
                self.connection.close()
            except Exception:
                pass
        except Exception:
            import traceback
            tb = traceback.format_exc()
            traceback.print_exc()   # 真实服务错误保留可见，便于排查
            # ★ 2026-09-11：渲染异常必须回一个 500（正文带 traceback），**不能只 close 连接**。
            #   旧写法客户端只看到 RemoteDisconnected（"远程主机强迫关闭了一个现有的连接"），
            #   既判断不出是渲染报错、也不知道错在哪个策略/哪一行——一键测试与浏览器都只会报
            #   "连接被关闭"，无法定位（本次排查"重启后详情页 RemoteDisconnected"时踩到）。
            try:
                _body = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
                         '<title>500 页面渲染异常</title></head>'
                         '<body style="font:13px/1.7 Consolas,Menlo,monospace;padding:18px">'
                         '<h3 style="color:#c00">500 页面渲染异常</h3>'
                         '<p>URL: <code>%s</code></p><pre style="white-space:pre-wrap">%s</pre>'
                         '<p><a href="/">返回首页</a></p></body></html>'
                         % (html.escape(str(self.path)), html.escape(tb))).encode('utf-8')
                self._respond('text/html; charset=utf-8', _body, status=500)
            except Exception:
                try:
                    self.connection.close()
                except Exception:
                    pass

    def do_POST(self):
        if self.path == '/api/refresh':
            # ★ 2026-09-03：手动刷新改为后台线程执行——do_scan(force) 要拉全市场行情/指数、可能触发
            #   当日重建（数分钟），若仍在 HTTP 请求线程内执行会把整个服务堵死（全站页面打不开、超时断开）
            before = STATE['last_scan']

            def _run():
                try:
                    do_scan(force=True)
                except Exception as e:
                    log(f'手动刷新异常: {str(e)[:60]}')
                    try:
                        notify.send_error_alert(f'手动刷新异常: {str(e)[:200]}')
                    except Exception:
                        pass
            threading.Thread(target=_run, daemon=True).start()
            body = json.dumps({'ok': True, 'started': True, 'before': before},
                              ensure_ascii=False).encode('utf-8')
            self._respond('application/json; charset=utf-8', body)
        elif self.path == '/api/space_watch_refresh':
            # ★ 2026-09-28：盯盘页单独刷新（后台线程，避免拉行情时把服务堵死）
            if SPACE_REFRESH['running']:
                body = json.dumps({'ok': True, 'started': False, 'busy': True,
                                   'msg': SPACE_REFRESH['msg']},
                                  ensure_ascii=False).encode('utf-8')
            else:
                threading.Thread(target=_space_refresh_run, daemon=True).start()
                body = json.dumps({'ok': True, 'started': True},
                                  ensure_ascii=False).encode('utf-8')
            self._respond('application/json; charset=utf-8', body)
        elif self.path.startswith('/api/signal_flag'):
            # ★ 2026-09-10：信号记录「停用 / 恢复」——只改 deleted 标志位，**绝不物理删除**
            #   （旧 JSON 机制是直接删记录，导致事后无法调取；现在行始终留在 signals.db）
            try:
                _n = int(self.headers.get('Content-Length') or 0)
                _pl = json.loads(self.rfile.read(_n).decode('utf-8') or '{}')
            except Exception:
                _pl = {}
            _id, _act = _pl.get('id'), str(_pl.get('action') or 'hide')
            try:
                _cnt = (notify.signal_restore(_id) if _act == 'restore'
                        else notify.signal_soft_delete(_id, _pl.get('reason') or '人工停用'))
                body = json.dumps({'ok': True, 'action': _act, 'affected': _cnt},
                                  ensure_ascii=False).encode('utf-8')
            except Exception as e:
                body = json.dumps({'ok': False, 'err': str(e)[:120]},
                                  ensure_ascii=False).encode('utf-8')
            self._respond('application/json; charset=utf-8', body)
        else:
            try:
                self.send_response(404)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, *a):
        pass


def boot_scan():
    """首次扫描放后台执行：akshare 拉指数估值可能很慢，不能阻塞 HTTP 端口监听

    ★ 2026-09-28 修复：scan_thread 比本线程先启动，它那一轮会先拿到 _SCAN_LOCK
    （盘前那一轮很快 return 并释放，但持有期间本线程若来抢，do_scan 会走
    `if not _SCAN_LOCK.acquire(blocking=False): log('扫描进行中，跳过本次'); return`
    直接放弃，且**不再重试**）→ 启动时的完整扫描从未执行 → 首页一直沿用昨天的推荐
    （实测挂着"9月25日买入"这种休市日文案）。故先等锁空闲，再跑首次扫描。
    """
    for _i in range(15):
        if _SCAN_LOCK.acquire(blocking=False):
            _SCAN_LOCK.release()
            break
        time.sleep(2)
    try:
        do_scan(force=True)
    except Exception as e:
        log(f'首次扫描异常: {str(e)[:60]}')
        notify.send_error_alert(f'首次扫描异常: {str(e)[:200]}')


def _port_in_use(port, timeout=0.5):
    """端口是否已有进程在监听（TCP connect 探测：连得上=有人在听）"""
    import socket
    _s = socket.socket()
    _s.settimeout(timeout)
    try:
        _s.connect(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        try:
            _s.close()
        except Exception:
            pass


def _space_watch_warm():
    """后台预热空间龙盯盘的"昨日连板底表"（首次构建 _s16_leaders 缓存约 80s）。

    不预热的话，盘中第一轮 tick 会被这 80 秒同步阻塞（错过一轮盯盘且拖慢该轮扫描）。
    同时尝试恢复当日盯盘进度（服务盘中重启时不丢轨迹）。
    """
    try:
        t0 = time.time()
        prev = space_watch.ensure_prev()
        space_watch.load_today()
        log('空间龙盯盘底表就绪：截止 %s，耗时 %.0fs'
            % ((prev or {}).get('date') or '-', time.time() - t0))
    except Exception as e:
        log(f'空间龙盯盘底表预热失败: {str(e)[:80]}')


def _space_watch_close_guard():
    """★ 空间龙盯盘【收盘确认加密轮询】线程：14:55~15:05 每 30s 拉一次行情并 tick。

    为什么必须独立起线程：主扫描 SCAN_SECONDS=300，实际时点取决于服务启动时刻。
    2026-09-24 实测最后一轮落在 14:50，下一轮 14:55 差 2 分钟没到 CONFIRM_HM(14:57)，
    再下一轮 15:00 时 do_scan 已被 `close_scan_date` 拦住 return → **确认窗口整个被跳过**，
    当天 confirmed=None，白盯一天。

    本线程只守这个窗口：只要还没确认就每 30s 拉一次实时行情并 tick，确认后立即停。
    """
    while True:
        try:
            now = datetime.datetime.now()
            if now.weekday() < 5:
                hm = now.hour * 60 + now.minute
                if (14 * 60 + 55) <= hm <= (15 * 60 + 5):
                    # stale_logged：行情日期≠盯盘日（休市）时 space_watch 会置位，
                    #   此时再拉行情也没意义，直接停手，避免休市日空转 20 次。
                    if (not space_watch.STATE.get('confirmed')
                            and not space_watch.STATE.get('stale_logged')):
                        _space_watch_tick(fetch_realtime() or {}, now)
        except Exception as e:
            log(f'空间龙收盘确认轮询失败: {str(e)[:60]}')
        time.sleep(30)


if __name__ == '__main__':
    # ★ 2026-09-11 单实例护栏：ThreadingHTTPServer 默认 allow_reuse_address=1，Windows 上的
    #   SO_REUSEADDR 允许**第二个进程照样 bind 成功**同一端口（双监听）。一旦出现两个监听者，
    #   客户端连接会随机落到"后来者"身上，后来者被 kill 时在途请求就是 RemoteDisconnected
    #   （即本次排查的"服务重启后详情页 RemoteDisconnected"；实测：8081 已有实例在听时再启动
    #   一个 app.py 不报错、能一直跑）。这里改成**启动前先探测**：端口被占用就打印原因并退出，
    #   绝不产生第二个监听者。运维重启请先结束旧实例（_restart_sb2.ps1 / _runtest.py 都会先杀）。
    if _port_in_use(PORT):
        _m = (f'端口 {PORT} 已有实例在监听，本次启动中止：避免 Windows 双监听导致页面随机断连'
              f'（RemoteDisconnected）。请先结束旧实例再启动。')
        print(_m)
        log(_m)
        sys.exit(2)
    print('启动策略选股回放服务 http://127.0.0.1:%d' % PORT)
    # 2026-08-28 起不再推送启动提示（启动时人就在电脑前），异常/简报仍正常推送
    ensure_history()
    threading.Thread(target=scan_thread, daemon=True).start()
    # ★ 2026-09-24：空间龙盯盘底表预热（约 80s，后台跑，避免阻塞盘中首轮 tick）
    threading.Thread(target=_space_watch_warm, daemon=True).start()
    # ★ 收盘确认加密轮询（只守 14:55~15:05，保证 14:57 那个窗口不被 300s 扫描跳过）
    threading.Thread(target=_space_watch_close_guard, daemon=True).start()
    # 先启动 HTTP 服务（端口立即监听），首次扫描在后台完成后再刷新页面
    threading.Thread(target=boot_scan, daemon=True).start()
    # ★ 2026-09-03：ThreadingHTTPServer（每请求独立线程）——后台扫描/当日重建再久也不会阻塞页面。
    #   渲染本身有 PAGE_CACHE 兜底；STATE/HISTORY 均以「整体替换引用」方式更新，多线程读取安全
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
