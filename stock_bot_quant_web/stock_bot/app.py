# -*- coding: utf-8 -*-
"""策略选股回放 + 每日推荐 Web 服务
- 后台引擎（history_engine）生成每个策略的每日选股记录与历史交易列表（本金5000）
- 页面按策略展示：买卖时点速览 + 每日选股（含T+1涨跌、持仓/空仓状态）+ 历史交易明细
- 交易日每5分钟扫描一次行情并刷新页面；信号收盘后判定、T+1开盘执行
- 页面极简：纯 HTML + 少量 JS（折叠/过滤/锚点导航）
启动：start.bat 或 python app.py
"""
import sys, os, re, json, time, threading, datetime, html, math, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WORKSPACE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKSPACE)

import advisor
import notify

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


def is_trading_time(dt=None):
    dt = dt or datetime.datetime.now()
    if dt.weekday() >= 5:
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
        for i in range(0, len(symbols), CHUNK):
            part = symbols[i:i + CHUNK]
            try:
                url = 'https://qt.gtimg.cn/q=' + ','.join(part)
                r = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
                r.encoding = 'gbk'
                for line in r.text.strip().split(';'):
                    if '="' not in line:
                        continue
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
                log(f'实时行情第 {i // CHUNK + 1} 批失败，继续下一批')
        if out:
            log(f'实时行情获取成功 {len(out)} 只')
            return out
        log('实时行情为空，使用本地数据')
        return {}
    except Exception as e:
        log(f'实时行情不可用: {str(e)[:40]}，使用本地数据')
        return {}


_SCAN_LOCK = threading.Lock()


def do_scan(force=False):
    global PAGE_CACHE
    # 非阻塞互斥：boot_scan(force) 与 scan_thread 定时扫描并发时只放行一个，
    # 避免重复拉行情/重复跑全量选股导致耗时翻倍、STATE 互相覆盖
    if not _SCAN_LOCK.acquire(blocking=False):
        log('扫描进行中，跳过本次')
        return
    try:
        now = datetime.datetime.now()
        if not force and not is_trading_time(now):
            return
        STATE['last_scan'] = now.strftime('%Y-%m-%d %H:%M:%S')
        STATE['scans_today'] += 1
        sname = advisor.today_strategy(now.date())
        STATE['strategy'] = sname
        STATE['date'] = now.strftime('%Y-%m-%d')
        rt = fetch_realtime()
        STATE['realtime_ok'] = bool(rt)
        # 启动太晚时先补齐今日数据：已收盘用实时行情修正/补全个股当日 bar，盘中只做内存叠加
        STATE['data_asof'] = advisor.backfill_today(rt)
        # 指数估值：收盘后或启动时尝试拉取最新数据（乐咕有发布时滞，拉不到则标注截至日）
        if now.hour >= 15 or force:
            STATE['idx_asof'] = advisor.backfill_index(force=force)
        # 指数持久化补齐：历史指数已固定、只补新增交易日（错过日期时在此自动重跑补齐）。
        # 收盘后（个股当日 bar 已写回）增量补选股池指数；上证指数按缺失日期从接口补齐。
        if (now.hour >= 15 and now.weekday() < 5) or force:
            try:
                import history_engine
                global POOL_INDEX
                history_engine.refresh_sh_index()      # 上证指数补齐缺失日期（幂等）
                # build_pool_index 已停用（选股池指数 2026-08-27 移除）
                if os.path.exists(POOL_INDEX_FILE):
                    POOL_INDEX = json.load(open(POOL_INDEX_FILE, encoding='utf-8'))
            except Exception as e:
                log(f'指数补齐失败: {str(e)[:60]}')
        rank = advisor.scan_stock_strategy(sname, realtime=rt or None)
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
                    # 信号消失/未触发：清除当日去重记录，日后再次触发视为新事件可再推（2026-09-09）
                    notify.intraday_signal_clear(sname)
        else:
            STATE['recommend'] = None
            log(f'[{sname}] 今日无候选')
            if is_trading_time(now):
                notify.intraday_signal_clear(sname)
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
                # ★ 收盘后每日一次后台重建当日记录（完成后重载数据 + 页面自动刷新）
                maybe_daily_build()
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

def _md(s):
    """预判标签日期：'2026-09-03' → '9月3日'；无日期返回 '明日'"""
    try:
        y, mo, d = str(s).split('-')
        return f'{int(mo)}月{int(d)}日'
    except Exception:
        return str(s) if s else '明日'


def _next_td(d):
    """d（'YYYY-MM-DD'）之后的下一交易日近似（系统无节假日表，按周一~五推进）"""
    try:
        t = datetime.date.fromisoformat(str(d)[:10]) + datetime.timedelta(days=1)
        while t.weekday() >= 5:
            t += datetime.timedelta(days=1)
        return t.isoformat()
    except Exception:
        return None


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
    if not force and (now.weekday() >= 5 or now.hour < 15):
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
    if now.weekday() >= 5:
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
# ★ 动态文案中「提到的标的」统一加粗（2026-09-09）：引擎 note 等是纯文本、渲染处需 html.escape，
# 故在转义后把文本里出现的全市场股票简称包成 <b>…</b>；按名称长度降序一次性匹配，避免短名吞长名。
_STOCK_BOLD_RE = None
def _esc_note(x):
    """纯文本 note → HTML：html.escape 后高亮文中出现的股票简称（标的名字段亦可传入）。"""
    s = html.escape(str(x if x is not None else ''))
    global _STOCK_BOLD_RE
    if _STOCK_BOLD_RE is None:
        _names = sorted({str(n).strip() for n in advisor.STOCKS.values() if len(str(n).strip()) >= 3},
                        key=lambda n: (len(n), n), reverse=True)
        _STOCK_BOLD_RE = (re.compile('|'.join(re.escape(n) for n in _names)) if _names else None)
    if not s or _STOCK_BOLD_RE is None:
        return s
    return _STOCK_BOLD_RE.sub(lambda m: '<b>%s</b>' % m.group(0), s)

