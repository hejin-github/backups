# -*- coding: utf-8 -*-
"""多通道推送模块（零第三方依赖，纯标准库 urllib）。

支持通道（data/notify_config.json 的 channel 字段切换）：
- pushplus   https://www.pushplus.plus/send           （需实名认证）
- qywx       企业微信群机器人 webhook                  （免费，需企业微信）
- dingtalk   钉钉群自定义机器人 webhook（支持加签）    （免费，需钉钉）
- feishu     飞书群自定义机器人 webhook                （免费，需飞书）
- serverchan Server酱（微信服务号，扫码即用）         （免费 5 条/天，无需建群/企业）
- qmail      QQ邮箱 SMTP 推送                         （免费无限条，需邮箱授权码）

推送场景（均由 app.py 在对应时机调用）：
- 启动提示        send_startup()（2026-08-28 起不再调用：启动时人就在电脑前，无需推送）
- 盘中触发信号    send_intraday_signal(rec, sname)     当日同策略同标的去重
- 收盘简报        send_close_brief(market, rec, sname, hist)   跨启动去重：同一天已成功推送过则不再重发
- 行情异常告警    send_market_alert(m)
- 程序异常告警    send_error_alert(msg)                5分钟节流

配置示例 data/notify_config.json：
{
  "enabled": true,
  "channel": "qywx",                 # pushplus | qywx | dingtalk | feishu
  "daily_limit": 100,
  "pushplus": {"token": ""},
  "qywx": {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."},
  "dingtalk": {"webhook": "https://oapi.dingtalk.com/robot/send?access_token=...", "secret": ""},
  "feishu": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/..."},
  "serverchan": {"sendkey": "https://sctapi.ftqq.com/xxx.send 中的 xxx"},
  "qmail": {"from": "你的QQ邮箱", "auth_code": "16位授权码", "to": "接收邮箱（默认同from）"}
}
对应通道未配置 webhook/token 时全部静默跳过，不影响主流程。
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import smtplib
import time
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

try:
    import signal_store as _store          # 同目录，SQLite 信号存储层
except ImportError:                        # 任意 CWD 下也能导入
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import signal_store as _store

_PUSHPLUS_URL = 'https://www.pushplus.plus/send'
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_WORKSPACE, 'data', 'notify_config.json')
# 收盘简报跨启动去重标记：成功推送后记录日期，当日重启服务不再重发
_BRIEF_FILE = os.path.join(_WORKSPACE, 'data', 'notify_brief_sent.json')
# ★ 2026-09-10 盘中信号改 SQLite 存储（signal_store.py，data/signals.db 表 intraday_signal）：
#   一条信号一行、按 trade_date 分区，页面可查**任意历史日**的全部信号。
#   换库原因（用户反馈）：
#   ① 旧事件流 JSON 只存"当日"，跨日 _ev_state() 直接重建 → 昨天的信号今天就查不到；
#   ② 旧去重标记在信号消失时**直接删除记录** → 当天更早推送过的标的（华金资本）无法找回。
#   现在"消失/停用"一律改标志位（gone / deleted=1，行保留），历史永不丢、可随时调取。
#   下面两个 JSON 路径仅作一次性迁移来源，迁移后备份为 *.pre_sqlite.bak，不再读写。
_SIG_FILE = os.path.join(_WORKSPACE, 'data', 'notify_signal_sent.json')   # 历史遗留（仅迁移）
_EV_FILE = os.path.join(_WORKSPACE, 'data', 'intraday_signals.json')       # 历史遗留（仅迁移）

# ★ 2026-10-09 用户要求：盘中信号"发出去又消失"太误导 —— 改为**稳定后再推**。
#   盘中每轮扫描（app.SCAN_SECONDS=300，即 5 分钟）触发一次，其中相当一部分下一轮就不成立
#   （盘中用实时价拼伪 bar 估算，冲高即触发、回落即消失）。现在要求**连续 N 轮扫描仍在触发**
#   才发邮件；中途消失的不发（仍照常落库留痕，/signals 页可查）。
#   改阈值只动这里：1=恢复"触发即推"，3=更严（约 10~15 分钟）。
INTRADAY_STREAK_MIN = 2
# 连续轮数计数器：{(trade_date, sname, code): 连续触发轮数}，进程内维护。
# 只影响"什么时候发邮件"，不影响落库/留痕 —— 服务重启计数归零，最坏是当天首条推迟一轮再发
# （宁可晚发/不发，也不发一条马上就消失的信号）。未触发由 intraday_signal_clear 清零。
_STREAK = {}


def _streak_bump(sname, code, trade_date=None):
    """本轮该策略该标的**仍在触发** → 连续轮数 +1，并清掉同策略其它标的（已换标的/gone）。"""
    d = trade_date or _today()
    _key = (d, str(sname), str(code or ''))
    for k in [k for k in _STREAK if k[0] != d or (k[1] == str(sname) and k[2] != str(code or ''))]:
        _STREAK.pop(k, None)        # 顺带清掉往日残留，避免无限增长
    _STREAK[_key] = _STREAK.get(_key, 0) + 1
    return _STREAK[_key]


def _streak_reset(sname, trade_date=None):
    """本轮该策略未触发/无候选 → 连续轮数清零（下一轮重新计数）。"""
    d = trade_date or _today()
    for k in [k for k in _STREAK if k[0] == d and k[1] == str(sname)]:
        _STREAK.pop(k, None)

_state = {
    'sent_date': None,     # 当日已发送日期（用于跨日重置计数/去重）
    'sent_today': 0,       # 当日普通消息计数
    'once_keys': set(),    # 当日已推送的 once_key
    'last_err': None,      # (msg, datetime) 程序异常告警节流
}


def _log(msg):
    print(f'[notify] {datetime.datetime.now().strftime("%H:%M:%S")} {msg}')


def _md(s):
    """预判标签日期：'2026-09-03' → '9月3日'；无日期返回 '明日'"""
    try:
        y, mo, d = str(s).split('-')
        return f'{int(mo)}月{int(d)}日'
    except Exception:
        return str(s) if s else '明日'


def _cfg():
    try:
        with open(_CONFIG_FILE, encoding='utf-8') as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _channel():
    """当前推送通道：pushplus | qywx | dingtalk | feishu | serverchan（默认 pushplus 向后兼容）"""
    return str(_cfg().get('channel') or 'pushplus').strip().lower()


def _cfg_chan():
    """当前通道的配置块（dict）"""
    c = _cfg()
    return c.get(_channel(), {}) if isinstance(c.get(_channel()), dict) else {}


def enabled():
    c = _cfg()
    if not c.get('enabled', True):
        return False
    ch = _channel()
    if ch == 'pushplus':
        return bool((c.get('pushplus', {}).get('token') if isinstance(c.get('pushplus'), dict) else None) or c.get('token'))
    if ch == 'serverchan':
        return bool((c.get('serverchan') or {}).get('sendkey'))
    if ch == 'qmail':
        return bool((c.get('qmail') or {}).get('auth_code'))
    return bool((c.get(ch) or {}).get('webhook'))


def _today():
    return datetime.date.today().isoformat()


def _brief_sent_date():
    """收盘简报已成功推送的日期（跨启动去重，读 data/notify_brief_sent.json）"""
    try:
        with open(_BRIEF_FILE, encoding='utf-8') as f:
            return json.load(f).get('date')
    except Exception:
        return None


def _mark_brief_sent(date):
    try:
        with open(_BRIEF_FILE, 'w', encoding='utf-8') as f:
            json.dump({'date': date}, f)
    except Exception as e:
        _log(f'收盘简报去重标记写入失败: {str(e)[:60]}')


def _reset_day(date):
    """跨日时清空当日去重与计数"""
    if _state['sent_date'] != date:
        _state['sent_date'] = date
        _state['sent_today'] = 0
        _state['once_keys'] = set()


def send(title, content, template='markdown', once_key=None, priority=False):
    """发送一条消息。once_key：当日同 key 只推一次。
    priority=True 的消息（异常/行情告警）不受每日限额限制。"""
    if not enabled():
        return False
    date = _today()
    _reset_day(date)
    if once_key:
        key = f'{date}|{once_key}'
        if key in _state['once_keys']:
            return False
    if not priority and _state['sent_today'] >= _cfg().get('daily_limit', 100):
        _log(f'已达每日推送上限（{_cfg().get("daily_limit", 100)}），跳过: {title}')
        return False
    ch = _channel()
    if ch == 'qywx':
        ok, info = _send_qywx(_cfg_chan().get('webhook', ''), title, content)
    elif ch == 'dingtalk':
        ok, info = _send_dingtalk(_cfg_chan().get('webhook', ''), title, content)
    elif ch == 'feishu':
        ok, info = _send_feishu(_cfg_chan().get('webhook', ''), title, content)
    elif ch == 'serverchan':
        ok, info = _send_serverchan(_cfg_chan().get('sendkey', ''), title, content)
    elif ch == 'qmail':
        ok, info = _send_qmail(title, content)
    else:
        ok, info = _send_pushplus(title, content)
    if ok:
        _state['sent_today'] += 1
        if once_key:
            _state['once_keys'].add(f'{date}|{once_key}')
        _log(f'已推送[{ch}]: {title}')
    else:
        _log(f'推送失败[{ch}]: {title} {info}')
    return ok


# ---------------- 各通道实现 ----------------

def _http_json(url, payload):
    """POST JSON，返回响应文本"""
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode('utf-8', errors='replace')


def _send_pushplus(title, content):
    """PushPlus：表单 POST /send，成功响应含 "code":200"""
    c = _cfg()
    tok = ((c.get('pushplus', {}).get('token') if isinstance(c.get('pushplus'), dict) else None)
           or c.get('token') or '').strip()
    data = urllib.parse.urlencode({
        'token': tok, 'title': title,
        'content': content, 'template': 'markdown',
    }).encode('utf-8')
    try:
        req = urllib.request.Request(
            _PUSHPLUS_URL, data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return False, f'请求失败 {str(e)[:100]}'
    return '"code":200' in body, body[:160]


def _send_qywx(webhook, title, content):
    """企业微信群机器人：POST JSON msgtype=markdown，成功响应 "errcode":0"""
    try:
        body = _http_json(webhook, {
            'msgtype': 'markdown',
            'markdown': {'content': content},
        })
    except Exception as e:
        return False, f'请求失败 {str(e)[:100]}'
    return '"errcode":0' in body, body[:160]


def _send_dingtalk(webhook, title, content):
    """钉钉自定义机器人：POST JSON msgtype=markdown，可选加签 secret，成功响应 "errcode":0"""
    secret = (_cfg_chan().get('secret') or '').strip()
    url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        sign = urllib.parse.quote_plus(base64.b64encode(
            hmac.new(secret.encode('utf-8'), f'{ts}\n{secret}'.encode('utf-8'),
                     hashlib.sha256).digest()))
        url = f"{webhook}{'&' if '?' in webhook else '?'}timestamp={ts}&sign={sign}"
    try:
        body = _http_json(url, {
            'msgtype': 'markdown',
            'markdown': {'title': title[:20], 'text': content},
        })
    except Exception as e:
        return False, f'请求失败 {str(e)[:100]}'
    return '"errcode":0' in body, body[:160]


def _send_feishu(webhook, title, content):
    """飞书自定义机器人：interactive 卡片（header + lark_md），成功响应 "code":0"""
    try:
        body = _http_json(webhook, {
            'msg_type': 'interactive',
            'card': {
                'header': {'title': {'tag': 'plain_text', 'content': title[:50]}},
                'elements': [{'tag': 'markdown', 'content': content}],
            },
        })
    except Exception as e:
        return False, f'请求失败 {str(e)[:100]}'
    return '"code":0' in body, body[:160]


def _send_serverchan(sendkey, title, content):
    """Server酱：POST https://sctapi.ftqq.com/{SendKey}.send，成功响应 "code":0"""
    url = f'https://sctapi.ftqq.com/{sendkey}.send'
    data = urllib.parse.urlencode({'title': title, 'desp': content}).encode('utf-8')
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return False, f'请求失败 {str(e)[:100]}'
    return '"code":0' in body, body[:160]


