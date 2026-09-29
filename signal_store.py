# -*- coding: utf-8 -*-
"""盘中信号 SQLite 存储层（标准库 sqlite3，零第三方依赖）。

为什么换掉 JSON（2026-09-10）：
- 旧实现 data/intraday_signals.json 只存"当日"，_ev_state() 跨日直接重建 →
  **昨天的信号今天就查不到了**；更早的 notify_signal_sent.json 旧去重机制在
  信号消失时**直接删除记录**（用户反馈的"华金资本明明推荐过，却哪里都查不到"）。
- 现在落 SQLite：一条信号 = 一行，按 trade_date 分区，**历史永不因跨日丢失**。

★ 删除语义（用户明确要求）：**不做物理删除**，一律改标志位——
  - gone    = 1  本轮不再触发（信号短暂消失/换标的），页面仍显示"曾触发过"
  - deleted = 1  人工停用（误报等），默认查询隐藏，include_deleted=True 仍可调取
  想恢复：restore(id)。想真删：只能手动动库文件（本模块不提供物理删除入口）。

表结构 intraday_signal：
  id / trade_date / sname / sid / code / name / score / price / buy_ref / stop /
  target / tag / note / first_at / last_at / cnt / pushed / pushed_at /
  gone / gone_at / gone_reason / deleted / deleted_at / deleted_reason /
  created_at / updated_at，唯一键 (trade_date, sname, code)

★ 2026-09-18 追加「前向收益 / 收盘确认」列（旧库自动 ALTER 补齐，见 _ensure_cols）：
  close_px      触发日收盘价（前向收益基准）
  t1_ret/t3_ret/t5_ret   自触发日收盘起第 1/3/5 个**交易日**收盘的涨跌幅（%，qfq）
  fwd_date      前向计算实际用到的最后一个交易日（数据未入库时 < 目标日）
  confirmed     收盘判定是否采纳本行标的（1=是，0/空=否）
  confirm_px    收盘确认价（优先 T+1 实际成交价 deal_price，退回触发日收盘）
  confirm_name  当日收盘判定最终采纳的标的名（可能与本行不同）
  watch         1=**持仓跟踪**行（非轮值策略盘中留痕）：不推送、不算待执行买入信号
  evaluated_at  前向收益回填时间；为空表示"数据未到齐，下次再算"

意义：此前库里只有触发瞬间的价格，历史只能"看"（审计），无法"评估"。
现在 /signals 页能直接给出"盘中触发 → 后续 T+1/T+3/T+5 表现 + 收盘确认率"。
"""
import contextlib
import datetime
import json
import math
import os
import sqlite3
import threading

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_FILE = os.path.join(_WORKSPACE, 'data', 'signals.db')
_JSON_EV = os.path.join(_WORKSPACE, 'data', 'intraday_signals.json')
_JSON_SIG = os.path.join(_WORKSPACE, 'data', 'notify_signal_sent.json')
_MIGRATED_FLAG = os.path.join(_WORKSPACE, 'data', '_signal_store_migrated.json')

