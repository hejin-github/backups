# -*- coding: utf-8 -*-
"""公共展示层基础件（2026-09-10 从 app.py 抽出，行为完全不变，仅搬家）。

包含：
- 页面样式/脚本常量：CSS、JS
- 文本与日期小工具：_md（9月3日）、_next_td（下一交易日近似）、_num2
- 富文本：_esc_note（转义后把文中股票简称加粗）
- 图表：_nice_ticks、svg_line_chart（纯字符串拼 SVG，无外部依赖）

设计约定：
- 本模块**不持有任何业务状态**，全部为纯函数/常量，可被 app.py、signal_rows.py 等安全复用；
- 需要 app 全局状态（TODAY_STATUS/HISTORY/RT_SNAPSHOT）的函数一律**不放在这里**，
  以免形成 app ↔ 模块 的循环导入（app.py 以 __main__ 运行，被二次 import 会产生两份全局状态）。
"""
import re
import html
import json
import math
import datetime

import advisor

# ============================================================
# 文本 / 日期小工具
# ============================================================


def _md(s):
    """预判标签日期：'2026-09-03' → '9月3日'；无日期返回 '明日'"""
    try:
        y, mo, d = str(s).split('-')
        return f'{int(mo)}月{int(d)}日'
    except Exception:
        return str(s) if s else '明日'


# ★ 2026-09-28 修复：周末之外的休市日（系统无官方节假日表，按年人工维护，务必每年补充）。
#   教训：2026-09-25（周五，中秋节）休市，但 _next_td 只跳周末 →
#   _next_td('2026-09-24') 算出 9/25 → 首页出现"9月25日买入"，而当天根本不开盘。
MARKET_HOLIDAYS = {
    '2026-09-25',   # 中秋节（周五）休市
}


def _next_td(d):
    """d（'YYYY-MM-DD'）之后的下一交易日：跳过周末 + MARKET_HOLIDAYS 中的休市日。"""
    try:
        t = datetime.date.fromisoformat(str(d)[:10]) + datetime.timedelta(days=1)
        while t.weekday() >= 5 or t.isoformat() in MARKET_HOLIDAYS:
            t += datetime.timedelta(days=1)
        return t.isoformat()
    except Exception:
        return None


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


def _dash(v, alt='-'):
    """渲染占位兜底：None → alt（默认 '-'）。

    ★ 2026-09-10 修复：不要用 dict.get(k, '-') —— 当**键存在但值为 None** 时它仍返回 None，
      页面就会渲染出字面量 "None"（典型：指数策略 S7/S8 持仓无股数 → 成交明细表出现 None）。
      所有可能为 None 的字段一律经此函数。"""
    return alt if v is None else v


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


# ============================================================
# 图表（SVG 字符串）
# ============================================================

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


# ============================================================
# 页面样式 / 脚本常量
# ============================================================

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
/* ★ 2026-09-10 用户要求：仅「策略详情页」里加粗的标的纯黑（首页/总账/其它页面保持原色；
   带 class 的语义色如 .st.ok/.st.hold/.up 不受影响）。范围由 page_frame 给详情页 <main> 加 .sdetail 限定 */
.sdetail b:not([class]){color:#000}
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
