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

_PUSHPLUS_URL = 'https://www.pushplus.plus/send'
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_WORKSPACE, 'data', 'notify_config.json')
# 收盘简报跨启动去重标记：成功推送后记录日期，当日重启服务不再重发
_BRIEF_FILE = os.path.join(_WORKSPACE, 'data', 'notify_brief_sent.json')
# 盘中触发信号去重标记（2026-09-09 持久化）：当日"策略→标的"首次触发推送后记录；
# 仅存内存会在服务重启后失效导致同信号重复推送。信号消失/废弃后由 app 调用
# intraday_signal_clear 清除，再次触发视为新事件可再推。
_SIG_FILE = os.path.join(_WORKSPACE, 'data', 'notify_signal_sent.json')

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

def _sig_state():
    """盘中信号去重持久状态：{'date', 'sent': {sname: {code,name,at}}}。跨日/无记录时重建。"""
    try:
        st = json.load(open(_SIG_FILE, encoding='utf-8'))
        if isinstance(st, dict) and st.get('date') == _today() and isinstance(st.get('sent'), dict):
            return st
    except Exception:
        pass
    return {'date': _today(), 'sent': {}}


def _sig_save(st):
    try:
        with open(_SIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        _log(f'盘中信号去重标记写入失败: {str(e)[:60]}')


def send_intraday_signal(rec, sname=''):
    """盘中扫描到买入信号时实时推送。

    去重口径（2026-09-09 起跨启动持久化，且“信号废弃后可再推”）：
    - 当日同策略同标的首次触发 → 推送并记录；
    - 此后盘中扫描仍触发同标的 → 不再重复推送（服务重启也不重发）；
    - 期间该策略信号消失/被废弃（app 每次扫描后调用 intraday_signal_clear）
      → 清除记录，日后再次触发视为新事件、允许重新推送。
    去重不依赖 send 的内存 once_key（重启即丢），由本函数自行持久化控制。"""
    if not enabled() or not rec or not rec.get('triggered'):
        return False
    code = rec.get('code', '')
    nm = str(rec.get('name') or '')
    st = _sig_state()
    prev = (st.get('sent') or {}).get(sname)
    if prev and prev.get('code') == code and prev.get('name') == nm:
        return False  # 同一策略同一标的当日已推送且未废弃，不重复发
    # ★ 扫描触发具体时刻（2026-09-09）：由扫描方 rec['trigger_time'] 记录（YYYY-MM-DD HH:MM:SS），
    # 缺省退回当前时刻；页面/邮件均以它为准，避免正文只有日期
    ts = str(rec.get('trigger_time') or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')).strip()
    if len(ts) == 8:
        ts = f'{_today()} {ts}'
    elif '-' not in ts:
        ts = f'{_today()} {ts}'
    lines = [
        f'# 📡 盘中触发买入信号',
        '',
        f'**扫描触发**：{ts}（盘中实时预估；收盘价确认后于下一交易日 T+1 按策略时点买入）',
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
        st['sent'][sname] = {'code': code, 'name': nm, 'at': ts}
        _sig_save(st)
        _log(f'盘中信号去重已记录: {sname} {nm}（{code}） @{ts}')
    return ok


def intraday_signal_clear(sname, code=None):
    """每次盘中扫描后由 app.py 调用：本策略当前已不再持有该触发信号
    （未触发/无候选/换标）。清除当日去重记录 → 该策略日后再次触发可重新推送。
    code=None 时清除该策略全部（默认）。非盘中时段不要调用（收盘信号等待执行期间不算消失）。"""
    st = _sig_state()
    sent = st.get('sent') or {}
    cur = sent.get(sname)
    if cur and (code is None or cur.get('code') == code):
        sent.pop(sname, None)
        _sig_save(st)
        _log(f'盘中信号去重已清除: {sname}（{code if code is not None else "全部"}）')


# ---------------- 收盘简报 ----------------

def send_close_brief(market=None, rec=None, sname=None, hist=None):
    """收盘简报：今日推荐 + 行情判断/明日预判 + 昨日信号T+1结果 + 各策略状态。
    所有参数均为纯数据，由 app.py 组装传入，本模块不 import 业务代码。
    跨启动去重：同一自然日已成功推送过（无论是否重启）则不再重复推送。"""
    if not enabled():
        return False
    today = _today()
    if _brief_sent_date() == today:
        _log('收盘简报今日已推送过（跨启动去重），跳过')
        return False
    lines = [f'# 📊 收盘简报  {today}', '']
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
    if hist:
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