CSS = '''
body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#f7f8fa;color:#222;margin:0}
.layout{display:flex;gap:16px;align-items:flex-start;max-width:1420px;margin:16px auto;padding:0 12px}
.content{flex:1;min-width:0}
.sidebar{position:sticky;top:12px;width:196px;flex-shrink:0;background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:10px 8px;max-height:calc(100vh - 24px);overflow-y:auto}
.sidebar .brand{font-weight:bold;font-size:15px;color:#1a5fb4;padding:6px 8px;border-bottom:1px solid #eee;margin-bottom:4px;white-space:nowrap}
.sidebar .s-group{font-size:11px;color:#888;padding:8px 8px 2px;white-space:nowrap}
.sidebar a.m-item{display:block;padding:6px 8px;border-radius:6px;color:#333;text-decoration:none;font-size:13px;margin:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sidebar a.m-item:hover{background:#eef4fb}
.sidebar a.m-item.active{background:#1a5fb4;color:#fff}
.sidebar a.m-item.lane{color:#1a5fb4;font-weight:600;border:1px solid #dce6f5}
.sidebar a.m-item.lane:hover{background:#eef4fb}
.sidebar a.m-item.lane.active{background:#1a5fb4;border-color:#1a5fb4;color:#fff}
.refresh-box{padding:8px 6px 2px;border-top:1px solid #eee;margin-top:8px}
.refresh-box button{width:100%;padding:7px 0;border:1px solid #0a7a2e;border-radius:6px;background:#fff;color:#0a7a2e;cursor:pointer;font-size:13px}
.refresh-box button:hover{background:#eef7f3}
.strat-card{border:1px solid #e3e6ea;border-radius:10px;background:#fff;padding:12px 14px;margin:8px 0;cursor:pointer;transition:box-shadow .15s,border-color .15s}
.strat-card:hover{box-shadow:0 2px 10px rgba(26,95,180,.16);border-color:#1a5fb4}
.sc-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sc-pnl{font-weight:bold;margin-left:auto;font-size:14px}
.sc-env{font-size:11.5px;padding:2px 8px;border-radius:10px;border:1px solid}
.sc-env.ok{color:#0a7a2e;border-color:#b5dfc6;background:#eef9f2}
.sc-env.no{color:#c0392b;border-color:#f0c4be;background:#fdf0ee}
.strat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px 14px;margin-top:8px;font-size:12.5px;color:#555}
.strat-grid b{color:#222}
@media(max-width:900px){.layout{flex-direction:column}.sidebar{position:static;width:100%;max-height:none;display:flex;flex-wrap:wrap;align-items:center;padding:8px}.sidebar .brand{width:100%}.sidebar a.m-item{width:auto;margin:2px}.refresh-box{width:100%;border-top:none;margin-top:4px}}
h1{font-size:20px} h2{margin:14px 0 8px;font-size:18px} h3{margin:6px 0;font-size:15px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:12px 16px;margin:10px 0}
.sec{margin:18px 0}
.sec-head{cursor:pointer;user-select:none;padding:8px 10px;background:#fff;border:1px solid #e3e6ea;border-radius:8px}
.sec-head:hover{background:#eef4fb}
table{border-collapse:collapse;width:100%;font-size:12.5px;background:#fff}
th,td{border:1px solid #e3e6ea;padding:5px 7px;text-align:left;white-space:nowrap}
th{background:#f0f2f5}
.tbl-wrap{overflow-x:auto}
.code{color:#888} .muted{color:#888;font-size:13px;font-weight:normal}
.note{color:#777;font-size:12px}
.st{font-weight:bold}
.st.ok{color:#0a7a2e} .st.hold{color:#1a5fb4} .st.sell{color:#c22}
.st.warn{color:#b26a00} .st.gray{color:#999} .st.flat{color:#666} .st.up{color:#c22} .st.down{color:#0a7a2e} .st.ok{color:#0a7a2e}
.up{color:#c22} .down{color:#0a7a2e}
.rec-ok{color:#0a7a2e;font-weight:bold} .rec-warn{color:#b26a00;font-weight:bold} .rec-bad{color:#c22;font-weight:bold} .rec-gray{color:#888;font-weight:bold}
.idx-sum{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;color:#444;margin:6px 0}
.rng-switch{font-size:12px;color:#777;margin:4px 0 2px}
.rng-switch a{display:inline-block;padding:2px 10px;margin-right:6px;border:1px solid #ccc;border-radius:12px;text-decoration:none;color:#444}
.rng-switch a.on{background:#1a5fb4;color:#fff;border-color:#1a5fb4;font-weight:bold}
.prob-bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin:4px 0 2px;background:#eee;max-width:420px}
.prob-bar i{display:block;height:100%}
.pb-up{background:#d9534f}.pb-flat{background:#e6c34a}.pb-down{background:#57a05a}
.prob-legend{font-size:12px;margin:2px 0 4px}
.lg-up{color:#c22;margin-right:12px}.lg-flat{color:#8a6d1a;margin-right:12px}.lg-down{color:#0a7a2e}
.inv-advice{background:#fff7e6;border:1px solid #f0d9a8;border-radius:8px;padding:8px 12px;margin:10px 0;font-size:13px}
.inv-advice b{font-size:14px}
.fund-note,.official-note{background:#f6f8ff;border:1px solid #cfe0ff;border-radius:8px;padding:8px 12px;margin:10px 0;font-size:13px;line-height:1.7}
.official-note{background:#fff5f5;border-color:#f2c9c9}
.fund-note b,.official-note b{font-size:14px}
#info{font-size:13px;color:#555;margin:8px 0}
details{margin-top:8px} summary{cursor:pointer;color:#1a5fb4;font-size:13px}
ul{font-size:12px;color:#777;padding-left:20px}
.bp td{font-weight:bold}
.d-key{font-weight:bold}
tr.row-buy{background:#e6f6ec}
tr.row-sell{background:#fdeeee}
tr.row-hold{background:#eef4fb}
tr.row-warn{background:#fff7e6}
tr.row-plain{background:#fafafa;color:#777}
.nav a{display:inline-block;margin:2px 4px 2px 0;padding:3px 10px;background:#1a5fb4;color:#fff;border-radius:4px;text-decoration:none;font-size:13px}
.nav a:hover{background:#0e4a91}
.nav-btns{float:right}
.nav button{margin:2px 4px 2px 0;padding:5px 12px;border:1px solid #1a5fb4;border-radius:4px;background:#fff;color:#1a5fb4;cursor:pointer;font-size:13px}
.nav button:hover{background:#eef4fb}
#backTop{position:fixed;right:16px;bottom:16px;background:#1a5fb4;color:#fff;padding:8px 12px;border-radius:6px;text-decoration:none;font-size:13px;z-index:99}
#backTop:hover{background:#0e4a91}
.kp table th{background:#eef4fb}
.guide{background:#fffbe8;border-color:#f0e0a0}
.timing{background:#eef7f3;border-color:#bfe3d2}
.timing p{margin:6px 0;font-size:14px}
.timing table td.timing-tag{font-weight:bold;color:#0a5c30;white-space:nowrap}
.timing-badge{display:inline-block;background:#0a7a2e;color:#fff;border-radius:4px;padding:1px 8px;font-size:12px;font-weight:bold;margin-left:6px;vertical-align:1px}
.timing-tag{font-size:12px;font-weight:bold;color:#0a5c30;white-space:nowrap}
td.timing-tag{font-size:12px}
td.rec-buy{color:#b45309;font-size:12px}
tr.tot{background:#f0f4ff;font-weight:bold}
'''