def _send_qmail(title, content):
    """QQ邮箱 SMTP 推送：smtp.qq.com:465 (SSL)，16 位授权码登录，免费无限条。
    配置：{"from": 发件QQ邮箱, "auth_code": 授权码, "to": 接收邮箱(默认同from)}"""
    c = _cfg_chan()
    from_addr = (c.get('from') or '').strip()
    auth_code = (c.get('auth_code') or '').strip()
    to_addr = (c.get('to') or '').strip() or from_addr
    if not from_addr or not auth_code or not to_addr:
        return False, '缺少 from/auth_code/to 配置'
    msg = MIMEText(content, 'plain', 'utf-8')
    msg['Subject'] = Header(f'[策略系统] {title}', 'utf-8')
    msg['From'] = formataddr(('策略系统', from_addr))
    msg['To'] = to_addr
    try:
        with smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=15) as server:
            server.login(from_addr, auth_code)
            server.sendmail(from_addr, [to_addr], msg.as_string())
    except Exception as e:
        return False, f'发送失败 {str(e)[:100]}'
    return True, 'ok'


# ---------------- 启动提示 ----------------

def send_startup(info=None):
    """服务启动时推送一条状态提示。info: 可选的说明行列表"""
    if not enabled():
        return False
    now = datetime.datetime.now()
    lines = [f'# 🚀 策略系统已启动  {now:%Y-%m-%d %H:%M:%S}', '']
    for x in (info or []):
        lines.append(f'- {x}')
    return send('系统启动', '\n'.join(lines))


