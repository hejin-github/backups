# -*- coding: utf-8 -*-
"""总市值硬闸门（用户口径 2026-11-05，任务 #19）

stock_bot 选股宇宙 / 动态池统一：**总市值 ≤ 250 亿元**。
三工程同口径：quant_trader `market_cap.MAX_TOTAL_MV`、hubin_daban `engine.MAX_TOTAL_MV`。

设计要点
--------
1. 市值只能取「当前快照」，故闸门作用于**实盘选股宇宙**（history_engine.load_all
   加载哪些标的、动态池从哪些标的里选），不动父目录 `backtest.py` 的历史回测口径；
   历史账本重放随之收窄宇宙，属于策略定义变更，已在 OOS_REPORT / 交付说明中披露。
2. 全市场市值快照缓存到 `data/market_caps.json`（7 天 TTL），避免每次启动联网扫 2600 只。
3. 缓存缺失且联网失败 → **放行全量并告警**（行情源异常不静默清空策略）。
4. 市值缺失的个股放行（数据不足 ≠ 超标），与胜率门槛 None 放行同口径。
"""
import os
import json
import time
import datetime
import urllib.request

MAX_TOTAL_MV = 250e8          # 250 亿元（单位=元，含等号）
TTL_DAYS = 7

BASE = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(os.path.dirname(BASE), "data", "market_caps.json")
_UA = {"User-Agent": "Mozilla/5.0"}
_MEM = {"caps": None, "loaded": False}


def _fetch_caps(codes):
    """腾讯批量快照 [44] 总市值(亿元) → 元。分批 15 只，避免 431 请求头过长。"""
    out = {}
    codes = [str(c).zfill(6) for c in codes]
    for i in range(0, len(codes), 15):
        batch = codes[i:i + 15]
        q = ",".join(("sh" + c if c.startswith(("6", "9")) else "sz" + c) for c in batch)
        try:
            req = urllib.request.Request("http://qt.gtimg.cn/q=" + q, headers=_UA)
            txt = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
        except Exception:
            continue
        for line in txt.strip().split(";"):
            if "=" not in line:
                continue
            p = line.split("=", 1)[1].strip().strip('"').split("~")
            if len(p) < 46 or not p[2]:
                continue
            try:
                v = float(p[44]) * 1e8
            except (ValueError, TypeError):
                continue
            if v > 0:
                out[p[2].zfill(6)] = v
    return out


def refresh(codes, force=False):
    """生成/刷新缓存，返回 {code: 总市值(元)}。"""
    if not force and os.path.exists(_CACHE):
        try:
            d = json.load(open(_CACHE, encoding="utf-8"))
            age = (datetime.date.today() - datetime.date.fromisoformat(d["asof"])).days
            if 0 <= age <= TTL_DAYS and d.get("caps"):
                _MEM["caps"], _MEM["loaded"] = d["caps"], True
                return d["caps"]
        except Exception:
            pass
    caps = _fetch_caps(codes)
    if not caps:
        # 联网失败 → 退回旧缓存；仍无则放行全量
        try:
            d = json.load(open(_CACHE, encoding="utf-8"))
            caps = d.get("caps") or {}
        except Exception:
            caps = {}
        if caps:
            print(f"[universe_cap] ⚠️ 刷新失败，沿用旧缓存 asof={d.get('asof')}", flush=True)
    else:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump({"asof": datetime.date.today().isoformat(),
                       "max_total_mv": MAX_TOTAL_MV, "caps": caps}, f, ensure_ascii=False)
        print(f"[universe_cap] 市值快照已刷新 {len(caps)} 只 → {_CACHE}", flush=True)
    _MEM["caps"], _MEM["loaded"] = caps, True
    return caps


def load_caps(codes):
    if not _MEM["loaded"]:
        return refresh(codes)
    return _MEM["caps"] or {}


def selectable_codes(codes, label="选股宇宙"):
    """返回合规代码集合（总市值 ≤ 250亿；市值缺失或缓存不可用时放行）。"""
    codes = [str(c).zfill(6) for c in codes]
    caps = load_caps(codes)
    known = [c for c in codes if c in caps]
    if len(known) < len(codes) * 0.5:
        print(f"[universe_cap] ⚠️ {label} 市值覆盖率 {len(known)}/{len(codes)} < 50%，"
              f"本轮放行全量（不施加 250亿 闸门），请检查行情源", flush=True)
        return set(codes)
    keep = {c for c in codes if caps.get(c, 0) <= MAX_TOTAL_MV}
    print(f"[universe_cap] {label}: {len(codes)} → {len(keep)} 只 "
          f"(>250亿剔除 {len(codes) - len(keep)}，市值缺失放行 {len(codes) - len(known)})", flush=True)
    return keep