_LOCK = threading.RLock()
_ready = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_signal (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date     TEXT    NOT NULL,
    sname          TEXT    NOT NULL,
    sid            TEXT,
    code           TEXT    NOT NULL,
    name           TEXT,
    score          REAL,
    price          REAL,
    buy_ref        REAL,
    stop           REAL,
    target         REAL,
    tag            TEXT,
    note           TEXT,
    first_at       TEXT,
    last_at        TEXT,
    cnt            INTEGER NOT NULL DEFAULT 0,
    pushed         INTEGER NOT NULL DEFAULT 0,
    pushed_at      TEXT,
    gone           INTEGER NOT NULL DEFAULT 0,
    gone_at        TEXT,
    gone_reason    TEXT,
    deleted        INTEGER NOT NULL DEFAULT 0,
    deleted_at     TEXT,
    deleted_reason TEXT,
    close_px       REAL,
    t1_ret         REAL,
    t3_ret         REAL,
    t5_ret         REAL,
    fwd_date       TEXT,
    confirmed      INTEGER,
    confirm_px     REAL,
    confirm_name   TEXT,
    watch          INTEGER,
    evaluated_at   TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    UNIQUE (trade_date, sname, code)
);
CREATE INDEX IF NOT EXISTS idx_sig_date  ON intraday_signal (trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_sig_sname ON intraday_signal (sname);
CREATE INDEX IF NOT EXISTS idx_sig_flag  ON intraday_signal (deleted, gone);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

# 对外返回的字段（与原 JSON 事件流字段名保持一致，避免调用方改动）
_EV_KEYS = ('sname', 'sid', 'code', 'name', 'score', 'price', 'buy_ref', 'stop',
            'target', 'tag', 'note', 'first_at', 'last_at', 'count',
            'pushed', 'pushed_at', 'gone', 'gone_at', 'gone_reason')

# ★ 2026-09-18 前向收益 / 收盘确认 / 持仓跟踪（随行返回，页面直接用）
_FWD_KEYS = ('close_px', 't1_ret', 't3_ret', 't5_ret', 'fwd_date',
             'confirmed', 'confirm_px', 'confirm_name', 'watch', 'evaluated_at')

# 旧库升级用：缺哪些列就 ALTER 哪些（SQLite 不支持 ADD COLUMN IF NOT EXISTS）
_FWD_COLS = (
    ('close_px', 'REAL'), ('t1_ret', 'REAL'), ('t3_ret', 'REAL'), ('t5_ret', 'REAL'),
    ('fwd_date', 'TEXT'), ('confirmed', 'INTEGER'), ('confirm_px', 'REAL'),
    ('confirm_name', 'TEXT'), ('watch', 'INTEGER'), ('evaluated_at', 'TEXT'),
)

# 收盘判定里"确认采纳了买入信号"的状态（用于 confirmed 判定）
CONFIRM_STATUSES = ('买入信号', '买入信号(待成交)')

_CSV_DIR = os.path.join(_WORKSPACE, 'data', 'csv')
_DAILY_JSON = os.path.join(_WORKSPACE, 'data', 'strategy_daily.json')

_FWD_LOCK = threading.RLock()


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _today():
    return datetime.date.today().isoformat()


def _conn():
    d = os.path.dirname(_DB_FILE)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(_DB_FILE, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')      # 多线程/多进程读写不互相阻塞
    c.execute('PRAGMA busy_timeout=15000')
    c.execute('PRAGMA synchronous=NORMAL')
    return c


@contextlib.contextmanager
def _db():
    """连接上下文：正常提交/异常回滚，**退出即关闭连接**（sqlite3 的 with conn 只提交不关闭）"""
    c = _conn()
    try:
        with c:
            yield c
    finally:
        c.close()


def _row_to_ev(r):
    """DB 行 → 原 JSON 事件 dict（count 由 cnt 映射，pushed/gone 还原为 bool）"""
    d = {k: r[k] for k in _EV_KEYS if k in r.keys()}
    d['count'] = int(r['cnt'] or 0)
    d['pushed'] = bool(r['pushed'])
    d['gone'] = bool(r['gone'])
    d['trade_date'] = r['trade_date']
    d['id'] = r['id']
    d['deleted'] = bool(r['deleted'])
    d['deleted_at'] = r['deleted_at']
    d['deleted_reason'] = r['deleted_reason']
    for _k in _FWD_KEYS:                      # ★ 2026-09-18 前向收益 / 收盘确认 / 持仓跟踪
        if _k in r.keys():
            d[_k] = r[_k]
    d['watch'] = bool(r['watch']) if 'watch' in r.keys() else False
    d['confirmed'] = bool(r['confirmed']) if 'confirmed' in r.keys() else False
    return d


def _ensure_cols(c):
    """旧库平滑升级：2026-09-18 新增的前向收益/收盘确认/持仓跟踪列，缺哪列补哪列。

    已存在的库不会被 CREATE TABLE IF NOT EXISTS 改动，必须显式 ALTER；
    否则页面读这些列会报 "no such column"，功能静默失效。
    """
    try:
        have = {r['name'] for r in c.execute('PRAGMA table_info(intraday_signal)')}
    except Exception:
        return 0
    n = 0
    for col, typ in _FWD_COLS:
        if col not in have:
            try:
                c.execute('ALTER TABLE intraday_signal ADD COLUMN %s %s' % (col, typ))
                n += 1
            except Exception:
                pass
    return n


def init():
    """建表 + 一次性迁移旧 JSON（幂等，进程内只跑一次）"""
    global _ready
    with _LOCK:
        if _ready:
            return
        with _db() as c:
            c.executescript(_SCHEMA)
            _ensure_cols(c)
        _migrate_json()
        _ready = True


def _meta_get(c, k):
    row = c.execute('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
    return row['v'] if row else None


def _meta_set(c, k, v):
    c.execute('INSERT INTO meta(k, v) VALUES(?,?) '
              'ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v)))


def _migrate_json():
    """把旧 JSON 里的历史信号导入 SQLite（只跑一次，原文件保留不删）。

    - intraday_signals.json：当日事件流（date + events{策略: [事件]}）
    - notify_signal_sent.json：更早的唯一去重标记（sent{策略: {code,name,at}}）
    迁移后写 data/_signal_store_migrated.json 记录时间与条数，原 JSON 改名 .pre_sqlite.bak 备份。
    """
    init_done = os.path.exists(_MIGRATED_FLAG)
    with _db() as c:
        if _meta_get(c, 'json_migrated'):
            return
    n_ev = n_sig = 0
    if not init_done:
        # ① 当日事件流
        try:
            with open(_JSON_EV, encoding='utf-8') as f:
                st = json.load(f)
            d0 = str(st.get('date') or '')
            if d0:
                for sname, lst in (st.get('events') or {}).items():
                    for e in (lst or []):
                        if not (isinstance(e, dict) and e.get('code')):
                            continue
                        _insert_raw(d0, str(sname), e)
                        n_ev += 1
        except Exception:
            pass
        # ② 旧去重标记（仅当日；历史日期无从恢复，这正是改用 SQLite 的原因）
        try:
            with open(_JSON_SIG, encoding='utf-8') as f:
                old = json.load(f)
            d0 = str(old.get('date') or '')
            for sname, p in (old.get('sent') or {}).items():
                if d0 and isinstance(p, dict) and p.get('code'):
                    _insert_raw(d0, str(sname), {
                        'code': p.get('code'), 'name': p.get('name'),
                        'first_at': p.get('at'), 'last_at': p.get('at'),
                        'count': 1, 'pushed': True, 'pushed_at': p.get('at')})
                    n_sig += 1
        except Exception:
            pass
        try:
            with open(_MIGRATED_FLAG, 'w', encoding='utf-8') as f:
                json.dump({'migrated_at': _now(), 'events': n_ev, 'sent': n_sig},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        for p in (_JSON_EV, _JSON_SIG):
            try:
                if os.path.exists(p) and not os.path.exists(p + '.pre_sqlite.bak'):
                    os.replace(p, p + '.pre_sqlite.bak')
            except Exception:
                pass
    with _db() as c:
        _meta_set(c, 'json_migrated', _now())
    if n_ev or n_sig:
        print(f'[signal_store] 旧 JSON 已迁入 SQLite：事件 {n_ev} 条 / 去重标记 {n_sig} 条')


def _insert_raw(trade_date, sname, e):
    """按原样插入一行（迁移用；已存在则跳过，不覆盖新数据）"""
    ts = str(e.get('first_at') or e.get('last_at') or _now())
    with _db() as c:
        c.execute(
            'INSERT OR IGNORE INTO intraday_signal '
            '(trade_date, sname, sid, code, name, score, price, buy_ref, stop, target, '
            ' tag, note, first_at, last_at, cnt, pushed, pushed_at, gone, gone_at, '
            ' gone_reason, deleted, created_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)',
            (trade_date, sname, str(e.get('sid') or str(sname).split('_')[0]),
             str(e.get('code')), e.get('name'), e.get('score'), e.get('price'),
             e.get('buy_ref'), e.get('stop'), e.get('target'), e.get('tag'), e.get('note'),
             ts, str(e.get('last_at') or ts), int(e.get('count') or 1),
             1 if e.get('pushed') else 0, e.get('pushed_at'),
             1 if e.get('gone') else 0, e.get('gone_at'), e.get('gone_reason'),
             ts, ts))


def upsert(rec, sname, trade_date=None, watch=0):
    """本轮盘中触发写入（等价旧 record_intraday_signal 语义，不负责推送）。

    watch=1 时写入的是**持仓跟踪**行（见 upsert_watch），不推送、不算待执行买入信号。

    - 同策略同标的 → 累加次数、刷新最近触发时刻（保留首次触发时刻）
    - 同策略换标的 → 新增行，并把该策略旧行标 gone（**保留历史**，不删）
    - 曾被标 gone / deleted 的行再次触发 → 恢复为"仍在触发"

    ★ 2026-09-17 修订（用户反馈"缠论三买方大集团被高新发展顶掉、首页却仍提示方大"）：
    旧实现只在"出现新标的"时把旧行标 gone='换标的'，且**带 '换标的' 的旧行再次触发不再恢复**
    （旧 restore_gone 显式排除该原因）→ 同一策略同一天里"最后触发的标的"反而不是触发中：
    9/16 S15 方大集团 10:04 首次、14:57:44 仍在触发（cnt=14），却因 14:33:57 高新发展插队被
    永久标成"已消失（换标的）"，库里"触发中"反而是只触发过 1 次的高新发展，与收盘判定完全相反。
    现统一口径：**本行刚触发 = 该策略当日最新活跃标的 → 本行 gone 无条件清零**；
    同策略当日其它仍"触发中"的行统一标 gone='换标的'。谁最后触发谁触发中。
    返回事件 dict。
    """
    if not rec or not rec.get('triggered') or not rec.get('code'):
        return None
    init()
    sname = str(sname)
    code = str(rec.get('code'))
    d0 = str(trade_date or _today())
    ts = str(rec.get('trigger_time') or _now())
    if len(ts) == 8:
        ts = f'{d0} {ts}'
    elif '-' not in ts:
        ts = f'{d0} {ts}'
    with _LOCK, _db() as c:
        row = c.execute('SELECT * FROM intraday_signal WHERE trade_date=? AND sname=? AND code=?',
                        (d0, sname, code)).fetchone()
        if row is None:
            c.execute(
                'INSERT INTO intraday_signal '
                '(trade_date, sname, sid, code, name, score, price, buy_ref, stop, target, '
                ' tag, note, first_at, last_at, cnt, pushed, gone, watch, '
                ' created_at, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,0,?,?,?)',
                (d0, sname, str(sname).split('_')[0], code, rec.get('name'),
                 rec.get('score'), rec.get('price'), rec.get('buy_ref'), rec.get('stop'),
                 rec.get('target'), rec.get('tag'), rec.get('note'), ts, ts,
                 1 if watch else 0, ts, ts))
            row = c.execute('SELECT * FROM intraday_signal WHERE trade_date=? AND sname=? AND code=?',
                            (d0, sname, code)).fetchone()
        else:
            # ★ 本行刚触发 → 无条件恢复"触发中"（含曾被标 '换标的' 的行：它现在才是最新活跃标的）
            c.execute(
                'UPDATE intraday_signal SET cnt=cnt+1, last_at=?, updated_at=?, '
                'price=COALESCE(?, price), score=COALESCE(?, score), '
                'tag=COALESCE(?, tag), note=COALESCE(?, note), '
                'gone=0, gone_at=NULL, gone_reason=NULL, '
                'deleted=0, deleted_at=NULL, deleted_reason=NULL '
                'WHERE id=?',
                (ts, ts, rec.get('price'), rec.get('score'), rec.get('tag'), rec.get('note'),
                 row['id']))
            row = c.execute('SELECT * FROM intraday_signal WHERE id=?', (row['id'],)).fetchone()
        # 同策略当日**其它**仍"触发中"的行 → 标"换标的"（谁最后触发谁触发中，与页面/邮件一致）
        c.execute('UPDATE intraday_signal SET gone=1, gone_at=?, gone_reason=?, updated_at=? '
                  'WHERE trade_date=? AND sname=? AND id<>? AND gone=0 AND deleted=0',
                  (ts, '换标的', ts, d0, sname, row['id']))
        row = c.execute('SELECT * FROM intraday_signal WHERE id=?', (row['id'],)).fetchone()
        return _row_to_ev(row)


def upsert_watch(rec, sname, code, name=None, trade_date=None, note=None):
    """★ 2026-09-18 新增：**持仓跟踪**留痕（轮值外策略的盘中"下文"）。

    背景（用户反馈）：盘中扫描只覆盖**当日轮值策略**（一天 1 个，约 12 个交易日轮一圈），
    有持仓的非轮值策略在轮值外完全没有盘中踪迹 → "推荐的下文跟踪断了"。
    全市场扫 12 个策略成本过高，故持仓策略**只扫它自己的持仓股**（1 只，成本可忽略）。

    口径（与轮值触发行严格区分，避免污染既有信号语义）：
      - watch=1：**不推送**邮件、**不进"待执行买入信号"口径**（首页信号登记 / 总账 /
        速览表 / 行动清单都过滤 watch 行，见 signal_rows.intraday_rows），只在 /signals 页展示；
      - 每天每策略一行（唯一键 trade_date+sname+code），每轮扫描刷新 last_at/price/note；
      - **不参与**"同策略换标的 → 旧行标 gone"逻辑（持仓股不会被别的标的顶掉）。
    返回事件 dict。
    """
    if not code:
        return None
    init()
    sname = str(sname)
    code = str(code)
    d0 = str(trade_date or _today())
    rec = rec or {}
    ts = str(rec.get('trigger_time') or _now())
    if len(ts) == 8:
        ts = f'{d0} {ts}'
    with _LOCK, _db() as c:
        row = c.execute('SELECT * FROM intraday_signal WHERE trade_date=? AND sname=? AND code=?',
                        (d0, sname, code)).fetchone()
        if row is None:
            c.execute(
                'INSERT INTO intraday_signal '
                '(trade_date, sname, sid, code, name, score, price, tag, note, '
                ' first_at, last_at, cnt, pushed, gone, watch, created_at, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,1,0,0,1,?,?)',
                (d0, sname, sname.split('_')[0], code, name or rec.get('name'),
                 rec.get('score'), rec.get('price'), '持仓跟踪', note, ts, ts, ts, ts))
        else:
            c.execute(
                'UPDATE intraday_signal SET cnt=cnt+1, last_at=?, updated_at=?, '
                'price=COALESCE(?, price), score=COALESCE(?, score), '
                'tag=?, note=COALESCE(?, note), watch=1 WHERE id=?',
                (ts, ts, rec.get('price'), rec.get('score'), '持仓跟踪', note, row['id']))
        row = c.execute('SELECT * FROM intraday_signal WHERE trade_date=? AND sname=? AND code=?',
                        (d0, sname, code)).fetchone()
        return _row_to_ev(row)


# ============================================================
# ★ 2026-09-18 前向收益 / 收盘确认回填（历史从"可审计"变"可评估"）
# ============================================================

def _num_or_none(v):
    try:
        if v is None or v == '':
            return None
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else round(f, 2)
    except Exception:
        return None


def _load_closes(code):
    """读 data/csv/{code}.csv → (dates, closes)（qfq 收盘，日期升序）；不可用返回 (None, None)"""
    p = os.path.join(_CSV_DIR, '%s.csv' % str(code))
    if not os.path.exists(p):
        return None, None
    ds, cs = [], []
    try:
        with open(p, encoding='utf-8') as f:
            f.readline()                      # 表头：日期,股票代码,开盘,收盘,...
            for line in f:
                q = line.strip().split(',')
                if len(q) < 4 or len(q[0]) != 10:
                    continue
                try:
                    c = float(q[3])
                except Exception:
                    continue
                if c > 0:
                    ds.append(q[0])
                    cs.append(c)
    except Exception:
        return None, None
    return (ds, cs) if ds else (None, None)


def _idx_of(ds, trade_date):
    """触发日（或之前最近交易日）在日线序列里的下标；无数据返回 None"""
    for j in range(len(ds) - 1, -1, -1):
        if ds[j] <= trade_date:
            return j
    return None


def _fwd_from_closes(ds, cs, trade_date, n):
    """(基准收盘价, 之后第 n 个交易日收盘的涨跌幅%, 基准日) —— 数据未到则收益为 None。

    ★ 触发日 bar 尚未入库时退回其**之前最近交易日**作基准（基准日随结果返回），
      保证盘中触发当天也能算出"以最近可得收盘为基准"的前向收益。
    """
    if not ds:
        return None, None, None
    idx = _idx_of(ds, trade_date)
    if idx is None:
        return None, None, None
    base = cs[idx]
    if base <= 0:
        return None, None, None
    j = idx + int(n)
    if j >= len(ds):
        return base, None, ds[idx]
    return base, round((cs[j] / base - 1) * 100, 2), ds[idx]


def _load_daily_index():
    """{策略全名: {date: 账本行}} —— 收盘确认判定用；文件缺失/损坏返回 {}"""
    try:
        with open(_DAILY_JSON, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return {}
    out = {}
    for s in (d.get('strategies') or []):
        m = {}
        for r in (s.get('daily') or []):
            dt = str(r.get('date') or '')
            if dt:
                m[dt] = r
        out[str(s.get('name') or '')] = m
    return out


def _confirm_from_daily(idx, sname, trade_date, name, code):
    """(confirmed, confirm_px, confirm_name)：账本里该策略 trade_date 的收盘判定是否采纳本行标的。

    confirm_px 优先取 T+1「建仓」行的 deal_price（**真实成交价**），退回触发日收盘价。
    confirm_name 恒记录当日收盘判定最终采纳的标的（可能与本行不同，便于页面对比）。
    """
    rows = (idx or {}).get(str(sname)) or {}
    r0 = rows.get(trade_date)
    if r0 is None:
        return 0, None, None
    nm0 = str(r0.get('name') or '')
    cd0 = str(r0.get('code') or '')
    hit = (nm0 and nm0 == str(name or '')) or (cd0 and cd0 == str(code or ''))
    px = r0.get('price')
    for d in sorted(k for k in rows.keys() if k > trade_date)[:3]:
        r1 = rows[d]
        if str(r1.get('status') or '') == '建仓' and (not code or str(r1.get('code') or '') == str(code)):
            if r1.get('deal_price'):
                px = r1['deal_price']
            break
    ok = 1 if (hit and str(r0.get('status') or '') in CONFIRM_STATUSES) else 0
    return ok, _num_or_none(px), (nm0 or None)


def backfill_forward(days=400, force=False, limit=5000):
    """回填前向收益（T+1/T+3/T+5）与收盘确认（幂等，只处理数据没到齐的行）。

    口径：以**触发日收盘（qfq）**为基准 → 之后第 1/3/5 个**交易日**收盘的涨跌幅（%）；
    触发日 bar 未入库则退回之前最近交易日。收盘确认取自 data/strategy_daily.json。

    ★ 数据未到齐（连 T+1 都没有 / T+5 未到期）的行：先写入已能算出的列，但**不写
      evaluated_at** → 下次调用继续补（force=True 则全量重算）。
    返回 {'scanned', 'filled', 'pending'}。
    """
    init()
    since = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    with _FWD_LOCK:
        with _LOCK, _db() as c:
            sql = ('SELECT id, trade_date, sname, code, name FROM intraday_signal '
                   'WHERE trade_date>=? AND deleted=0')
            if not force:
                sql += " AND IFNULL(evaluated_at,'')=''"
            sql += ' ORDER BY trade_date DESC LIMIT %d' % int(limit)
            rows = [dict(r) for r in c.execute(sql, (since,)).fetchall()]
        if not rows:
            return {'scanned': 0, 'filled': 0, 'pending': 0}
        _dl = _load_daily_index()
        _cache = {}
        filled = pending = 0
        with _LOCK, _db() as c:
            ts = _now()
            for r in rows:
                code = str(r['code'] or '')
                if code not in _cache:
                    _cache[code] = _load_closes(code)
                ds, cs = _cache[code]
                if not ds:
                    continue
                d0 = str(r['trade_date'])
                base, _, _bd = _fwd_from_closes(ds, cs, d0, 0)
                if base is None:
                    continue
                vals = {'close_px': round(base, 2)}
                for n, key in ((1, 't1_ret'), (3, 't3_ret'), (5, 't5_ret')):
                    _, ret, _ = _fwd_from_closes(ds, cs, d0, n)
                    if ret is not None:
                        vals[key] = ret
                if not vals or len(vals) == 1:      # 连 T+1 都没到 → 待下次
                    pending += 1
                    continue
                _i = _idx_of(ds, d0)
                vals['fwd_date'] = ds[min(len(ds) - 1, (_i or 0) + 5)]
                cf, cpx, cnm = _confirm_from_daily(_dl, str(r['sname']), d0,
                                                   str(r['name'] or ''), code)
                vals.update({'confirmed': cf, 'confirm_px': cpx, 'confirm_name': cnm})
                if vals.get('t5_ret') is not None:  # T+5 到齐 → 视为终值，不再重算
                    vals['evaluated_at'] = ts
                sets = ', '.join('%s=?' % k for k in vals) + ', updated_at=?'
                c.execute('UPDATE intraday_signal SET %s WHERE id=?' % sets,
                          tuple(list(vals.values()) + [ts, int(r['id'])]))
                filled += 1
        return {'scanned': len(rows), 'filled': filled, 'pending': pending}


def fwd_stats(days=90, watch=False):
    """区间内信号（默认排除持仓跟踪行）的前向表现汇总 —— /signals 页顶部统计条用。

    返回 {n, confirmed, confirm_rate, t1:{n,mean,win}, t3:..., t5:...}
    mean=均值%(相对触发日收盘)，win=上涨占比%；样本不足（n=0）时各项为 None。
    """
    init()
    since = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    sql = ('SELECT t1_ret, t3_ret, t5_ret, confirmed FROM intraday_signal '
           'WHERE trade_date>=? AND deleted=0')
    if not watch:
        sql += ' AND IFNULL(watch,0)=0'
    with _LOCK, _db() as c:
        rows = [dict(r) for r in c.execute(sql, (since,)).fetchall()]

    def _agg(key):
        vs = [float(r[key]) for r in rows if r.get(key) is not None]
        if not vs:
            return {'n': 0, 'mean': None, 'win': None}
        return {'n': len(vs), 'mean': round(sum(vs) / len(vs), 2),
                'win': round(100.0 * sum(1 for v in vs if v > 0) / len(vs), 1)}

    n = len(rows)
    cf = sum(1 for r in rows if r.get('confirmed'))
    return {'n': n, 'confirmed': cf,
            'confirm_rate': round(100.0 * cf / n, 1) if n else None,
            't1': _agg('t1_ret'), 't3': _agg('t3_ret'), 't5': _agg('t5_ret')}


def mark_pushed(sname, code, at=None, trade_date=None):
    """标记已推送（去重依据：当日同策略同标的只推一次，跨重启持久）"""
    init()
    ts = str(at or _now())
    with _LOCK, _db() as c:
        cur = c.execute('UPDATE intraday_signal SET pushed=1, pushed_at=?, updated_at=? '
                        'WHERE trade_date=? AND sname=? AND code=?',
                        (ts, ts, str(trade_date or _today()), str(sname), str(code)))
        return cur.rowcount


def mark_gone(sname, reason='信号消失', trade_date=None):
    """本轮该策略未触发/无候选：改标志位 gone=1（**保留历史**，页面仍能看到"曾触发过"）。

    ★ 2026-09-10 前这里是直接删除记录，导致当天更早的信号在页面上凭空消失。"""
    init()
    ts = _now()
    with _LOCK, _db() as c:
        cur = c.execute(
            'UPDATE intraday_signal SET gone=1, gone_at=?, gone_reason=?, updated_at=? '
            'WHERE trade_date=? AND sname=? AND gone=0 AND deleted=0',
            (ts, str(reason), ts, str(trade_date or _today()), str(sname)))
        return cur.rowcount


def expire_stale(before_date=None, reason='隔日失效（盘中信号仅在触发当日有效）'):
    """★ 2026-09-17 新增：把 before_date（默认今天）**之前**仍未消失的行统一标 gone。

    背景（用户反馈）：/signals 页里 9/15 的和泰机电至今显示"触发中"（gone=0），但 9/16、9/17
    轮值策略是 S15/S16，扫描根本不覆盖 S10 → 该行再也没人跟踪，成了"僵尸触发中"。
    根因：`mark_gone` 只作用于**当日轮值策略**（app.do_scan 每轮只调 `intraday_signal_clear(sname)`），
    跨日轮值切换后旧策略的行永远没人收尾。

    口径：盘中信号只在**触发当日**有效——交易日切换时，凡是"上一日盘中触发、当日再未被扫描到"
    的行一律标 gone（行保留、原因可查），/signals 页因此不会再出现陈旧的"触发中"。
    幂等：只动 gone=0 的行，重复调用无副作用。
    """
    init()
    ts = _now()
    d0 = str(before_date or _today())
    with _LOCK, _db() as c:
        cur = c.execute(
            'UPDATE intraday_signal SET gone=1, gone_at=?, gone_reason=?, updated_at=? '
            'WHERE trade_date<? AND gone=0 AND deleted=0',
            (ts, str(reason), ts, d0))
        return cur.rowcount


def reconcile_close(sname, code=None, name=None, trade_date=None):
    """★ 2026-09-17 新增：**收盘对账**——把某策略当日信号统一到"收盘判定"口径。

    背景（用户反馈）：9/16 S15 盘中最后一刻换成高新发展，但收盘判定（strategy_daily.json 9/16
    该策略行）是**方大集团 买入信号（评分 80）**，9/17 开盘实际买的也是方大集团。而信号库里
    方大被标"已消失（换标的）"、高新发展显示"触发中"——与正式信号完全相反，因为收盘判定
    此前**从不回写信号库**（只有盘中扫描写）。

    口径：收盘判定为正式信号（T+1 执行的实际标的）。
      - code 非空 → 该行 gone=0（= 收盘确认），同策略当日其它行标 gone（原因注明收盘判定是谁）
      - code 为空（收盘无候选/未触发）→ 同策略当日所有行标 gone（原因='收盘未判定买入信号'）
    返回被改动的行数。
    """
    init()
    ts = _now()
    d0 = str(trade_date or _today())
    sname = str(sname or '')
    if not sname:
        return 0
    with _LOCK, _db() as c:
        if code:
            _nm = str(name or '')
            _why = f'收盘未持续（{d0} 收盘判定为 {_nm or code}）'
            # ★ 注意这里**不限 gone=0**：盘中已标过"换标的"的行也要改写成收盘口径，
            #   否则同一天里会残留两种原因、用户看不出"收盘到底采纳了谁"。
            c.execute(
                'UPDATE intraday_signal SET gone=1, gone_at=COALESCE(gone_at, ?), gone_reason=?, '
                'updated_at=? WHERE trade_date=? AND sname=? AND code<>? AND deleted=0',
                (ts, _why, ts, d0, sname, str(code)))
            # 收盘确认的那一行：恢复为"触发中"并清空消失原因
            cur = c.execute(
                'UPDATE intraday_signal SET gone=0, gone_at=NULL, gone_reason=NULL, updated_at=? '
                'WHERE trade_date=? AND sname=? AND code=? AND deleted=0',
                (ts, d0, sname, str(code)))
            return cur.rowcount
        cur = c.execute(
            'UPDATE intraday_signal SET gone=1, gone_at=COALESCE(gone_at, ?), gone_reason=?, '
            'updated_at=? WHERE trade_date=? AND sname=? AND deleted=0',
            (ts, '收盘未判定买入信号', ts, d0, sname))
        return cur.rowcount


def soft_delete(ev_id=None, sname=None, code=None, trade_date=None, reason='人工停用'):
    """★ 软删除：只置 deleted=1（行保留，历史可调取），绝不物理删除。"""
    init()
    ts = _now()
    with _LOCK, _db() as c:
        if ev_id is not None:
            cur = c.execute('UPDATE intraday_signal SET deleted=1, deleted_at=?, deleted_reason=?, '
                            'updated_at=? WHERE id=?', (ts, str(reason), ts, int(ev_id)))
        else:
            cur = c.execute('UPDATE intraday_signal SET deleted=1, deleted_at=?, deleted_reason=?, '
                            'updated_at=? WHERE trade_date=? AND sname=? AND code=?',
                            (ts, str(reason), ts, str(trade_date or _today()),
                             str(sname), str(code)))
        return cur.rowcount


def restore(ev_id):
    """恢复被软删除/标记消失的行（deleted=0，gone 保持原值由下次扫描决定）"""
    init()
    with _LOCK, _db() as c:
        cur = c.execute('UPDATE intraday_signal SET deleted=0, deleted_at=NULL, '
                        'deleted_reason=NULL, updated_at=? WHERE id=?', (_now(), int(ev_id)))
        return cur.rowcount


def query(date=None, sid=None, sname=None, include_gone=True, include_deleted=False,
          limit=None, include_watch=True):
    """按交易日查询事件（date=None → 当日）。返回按首次触发时刻升序的事件列表。

    sid 可为策略全名（S2_MACD金叉KDJ死叉）或简称（S2）。
    ★ 2026-09-18：include_watch=False 可排除**持仓跟踪**行（watch=1）——
      待执行信号口径（首页/总账/行动清单）不该把它们当买入信号。"""
    init()
    d0 = str(date or _today())
    sql = 'SELECT * FROM intraday_signal WHERE trade_date=?'
    args = [d0]
    if not include_deleted:
        sql += ' AND deleted=0'
    if not include_gone:
        sql += ' AND gone=0'
    if not include_watch:
        sql += ' AND IFNULL(watch,0)=0'
    if sname:
        sql += ' AND sname=?'
        args.append(str(sname))
    elif sid not in (None, ''):
        sql += ' AND (sname=? OR sid=?)'
        args.extend([str(sid), str(sid)])
    sql += ' ORDER BY first_at ASC, id ASC'
    if limit:
        sql += ' LIMIT %d' % int(limit)
    with _LOCK, _db() as c:
        return [_row_to_ev(r) for r in c.execute(sql, args).fetchall()]


def history(days=30, sid=None, include_gone=True, include_deleted=False, limit=1000,
            include_watch=True):
    """最近 N 天全部信号（跨日历史——旧 JSON 方案在这里会丢数据）。按日期倒序、日内升序。"""
    init()
    since = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    sql = 'SELECT * FROM intraday_signal WHERE trade_date>=?'
    args = [since]
    if not include_deleted:
        sql += ' AND deleted=0'
    if not include_gone:
        sql += ' AND gone=0'
    if not include_watch:
        sql += ' AND IFNULL(watch,0)=0'
    if sid not in (None, ''):
        sql += ' AND (sname=? OR sid=?)'
        args.extend([str(sid), str(sid)])
    sql += ' ORDER BY trade_date DESC, first_at ASC, id ASC LIMIT %d' % int(limit)
    with _LOCK, _db() as c:
        return [_row_to_ev(r) for r in c.execute(sql, args).fetchall()]


def day_stats(date=None, include_deleted=False):
    """某日汇总：{total, live, gone, pushed, deleted}"""
    init()
    d0 = str(date or _today())
    with _LOCK, _db() as c:
        sql = 'SELECT COUNT(*) n, SUM(gone) g, SUM(pushed) p, SUM(deleted) dl ' \
              'FROM intraday_signal WHERE trade_date=?'
        if not include_deleted:
            sql += ' AND deleted=0'
        r = c.execute(sql, (d0,)).fetchone()
    total = int(r['n'] or 0)
    gone = int(r['g'] or 0)
    return {'total': total, 'gone': gone, 'live': total - gone,
            'pushed': int(r['p'] or 0), 'deleted': int(r['dl'] or 0)}


def db_path():
    return _DB_FILE


if __name__ == '__main__':
    import sys
    init()
    print('库文件:', _DB_FILE)
    if '--backfill' in sys.argv:
        # 手动回填/重算：python -X utf8 -u signal_store.py --backfill [--force]
        r = backfill_forward(force=('--force' in sys.argv))
        print('前向收益回填:', r)
        st = fwd_stats(days=400)
        print('区间表现: 样本%d 收盘确认%d (%.1f%%) ｜ T+1 n=%d mean=%s win=%s ｜ '
              'T+3 n=%d mean=%s ｜ T+5 n=%d mean=%s'
              % (st['n'], st['confirmed'], st['confirm_rate'] or 0.0,
                 st['t1']['n'], st['t1']['mean'], st['t1']['win'],
                 st['t3']['n'], st['t3']['mean'], st['t5']['n'], st['t5']['mean']))
    for d in sorted({e['trade_date'] for e in history(days=365, include_deleted=True)}):
        s = day_stats(d, include_deleted=True)
        print(f"  {d}  共{s['total']} 触发中{s['live']} 已消失{s['gone']} "
              f"已推送{s['pushed']} 已停用{s['deleted']}")