JS = '''
function filterKey(btn){var on=btn.dataset.on==='1';btn.dataset.on=on?'0':'1';btn.textContent=on?'只看买卖时点':'显示全部行情';document.querySelectorAll('table.daily tbody tr').forEach(function(tr){if(on){tr.style.display='';}else{tr.style.display=(tr.classList.contains('row-buy')||tr.classList.contains('row-sell'))?'':'none';}});}
function refreshNow(btn){if(btn&&btn.disabled)return;if(btn){btn.disabled=true;btn.textContent='刷新中...';}var before=null,poll=0;fetch('/api/refresh',{method:'POST'}).then(function(r){return r.json();}).then(function(d){if(!d||!d.ok){alert('刷新失败：'+(d&&d.error?d.error:'未知错误'));if(btn){btn.disabled=false;btn.textContent='手动刷新';}return;}before=d.before||'';(function wait(){fetch('/api').then(function(r){return r.json();}).then(function(x){if(x.last_scan!==before){location.reload();return;}if(++poll<30){setTimeout(wait,3000);}else{if(btn){btn.disabled=false;btn.textContent='手动刷新';}alert('扫描仍在后台进行，稍后页面会自动更新');}}).catch(function(){if(btn){btn.disabled=false;btn.textContent='手动刷新';}alert('刷新失败，请检查网络/服务');});})();}).catch(function(){alert('刷新失败，请检查网络/服务');if(btn){btn.disabled=false;btn.textContent='手动刷新';}});}
function tip(ev,el){var t=document.getElementById('eTip');if(!t){t=document.createElement('div');t.id='eTip';t.style.cssText='position:fixed;pointer-events:none;background:rgba(30,30,30,.92);color:#fff;font-size:12px;padding:4px 8px;border-radius:4px;z-index:99;white-space:nowrap';document.body.appendChild(t);}t.innerHTML=el.getAttribute('data-tip');t.style.left=(ev.clientX+12)+'px';t.style.top=(ev.clientY+12)+'px';t.style.display='block';}
function tipHide(){var t=document.getElementById('eTip');if(t)t.style.display='none';}
function _ptsOf(el){try{return JSON.parse(el.getAttribute('data-pts')||'[]');}catch(e){return [];}}
function chartMove(ev,el){var svg=el.ownerSVGElement,pt={x:0,y:0};try{var r=svg.createSVGPoint();r.x=ev.clientX;r.y=ev.clientY;var p=r.matrixTransform(svg.getScreenCTM().inverse());pt.x=p.x;pt.y=p.y;}catch(e){}var pts=_ptsOf(el);if(!pts.length)return;var i=0,b=1e18;for(var k=0;k<pts.length;k++){var dx=Math.abs(pts[k][0]-pt.x);if(dx<b){b=dx;i=k;}}var p=pts[i],root=el.parentNode,v=root.querySelector('.cv'),hh=root.querySelector('.ch');if(v){v.setAttribute('x1',p[0]);v.setAttribute('x2',p[0]);v.setAttribute('y1',0);v.setAttribute('y2',2000);v.style.display='';}if(hh){hh.setAttribute('y1',p[1]);hh.setAttribute('y2',p[1]);hh.setAttribute('x1',0);hh.setAttribute('x2',2000);hh.style.display='';}var cs=root.querySelectorAll('circle.pt');for(var c=0;c<cs.length;c++){cs[c].setAttribute('r',c===i?4.5:2.2);}var t=document.getElementById('eTip');if(!t){t=document.createElement('div');t.id='eTip';t.style.cssText='position:fixed;pointer-events:none;background:rgba(30,30,30,.92);color:#fff;font-size:12px;padding:4px 8px;border-radius:4px;z-index:99;white-space:nowrap';document.body.appendChild(t);}t.innerHTML=p[2]+'<br>'+el.getAttribute('data-tlab')+' <b>'+Number(p[3]).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})+'</b>';t.style.left=(ev.clientX+12)+'px';t.style.top=(ev.clientY+12)+'px';t.style.display='block';}
function chartLeave(el){var root=el.parentNode,v=root.querySelector('.cv'),hh=root.querySelector('.ch');if(v)v.style.display='none';if(hh)hh.style.display='none';var cs=root.querySelectorAll('circle.pt');for(var c=0;c<cs.length;c++){cs[c].setAttribute('r',2.2);}tipHide();}
'''


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


def _nice_ticks(lo, hi, n=5):
    """在 [lo, hi] 之间生成约 n 个"好看"的等距刻度值（Y轴刻度）"""
    if hi <= lo:
        return [round(lo, 2)]
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(max(raw, 1e-9)))
    norm = raw / mag
    step = (1 if norm <= 1 else 2 if norm <= 2 else 5 if norm <= 5 else 10) * mag
    start = math.ceil(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step) + 1)]