# ---------------- 盘中触发信号 ----------------

def _ev_date(rec=None):
    """信号归属交易日：优先取扫描触发时刻的日期（跨零点兜底为今日）"""
    ts = str((rec or {}).get('trigger_time') or '').strip()
    return ts[:10] if len(ts) >= 10 and ts[4] == '-' else _today()


def _ev_ts(rec):
    """扫描触发时刻（YYYY-MM-DD HH:MM:SS）：由扫描方 rec['trigger_time'] 记录，
    缺省退回当前时刻（页面/邮件均以它为准，避免只有日期）。"""
    ts = str(rec.get('trigger_time') or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')).strip()
    if len(ts) == 8:
        ts = f'{_today()} {ts}'
    elif '-' not in ts:
        ts = f'{_today()} {ts}'
    return ts


def record_intraday_signal(rec, sname):
    """把本轮盘中触发写入 SQLite（不负责推送），返回该事件 dict。

    - 同策略同标的 → 累加触发次数、刷新最近触发时刻（保留首次触发时刻）；
    - 同策略换标的 → 新增一行，并把该策略旧行**改标志位** gone=1（行保留，历史可查）；
    - 曾被标 gone / deleted 的行再次触发 → 恢复为"仍在触发"。
    """
    if not rec or not rec.get('triggered') or not rec.get('code'):
        return None
    try:
        return _store.upsert(rec, sname, trade_date=_ev_date(rec))
    except Exception as e:
        _log(f'盘中信号落库失败: {str(e)[:80]}')
        return None


def mark_intraday_gone(sname, reason='信号消失'):
    """本轮该策略未触发/无候选：把其事件**改标志位** gone=1（行保留，页面仍能看到"曾触发过"）。

    ★ 2026-09-10 前是直接删除记录，导致当天更早的信号在页面上凭空消失、事后无法调取。"""
    try:
        return bool(_store.mark_gone(str(sname), reason, trade_date=_today()))
    except Exception as e:
        _log(f'盘中信号标记消失失败: {str(e)[:80]}')
        return False


def intraday_expire_stale(reason='隔日失效（盘中信号仅在触发当日有效）'):
    """★ 2026-09-17 新增：交易日切换时把**昨日及更早**仍"触发中"的盘中行统一标消失。

    解决"僵尸触发中"：某策略当天最后一次触发后，若次日轮值换到别的策略，该行再也不会被
    `mark_intraday_gone` 收尾（那个只作用于当日轮值策略）→ 页面上永远显示"触发中"。
    """
    try:
        return _store.expire_stale(reason=reason)
    except Exception as e:
        _log(f'盘中信号跨日失效失败: {str(e)[:80]}')
        return 0


def intraday_reconcile_close(sname, rec, trade_date=None):
    """★ 2026-09-17 新增：**收盘对账**——把某策略当日盘中信号统一到收盘判定口径。

    收盘判定（rec）才是正式信号（T+1 实际执行的那个标的）。此前收盘判定从不回写信号库，
    于是出现"库里显示高新发展触发中、首页/账本却是方大集团"的相反口径（9/16 S15）。
    rec 为 None / 未触发 → 该策略当日所有行标 gone（= 收盘未判定买入信号）。
    """
    try:
        code = str((rec or {}).get('code') or '') if (rec or {}).get('triggered') else ''
        nm = str((rec or {}).get('name') or '')
        return _store.reconcile_close(str(sname), code=code or None, name=nm,
                                      trade_date=trade_date)
    except Exception as e:
        _log(f'收盘信号对账失败: {str(e)[:80]}')
        return 0


def intraday_signals_today(sid=None, include_gone=True, include_deleted=False,
                           include_watch=True):
    """当日盘中信号事件列表（按首次触发时刻升序）；sid 可为策略全名或简称（S2）。

    ★ include_watch=False 排除**持仓跟踪**行（watch=1，轮值外策略对自己持仓股的盘中留痕）——
      它们不是买入信号，不该进"待执行信号"口径（signal_rows.intraday_rows 默认即如此）。"""
    try:
        return _store.query(date=_today(), sid=sid, include_gone=include_gone,
                            include_deleted=include_deleted, include_watch=include_watch)
    except Exception as e:
        _log(f'盘中信号查询失败: {str(e)[:80]}')
        return []


def intraday_signals_history(days=30, sid=None, include_gone=True, include_deleted=False,
                             limit=1000):
    """最近 N 天全部盘中信号（**跨日历史**——旧 JSON 方案在这里会丢数据）。

    include_deleted=True 可把"人工停用"的记录一并调取（软删除的行始终在库里）。"""
    try:
        return _store.history(days=days, sid=sid, include_gone=include_gone,
                              include_deleted=include_deleted, limit=limit)
    except Exception as e:
        _log(f'盘中信号历史查询失败: {str(e)[:80]}')
        return []


def intraday_backfill_fwd(days=400, force=False, limit=5000):
    """★ 2026-09-18：回填盘中信号的**前向收益**（T+1/T+3/T+5）与**收盘确认**（confirmed/confirm_px/confirm_name）。

    口径（与 _signal_health.py 一致）：以触发日收盘（qfq）为基准 → 之后第 1/3/5 个交易日收盘的涨跌幅%。
    幂等：只处理 evaluated_at 为空的行（数据没到齐的下次再算）；force=True 全量重算。
    返回 {'scanned', 'filled', 'pending'}，异常返回 {'scanned': 0, 'filled': 0, 'pending': 0, 'err': ...}。"""
    try:
        return _store.backfill_forward(days=days, force=force, limit=limit)
    except Exception as e:
        _log(f'前向收益回填失败: {str(e)[:80]}')
        return {'scanned': 0, 'filled': 0, 'pending': 0, 'err': str(e)[:200]}


def intraday_watch_record(rec, sname, code=None, name=None, trade_date=None, note=None):
    """★ 2026-09-18：**持仓跟踪**留痕（轮值外策略的盘中"下文"）。

    盘中扫描原本只覆盖当日轮值策略（一天 1 个），有持仓的非轮值策略在轮值外完全没有盘中踪迹。
    这里只对**持仓股**做窄扫描（每策略 1 只，成本可忽略）后落一行 watch=1 的记录：
    不推送邮件、不算待执行买入信号（signal_rows.intraday_rows 已过滤），只在 /signals 页展示。
    返回事件 dict（失败返回 None）。"""
    if not sname:
        return None
    _code = str(code or (rec or {}).get('code') or '')
    if not _code:
        return None
    try:
        return _store.upsert_watch(rec or {}, sname, _code, name=name,
                                   trade_date=trade_date, note=note)
    except Exception as e:
        _log(f'持仓跟踪落库失败 [{sname} {_code}]: {str(e)[:80]}')
        return None


def signal_soft_delete(ev_id, reason='人工停用'):
    """★ 软删除：只置 deleted 标志位（行保留、历史可调取），绝不物理删除"""
    try:
        return _store.soft_delete(ev_id=int(ev_id), reason=reason)
    except Exception as e:
        _log(f'信号停用失败: {str(e)[:80]}')
        return 0


def signal_restore(ev_id):
    """恢复被停用的信号记录（deleted 标志位复位）"""
    try:
        return _store.restore(int(ev_id))
    except Exception as e:
        _log(f'信号恢复失败: {str(e)[:80]}')
        return 0


def signal_day_stats(date=None, include_deleted=False):
    """某日信号汇总：{total, live, gone, pushed, deleted}"""
    try:
        return _store.day_stats(date=date, include_deleted=include_deleted)
    except Exception:
        return {'total': 0, 'live': 0, 'gone': 0, 'pushed': 0, 'deleted': 0}


def send_intraday_signal(rec, sname='', require_streak=True):
    """盘中扫描到买入信号时实时推送（**稳定后**才推）。

    ★ 2026-10-09：默认 require_streak=True —— 需**连续 INTRADAY_STREAK_MIN 轮扫描仍在触发**
      才发邮件（用户反馈"信号发出去又消失了"，盘中用实时价拼伪 bar，冲高即触发、回落即消失）。
      未达轮数 → 照常落库留痕、不发邮件；信号中途消失 → 计数清零，永不补发。
    ★ require_streak=False 只给**确定性决策**用（如 space_watch 14:57 收盘确认封板买入，
      它一天只确认一次、不存在"下一轮还在不在"的问题，被轮数挡住会漏发）。

    去重口径（2026-09-10 修订，用户反馈"同一标的当天被推了两次"）：
    - 当日同策略同标的 **只推一次**（即使盘中信号短暂消失又回来，也不再重推）；
    - 同策略换了新标的 → 视为新事件，可再推一次；
    - 每轮扫描都会把触发写入 SQLite 信号库（record_intraday_signal），
      页面「📡 盘中信号历史」（/signals）可调取**任意历史日**的全部信号。
    去重不依赖 send 的内存 once_key（重启即丢），由库里的 pushed 标志位持久控制。"""
    if not rec or not rec.get('triggered'):
        return False
    code = str(rec.get('code') or '')
    nm = str(rec.get('name') or '')
    # ★ 2026-09-17：**先落库、再判推送开关**。旧顺序把 record_intraday_signal 放在 enabled()
    #   之后 → 推送通道一关（notify_config.json enabled=false 或通道配错），信号连库都不落，
    #   又会重现"推荐过却哪里都查不到"。留痕与推送彻底解耦，/signals 历史始终完整。
    ev = record_intraday_signal(rec, sname) or {}
    # ★ 连续轮数计数（落库之后、推送开关之前：关了推送也要正确计数，且留痕不受影响）
    _n = _streak_bump(sname, code, trade_date=_ev_date(rec)) if require_streak else 0
    if not enabled():
        return False
    if ev.get('pushed'):
        _log(f'盘中信号当日已推送过，跳过: {sname} {nm}（{code}）')
        return False
    if require_streak and _n < INTRADAY_STREAK_MIN:
        _log(f'盘中信号仅连续 {_n}/{INTRADAY_STREAK_MIN} 轮触发，待稳定后再推: '
             f'{sname} {nm}（{code}）')
        return False
    ts = _ev_ts(rec)
    # 稳定性说明：首轮触发时刻 + 已持续轮数（让收件人知道这不是一闪而过的信号）
    _stab = ''
    if require_streak and _n >= INTRADAY_STREAK_MIN:
        _fa = str(ev.get('first_at') or '')
        _stab = f'（已连续 {_n} 轮扫描持续触发'
        if len(_fa) >= 16:
            _stab += f'，自 {_fa[11:16]} 起'
        _stab += '）'
    lines = [
        f'# 📡 盘中触发买入信号',
        '',
        f'**扫描触发**：{ts}{_stab}（盘中实时预估；收盘价确认后于下一交易日 T+1 按策略时点买入）',
        f'**策略**：{sname}',
        f"**标的**：{rec.get('name')}（{code}）",
        f"**现价**：{rec.get('price')} ｜ **评分**：{rec.get('score')}",
    ]
    # 简要推荐理由：策略规则一句话（tag）＋ 当日关键数值（note），如 S1：60日涨幅区间+突破20日高 / 60日涨幅36%
    _why = []
    if rec.get('tag'):
        _why.append(str(rec['tag']))
    if rec.get('note'):
        _why.append(str(rec['note']))
    if _why:
        lines.append(f'**推荐理由**：{"；".join(_why)}')
    _plan = []
    if rec.get('buy_ref') is not None:
        _plan.append(f'买入参考 {rec["buy_ref"]}')
    if rec.get('stop') is not None:
        _plan.append(f'止损 {rec["stop"]}')
    if rec.get('target') is not None:
        _plan.append(f'目标 {rec["target"]}')
    if _plan:
        lines.append(f'**计划**：{" ｜ ".join(_plan)}')
    lines.append('')
    if rec.get('status'):
        lines.append(f'_{rec.get("status")}_')
    ok = send('盘中触发买入信号', '\n'.join(lines))
    if ok:
        # 回写 pushed 标志位（按 (交易日, 策略, 标的) 精确 UPDATE，天然并发安全）
        try:
            _store.mark_pushed(sname, code, ts, trade_date=_ev_date(rec))
        except Exception as e:
            _log(f'盘中信号推送标记写入失败: {str(e)[:80]}')
        _log(f'盘中信号已推送并登记 SQLite: {sname} {nm}（{code}） @{ts}')
    return ok


def intraday_signal_clear(sname, code=None):
    """每次盘中扫描后由 app.py 调用：本策略当前已不再持有该触发信号（未触发/无候选/换标的）。
    ★ 2026-09-10 起只把事件**改标志位** gone=1（行保留在 SQLite，页面仍可见"曾触发过"，
    事后也能在「盘中信号历史」页调取），不再删除记录；
    是否重推由 pushed 标志位决定（当日同策略同标的只推一次）。
    ★ 2026-10-09：同时清零该策略的连续触发计数（下一轮重新累计，见 INTRADAY_STREAK_MIN）。"""
    _streak_reset(sname)
    mark_intraday_gone(str(sname))


# ---------------- 收盘简报 ----------------

def send_close_brief(market=None, rec=None, sname=None, hist=None, review=None):
    """收盘简报：**复盘** + 行情判断/明日预判 + 今日推荐 + 昨日信号T+1结果 + 各策略状态。

    review（2026-10-08 新增）：review.py 产出的复盘 dict（或已渲染好的文本），
    含大盘/量价/情绪（涨停·炸板·连板·晋级）/估值/消息面 + 持仓复盘 + 当日卖出复盘 +
    明日行动。传入为 dict 时在此渲染为 Markdown。
    所有参数均为纯数据，由 app.py 组装传入，本模块不 import 业务代码。
    跨启动去重：同一自然日已成功推送过（无论是否重启）则不再重复推送。"""
    if not enabled():
        return False
    today = _today()
    if _brief_sent_date() == today:
        _log('收盘简报今日已推送过（跨启动去重），跳过')
        return False
    lines = [f'# 📊 收盘简报  {today}', '']
    # ★ 复盘（大盘/量价/情绪/持仓/卖出/明日行动）
    _rv_txt = review if isinstance(review, str) else ''
    if isinstance(review, dict):
        try:
            import review as _rmod
            _rv_txt = _rmod.render_md(review)
        except Exception as e:
            _rv_txt = ''
            _log(f'复盘渲染失败: {str(e)[:80]}')
        if _rv_txt:
            lines.append('## 🔍 今日复盘')
            lines.append(_rv_txt)
            lines.append('')
    elif _rv_txt:
        lines.append('## 🔍 今日复盘')
        lines.append(_rv_txt)
        lines.append('')
    # 行情判断 / 明日预判
    if market and market.get('asof'):
        sh = market['sh']
        n = market['next']
        darr = '▲' if sh['chg'] >= 0 else '▼'
        lines.append(f"**上证 {sh['close']:,.2f}** {darr}{abs(sh['chg']):.2f}% ｜ 形态：{market.get('label')}")
        lines.append(
            f"{_md(market.get('target'))} 预判：**{n['verdict']}**（概率 {n['prob']:.0f}%｜"
            f"强修复{n['strong']:.0f}% / 弱修复{n['weak']:.0f}% / 回落{n['down']:.0f}%"
            f"｜样本{n['n_sim']}例，平均次日{n['avg_next']:+.2f}%）")
        lines.append('')
    # 今日推荐
    if rec and rec.get('name'):
        lines.append(f"**📈 今日推荐（{sname or ''}）**")
        tr = '**触发买入信号**' if rec.get('triggered') else '接近买入（未触发）'
        lines.append(f"- {rec.get('name')} 现价 {rec.get('price')} 评分 {rec.get('score')} {tr}")
        if rec.get('buy_ref') is not None:
            lines.append(f"- 买入参考 {rec['buy_ref']} ｜ 止损 {rec.get('stop')} ｜ 目标 {rec.get('target')}")
        if rec.get('status'):
            lines.append(f"- {rec.get('status')}")
        if rec.get('note'):
            lines.append(f"- {rec.get('note')}")
        lines.append('')
    # 昨日信号 → 今日 T+1 结果
    if hist:
        t1 = _yesterday_t1_lines(hist)
        if t1:
            lines.append('**✅ 昨日信号 → 今日 T+1 结果**')
            lines.extend(t1)
            lines.append('')
    # 各策略今日状态摘要
    # ★ 有复盘时省略：持仓/明日行动已在复盘块里逐条列出，避免同一信息重复两遍
    if hist and not _rv_txt:
        lines.append('**📋 各策略今日状态**')
        for s in hist['strategies']:
            daily = s.get('daily', [])
            if not daily:
                continue
            r = daily[-1]
            pos = ''
            if isinstance(r.get('pos_pnl'), (int, float)):
                pos = f" 浮盈{r['pos_pnl']:+.0f}"
            lines.append(f"- {s['name']}：{r.get('status', '')}{pos}")
        lines.append('')
    ok = send('收盘简报', '\n'.join(lines), once_key=f'{today}|brief')
    if ok:
        _mark_brief_sent(today)
    return ok


def _yesterday_t1_lines(hist):
    """从各策略 daily 序列中取「上一个交易日触发 → 最新交易日结果」"""
    out = []
    for s in hist['strategies']:
        daily = s.get('daily', [])
        if len(daily) < 2:
            continue
        prev, cur = daily[-2], daily[-1]
        if prev.get('triggered'):
            line = f"- {s['name']}：{prev.get('name')}（{prev.get('date')}触发）→ {cur.get('date')} {cur.get('status', '')}"
            if cur.get('invest') is not None:
                line += f" 投入{cur['invest']:,.0f}元"
            note = str(cur.get('note', ''))[:40]
            if note:
                line += f" {note}"
            out.append(line)
    return out


# ---------------- 行情异常告警 ----------------

def send_market_alert(m):
    """行情出现异常/极端信号时告警：大跌、放量长阴、超跌、预判调整概率高、大涨。"""
    if not enabled() or not m or not m.get('asof'):
        return False
    sh = m['sh']
    n = m['next']
    reasons = []
    if sh['chg'] <= -2.0:
        reasons.append(f'上证大跌 {sh["chg"]:+.2f}%')
    if m.get('label') in ('放量长阴', '超跌'):
        reasons.append(f'形态：{m.get("label")}')
    if n.get('verdict') in ('调整/回落', '偏弱/调整') and (n.get('prob') or 0) >= 60:
        reasons.append(f"{_md(m.get('target'))} 预判 {n['verdict']} 概率 {n.get('prob'):.0f}%")
    if sh['chg'] >= 3.0:
        reasons.append(f'上证大涨 {sh["chg"]:+.2f}%')
    if not reasons:
        return False
    content = (f"# ⚠️ 行情异常告警  {m.get('asof')}\n\n"
               + '\n'.join(f'- {r}' for r in reasons)
               +                f"\n\n上证 {sh['close']:,.2f}（{sh['chg']:+.2f}%），形态 {m.get('label')}，"
               f"{_md(m.get('target'))} 预判 {n.get('verdict')}（{n.get('prob'):.0f}%）")
    return send('行情异常告警', content, once_key=f'{_today()}|market', priority=True)


# ---------------- 程序异常告警 ----------------

def send_error_alert(msg):
    """程序异常/构建失败告警。5 分钟节流，不受每日限额限制。"""
    if not enabled():
        return False
    now = datetime.datetime.now()
    if _state['last_err'] and (now - _state['last_err'][1]).total_seconds() < 300:
        return False
    _state['last_err'] = (msg, now)
    content = f"# ⚠️ 程序异常  {now:%Y-%m-%d %H:%M:%S}\n\n{msg}"
    return send('程序异常告警', content, priority=True)