def svg_line_chart(pts, base=None, base_label=None, tip_label='权益', h=190, ma=None):
    """pts: [(date_str, value)] → SVG 折线图
    - Y轴刻度/网格线、X轴首末日期、本金基准虚线、悬停显示日期+数值（红涨绿跌着色）
    - ma: 如 [5,20] → 叠加移动平均线（MA5橙/MA20蓝虚线，趋势平滑）"""
    if len(pts) < 2:
        return ''
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    if base is not None:
        lo, hi = min(lo, base), max(hi, base)
    span = max(hi - lo, 1e-9)
    pad_l, pad_r, pad_t, pad_b = 64, 14, 12, 24
    w, mw, ph = 1000, 1000 - pad_l - pad_r, h - pad_t - pad_b
    chg = vals[-1] - (base if base is not None else vals[0])
    color = '#c01c28' if chg >= 0 else '#0a7a2e'  # 红涨绿跌（A股习惯）
    step = max(1, len(pts) // 220)
    idxs = list(range(len(pts)))[::step]
    if idxs[-1] != len(pts) - 1:
        idxs.append(len(pts) - 1)
    sampled = [pts[j] for j in idxs]
    m = len(sampled) - 1

    def X(i):
        return pad_l + mw * i / m if m else pad_l

    def Y(v):
        return pad_t + ph * (1 - (v - lo) / span)

    poly = ' '.join(f'{X(i):.1f},{Y(v):.1f}' for i, (_, v) in enumerate(sampled))
    grid = ''
    for v in _nice_ticks(lo, hi):
        y = Y(v)
        grid += (f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" stroke="#eee"/>'
                 f'<text x="{pad_l - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="10" fill="#888">{v:,.0f}</text>')
    base_line = ''
    if base is not None:
        yb = Y(base)
        base_line = (f'<line x1="{pad_l}" y1="{yb:.1f}" x2="{w - pad_r}" y2="{yb:.1f}" '
                     f'stroke="#bbb" stroke-dasharray="4,3"/>'
                     f'<text x="{pad_l + 4}" y="{yb - 4:.1f}" font-size="10" fill="#999">'
                     f'{html.escape(base_label or f"{base:,.0f}")}</text>')
    xlab = (f'<text x="{pad_l}" y="{h - 7}" font-size="10" fill="#888">{html.escape(pts[0][0])}</text>'
            f'<text x="{w - pad_r}" y="{h - 7}" text-anchor="end" font-size="10" fill="#888">{html.escape(pts[-1][0])}</text>')
    # MA 均线（基于全量序列计算，只画采样点处取值；MA5橙 / MA20蓝 虚线 + 图例）
    ma_html, legend_html = '', ''
    if ma:
        ma_colors = {5: '#d95f02', 20: '#2b6cb0'}
        lg_items = []
        for n in ma:
            mv = [None] * len(pts)
            run = 0.0
            for j, v in enumerate(vals):
                run += v
                if j >= n:
                    run -= vals[j - n]
                if j >= n - 1:
                    mv[j] = run / n
            ptsm = [(i, mv[j]) for i, j in enumerate(idxs) if mv[j] is not None]
            if len(ptsm) >= 2:
                c_ma = ma_colors.get(n, '#888')
                p = ' '.join(f'{X(i):.1f},{Y(v):.1f}' for i, v in ptsm)
                ma_html += (f'<polyline points="{p}" fill="none" stroke="{c_ma}" '
                            f'stroke-width="1.2" stroke-dasharray="5,3" opacity="0.9"/>')
                lg_items.append((c_ma, f'MA{n}'))
        if lg_items:
            lx = w - pad_r - 62 * len(lg_items)
            ly = pad_t + 6
            for k, (c_ma, lab) in enumerate(lg_items):
                x0 = lx + k * 62
                legend_html += (f'<line x1="{x0}" y1="{ly}" x2="{x0 + 16}" y2="{ly}" '
                                f'stroke="{c_ma}" stroke-width="2"/>'
                                f'<text x="{x0 + 20}" y="{ly + 3}" font-size="10" fill="#888">{lab}</text>')
    # 数据点 + 十字线（鼠标在图表范围内移动即显示 X/Y 定位线，无需精确对准数据点）
    circ = ''
    pts_json = []
    for i, (dt, v) in enumerate(sampled):
        x, y = X(i), Y(v)
        circ += (f'<circle class="pt" cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="#fff" stroke="{color}" '
                 f'stroke-width="1.2"/>')
        pts_json.append([round(x, 1), round(y, 1), dt, round(v, 2)])
    crosshair = (f'<line class="cv" x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{h - pad_b}" '
                 f'stroke="#888" stroke-width="0.8" stroke-dasharray="3,3" style="display:none"/>'
                 f'<line class="ch" x1="{pad_l}" y1="{pad_t}" x2="{w - pad_r}" y2="{pad_t}" '
                 f'stroke="#888" stroke-width="0.8" stroke-dasharray="3,3" style="display:none"/>')
    overlay = (f'<rect x="{pad_l}" y="{pad_t}" width="{mw:.1f}" height="{ph:.1f}" fill="transparent" '
               f'style="cursor:crosshair" '
               f'data-pts="{html.escape(json.dumps(pts_json, ensure_ascii=False))}" '
               f'data-tlab="{html.escape(tip_label)}" '
               f'onmousemove="chartMove(event,this)" onmouseleave="chartLeave(this)"/>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto" role="img" aria-label="资金曲线">'
            f'{grid}{base_line}{xlab}'
            f'{ma_html}'
            f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.6"/>'
            f'{legend_html}{crosshair}{circ}{overlay}</svg>')


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
            f'<span class="muted">共 {d.get("n_stocks", 0)} 只 · 基日 {d.get("base_date", "-")} = {d.get("base", 100)} 点</span></h3>'
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
    rows.sort(key=lambda r: (r['buy_date'] or '', r['sid']), reverse=True)
    return rows


def _num2(x):
    """金额/价格显示：None→'—'，字符串原样，数字保留2位小数"""
    if x is None:
        return '—'
    if isinstance(x, str):
        return x
    try:
        return '%.2f' % float(x)
    except Exception:
        return str(x)


def _ledger_row(r):
    nm = html.escape(str(r.get('name') or '—'))
    cd = str(r.get('code') or '').strip()
    sym = f'<b>{nm}</b>' if not cd else f'<b>{nm}</b> <span class="muted">{cd}</span>'
    st = r.get('state', '')
    st_cls = 'hold' if '持仓' in st else 'gray'
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
    覆盖：①今日盘中实时触发（TODAY_STATUS，与提醒卡/微信同源）；②各策略最新行仍待执行的
    买入信号/卖出信号/未决换仓候选。买入成交/卖出成交由下方成交流水在成交后自动记录。"""
    try:
        rows_html = []
        ts = TODAY_STATUS or {}
        sig = ts.get('signal') or {}
        rot = ts.get('rotation') or ''
        if sig.get('triggered') and sig.get('name') and rot:
            _d0 = str(ts.get('date') or '')
            _ex = _next_td(_d0)
            _sid = rot.split('_')[0]
            _short = rot.split('_', 1)[1] if '_' in rot else ''
            rows_html.append((
                f'<a href="/s/{urllib.parse.quote(rot)}">{html.escape(_sid + " " + _short)}</a>',
                '买入信号', '盘中触发（待收盘确认）',
                f'<b>{html.escape(str(sig.get("name")))}</b>'
                f'<span class="muted">（{html.escape(str(sig.get("code") or ""))}）</span>',
                f'{_md(_d0)} 盘中',
                f'现价 {sig.get("price", "—")} ｜ 评分 {sig.get("score", "—")}'
                f' —— 收盘确认后于 {_md(_ex)} 按策略时点执行（T+1）'))
        for s in HISTORY['strategies']:
            dl = s.get('daily') or []
            if not dl:
                continue
            last = dl[-1]
            st = str(last.get('status') or '')
            nm = str(last.get('name') or '')
            d0 = str(last.get('date') or '')
            ex = _next_td(d0)
            _sid = s['name'].split('_')[0]
            _short = s['name'].split('_', 1)[1] if '_' in s['name'] else ''
            _slink = f'<a href="/s/{urllib.parse.quote(s["name"])}">{html.escape(_sid + " " + _short)}</a>'
            if st in ('买入信号', '买入信号(待成交)') and nm:
                rows_html.append((
                    _slink, '买入信号', '收盘已判定（待执行）',
                    f'<b>{html.escape(nm)}</b>', f'{_md(d0)} 收盘',
                    f'信号确认，{_md(ex)} 按策略时点买入（T+1，不追价）'))
            elif st == '卖出信号' and nm:
                rows_html.append((
                    _slink, '卖出信号', '收盘判定（待执行）',
                    f'<b>{html.escape(nm)}</b>', f'{_md(d0)} 收盘',
                    f'判定离场，{_md(ex)} 按策略时点卖出'))
            # 未决换仓候选 → 独立「买入信号」登记（9/8 建仓龙版传媒→百大集团 场景）
            rb = str(last.get('rec_buy') or '').strip()
            if rb and not rb.startswith('已换仓') and '已废弃' not in rb and '（' in rb \
                    and st in ('建仓', '卖出成交', '卖出信号', '卖出顺延', '持有'):
                _cn = rb.split('（', 1)[0].strip()
                if _cn and _cn not in ('—', '-'):
                    _tag = '卖出顺延' if st == '卖出顺延' else '换仓候选'
                    rows_html.append((
                        _slink, '买入信号', f'{_tag}（收盘判定）',
                        f'<b>{html.escape(_cn)}</b>'
                        f'<span class="muted">{html.escape(rb[rb.find("（"):])}</span>',
                        f'{_md(d0)} 收盘',
                        f'{_md(d0)} 动作扫出的换仓候选：{_md(ex)} 先卖出持仓、成交后换仓买入'
                        '（卖出受阻则顺延；顶板/低开证伪则废弃）'))
        if not rows_html:
            return ''
        body = ''
        for _sl, _type, _tag, _nm, _tm, _desc in rows_html:
            _cls = {'买入信号': 'ok', '卖出信号': 'sell'}.get(_type, '')
            body += (f'<tr><td class="note">{_sl}</td>'
                     f'<td><b class="st {_cls}">{_type}</b>'
                     f'<span class="muted">（{html.escape(_tag)}）</span></td>'
                     f'<td>{_nm}</td><td>{html.escape(_tm)}</td>'
                     f'<td class="note">{html.escape(_desc)}</td></tr>')
        return ('<details open style="margin:2px 0 10px"><summary><b>📌 信号登记</b>'
                '<span class="muted">（盘中触发/收盘判定的买入、卖出信号在<b>当日即登记</b>，不用等收盘'
                '模拟成交；下方成交流水继续记录买入成交/卖出成交）</span></summary>'
                '<div class="tbl-wrap"><table><thead><tr><th>策略</th><th>记录类型</th>'
                '<th>标的</th><th>登记时点</th><th>说明 / 下一步</th></tr></thead><tbody>'
                + body + '</tbody></table></div></details>')
    except Exception:
        return ''


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
_SW_BUY_RE = re.compile(r'；同日(开盘|收盘)(\d+\.\d+)换仓买入([^\s（(]+)\s*(\d+)股[（(]投入([\d.]+)元[)）]')

_NAME2CODE_CACHE = None


def _name2code(name):
    """按股票名称反查代码（全市场懒加载一次并缓存）。换仓买入行里新标的的
    代码不在结构化字段中（引擎记录的 name/code 是卖出标的），据此补正确代码。"""
    global _NAME2CODE_CACHE
    if _NAME2CODE_CACHE is None:
        try:
            import history_engine as _he
            _NAME2CODE_CACHE = {n: c for c, n in _he.STOCKS.items()}
        except Exception:
            _NAME2CODE_CACHE = {}
    return _NAME2CODE_CACHE.get(str(name))


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
    v['name'] = mb.group(3)
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
    """最新行携带的「未决换仓候选」→ 独立「买入信号」展示行（2026-09-09 用户要求）。

    场景：S16 龙头 9/8 早盘建仓龙版传媒、当日收盘断板扫出换仓候选百大集团——
    此前只在「同日买入推荐」列出现一行小字，买卖时点速览没有独立记录；
    现把该候选作为一条「买入信号」行置顶登记（9/9 开盘卖出持仓成交后换仓买入）。
    候选已废弃/已成交时最新行会演进出新状态（信号废弃/换仓买入…），此候选行自然消失。"""
    try:
        rb = str((last or {}).get('rec_buy') or '').strip()
        if not rb or rb.startswith('已换仓') or '已废弃' in rb or '（' not in rb:
            return None
        st = str((last or {}).get('status') or '')
        if st not in ('建仓', '卖出成交', '卖出信号', '卖出顺延', '持有'):
            return None
        nm = rb.split('（', 1)[0].strip()
        if not nm:
            return None
        d0 = str((last or {}).get('date') or '')
        ex = _next_td(d0)
        exs = f'{_md(ex)} 开盘' if ex else '下一交易日开盘'
        if st == '卖出顺延':
            why = (f'{_md(d0)} 收盘扫出的换仓候选：持仓股停牌致卖出顺延，模拟换仓未执行'
                   f'（信号保留至复牌日）；若你无持仓可自行参考该龙头 {exs}，不追高、开盘顶板买不进即放弃')
        else:
            why = (f'{_md(d0)} 收盘判定：{exs} 先卖出当前持仓、成交后换仓买入该候选'
                   '（模拟先卖后买；若持仓当日卖出受阻则顺延，开盘顶板/低开证伪则废弃）')
        return {'_cand': True, 'status': '买入信号', 'date': d0,
                'name': f'{nm}{rb[rb.find("（"):]}', 'note': why}
    except Exception:
        return None


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
    buy_name = mb.group(3)
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
        if '已换仓' in str(r['rec_buy']):
            note += f"；同日已自动换仓买入 {r['rec_buy']}"
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
            break
        if '换仓买入' in str(r.get('note', '')) and r.get('deal_price') is not None:
            # 换仓行 name 是卖出标的，当前持仓名须从 note 解析（如"换仓买入万向德农 600股"）
            buy_date, buy_price = r['date'], r.get('deal_price')
            invest = r.get('invest')
            _n = str(r['note']).split('换仓买入')[-1].split(' ')[0].strip()
            _m = re.search(r'换仓买入\S*\s*(\d+)股', str(r.get('note', '')))
            shares = int(_m.group(1)) if _m else None
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
                f'<td>{t["buy_price"]}</td>'
                f'<td><b class="st ok">持仓中</b></td><td>-</td>'
                f'<td>{t.get("shares", "-")}</td><td>{t.get("invest", "-")}</td>'
                f'<td>-</td><td class="{pc}">{pct_s}</td><td>-</td>'
                f'<td>当前持仓（截至 {t["last_date"]}，未实现浮盈随行情更新）</td></tr>')
    pnl = t.get('net_pnl') or 0
    cls = 'up' if pnl > 0 else 'down'
    nm = html.escape(str(t.get('stock_name') or ''))
    cd = str(t.get('stock_code') or '').strip()
    sym = f'<b>{nm}</b>' if not cd else f'<b>{nm}</b> <span class="muted">{cd}</span>'
    return (f'<tr><td>{sym}</td><td class="d-key">{t["buy_date"]}</td><td>{t["buy_price"]}</td>'
            f'<td class="d-key">{t["sell_date"]}</td><td>{t["sell_price"]}</td>'
            f'<td>{t["shares"]}</td><td>{t["invest"]}</td>'
            f'<td class="{cls}">{pnl:+.2f}</td>'
            f'<td class="{cls}">{t.get("ret_pct", 0):+.2f}%</td>'
            f'<td>{t["hold_days"]}天</td>'
            f'<td>{html.escape(t.get("sell_tag", ""))}</td></tr>')


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


def keypoints_table(daily, max_shown=5, timing=None):
    """买卖时点速览：从每日记录中筛出买入/卖出信号与成交日，按时间倒序；
    换仓日（卖出成交+同日换仓买入）拆成两行：换仓买入行在上、卖出成交行在下。"""
    rows = [r for r in daily if r['status'] in KEY_STATES]
    if not rows:
        return ''
    rows = list(reversed(rows))  # 最新在上
    # 换仓日拆成两笔展示（卖出成交+换仓买入），计数/切片按拆后操作计，与表内行数一致
    rows_all = [rr for r in rows for rr in _split_switch(r)]
    # ★ 换仓候选独立「买入信号」行（2026-09-09）：最新行 rec_buy 为未决候选时
    # 置顶登记（如 9/8 建仓龙版传媒收盘扫出百大集团 → 表内新增 买入信号：百大集团）
    _cand = _candidate_row(daily[-1]) if daily else None
    if _cand:
        rows_all.insert(0, _cand)
    rows = rows_all
    nb = sum(1 for r in rows if r['status'] in BUY_STATES)
    ns = len(rows) - nb

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
            if '已换仓' in str(rec_buy):
                rec_td = (f'<td class="rec-buy">🔄 已换仓买入：{_esc_note(str(rec_buy))}'
                          f'<br><span class="muted">同日收盘先卖后买（自动）</span></td>')
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
                f'<td>{_t1_html(r)}</td>{pnl_td}{rec_td}'
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
        # 官方回测口径汇总（真实成交校验：开盘顶板买不进/低开>3%证伪即废弃；2026-09-07 全史重放确认维持放弃式）
        head += ('<div class="official-note"><b>官方回测口径（2026-09-04 起与落地引擎同规则）</b>：'
                 '信号次日若开盘顶板(≥+9.8%)买不进、或低开>3%证伪，该信号<b>废弃、不计成交</b>并记录原因。'
                 '全史重放（2019-01 起·沪深主板 2614 只，数据至 2026-09-04）同底统计：'
                 '<b>base 642笔 净 -18,087 胜率37.5%</b>（顶板买不进废弃755次/低开189次）；'
                 '<b>V2 563笔 净 -15,868 胜率38.5%</b>（废弃748次/172次）。'
                 '此前按"开盘必成交"回测的 +16.5万/+16.8万，恰是买进了真实世界买不到的顶板龙头，已作废。'
                 '★2026-09-07 曾试点"最强龙头买不进→自动顺延队内第2~5强开盘可买者"：'
                 '2614 只全史重放证明顺延多成交的 562/293 笔净亏约 -26.0万/-23.6万'
                 '（接盘的是开盘即弱于龙头的备选）→ 维持<b>每日只买最强龙头、命中不了即放弃</b>。'
                 '<br><b>每日候选机制</b>：每策略每交易日只选全市场涨停池<b>最强龙头 1 只</b>，命中不了就放弃：'
                 '次日集合竞价(9:15-9:25)以参考价/涨停价限价挂单，开盘未顶板即按开盘价成交；'
                 '开盘顶板买不进或低开&gt;3%则当日放弃并记录废弃原因，收盘后按当日涨停池重选次日候选'
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
        if _rb and '被' not in str(_rb) and '已换仓' not in str(_rb):
            _st = str(last.get('status') or '')
            _cn, _, _cs = str(_rb).partition('（')
            _cs = _cs.rstrip('）')
            if _cs.startswith('评分'):
                _cs = _cs[2:]
            _tt = f'<b>{html.escape(str(_cn).strip())}</b>'
            if _cs:
                _tt += f'<span class="muted">（评分 {html.escape(_cs)}）</span>'
            if _st == '卖出顺延':
                _hint = ('模拟持仓股停牌/无行情致卖出顺延，换仓未执行（挂单保留至复牌日，信号不废弃）；'
                         '该候选<b>今日实际可买</b>——若你未持有该持仓股，可自行参考：开盘顶板(≥+9.8%)买不进'
                         '或低开&gt;3%证伪即放弃，不追高')
                _hc = '#e8a13c'
            else:
                _ex = _next_td(last.get('date'))
                _hint = (f'模拟账本将在 {_md(_ex)} 开盘先卖出当前持仓、再换仓买入该候选'
                         '（若届时持仓可正常卖出；开盘顶板买不进或低开&gt;3%证伪则废弃）')
                _hc = '#2b7ae2'
            card += (f'<div style="margin:8px 0 2px;padding:8px 12px;border-left:4px solid {_hc};'
                     f'background:#f3f7ff;border-radius:4px">'
                     f'🎯 <b>换仓候选 / 买入信号提示</b>：{_tt}'
                     f'<span class="muted">（{html.escape(str(last.get("date") or ""))} 收盘）</span><br>'
                     f'<span class="muted" style="font-size:12px">{_hint}</span></div>')
    except Exception:
        pass
    kp = keypoints_table(daily, timing=_tm)
    if not kp:
        kp = ('<div class="card kp"><h3>买卖时点速览</h3>'
              f'<p class="muted">{SIM_LABEL}（{SIM_START}）以来暂无买卖时点'
              f'（当前状态：{html.escape(daily[-1]["status"])}）</p></div>')
    # 每日选股：先合并连续空仓/观望段与连续持有（同标的）段为区间行（仅展示层），再默认展示最近10条
    daily_disp = _fold_rows(daily)
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
    menu += _a('/s15ab', '🔬 S15 回测对照', ' lane')
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
    return ('<!DOCTYPE html>\n<html lang="zh"><head><meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f'<title>{html.escape(title)}</title>\n'
            f'<style>{CSS}</style></head><body>\n'
            f'<div class="layout">{sidebar_html(active)}<main class="content">{body}</main></div>\n'
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
        for s in HISTORY['strategies']:
            daily = s.get('daily') or []
            if not daily:
                continue
            last = daily[-1]
            L = str(last.get('date') or '')
            _ref = L if L > _ref else _ref
            lv = _switch_view(last)
            st = str((lv or last).get('status') or '')
            nm = html.escape(str((lv or last).get('name') or ''))
            cd = html.escape(str((lv or last).get('code') or ''))
            sc = last.get('score')
            sc_txt = f' 评分{sc}' if sc is not None else ''
            sid = s['name'].split('_')[0]
            short = html.escape(s['name'].split('_', 1)[1])
            link = f'<a href="/s/{urllib.parse.quote(s["name"])}">{sid} {short}</a>'
            tm = html.escape(str(s.get('timing') or ''))
            head = f'{link}<span class="muted">（{tm}）</span>'
            # ★ 今日(L==today)回放行记录的是"当日已发生的成交"：卖出成交/建仓/换仓买入
            # 必须按动作列出（换仓日拆出『已买入新标的 + 已卖出旧标的』两条），
            # 不能沿用 _switch_view 的"持仓(换仓后)"口径——否则当天换仓买入会被
            # 归入持仓组，首页误报"今日无需买卖操作"（2026-09-03 新赛股份换仓案例）。
            if L == today and str(last.get('status') or '') in ('卖出成交', '建仓', '换仓买入'):
                # 先卖后买：引擎真实顺序是先平仓旧标的、再按推荐换仓买入新标的
                for _p in reversed(_split_switch(last)):
                    _ps = str(_p.get('status') or '')
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
                continue
            if st in ('买入信号', '买入信号(待成交)'):
                ex = _next_td(L)
                if ex == today:
                    _act.append(('buy', f'{head} → <b class="st ok">{_md(ex)}买入 {nm}（{cd}）{sc_txt}</b>'
                                        f'：{_md(L)} 收盘信号确认，{_md(ex)}按本策略时点成交，不追价'))
                else:
                    _fut.append(('buy', f'{head} → <b class="st ok">{_md(ex)}买入 {nm}（{cd}）{sc_txt}</b>'
                                        f'：{_md(L)} 收盘确认，{_md(ex)}按本策略时点执行'))
            elif st == '卖出信号':
                ex = _next_td(L)
                if ex == today:
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
                    _hold.append(f'{head} 持仓 {nm}')
            elif st == '换仓买入':
                _hold.append(f'{head} 持仓（换仓后）{nm}（{cd}）')
            elif st == '卖出成交':
                if L == today:
                    _done.append(('sell', f'{head} → <b class="st sell">{_md(L)}已卖出 {nm}</b>'))
                else:
                    _idle.append(f'{head} 空仓（{_md(L)} 已卖出）')
            elif st == '卖出顺延':
                _hold.append(f'{head} 持仓 {nm}（{cd}）——持仓股停牌、卖出信号顺延至复牌日（详见详情）')
            elif st == '持有':
                _hold.append(f'{head} 持仓 {nm}'
                             + (f' 浮盈{last["pos_pnl_pct"]:+.1f}%'
                                if last.get('pos_pnl_pct') is not None else ''))
            else:
                _idle.append(f'{head} ' + {'空仓': '空仓等信号', '观望': '观望',
                                           '资金不足': '资金不足',
                                           '信号废弃': '昨日买入信号已废弃（未买入）'}.get(st, '空仓等信号'))
        # 今日盘中新触发（统一今日状态，收盘确认后明日才执行，与"今日要买什么"区分开）
        rot_sig = ''
        ts = TODAY_STATUS or {}
        _sig = ts.get('signal') or {}
        _rot = ts.get('rotation') or ''
        if _sig.get('triggered') and _sig.get('name') and _rot:
            dup = False
            rs = next((x for x in HISTORY['strategies'] if x['name'] == _rot), None)
            if rs and rs.get('daily'):
                rl = rs['daily'][-1]
                if (str(rl.get('date')) == today and rl.get('status') in ('买入信号', '买入信号(待成交)')
                        and str(rl.get('name')) == str(_sig.get('name'))):
                    dup = True  # 收盘后今日行已体现同标的买入信号，不重复提示
            if not dup:
                ex = _next_td(today)
                _rsid = _rot.split('_')[0]
                _rshort = html.escape(_rot.split('_', 1)[1]) if '_' in _rot else ''
                rot_sig = (f'<p class="st warn">⚠️ {_md(today)}盘中刚触发（{_md(ex)}执行，勿提前操作）：'
                           f'<b>{_rsid} {_rshort}</b> {html.escape(str(_sig.get("name") or ""))}'
                           f'（{html.escape(str(_sig.get("code") or ""))}）'
                           f'现价 {_sig.get("price", "—")} 评分 {_sig.get("score", "—")} —— '
                           f'收盘确认后按该策略时点在 {_md(ex)} 执行（T+1）。</p>')
        h = (f'<div class="card" style="border-left:4px solid #2b7ae2">'
             f'<h2>📋 今日行动清单 · {_md(today)}'
             '<span class="muted">——早盘/每次刷新先看这里：按日期核对买/卖/持仓动作</span></h2>')
        if rot_sig:
            h += rot_sig
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
        yrec = next((r for r in s['daily'] if r['date'] == y_str), None)
        if yrec is None or not yrec.get('triggered'):
            return ''
        # 今日(T+1)是否已按昨日信号买入成交（引擎已模拟：状态为建仓/买入成交 或 有 invest 字段）
        t_str = today.strftime('%Y-%m-%d')
        trec = next((r for r in s['daily'] if r['date'] == t_str), None)
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
                f'（评分 {yrec.get("score", "-")}）；{html.escape(str(yrec.get("note", "")))}'
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
        # ★ 今日盘中信号（2026-09-03）：本策略为今日轮换且统一今日状态有 signal 时，
        #   详情页同样展示（与首页「今日盘中提醒」卡同源），避免"首页提醒了、详情页看不到"
        sig_html = ''
        if is_rot and (ts.get('signal') or {}).get('name'):
            _sig = ts['signal']
            _nm = html.escape(str(_sig.get('name') or '—'))
            _cd = html.escape(str(_sig.get('code') or ''))
            _px = _sig.get('price') if _sig.get('price') is not None else '—'
            _sc = _sig.get('score') if _sig.get('score') is not None else '—'
            if _sig.get('triggered'):
                _st_txt = ('<b class="ok">已触发买入信号</b>'
                           '（收盘确认后，下一交易日 T+1 按策略时点执行）')
            else:
                _st_txt = ('<b class="warn">接近买入（未触发）</b>，按规则不买入、继续观察')
            sig_html = (f'<p class="note">📌 今日盘中信号（{_md(str(ts.get("date") or ""))}）：'
                        f'<b>{_nm}（{_cd}）</b> 现价 {_px} 评分 {_sc} —— {_st_txt}；'
                        f'上方各卡片为<b>模拟记录</b>（截止 %s 最近收盘），今日信号待收盘确认后计入「每日选股与状态」。'
                        % html.escape(str(last.get('date') or '—')) + '</p>')
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
                '<p class="note">执行时点：%s；信号收盘后判定、于下一交易日（T+1）按此时点执行，'
                '不追价、盘中不改单。</p></div>'
                % (html.escape(str(last.get('date') or '—')), lc, html.escape(_st),
                   nm, act_cls, act, rot_txt, env_fit, win_html, sig_html, tm))
    except Exception:
        return ''


def render_page(path='/', query=None):
    global PAGE_CACHE
    today = datetime.date.today().isoformat()
    key = (path, json.dumps(query or {}, sort_keys=True, ensure_ascii=False))
    hit = PAGE_CACHE.get(key)
    if hit is not None and hit[0] == today:
        return hit[1]
    if path == '/s15ab':                      # ★ S15 可做窗口(gtma20)变体对照回测报告页
        body = page_frame('S15 回测对照（可做窗口 gtma20）', '/s15ab',
                          render_bt_s15(), is_trading_time())
    elif path.startswith('/s/'):
        body = render_strategy(path, query)
    else:
        body = render_index(query or {})
    data = body.encode('utf-8')
    PAGE_CACHE[key] = (today, data)
    return data
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
            data_line = (f'<p class="note"><b>判断数据：</b>估值截至 {rec.get("date", "-")}'
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
                    f'<tr><td>{rec.get("price", "-")}</td><td>{rec.get("buy_ref", "-")}</td>'
                    f'<td>{rec.get("stop") if rec.get("stop") is not None else "-"}</td>'
                    f'<td>{rec.get("target") if rec.get("target") is not None else "动态"}</td>'
                    f'<td>{rec.get("score", "-")}</td></tr></table>'
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

    def _respond(self, ctype, body):
        try:
            self.send_response(200)
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
            elif path == '/s15ab':           # ★ S15 回测对照报告页（统一外壳）
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
            traceback.print_exc()   # 真实服务错误保留可见，便于排查
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
        else:
            try:
                self.send_response(404)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, *a):
        pass


def boot_scan():
    """首次扫描放后台执行：akshare 拉指数估值可能很慢，不能阻塞 HTTP 端口监听"""
    try:
        do_scan(force=True)
    except Exception as e:
        log(f'首次扫描异常: {str(e)[:60]}')
        notify.send_error_alert(f'首次扫描异常: {str(e)[:200]}')


if __name__ == '__main__':
    print('启动策略选股回放服务 http://127.0.0.1:%d' % PORT)
    # 2026-08-28 起不再推送启动提示（启动时人就在电脑前），异常/简报仍正常推送
    ensure_history()
    threading.Thread(target=scan_thread, daemon=True).start()
    # 先启动 HTTP 服务（端口立即监听），首次扫描在后台完成后再刷新页面
    threading.Thread(target=boot_scan, daemon=True).start()
    # ★ 2026-09-03：ThreadingHTTPServer（每请求独立线程）——后台扫描/当日重建再久也不会阻塞页面。
    #   渲染本身有 PAGE_CACHE 兜底；STATE/HISTORY 均以「整体替换引用」方式更新，多线程读取安全
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
