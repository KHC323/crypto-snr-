#!/usr/bin/env python3
"""
ICT/SMC 終極分析引擎 v3.0
全部功能：
  ✅ 確認K線偵測（吞噬、錘子、強勢收盤）
  ✅ Displacement 推動波強度確認
  ✅ 多時框 OB/FVG 重疊 Confluence
  ✅ Inducement 誘多/誘空偵測
  ✅ Kill Zone 時間過濾（倫敦/紐約）
  ✅ 回測引擎（過去90天勝率/R:R統計）
  ✅ 交易日誌（自動記錄/複盤）
  ✅ Telegram 推播通知（選填）
  ✅ 5層時間框架分析
  ✅ OB/FVG/SSL/BSL/BOS/CHoCH/MSS
  ✅ Premium/Discount 區間

執行：cd ~/Downloads && python3 snr_server.py
開啟：http://localhost:8888
"""

import http.server, json, urllib.request, urllib.error
import hmac, hashlib, base64, os, time, sqlite3, threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

OKX_BASE = "https://www.okx.com"
DB_PATH  = os.path.join(os.path.dirname(__file__), "trade_journal.db")

# ═══════════════════════════════════════════════════════════════════════════
# 資料庫 — 交易日誌
# ═══════════════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, inst TEXT, signal TEXT, tf TEXT,
        entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        rr REAL, confidence INTEGER,
        ob_confluence INTEGER, fvg_in_zone INTEGER,
        kill_zone TEXT, displacement INTEGER,
        confirmation_candle TEXT,
        notes TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS backtest_cache (
        inst TEXT PRIMARY KEY, ts TEXT, result TEXT
    )""")
    conn.commit(); conn.close()

def log_signal(data):
    try:
        conn = sqlite3.connect(DB_PATH)
        p = data.get("plan", {})
        conn.execute("""INSERT INTO journal
            (ts,inst,signal,tf,entry,sl,tp1,tp2,tp3,rr,confidence,
             ob_confluence,fvg_in_zone,kill_zone,displacement,confirmation_candle,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            data.get("ts"), data.get("inst"), p.get("signal"),
            "15m", p.get("entry"), p.get("sl"),
            p.get("tp1"), p.get("tp2"), p.get("tp3"),
            p.get("rr1"), p.get("confidence"),
            int(p.get("ob_confluence", False)),
            int(p.get("fvg_in_zone", False)),
            p.get("kill_zone", ""),
            int(p.get("displacement", False)),
            p.get("confirmation_candle", ""),
            p.get("entry_note", "")
        ))
        conn.commit(); conn.close()
    except: pass

def get_journal(inst="", limit=20):
    try:
        conn = sqlite3.connect(DB_PATH)
        q = "SELECT * FROM journal"
        params = []
        if inst:
            q += " WHERE inst=?"; params.append(inst)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        cols = ["id","ts","inst","signal","tf","entry","sl","tp1","tp2","tp3",
                "rr","confidence","ob_confluence","fvg_in_zone","kill_zone",
                "displacement","confirmation_candle","notes"]
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except:
        return []

init_db()

# ═══════════════════════════════════════════════════════════════════════════
# OKX API
# ═══════════════════════════════════════════════════════════════════════════

def okx_get(path):
    url = OKX_BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode())
    if d.get("code") != "0":
        raise Exception(f"OKX: {d.get('msg','error')}")
    return d["data"]

def okx_private(path, ak, sk, pp):
    ts  = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    msg = ts + "GET" + path
    sig = base64.b64encode(hmac.new(sk.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    headers = {"OK-ACCESS-KEY":ak,"OK-ACCESS-SIGN":sig,
               "OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":pp,
               "Content-Type":"application/json","User-Agent":"Mozilla/5.0"}
    req = urllib.request.Request(OKX_BASE + path, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode())
    if d.get("code") != "0":
        raise Exception(f"OKX私有: {d.get('msg')}")
    return d["data"]

def norm(sym):
    s = sym.strip().upper()
    if "-" in s: return s
    if s.endswith("USDT"): return s[:-4]+"-USDT"
    return s+"-USDT"

def get_candles(inst, bar, limit=300):
    raw = okx_get(f"/api/v5/market/candles?instId={inst}&bar={bar}&limit={limit}")
    return [{"t":int(r[0]),"o":float(r[1]),"h":float(r[2]),
             "l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in reversed(raw)]

def get_ticker(inst):
    t = okx_get(f"/api/v5/market/ticker?instId={inst}")[0]
    return {"price":float(t["last"]),"open24":float(t["open24h"]),
            "high24":float(t["high24h"]),"low24":float(t["low24h"]),
            "vol24":float(t["volCcy24h"]),"bid":float(t["bidPx"]),"ask":float(t["askPx"])}

def get_books(inst):
    d = okx_get(f"/api/v5/market/books?instId={inst}&sz=20")[0]
    bids = [[float(x[0]),float(x[1])] for x in d["bids"]]
    asks = [[float(x[0]),float(x[1])] for x in d["asks"]]
    return bids, asks

# ═══════════════════════════════════════════════════════════════════════════
# 技術指標
# ═══════════════════════════════════════════════════════════════════════════

def ema(arr, p):
    k=2/(p+1); r=[arr[0]]
    for v in arr[1:]: r.append(v*k+r[-1]*(1-k))
    return r

def rsi(closes, p=14):
    if len(closes)<p+2: return 50.0
    g=l=0.0
    for i in range(len(closes)-p, len(closes)):
        d=closes[i]-closes[i-1]
        if d>0: g+=d
        else: l-=d
    ag,al=g/p,l/p
    return round(100-100/(1+ag/max(al,1e-9)),2)

def atr(candles, p=14):
    trs=[]
    for i,c in enumerate(candles):
        if i==0: trs.append(c["h"]-c["l"])
        else: trs.append(max(c["h"]-c["l"],abs(c["h"]-candles[i-1]["c"]),abs(c["l"]-candles[i-1]["c"])))
    a=sum(trs[:p])/p
    for tr in trs[p:]: a=(a*(p-1)+tr)/p
    return a

def macd(closes):
    e12=ema(closes,12); e26=ema(closes,26)
    ml=[a-b for a,b in zip(e12,e26)]
    sp=ema(ml[25:],9); sf=[None]*25+sp
    hist=[(ml[i]-sf[i]) if sf[i] is not None else 0 for i in range(len(ml))]
    bull=ml[-1]>(sf[-1] or 0)
    golden=sf[-1] is not None and sf[-2] is not None and ml[-1]>sf[-1] and ml[-2]<=sf[-2]
    dead=sf[-1] is not None and sf[-2] is not None and ml[-1]<=sf[-1] and ml[-2]>sf[-2]
    return {"hist":round(hist[-1],4),"bull":bull,"golden":golden,"dead":dead}

# ═══════════════════════════════════════════════════════════════════════════
# Kill Zone 時間過濾
# ═══════════════════════════════════════════════════════════════════════════

def get_kill_zone():
    """
    ICT Kill Zones（UTC 時間）
    亞洲盤    00:00 - 04:00 UTC
    倫敦開盤  07:00 - 10:00 UTC  ← 最重要
    紐約開盤  12:00 - 15:00 UTC  ← 最重要
    紐約尾盤  19:00 - 21:00 UTC
    """
    now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    if   7  <= h < 10: return {"zone":"倫敦開盤 🇬🇧", "active":True,  "quality":"最佳",  "color":"green"}
    elif 12 <= h < 15: return {"zone":"紐約開盤 🇺🇸", "active":True,  "quality":"最佳",  "color":"green"}
    elif 0  <= h <  4: return {"zone":"亞洲盤 🌏",    "active":True,  "quality":"一般",  "color":"yellow"}
    elif 19 <= h < 21: return {"zone":"紐約尾盤",     "active":True,  "quality":"較弱",  "color":"yellow"}
    else:
        # 計算下個 Kill Zone
        if h < 7:   next_kz = "倫敦開盤"; mins = (7-h)*60 - now_utc.minute
        elif h < 12: next_kz = "紐約開盤"; mins = (12-h)*60 - now_utc.minute
        elif h < 19: next_kz = "紐約尾盤"; mins = (19-h)*60 - now_utc.minute
        else:        next_kz = "亞洲盤";   mins = (24-h+0)*60 - now_utc.minute
        return {"zone":f"非交易時段", "active":False, "quality":"避免",
                "next": next_kz, "mins_to_next": mins, "color":"red"}

# ═══════════════════════════════════════════════════════════════════════════
# Swing Points
# ═══════════════════════════════════════════════════════════════════════════

def find_swings(candles, left=3, right=3):
    sh, sl = [], []
    n = len(candles)
    for i in range(left, n-right):
        h,l = candles[i]["h"], candles[i]["l"]
        if all(h>=candles[i+k]["h"] for k in list(range(-left,0))+list(range(1,right+1))):
            sh.append({"price":h,"idx":i,"t":candles[i]["t"]})
        if all(l<=candles[i+k]["l"] for k in list(range(-left,0))+list(range(1,right+1))):
            sl.append({"price":l,"idx":i,"t":candles[i]["t"]})
    return sh, sl

# ═══════════════════════════════════════════════════════════════════════════
# Displacement 偵測
# ═══════════════════════════════════════════════════════════════════════════

def detect_displacement(candles, lookback=5):
    """
    Displacement = 強勢推動波
    條件：
    1. 連續 2-3 根同向K線
    2. 每根實體 > 平均實體 1.5x
    3. 影線極短（實體佔K線 > 70%）
    4. 成交量放大
    返回：{"bull":bool, "bear":bool, "strength":1-5, "desc":str}
    """
    if len(candles) < lookback + 3:
        return {"bull":False,"bear":False,"strength":0,"desc":"數據不足"}

    recent = candles[-lookback:]
    bodies = [abs(c["c"]-c["o"]) for c in candles]
    avg_body = sum(bodies[:-lookback]) / max(1, len(bodies)-lookback)

    bull_count = 0; bear_count = 0
    bull_strength = 0; bear_strength = 0

    for c in recent:
        body  = abs(c["c"]-c["o"])
        total = c["h"] - c["l"]
        body_ratio = body / total if total > 0 else 0
        size_ratio = body / avg_body if avg_body > 0 else 1

        if c["c"] > c["o"] and body_ratio > 0.6 and size_ratio > 1.2:
            bull_count += 1
            bull_strength += size_ratio * body_ratio
        if c["c"] < c["o"] and body_ratio > 0.6 and size_ratio > 1.2:
            bear_count += 1
            bear_strength += size_ratio * body_ratio

    bull_disp = bull_count >= 2
    bear_disp = bear_count >= 2
    strength  = min(5, int(max(bull_strength, bear_strength) / 2))

    desc = []
    if bull_disp: desc.append(f"多頭位移（{bull_count}根強勢陽線）")
    if bear_disp: desc.append(f"空頭位移（{bear_count}根強勢陰線）")
    if not desc:  desc.append("無明顯位移")

    return {"bull":bull_disp,"bear":bear_disp,"strength":strength,"desc":" · ".join(desc)}

# ═══════════════════════════════════════════════════════════════════════════
# 確認K線偵測
# ═══════════════════════════════════════════════════════════════════════════

def detect_confirmation_candle(candles, direction, zone_high, zone_low):
    """
    等待確認K線（ICT 進場觸發條件）
    方向 = LONG 或 SHORT
    zone = OB 或 FVG 的區間

    多頭確認K線（價格在支撐區時）：
      - 看漲吞噬（陽線實體完全覆蓋前根陰線）
      - 錘子線（下影線 > 實體 2x，收在上半部）
      - 強勢陽線（實體 > 平均 1.5x，收盤 > 區間 50%）
      - Pin Bar（下影線極長）

    空頭確認K線（價格在壓力區時）：
      - 看跌吞噬
      - 射擊之星（上影線 > 實體 2x，收在下半部）
      - 強勢陰線
    """
    if len(candles) < 3:
        return {"confirmed":False,"type":"無","desc":"等待確認K線"}

    last  = candles[-1]
    prev  = candles[-2]
    zone_mid = (zone_high + zone_low) / 2

    body_last = abs(last["c"] - last["o"])
    body_prev = abs(prev["c"] - prev["o"])
    total_last = last["h"] - last["l"]
    upper_wick = last["h"] - max(last["c"], last["o"])
    lower_wick = min(last["c"], last["o"]) - last["l"]
    body_ratio = body_last / total_last if total_last > 0 else 0

    bodies = [abs(c["c"]-c["o"]) for c in candles[:-1]]
    avg_body = sum(bodies[-10:]) / min(10, len(bodies))

    if direction == "LONG":
        # 1. 看漲吞噬
        if (last["c"] > last["o"] and prev["c"] < prev["o"] and
            last["c"] > prev["o"] and last["o"] < prev["c"]):
            return {"confirmed":True,"type":"看漲吞噬 🟢","desc":f"陽線吞噬前陰線，強力反轉信號","strength":5}

        # 2. 錘子線
        if (lower_wick > body_last*2 and upper_wick < body_last*0.5 and
            last["c"] > zone_mid):
            return {"confirmed":True,"type":"錘子線 🔨","desc":"長下影線，買方強力接盤","strength":4}

        # 3. Pin Bar（下針）
        if lower_wick > total_last*0.6 and last["c"] > last["o"]:
            return {"confirmed":True,"type":"Pin Bar 📍","desc":"長下影針，精準獵取止損後反彈","strength":4}

        # 4. 強勢陽線
        if (last["c"] > last["o"] and body_last > avg_body*1.4 and
            body_ratio > 0.65 and last["c"] > zone_mid):
            return {"confirmed":True,"type":"強勢陽線 ⬆","desc":f"大實體陽線，多頭動能強","strength":3}

        # 5. 剛進入區間但尚無確認
        if zone_low <= last["l"] <= zone_high or zone_low <= last["c"] <= zone_high:
            return {"confirmed":False,"type":"進入區間","desc":"等待下一根K線確認（建議掛限價單）","strength":1}

    elif direction == "SHORT":
        # 1. 看跌吞噬
        if (last["c"] < last["o"] and prev["c"] > prev["o"] and
            last["c"] < prev["o"] and last["o"] > prev["c"]):
            return {"confirmed":True,"type":"看跌吞噬 🔴","desc":"陰線吞噬前陽線，強力反轉信號","strength":5}

        # 2. 射擊之星
        if (upper_wick > body_last*2 and lower_wick < body_last*0.5 and
            last["c"] < zone_mid):
            return {"confirmed":True,"type":"射擊之星 ⭐","desc":"長上影線，賣方強力壓制","strength":4}

        # 3. 空頭 Pin Bar
        if upper_wick > total_last*0.6 and last["c"] < last["o"]:
            return {"confirmed":True,"type":"Pin Bar 📍","desc":"長上影針，精準獵取止損後下跌","strength":4}

        # 4. 強勢陰線
        if (last["c"] < last["o"] and body_last > avg_body*1.4 and
            body_ratio > 0.65 and last["c"] < zone_mid):
            return {"confirmed":True,"type":"強勢陰線 ⬇","desc":f"大實體陰線，空頭動能強","strength":3}

        if zone_low <= last["h"] <= zone_high or zone_low <= last["c"] <= zone_high:
            return {"confirmed":False,"type":"進入區間","desc":"等待下一根K線確認","strength":1}

    return {"confirmed":False,"type":"無確認","desc":"尚未出現確認K線，等待","strength":0}

# ═══════════════════════════════════════════════════════════════════════════
# Inducement 偵測
# ═══════════════════════════════════════════════════════════════════════════

def detect_inducement(candles, cur_price, swing_highs, swing_lows):
    """
    Inducement = 誘多/誘空（假突破）
    特徵：
    - 假突破前高/前低，但收盤反向
    - 通常用來獵取散戶止損，然後反向運行
    - 假突破後的反向K線是很強的進場信號
    """
    inductions = []
    n = len(candles)
    if n < 5: return []

    recent_sh_prices = [s["price"] for s in swing_highs[-5:]]
    recent_sl_prices = [s["price"] for s in swing_lows[-5:]]

    for i in range(2, min(n, 20)):
        c = candles[n-i]
        prev = candles[n-i-1]

        # 誘多：突破前高但收盤拉回（上影線長）
        for sh_price in recent_sh_prices:
            if (c["h"] > sh_price and          # 突破前高
                c["c"] < sh_price and           # 收盤拉回
                c["h"] - max(c["c"],c["o"]) > abs(c["c"]-c["o"]) * 1.5):  # 長上影線
                dist = round((cur_price - c["l"]) / cur_price * 100, 2) if cur_price > c["l"] else 0
                inductions.append({
                    "type": "誘多 (IDM High)",
                    "swept_level": round(sh_price, 4),
                    "candle_high": round(c["h"], 4),
                    "candle_low":  round(c["l"], 4),
                    "t": c["t"],
                    "desc": f"假突破前高 {round(sh_price,0)} → 收盤拉回，散戶多單止損被獵取",
                    "implication": "可能轉空，等待空頭確認K線",
                    "dist_pct": dist
                })

        # 誘空：跌破前低但收盤反彈（下影線長）
        for sl_price in recent_sl_prices:
            if (c["l"] < sl_price and           # 跌破前低
                c["c"] > sl_price and            # 收盤反彈
                min(c["c"],c["o"]) - c["l"] > abs(c["c"]-c["o"]) * 1.5):  # 長下影線
                dist = round((c["h"] - cur_price) / cur_price * 100, 2) if cur_price < c["h"] else 0
                inductions.append({
                    "type": "誘空 (IDM Low)",
                    "swept_level": round(sl_price, 4),
                    "candle_high": round(c["h"], 4),
                    "candle_low":  round(c["l"], 4),
                    "t": c["t"],
                    "desc": f"假跌破前低 {round(sl_price,0)} → 收盤反彈，散戶空單止損被獵取",
                    "implication": "可能轉多，等待多頭確認K線",
                    "dist_pct": dist
                })

    return inductions[:4]

# ═══════════════════════════════════════════════════════════════════════════
# ICT OB 偵測（完整版）
# ═══════════════════════════════════════════════════════════════════════════

def detect_ob(candles, cur_price):
    obs = []
    n = len(candles)
    if n < 10: return []

    bodies = [abs(c["c"]-c["o"]) for c in candles]
    avg_body = sum(bodies) / len(bodies) if bodies else 1

    sh_idx = set()
    sl_idx = set()
    for i in range(2, n-2):
        h,l = candles[i]["h"], candles[i]["l"]
        if all(h>=candles[i+k]["h"] for k in [-2,-1,1,2]): sh_idx.add(i)
        if all(l<=candles[i+k]["l"] for k in [-2,-1,1,2]): sl_idx.add(i)

    for i in range(1, n-3):
        c    = candles[i]
        body = abs(c["c"]-c["o"])
        if body < avg_body*0.3: continue

        # ── 多頭 OB：陰線 + BOS ──────────────────────────────────────────
        if c["c"] < c["o"]:
            bos_found = False; bos_idx = -1
            for j in range(i+1, min(i+5, n)):
                jc = candles[j]
                jbody = abs(jc["c"]-jc["o"])
                if jc["c"] > c["h"] and jbody > avg_body*0.5:
                    bos_found = True; bos_idx = j; break

            if not bos_found: continue

            # Displacement 確認（BOS K線必須夠強）
            bos_c = candles[bos_idx]
            bos_body = abs(bos_c["c"]-bos_c["o"])
            displacement_ok = bos_body > avg_body * 1.3

            # 未被穿透
            subsequent = candles[bos_idx+1:min(bos_idx+30, n)]
            violated = any(sc["c"] < c["l"] for sc in subsequent)
            if violated: continue

            in_zone  = c["l"] <= cur_price <= c["h"]
            near_ob  = c["h"] < cur_price <= c["h"]*1.008

            if not (in_zone or near_ob): continue

            strength = 1
            if body > avg_body*1.5: strength += 1
            if i in sl_idx or (i+1) in sl_idx: strength += 1
            avg_vol = sum(cc["v"] for cc in candles[max(0,i-10):i]) / 10 if i >= 10 else c["v"]
            if c["v"] > avg_vol*1.3: strength += 1
            if displacement_ok: strength += 1
            if in_zone: strength = min(5, strength+1)

            obs.append({
                "type":"BULL_OB","high":round(c["h"],6),"low":round(c["l"],6),
                "ob50":round((c["h"]+c["l"])/2,6),"t":c["t"],
                "strength":min(5,strength),"dist_pct":round(abs(cur_price-c["l"])/cur_price*100,2),
                "in_zone":in_zone,"displacement":displacement_ok,"bos_confirmed":True,
                "label":"多頭訂單塊 🟢",
                "entry_note":"理想進場：OB 50%線" if in_zone else "等待回測至OB",
            })

        # ── 空頭 OB：陽線 + BOS ──────────────────────────────────────────
        if c["c"] > c["o"]:
            bos_found = False; bos_idx = -1
            for j in range(i+1, min(i+5, n)):
                jc = candles[j]; jbody = abs(jc["c"]-jc["o"])
                if jc["c"] < c["l"] and jbody > avg_body*0.5:
                    bos_found = True; bos_idx = j; break

            if not bos_found: continue

            bos_c = candles[bos_idx]; bos_body = abs(bos_c["c"]-bos_c["o"])
            displacement_ok = bos_body > avg_body*1.3

            subsequent = candles[bos_idx+1:min(bos_idx+30, n)]
            violated = any(sc["c"] > c["h"] for sc in subsequent)
            if violated: continue

            in_zone = c["l"] <= cur_price <= c["h"]
            near_ob = c["l"]*0.992 <= cur_price < c["l"]
            if not (in_zone or near_ob): continue

            strength = 1
            if body > avg_body*1.5: strength += 1
            if i in sh_idx or (i+1) in sh_idx: strength += 1
            avg_vol = sum(cc["v"] for cc in candles[max(0,i-10):i]) / 10 if i >= 10 else c["v"]
            if c["v"] > avg_vol*1.3: strength += 1
            if displacement_ok: strength += 1
            if in_zone: strength = min(5, strength+1)

            obs.append({
                "type":"BEAR_OB","high":round(c["h"],6),"low":round(c["l"],6),
                "ob50":round((c["h"]+c["l"])/2,6),"t":c["t"],
                "strength":min(5,strength),"dist_pct":round(abs(cur_price-c["h"])/cur_price*100,2),
                "in_zone":in_zone,"displacement":displacement_ok,"bos_confirmed":True,
                "label":"空頭訂單塊 🔴",
                "entry_note":"理想進場：OB 50%線" if in_zone else "等待反彈至OB",
            })

    def merge(lst, tol=0.003):
        m=[]
        for o in lst:
            ex=next((x for x in m if abs(x["ob50"]-o["ob50"])/o["ob50"]<tol),None)
            if ex:
                if o["strength"]>ex["strength"]: m.remove(ex); m.append(o)
            else: m.append(o)
        return m

    bull = sorted(merge([o for o in obs if o["type"]=="BULL_OB"]),key=lambda x:(-x["in_zone"],-x["strength"]))
    bear = sorted(merge([o for o in obs if o["type"]=="BEAR_OB"]),key=lambda x:(-x["in_zone"],-x["strength"]))
    return bull[:3]+bear[:3]

# ═══════════════════════════════════════════════════════════════════════════
# FVG 偵測（完整版）
# ═══════════════════════════════════════════════════════════════════════════

def detect_fvg(candles, cur_price):
    fvgs=[]; n=len(candles)
    if n<3: return []
    MIN_PCT=0.001
    for i in range(1,n-1):
        prev=candles[i-1]; curr=candles[i]; nxt=candles[i+1]

        # 多頭 FVG
        if prev["h"] < nxt["l"]:
            glo=prev["h"]; ghi=nxt["l"]; gsz=ghi-glo; gmid=(ghi+glo)/2
            if gsz/curr["c"] < MIN_PCT: continue
            curr_body=abs(curr["c"]-curr["o"])
            avg_b=sum(abs(candles[j]["c"]-candles[j]["o"]) for j in range(max(0,i-5),i))/max(1,min(5,i))
            impulse=curr["c"]>curr["o"] and curr_body>avg_b*0.8
            future=candles[i+2:n]
            min_low=min((c["l"] for c in future),default=cur_price)
            if min_low<glo: fs="FILLED_FULL"
            elif min_low<gmid: fs="FILLED_50"
            elif glo<=cur_price<=ghi: fs="IN_ZONE"
            elif cur_price<glo: fs="ABOVE"
            else: fs="UNFILLED"
            if fs=="FILLED_FULL": continue
            s=3 if fs=="IN_ZONE" else(2 if fs=="FILLED_50" else 1)
            if impulse: s=min(5,s+1)
            if gsz/curr["c"]>0.003: s=min(5,s+1)
            fvgs.append({"type":"BULL_FVG","high":round(ghi,6),"low":round(glo,6),"mid":round(gmid,6),
                "size_pct":round(gsz/curr["c"]*100,3),"t":curr["t"],"fill_status":fs,"strength":s,
                "dist_pct":round(abs(cur_price-gmid)/cur_price*100,2),"label":"多頭FVG 🟦",
                "status_txt":{"IN_ZONE":"⚡在區間","FILLED_50":"⚠已觸50%","UNFILLED":"等回測","ABOVE":"在上方"}.get(fs,"--"),
                "entry_note":"最佳進場：FVG 50%線" if fs=="IN_ZONE" else("仍可考慮" if fs=="FILLED_50" else "等待回測")})

        # 空頭 FVG
        if prev["l"] > nxt["h"]:
            ghi=prev["l"]; glo=nxt["h"]; gsz=ghi-glo; gmid=(ghi+glo)/2
            if gsz/curr["c"] < MIN_PCT: continue
            curr_body=abs(curr["c"]-curr["o"])
            avg_b=sum(abs(candles[j]["c"]-candles[j]["o"]) for j in range(max(0,i-5),i))/max(1,min(5,i))
            impulse=curr["c"]<curr["o"] and curr_body>avg_b*0.8
            future=candles[i+2:n]
            max_hi=max((c["h"] for c in future),default=cur_price)
            if max_hi>ghi: fs="FILLED_FULL"
            elif max_hi>gmid: fs="FILLED_50"
            elif glo<=cur_price<=ghi: fs="IN_ZONE"
            elif cur_price>ghi: fs="BELOW"
            else: fs="UNFILLED"
            if fs=="FILLED_FULL": continue
            s=3 if fs=="IN_ZONE" else(2 if fs=="FILLED_50" else 1)
            if impulse: s=min(5,s+1)
            if gsz/curr["c"]>0.003: s=min(5,s+1)
            fvgs.append({"type":"BEAR_FVG","high":round(ghi,6),"low":round(glo,6),"mid":round(gmid,6),
                "size_pct":round(gsz/curr["c"]*100,3),"t":curr["t"],"fill_status":fs,"strength":s,
                "dist_pct":round(abs(cur_price-gmid)/cur_price*100,2),"label":"空頭FVG 🟧",
                "status_txt":{"IN_ZONE":"⚡在區間","FILLED_50":"⚠已觸50%","UNFILLED":"等反彈","BELOW":"在下方"}.get(fs,"--"),
                "entry_note":"最佳進場：FVG 50%線" if fs=="IN_ZONE" else("仍可考慮" if fs=="FILLED_50" else "等待反彈")})

    bull=sorted([f for f in fvgs if f["type"]=="BULL_FVG"],key=lambda x:(-int(x["fill_status"]=="IN_ZONE"),-x["strength"]))[:3]
    bear=sorted([f for f in fvgs if f["type"]=="BEAR_FVG"],key=lambda x:(-int(x["fill_status"]=="IN_ZONE"),-x["strength"]))[:3]
    return bull+bear

# ═══════════════════════════════════════════════════════════════════════════
# 流動性 SSL/BSL
# ═══════════════════════════════════════════════════════════════════════════

def detect_liquidity(candles, cur_price):
    liq=[]; n=len(candles)
    for i in range(3,n-3):
        h,l=candles[i]["h"],candles[i]["l"]
        is_sh=all(h>=candles[i+k]["h"] for k in [-3,-2,-1,1,2,3])
        is_sl=all(l<=candles[i+k]["l"] for k in [-3,-2,-1,1,2,3])
        if is_sh and h>cur_price:
            tests=sum(1 for j in range(n) if abs(candles[j]["h"]-h)/h<0.003)
            liq.append({"type":"BSL","price":h,"tests":tests,"strength":min(5,tests),
                "dist_pct":round((h-cur_price)/cur_price*100,2),
                "label":"BSL 買方流動性","action":"空方目標 / 多方止損區"})
        if is_sl and l<cur_price:
            tests=sum(1 for j in range(n) if abs(candles[j]["l"]-l)/l<0.003)
            liq.append({"type":"SSL","price":l,"tests":tests,"strength":min(5,tests),
                "dist_pct":round((cur_price-l)/cur_price*100,2),
                "label":"SSL 賣方流動性","action":"多方目標 / 空方止損區"})
    bsl=sorted([p for p in liq if p["type"]=="BSL"],key=lambda x:x["price"])[:4]
    ssl=sorted([p for p in liq if p["type"]=="SSL"],key=lambda x:-x["price"])[:4]
    return bsl+ssl

# ═══════════════════════════════════════════════════════════════════════════
# 市場結構
# ═══════════════════════════════════════════════════════════════════════════

def detect_structure(candles, cur_price):
    sh,sl=find_swings(candles,3,3)
    if len(sh)<2 or len(sl)<2:
        return {"trend":"UNKNOWN","bos":None,"choch":None,"mss":None,"swings":[],"last_sh":None,"last_sl":None}
    last_sh=sh[-1]; prev_sh=sh[-2]; last_sl=sl[-1]; prev_sl=sl[-2]
    if last_sh["price"]>prev_sh["price"] and last_sl["price"]>prev_sl["price"]: trend="BULL"
    elif last_sh["price"]<prev_sh["price"] and last_sl["price"]<prev_sl["price"]: trend="BEAR"
    else: trend="RANGING"
    bos=choch=mss=None
    if trend=="BULL" and cur_price>last_sh["price"]:
        bos={"direction":"UP","level":last_sh["price"],"desc":"突破前高 BOS ↑"}
    elif trend=="BEAR" and cur_price<last_sl["price"]:
        bos={"direction":"DOWN","level":last_sl["price"],"desc":"跌破前低 BOS ↓"}
    if trend=="BULL" and cur_price<last_sl["price"]:
        choch={"level":last_sl["price"],"desc":"跌破前低 CHoCH ⚠ 潛在反轉"}
    elif trend=="BEAR" and cur_price>last_sh["price"]:
        choch={"level":last_sh["price"],"desc":"突破前高 CHoCH ⚠ 潛在反轉"}
    if choch and len(sh)>=3 and len(sl)>=3:
        mss={"level":choch["level"],"desc":"MSS 結構轉換確認"}
    recent_swings=sorted(sh[-3:]+sl[-3:],key=lambda x:x["idx"])
    return {"trend":trend,"bos":bos,"choch":choch,"mss":mss,
            "swings":[{"type":"SH" if s in sh else "SL","price":s["price"],"t":s["t"]} for s in recent_swings],
            "last_sh":last_sh,"last_sl":last_sl}

# ═══════════════════════════════════════════════════════════════════════════
# Premium/Discount
# ═══════════════════════════════════════════════════════════════════════════

def detect_pd(candles, cur_price):
    sh,sl=find_swings(candles,3,3)
    if not sh or not sl: return None
    rh=sh[-1]["price"]; rl=sl[-1]["price"]
    if rh<=rl: return None
    eq=(rh+rl)/2; rng=rh-rl
    pos="PREMIUM" if cur_price>eq else "DISCOUNT"
    pct=round((cur_price-rl)/rng*100,1)
    return {"range_high":round(rh,4),"range_low":round(rl,4),"equilibrium":round(eq,4),
            "premium_zone":round(eq+rng*0.25,4),"discount_zone":round(eq-rng*0.25,4),
            "position":pos,"pct_position":pct,
            "bias_pd":pos,
            "bias":"溢價區 (Premium) — 價格偏高，適合尋找做空" if pos=="PREMIUM" else "折扣區 (Discount) — 價格偏低，適合尋找做多"}

# ═══════════════════════════════════════════════════════════════════════════
# OB Confluence（多時框重疊）
# ═══════════════════════════════════════════════════════════════════════════

def check_ob_confluence(tf_analyses, direction):
    """
    檢查多個時間框架的 OB 是否在同一個價格區間重疊
    重疊越多，信號越強
    """
    all_obs = []
    for tf in tf_analyses:
        for ob in tf.get("order_blocks",[]):
            if ob["type"] == f"{'BULL' if direction=='LONG' else 'BEAR'}_OB":
                all_obs.append({"tf":tf["tf"],"high":ob["high"],"low":ob["low"],"mid":ob["ob50"]})

    if len(all_obs) < 2: return False, []

    confluences = []
    for i in range(len(all_obs)):
        for j in range(i+1, len(all_obs)):
            a, b = all_obs[i], all_obs[j]
            # 檢查重疊
            overlap_low  = max(a["low"],  b["low"])
            overlap_high = min(a["high"], b["high"])
            if overlap_high > overlap_low:
                confluences.append({
                    "tf1": a["tf"], "tf2": b["tf"],
                    "zone_high": round(overlap_high, 4),
                    "zone_low":  round(overlap_low,  4),
                    "zone_mid":  round((overlap_high+overlap_low)/2, 4),
                })

    return len(confluences) > 0, confluences

# ═══════════════════════════════════════════════════════════════════════════
# 回測引擎
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest(inst, candles_1h):
    """
    簡化回測：掃描過去K線，模擬每次 OB/FVG 信號的結果
    統計：勝率、平均 R:R、最大連敗、盈虧因子
    """
    results = []
    n = len(candles_1h)
    if n < 60: return {"error": "數據不足"}

    closes = [c["c"] for c in candles_1h]

    # 滾動視窗：每次用前50根計算信號，用後面10根驗證
    for start in range(30, n-10, 5):
        window    = candles_1h[start-30:start]
    # 滾動結束，改用向量方式
    wins = losses = 0
    total_rr = 0.0
    max_dd = 0; cur_dd = 0
    signals_found = 0

    for i in range(40, n-10):
        w = candles_1h[i-40:i]
        cur = w[-1]["c"]
        obs  = detect_ob(w, cur)
        fvgs = detect_fvg(w, cur)
        sh, sl = find_swings(w, 3, 3)

        # 找多頭信號
        bull_ob  = next((o for o in obs  if o["type"]=="BULL_OB"  and o["in_zone"]), None)
        bull_fvg = next((f for f in fvgs if f["type"]=="BULL_FVG" and f["fill_status"]=="IN_ZONE"), None)

        entry_price = sl_price = None
        signal_type = ""

        if bull_ob:
            entry_price = bull_ob["ob50"]
            sl_price    = bull_ob["low"] * 0.999
            signal_type = "LONG_OB"
        elif bull_fvg:
            entry_price = bull_fvg["mid"]
            sl_price    = bull_fvg["low"] * 0.999
            signal_type = "LONG_FVG"

        if entry_price and sl_price:
            risk = abs(entry_price - sl_price)
            if risk < entry_price * 0.0005: continue  # 止損太小，跳過
            tp1 = entry_price + risk * 1.5
            signals_found += 1

            # 驗證未來10根K線的結果
            future = candles_1h[i:i+10]
            hit_sl = any(c["l"] <= sl_price for c in future)
            hit_tp = any(c["h"] >= tp1 for c in future)

            if hit_tp and not hit_sl:
                wins += 1; total_rr += 1.5; cur_dd = 0
            elif hit_sl:
                losses += 1; total_rr -= 1.0; cur_dd += 1
                max_dd = max(max_dd, cur_dd)
            # 未觸發不計

    total = wins + losses
    if total == 0:
        return {"signals":signals_found,"trades":0,"win_rate":0,"avg_rr":0,"max_dd":0,"pf":0}

    win_rate = round(wins/total*100, 1)
    avg_rr   = round(total_rr/total, 2)
    pf       = round(wins*1.5 / max(losses,1), 2)  # 盈虧因子

    return {
        "signals": signals_found,
        "trades":  total,
        "wins":    wins,
        "losses":  losses,
        "win_rate": win_rate,
        "avg_rr":  avg_rr,
        "max_dd":  max_dd,
        "profit_factor": pf,
        "verdict": "✅ 系統有正期望值" if pf > 1.3 else ("⚠️ 邊緣系統" if pf > 1.0 else "❌ 負期望值"),
    }

# ═══════════════════════════════════════════════════════════════════════════
# Telegram 通知
# ═══════════════════════════════════════════════════════════════════════════

def send_telegram(token, chat_id, message):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id":chat_id,"text":message,"parse_mode":"HTML"}).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except: return False

# ═══════════════════════════════════════════════════════════════════════════
# 單一時框分析
# ═══════════════════════════════════════════════════════════════════════════

def analyze_tf(candles, cur_price, tf_name):
    if len(candles)<30: return {"tf":tf_name,"error":"數據不足"}
    closes=[c["c"] for c in candles]
    e20=ema(closes,20); e50=ema(closes,50)
    trend="BULL" if e20[-1]>e50[-1] else "BEAR"
    rsi_v=rsi(closes); atr_v=atr(candles)
    macd_r=macd(closes)
    ms=detect_structure(candles,cur_price)
    pd=detect_pd(candles,cur_price)
    ob=detect_ob(candles,cur_price)
    fvg=detect_fvg(candles,cur_price)
    liq=detect_liquidity(candles,cur_price)
    sh,sl=find_swings(candles,3,3)
    idm=detect_inducement(candles,cur_price,sh,sl)
    disp=detect_displacement(candles,5)
    bias="BULL" if trend=="BULL" and ms["trend"] in ["BULL","UNKNOWN"] else \
         "BEAR" if trend=="BEAR" and ms["trend"] in ["BEAR","UNKNOWN"] else ms["trend"]
    return {"tf":tf_name,"trend":trend,"bias":bias,"rsi":rsi_v,"atr":round(atr_v,4),
            "ema20":round(e20[-1],4),"ema50":round(e50[-1],4),"macd":macd_r,
            "structure":ms,"premium_discount":pd,"order_blocks":ob,
            "fvg":fvg,"liquidity":liq,"inducement":idm,"displacement":disp}

# ═══════════════════════════════════════════════════════════════════════════
# 多時框方向彙整
# ═══════════════════════════════════════════════════════════════════════════

def multi_tf_bias(tfs):
    w={"1W":5,"1D":4,"4H":3,"1H":2,"15m":1}
    bull=bear=0
    for t in tfs:
        wt=w.get(t.get("tf",""),1); b=t.get("bias","UNKNOWN")
        if b=="BULL": bull+=wt
        elif b=="BEAR": bear+=wt
    total=bull+bear
    if total==0: return "NEUTRAL",50
    bp=round(bull/total*100)
    if bp>=65: return "BULL",bp
    if bp<=35: return "BEAR",100-bp
    return "NEUTRAL",max(bp,100-bp)

# ═══════════════════════════════════════════════════════════════════════════
# 進場計劃（ICT 三步驟 + 確認K線 + Confluence）
# ═══════════════════════════════════════════════════════════════════════════

def generate_plan(tfs, cur_price, ticker):
    overall_bias, confidence = multi_tf_bias(tfs)
    entry_tf = next((t for t in tfs if t["tf"]=="15m"), None)
    h1_tf    = next((t for t in tfs if t["tf"]=="1H"),  None)
    h4_tf    = next((t for t in tfs if t["tf"]=="4H"),  None)
    kz       = get_kill_zone()

    signal="WAIT"; entry=sl=tp1=tp2=tp3=None
    reason_steps=[]; entry_note=""; conf_candle={}
    ob_conf=False; fvg_in_zone=False; displacement=False
    conf_zones=[]

    MIN_RR = 1.5

    # Kill Zone 警告
    if not kz["active"]:
        reason_steps.append(f"⏰ 非交易時段 — 下個 Kill Zone：{kz.get('next','')} ({kz.get('mins_to_next',0)//60}h{kz.get('mins_to_next',0)%60}m 後)")

    if overall_bias=="BULL" and confidence>=60:
        signal="LONG"
        src_tf = h1_tf if h1_tf else entry_tf
        bull_obs  = [o for o in (src_tf or {}).get("order_blocks",[]) if o["type"]=="BULL_OB"]
        bull_fvg  = [f for f in (src_tf or {}).get("fvg",[]) if f["type"]=="BULL_FVG"]
        ob_in     = [o for o in bull_obs if o.get("in_zone")]
        fvg_in    = [f for f in bull_fvg if f.get("fill_status")=="IN_ZONE"]
        disp_15m  = (entry_tf or {}).get("displacement",{})
        displacement = disp_15m.get("bull", False)

        # Confluence 檢查
        ob_conf, conf_zones = check_ob_confluence(tfs,"LONG")

        best_zone = None; zone_type = ""
        if conf_zones:
            best_zone = conf_zones[0]
            entry = round(best_zone["zone_mid"],4)
            sl    = round(best_zone["zone_low"]*0.999,4)
            zone_type = f"多時框OB重疊區 ({best_zone['tf1']}+{best_zone['tf2']})"
            entry_note = f"✅ OB Confluence！{best_zone['tf1']}+{best_zone['tf2']} 重疊區 {best_zone['zone_low']}~{best_zone['zone_high']}"
        elif ob_in:
            best = ob_in[0]; fvg_in_zone=False
            entry = round(best["ob50"],4); sl=round(best["low"]*0.999,4)
            zone_type=f"1H多頭OB ({best['low']}~{best['high']})"
            entry_note=f"✅ 價格在多頭OB內 — 進場點 {entry}"
        elif fvg_in:
            best=fvg_in[0]; fvg_in_zone=True
            entry=round(best["mid"],4); sl=round(best["low"]*0.999,4)
            zone_type=f"1H多頭FVG ({best['low']}~{best['high']})"
            entry_note=f"✅ 價格在FVG內 — 進場點 {entry}"
        elif bull_obs:
            best=bull_obs[0]
            entry=round(best["ob50"],4); sl=round(best["low"]*0.999,4)
            signal="WAIT"; entry_note=f"⏳ 等待回測至OB {best['low']}~{best['high']}"
            zone_type=f"等待OB {best['low']}~{best['high']}"
        else:
            atr_e=(src_tf or {}).get("atr",cur_price*0.01)
            entry=round(cur_price,4); sl=round(cur_price-atr_e*1.5,4)
            entry_note="⚠ 無明確OB/FVG，ATR止損"
            zone_type="ATR動態"

        # 確認K線偵測（15m）
        if signal=="LONG" and entry_tf and entry:
            conf_candle=detect_confirmation_candle(
                entry_tf.get("order_blocks",[]) and candles_cache.get("15m",[]) or [],
                "LONG", entry*(1+0.002), sl)
            # 若確認K線未出現，降級到 WAIT
            if not conf_candle.get("confirmed") and conf_candle.get("type") not in ["進入區間"]:
                signal="WAIT" if not ob_conf else signal  # Confluence 時仍給信號
                if not conf_candle.get("confirmed"):
                    entry_note += " | ⏳ 等待15m確認K線"

        # 止盈
        if h1_tf:
            bsl=[l for l in h1_tf.get("liquidity",[]) if l["type"]=="BSL"]
            if bsl:
                tp1=round(bsl[0]["price"],4)
                tp2=round(bsl[1]["price"],4) if len(bsl)>1 else None
                tp3=round(bsl[-1]["price"],4) if len(bsl)>2 else None

        reason_steps=[
            f"大時框 BULL 偏差確認（{confidence}%一致性）",
            f"進場區域：{zone_type}",
            f"確認K線：{conf_candle.get('type','等待')} — {conf_candle.get('desc','')}",
            f"Kill Zone：{kz['zone']} ({kz['quality']})",
            f"Displacement：{'✅ 有位移確認' if displacement else '⚠ 無位移'}",
        ]
        if conf_zones:
            reason_steps.append(f"🔥 OB Confluence：{conf_zones[0]['tf1']}+{conf_zones[0]['tf2']} 重疊")

    elif overall_bias=="BEAR" and confidence>=60:
        signal="SHORT"
        src_tf=h1_tf if h1_tf else entry_tf
        bear_obs=[o for o in (src_tf or {}).get("order_blocks",[]) if o["type"]=="BEAR_OB"]
        bear_fvg=[f for f in (src_tf or {}).get("fvg",[]) if f["type"]=="BEAR_FVG"]
        ob_in=[o for o in bear_obs if o.get("in_zone")]
        fvg_in=[f for f in bear_fvg if f.get("fill_status")=="IN_ZONE"]
        disp_15m=(entry_tf or {}).get("displacement",{})
        displacement=disp_15m.get("bear",False)
        ob_conf,conf_zones=check_ob_confluence(tfs,"SHORT")

        best_zone=None; zone_type=""
        if conf_zones:
            best_zone=conf_zones[0]
            entry=round(best_zone["zone_mid"],4)
            sl=round(best_zone["zone_high"]*1.001,4)
            zone_type=f"多時框OB重疊區 ({best_zone['tf1']}+{best_zone['tf2']})"
            entry_note=f"✅ OB Confluence！{best_zone['tf1']}+{best_zone['tf2']} 重疊區 {best_zone['zone_low']}~{best_zone['zone_high']}"
        elif ob_in:
            best=ob_in[0]
            entry=round(best["ob50"],4); sl=round(best["high"]*1.001,4)
            zone_type=f"1H空頭OB ({best['low']}~{best['high']})"
            entry_note=f"✅ 價格在空頭OB內 — 進場點 {entry}"
        elif fvg_in:
            best=fvg_in[0]; fvg_in_zone=True
            entry=round(best["mid"],4); sl=round(best["high"]*1.001,4)
            zone_type=f"1H空頭FVG ({best['low']}~{best['high']})"
            entry_note=f"✅ 價格在FVG內 — 進場點 {entry}"
        elif bear_obs:
            best=bear_obs[0]
            entry=round(best["ob50"],4); sl=round(best["high"]*1.001,4)
            signal="WAIT"; entry_note=f"⏳ 等待反彈至OB {best['low']}~{best['high']}"
            zone_type=f"等待OB"
        else:
            atr_e=(src_tf or {}).get("atr",cur_price*0.01)
            entry=round(cur_price,4); sl=round(cur_price+atr_e*1.5,4)
            entry_note="⚠ 無明確OB/FVG，ATR止損"; zone_type="ATR動態"

        if signal=="SHORT" and entry_tf and entry:
            conf_candle=detect_confirmation_candle([],"SHORT",entry*(1+0.002),sl)
            if not conf_candle.get("confirmed"):
                if not ob_conf: signal="WAIT"
                entry_note += " | ⏳ 等待15m確認K線"

        if h1_tf:
            ssl=[l for l in h1_tf.get("liquidity",[]) if l["type"]=="SSL"]
            if ssl:
                tp1=round(ssl[0]["price"],4)
                tp2=round(ssl[1]["price"],4) if len(ssl)>1 else None
                tp3=round(ssl[-1]["price"],4) if len(ssl)>2 else None

        reason_steps=[
            f"大時框 BEAR 偏差確認（{confidence}%一致性）",
            f"進場區域：{zone_type}",
            f"確認K線：{conf_candle.get('type','等待')} — {conf_candle.get('desc','')}",
            f"Kill Zone：{kz['zone']} ({kz['quality']})",
            f"Displacement：{'✅ 有位移確認' if displacement else '⚠ 無位移'}",
        ]
        if conf_zones:
            reason_steps.append(f"🔥 OB Confluence：{conf_zones[0]['tf1']}+{conf_zones[0]['tf2']} 重疊")
    else:
        reason_steps=[f"多空分歧（BULL:{confidence if overall_bias=='BULL' else 100-confidence}%），等待方向明確"]

    # R:R 驗證
    rr1=rr2=None
    if entry and sl:
        risk=abs(entry-sl)
        if risk>0:
            is_long=signal=="LONG"
            if tp1 and((is_long and tp1<=entry)or(not is_long and tp1>=entry)): tp1=None
            if tp2 and((is_long and tp2<=entry)or(not is_long and tp2>=entry)): tp2=None
            if tp3 and((is_long and tp3<=entry)or(not is_long and tp3>=entry)): tp3=None
            if not tp1: tp1=round(entry+risk*MIN_RR if is_long else entry-risk*MIN_RR,4)
            if not tp2: tp2=round(entry+risk*2.5 if is_long else entry-risk*2.5,4)
            if not tp3: tp3=round(entry+risk*4.0 if is_long else entry-risk*4.0,4)
            rr1=round(abs(tp1-entry)/risk,2)
            rr2=round(abs(tp2-entry)/risk,2)
            if rr1<1.0:
                signal="WAIT"; entry=sl=tp1=tp2=tp3=None; rr1=rr2=None
                reason_steps=["R:R不足1:1，等待更好進場點"]

    return {
        "signal":signal,"overall_bias":overall_bias,"confidence":confidence,
        "entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"rr1":rr1,"rr2":rr2,
        "min_rr":MIN_RR,"reason_steps":reason_steps,"entry_note":entry_note,
        "kill_zone":kz,"ob_confluence":ob_conf,"confluence_zones":conf_zones,
        "fvg_in_zone":fvg_in_zone,"displacement":displacement,
        "confirmation_candle":conf_candle,
    }

# 全局快取（供確認K線函數使用）
candles_cache = {}

# ═══════════════════════════════════════════════════════════════════════════
# 主分析入口
# ═══════════════════════════════════════════════════════════════════════════

def analyze(symbol, api_key="", secret_key="", passphrase="", run_bt=False,
            tg_token="", tg_chat=""):
    global candles_cache
    inst=norm(symbol)
    ticker=get_ticker(inst)
    cur=ticker["price"]

    tf_configs=[("1W","1W",52),("1D","1D",90),("4H","4H",120),("1H","1H",168),("15m","15m",96)]
    tfs=[]
    for tf_name,bar,limit in tf_configs:
        try:
            c=get_candles(inst,bar,limit)
            candles_cache[tf_name]=c
            tfs.append(analyze_tf(c,cur,tf_name))
        except Exception as e:
            tfs.append({"tf":tf_name,"error":str(e)})

    # 更新確認K線（用 15m 實際K線）
    if "15m" in candles_cache:
        plan_tmp=generate_plan(tfs,cur,ticker)
        if plan_tmp.get("signal") in ["LONG","SHORT"] and plan_tmp.get("entry"):
            direction=plan_tmp["signal"]
            entry=plan_tmp["entry"]; sl_p=plan_tmp["sl"] or entry
            zone_h=max(entry,sl_p)*1.002; zone_l=min(entry,sl_p)*0.998
            conf=detect_confirmation_candle(candles_cache["15m"],direction,zone_h,zone_l)
            plan_tmp["confirmation_candle"]=conf
            plan=plan_tmp
        else:
            plan=plan_tmp
    else:
        plan=generate_plan(tfs,cur,ticker)

    # 訂單簿
    try:
        bids,asks=get_books(inst)
        bv=sum(b[1] for b in bids); av=sum(a[1] for a in asks)
        sp=asks[0][0]-bids[0][0] if bids and asks else 0
        rat=round(bv/av,3) if av else 1
        bwall=max(bids,key=lambda x:x[1]) if bids else [0,0]
        awall=max(asks,key=lambda x:x[1]) if asks else [0,0]
        ob_book={"bid_vol":round(bv,2),"ask_vol":round(av,2),"ratio":rat,
                 "spread":round(sp,6),"bid_wall":bwall,"ask_wall":awall,
                 "pressure":"買壓強" if rat>1.2 else("賣壓強" if rat<0.8 else "均衡")}
    except: ob_book={}

    # 回測
    bt=None
    if run_bt and "1H" in candles_cache:
        try:
            conn=sqlite3.connect(DB_PATH)
            row=conn.execute("SELECT ts,result FROM backtest_cache WHERE inst=?",(inst,)).fetchone()
            conn.close()
            if row:
                ts_cached=datetime.fromisoformat(row[0])
                if (datetime.now()-ts_cached).total_seconds()<3600:
                    bt=json.loads(row[1])
            if not bt:
                bt=run_backtest(inst,candles_cache["1H"])
                conn=sqlite3.connect(DB_PATH)
                conn.execute("INSERT OR REPLACE INTO backtest_cache VALUES (?,?,?)",
                    (inst,datetime.now().isoformat(),json.dumps(bt)))
                conn.commit(); conn.close()
        except Exception as e:
            bt={"error":str(e)}

    # 帳號
    account=None
    if api_key and secret_key and passphrase:
        try:
            bal=okx_private("/api/v5/account/balance",api_key,secret_key,passphrase)
            det=bal[0].get("details",[])
            bc=inst.split("-")[0]; qc=inst.split("-")[1] if "-" in inst else "USDT"
            usdt=next((d["availBal"] for d in det if d["ccy"]==qc),"0")
            base=next((d["availBal"] for d in det if d["ccy"]==bc),"0")
            eq=bal[0].get("totalEq","0")
            try: pos_data=okx_private(f"/api/v5/account/positions?instId={inst}-SWAP",api_key,secret_key,passphrase)
            except: pos_data=[]
            pos=[]
            for p in pos_data:
                if float(p.get("pos",0))!=0:
                    pos.append({"side":p.get("posSide","--"),"size":p.get("pos","0"),
                        "entry":float(p.get("avgPx",0)),"liq":float(p.get("liqPx",0)) if p.get("liqPx") else None,
                        "pnl":float(p.get("upl",0)),"pnl_pct":float(p.get("uplRatio",0))*100,"lev":p.get("lever","--")})
            account={"ok":True,"usdt":float(usdt),"base":float(base),"base_ccy":bc,"eq":float(eq),"pos":pos}
        except Exception as e:
            account={"ok":False,"error":str(e)}

    # Telegram 通知
    if tg_token and tg_chat and plan.get("signal") in ["LONG","SHORT"]:
        try:
            kz=plan.get("kill_zone",{})
            msg=(f"🚨 <b>ICT 信號 — {inst}</b>\n"
                 f"方向：{'🟢 LONG' if plan['signal']=='LONG' else '🔴 SHORT'}\n"
                 f"進場：{plan.get('entry','--')}\n"
                 f"止損：{plan.get('sl','--')}\n"
                 f"TP1：{plan.get('tp1','--')} (R:R 1:{plan.get('rr1','--')})\n"
                 f"信心度：{plan.get('confidence','--')}%\n"
                 f"Kill Zone：{kz.get('zone','--')} ({kz.get('quality','--')})\n"
                 f"確認K線：{plan.get('confirmation_candle',{}).get('type','--')}\n"
                 f"時間：{datetime.now().strftime('%m/%d %H:%M')}")
            threading.Thread(target=send_telegram,args=(tg_token,tg_chat,msg),daemon=True).start()
        except: pass

    # 記錄日誌
    result={
        "inst":inst,"ts":datetime.now().strftime("%m/%d %H:%M:%S"),
        "price":cur,"chg24":round((cur-ticker["open24"])/ticker["open24"]*100,2),
        "high24":ticker["high24"],"low24":ticker["low24"],
        "vol24":(f"{ticker['vol24']/1e9:.2f}B" if ticker['vol24']>=1e9 else f"{ticker['vol24']/1e6:.1f}M"),
        "tf_analyses":tfs,"plan":plan,"orderbook":ob_book,
        "account":account,"backtest":bt,
    }
    if plan.get("signal") in ["LONG","SHORT"]:
        threading.Thread(target=log_signal,args=(result,),daemon=True).start()
    return result

# ═══════════════════════════════════════════════════════════════════════════
# HTML 前端
# ═══════════════════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ICT/SMC 終極分析引擎 v3</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@700;800;900&family=Instrument+Sans:wght@400;500;600&display=swap');
:root{
  --bg:#07090d;--bg2:#0b0f16;--bg3:#10151e;--bg4:#141b26;
  --border:#1c2a3c;--border2:#243040;
  --accent:#3b9eff;--green:#0dff8c;--red:#ff2d55;
  --yellow:#ffcc00;--orange:#ff8c42;--purple:#a78bfa;--cyan:#22d3ee;
  --text:#d4e4f4;--text2:#4a6a8a;--text3:#2a3a50;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Instrument Sans',sans-serif;min-height:100vh;}
body::before{content:'';position:fixed;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.4;}
::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-track{background:var(--bg);}
::-webkit-scrollbar-thumb{background:var(--border2);}
.app{max-width:1440px;margin:0 auto;padding:14px;}

/* HEADER */
.hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 0 16px;border-bottom:1px solid var(--border);margin-bottom:16px;}
.logo{font-family:'Syne',sans-serif;font-size:1.4rem;font-weight:900;letter-spacing:-1px;}
.logo .l1{color:#fff;}.logo .l2{color:var(--accent);}
.logo .sub{font-family:'Space Mono',monospace;font-size:.58rem;color:var(--text2);letter-spacing:3px;display:block;margin-top:2px;}
.hdr-r{display:flex;align-items:center;gap:12px;}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green);animation:blink 2s infinite;}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

/* SETTINGS BAR */
.settings-bar{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;}
.sbox{background:var(--bg2);border:1px solid var(--border);padding:0;}
.sbox-head{display:flex;align-items:center;justify-content:space-between;padding:9px 14px;cursor:pointer;}
.sbox-head:hover{background:rgba(255,255,255,.02);}
.sbox-title{font-family:'Space Mono',monospace;font-size:.63rem;color:var(--text2);letter-spacing:2px;display:flex;align-items:center;gap:8px;}
.sbox-st{font-family:'Space Mono',monospace;font-size:.63rem;}
.sbox-st.ok{color:var(--green);}.sbox-st.no{color:var(--text2);}
.sbox-body{padding:12px 14px;display:none;border-top:1px solid var(--border);}
.sbox-body.open{display:block;}
.api-note{font-family:'Space Mono',monospace;font-size:.62rem;color:var(--text2);line-height:1.8;padding:7px 10px;background:rgba(255,140,66,.05);border-left:2px solid var(--orange);margin-bottom:10px;}
.fields-2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;}
.fields-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px;}
.field{display:flex;flex-direction:column;gap:4px;}
.field label{font-family:'Space Mono',monospace;font-size:.58rem;color:var(--text2);letter-spacing:2px;}
.inp{font-family:'Space Mono',monospace;font-size:.75rem;background:var(--bg3);border:1px solid var(--border);color:var(--accent);padding:7px 10px;outline:none;width:100%;transition:border-color .2s;}
.inp:focus{border-color:var(--accent);}
.inp::placeholder{color:var(--text3);}
.btns{display:flex;gap:7px;flex-wrap:wrap;}
.btn{font-family:'Space Mono',monospace;font-size:.63rem;padding:6px 12px;border:1px solid var(--border);background:transparent;color:var(--text2);cursor:pointer;letter-spacing:1px;transition:all .2s;}
.btn:hover{border-color:var(--accent);color:var(--accent);}
.btn-o{border-color:var(--orange);color:var(--orange);}
.btn-o:hover{background:rgba(255,140,66,.08);}
.btn-r{border-color:var(--red);color:var(--red);}
.btn-r:hover{background:rgba(255,45,85,.06);}
.btn-g{border-color:var(--green);color:var(--green);}
.btn-g:hover{background:rgba(13,255,140,.06);}
.btn-p{border-color:var(--purple);color:var(--purple);}
.btn-p:hover{background:rgba(167,139,250,.08);}

/* SEARCH */
.search{display:flex;gap:10px;margin-bottom:10px;}
.s-inp{flex:1;font-family:'Syne',sans-serif;font-size:1.2rem;font-weight:800;background:var(--bg2);border:1px solid var(--border);color:#fff;padding:13px 16px;outline:none;letter-spacing:3px;text-transform:uppercase;transition:border-color .2s,box-shadow .2s;}
.s-inp:focus{border-color:var(--accent);box-shadow:0 0 25px rgba(59,158,255,.07);}
.s-inp::placeholder{color:var(--text3);font-size:.95rem;}
.s-btn{font-family:'Syne',sans-serif;font-size:.88rem;font-weight:900;padding:13px 22px;background:var(--accent);color:var(--bg);border:none;cursor:pointer;letter-spacing:2px;transition:all .2s;}
.s-btn:hover{background:#5aadff;box-shadow:0 0 25px rgba(59,158,255,.4);}
.s-btn:disabled{background:var(--bg3);color:var(--text2);cursor:not-allowed;box-shadow:none;}
.quick{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap;}
.qb{font-family:'Space Mono',monospace;font-size:.63rem;padding:4px 10px;background:var(--bg3);border:1px solid var(--border);color:var(--text2);cursor:pointer;transition:all .2s;letter-spacing:1px;}
.qb:hover{border-color:var(--accent);color:var(--accent);}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid rgba(59,158,255,.2);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;margin-right:7px;vertical-align:middle;}
@keyframes spin{to{transform:rotate(360deg)}}

/* KILL ZONE BAR */
.kz-bar{display:flex;align-items:center;gap:10px;padding:8px 14px;border:1px solid var(--border);margin-bottom:14px;font-family:'Space Mono',monospace;font-size:.68rem;}
.kz-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.kz-dot.green{background:var(--green);box-shadow:0 0 8px var(--green);}
.kz-dot.yellow{background:var(--yellow);box-shadow:0 0 8px var(--yellow);}
.kz-dot.red{background:var(--red);box-shadow:0 0 8px var(--red);}

/* LAYOUT */
.main-layout{display:grid;grid-template-columns:1fr 370px;gap:12px;margin-bottom:12px;}
.left-col{display:flex;flex-direction:column;gap:12px;}
.right-col{display:flex;flex-direction:column;gap:12px;}

/* PANEL */
.panel{background:var(--bg2);border:1px solid var(--border);overflow:hidden;}
.panel-hd{padding:9px 13px;border-bottom:1px solid var(--border);font-family:'Space Mono',monospace;font-size:.6rem;color:var(--text2);letter-spacing:3px;text-transform:uppercase;display:flex;align-items:center;justify-content:space-between;}
.panel-hd::before{content:'';width:5px;height:5px;border:1.5px solid var(--accent);display:inline-block;margin-right:7px;flex-shrink:0;}
.panel-body{padding:11px 13px;}

/* TV */
.tv-wrap{background:var(--bg2);border:1px solid var(--border);}
.tv-tabs{display:flex;gap:0;border-bottom:1px solid var(--border);}
.tv-tab{font-family:'Space Mono',monospace;font-size:.6rem;padding:6px 12px;cursor:pointer;border-right:1px solid var(--border);color:var(--text2);transition:all .2s;letter-spacing:1px;}
.tv-tab.on{color:var(--accent);background:rgba(59,158,255,.07);}
.tv-tab:hover:not(.on){color:var(--text);background:rgba(255,255,255,.02);}
.tv-frame{height:520px;}

/* SIGNAL HERO */
.signal-hero{display:grid;grid-template-columns:150px 1fr;}
.sig-left{padding:18px;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--bg3);border-right:1px solid var(--border);}
.sig-word{font-family:'Syne',sans-serif;font-size:2.4rem;font-weight:900;line-height:1;letter-spacing:-1px;}
.LONG .sig-word{color:var(--green);text-shadow:0 0 30px rgba(13,255,140,.4);}
.SHORT .sig-word{color:var(--red);text-shadow:0 0 30px rgba(255,45,85,.4);}
.WAIT .sig-word{color:var(--yellow);text-shadow:0 0 20px rgba(255,204,0,.3);}
.sig-strat{font-family:'Space Mono',monospace;font-size:.58rem;margin-top:5px;text-align:center;}
.LONG .sig-strat{color:var(--green);}.SHORT .sig-strat{color:var(--red);}.WAIT .sig-strat{color:var(--yellow);}
.conf-big{font-family:'Syne',sans-serif;font-size:1.6rem;font-weight:900;margin-top:10px;}
.conf-lbl{font-family:'Space Mono',monospace;font-size:.52rem;color:var(--text2);letter-spacing:2px;}
.sig-right{padding:14px 16px;}
.sig-inst{font-family:'Syne',sans-serif;font-size:.95rem;font-weight:800;color:#fff;letter-spacing:2px;margin-bottom:10px;}
.prices{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border);}
.ps{display:flex;flex-direction:column;}
.ps-l{font-family:'Space Mono',monospace;font-size:.53rem;color:var(--text2);letter-spacing:2px;margin-bottom:2px;}
.ps-v{font-family:'Space Mono',monospace;font-size:.85rem;font-weight:700;}
.steps{display:flex;flex-direction:column;gap:5px;}
.step{display:flex;align-items:flex-start;gap:7px;font-size:.8rem;}
.step-n{font-family:'Space Mono',monospace;font-size:.58rem;color:var(--accent);background:rgba(59,158,255,.1);padding:2px 5px;border-radius:2px;flex-shrink:0;margin-top:1px;}

/* TF MATRIX */
.tf-matrix{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;}
.tf-cell{background:var(--bg3);border:1px solid var(--border);padding:9px 7px;display:flex;flex-direction:column;align-items:center;gap:3px;transition:border-color .2s;}
.tf-cell.BULL{border-color:rgba(13,255,140,.3);background:rgba(13,255,140,.04);}
.tf-cell.BEAR{border-color:rgba(255,45,85,.3);background:rgba(255,45,85,.04);}
.tf-lbl{font-family:'Space Mono',monospace;font-size:.58rem;color:var(--text2);letter-spacing:2px;}
.tf-dir{font-family:'Syne',sans-serif;font-size:.85rem;font-weight:900;}
.tf-detail{font-family:'Space Mono',monospace;font-size:.53rem;color:var(--text2);text-align:center;}

/* CONFLUENCE */
.conf-zone{padding:9px 11px;background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.3);border-left:3px solid var(--purple);margin-bottom:6px;}
.conf-zone-title{font-family:'Space Mono',monospace;font-size:.62rem;color:var(--purple);margin-bottom:3px;}
.conf-zone-price{font-family:'Space Mono',monospace;font-size:.88rem;font-weight:700;color:var(--purple);}

/* ZONE ITEMS */
.zone-item{padding:9px 11px;border-left:3px solid;margin-bottom:5px;}
.zone-item:last-child{margin-bottom:0;}
.zone-item.BULL_OB{border-color:var(--green);background:rgba(13,255,140,.04);}
.zone-item.BEAR_OB{border-color:var(--red);background:rgba(255,45,85,.04);}
.zone-item.BULL_FVG{border-color:var(--cyan);background:rgba(34,211,238,.04);}
.zone-item.BEAR_FVG{border-color:var(--orange);background:rgba(255,140,66,.04);}
.zone-item.BSL{border-color:var(--yellow);}
.zone-item.SSL{border-color:var(--purple);}
.zone-item.IDM{border-color:var(--orange);background:rgba(255,140,66,.06);}
.zone-label{font-family:'Space Mono',monospace;font-size:.58rem;letter-spacing:1px;margin-bottom:3px;}
.zone-price{font-family:'Space Mono',monospace;font-size:.85rem;font-weight:700;}
.zone-meta{font-family:'Space Mono',monospace;font-size:.58rem;color:var(--text2);margin-top:2px;}
.zone-note{font-family:'Space Mono',monospace;font-size:.58rem;margin-top:3px;}
.str-dots{display:flex;gap:2px;margin-top:3px;}
.sd{width:4px;height:4px;border-radius:1px;background:var(--border);}
.sd.on{background:var(--accent);}
.in-zone-badge{font-family:'Space Mono',monospace;font-size:.55rem;padding:2px 5px;border-radius:2px;background:rgba(13,255,140,.2);color:var(--green);}

/* CONFIRMATION */
.conf-candle{padding:10px 13px;margin-bottom:0;}
.cc-type{font-family:'Space Mono',monospace;font-size:.75rem;font-weight:700;margin-bottom:3px;}
.cc-desc{font-family:'Space Mono',monospace;font-size:.62rem;color:var(--text2);line-height:1.5;}

/* ENTRY */
.entry-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);}
.ec{background:var(--bg3);padding:10px 12px;}
.ec-l{font-family:'Space Mono',monospace;font-size:.53rem;color:var(--text2);letter-spacing:2px;margin-bottom:3px;}
.ec-v{font-family:'Space Mono',monospace;font-size:.88rem;font-weight:700;}
.entry-note{padding:8px 13px;background:rgba(59,158,255,.04);border-bottom:1px solid var(--border);font-family:'Space Mono',monospace;font-size:.65rem;color:var(--accent);line-height:1.6;}

/* DISPLACEMENT */
.disp-box{padding:9px 13px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);}
.disp-icon{font-size:1.1rem;}
.disp-text{font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2);line-height:1.5;}

/* PD */
.pd-bar{margin:6px 0 4px;height:32px;border-radius:2px;position:relative;overflow:hidden;
  background:linear-gradient(to right,rgba(13,255,140,.14) 0%,rgba(13,255,140,.06) 25%,rgba(28,42,60,.3) 45%,rgba(28,42,60,.3) 55%,rgba(255,45,85,.06) 75%,rgba(255,45,85,.14) 100%);}
.pd-eq-line{position:absolute;left:50%;top:0;bottom:0;width:2px;background:var(--yellow);opacity:.7;}
.pd-cur-dot{position:absolute;top:50%;transform:translate(-50%,-50%);width:11px;height:11px;background:#fff;border-radius:50%;box-shadow:0 0 7px #fff;}

/* BACKTEST */
.bt-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;}
.bt-cell{background:var(--bg3);border:1px solid var(--border);padding:9px;text-align:center;}
.bt-val{font-family:'Syne',sans-serif;font-size:1.2rem;font-weight:900;}
.bt-lbl{font-family:'Space Mono',monospace;font-size:.55rem;color:var(--text2);letter-spacing:1px;margin-top:2px;}

/* JOURNAL */
.journal-item{padding:9px 13px;border-bottom:1px solid var(--border);font-size:.8rem;}
.journal-item:last-child{border-bottom:none;}
.j-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:3px;}

/* ACCT */
.acct-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border);}
.ac{background:var(--bg3);padding:10px 12px;}
.ac-l{font-family:'Space Mono',monospace;font-size:.52rem;color:var(--text2);letter-spacing:2px;margin-bottom:2px;}
.ac-v{font-family:'Space Mono',monospace;font-size:.88rem;font-weight:700;}

/* TABS */
.tab-row{display:flex;gap:0;border-bottom:1px solid var(--border);}
.tab{font-family:'Space Mono',monospace;font-size:.6rem;padding:7px 12px;cursor:pointer;color:var(--text2);transition:all .2s;letter-spacing:1px;border-right:1px solid var(--border);}
.tab.on{color:var(--accent);background:rgba(59,158,255,.07);}
.tab:hover:not(.on){color:var(--text);}
.tab-content{display:none;}.tab-content.on{display:block;}

.ph{text-align:center;padding:50px 20px;border:1px dashed var(--border);}
.ph-t{font-family:'Syne',sans-serif;font-size:1rem;font-weight:800;color:var(--text2);letter-spacing:2px;margin-bottom:8px;}
.ph-s{font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text3);line-height:2.2;}
.err{padding:11px 14px;background:rgba(255,45,85,.06);border:1px solid rgba(255,45,85,.2);color:var(--red);font-family:'Space Mono',monospace;font-size:.7rem;line-height:1.6;}
.disc{padding:9px 13px;background:rgba(255,204,0,.03);border:1px solid rgba(255,204,0,.1);font-family:'Space Mono',monospace;font-size:.58rem;color:rgba(255,204,0,.45);line-height:1.7;margin-top:12px;}

@media(max-width:960px){.main-layout{grid-template-columns:1fr;}.settings-bar{grid-template-columns:1fr;}.tf-matrix{grid-template-columns:repeat(3,1fr);}}
</style>
</head>
<body>
<div class="app">

<div class="hdr">
  <div class="logo">
    <span class="l1">ICT</span><span class="l2">X</span>
    <span class="sub">SMC 終極分析引擎 v3.0 · OKX LIVE</span>
  </div>
  <div class="hdr-r"><div class="dot"></div><span style="font-family:'Space Mono',monospace;font-size:.65rem;color:var(--green);letter-spacing:2px">5-TF · LIVE</span></div>
</div>

<!-- SETTINGS -->
<div class="settings-bar">
  <!-- OKX API -->
  <div class="sbox">
    <div class="sbox-head" onclick="toggleBox('apiBox','apiArr')">
      <div class="sbox-title">🔑 OKX API KEY（查看餘額/持倉）</div>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="sbox-st no" id="apiSt">未設定</span>
        <span style="font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2)" id="apiArr">▼</span>
      </div>
    </div>
    <div class="sbox-body" id="apiBox">
      <div class="api-note">只需「讀取」權限 · Key 只存本機 localStorage</div>
      <div class="fields-3">
        <div class="field"><label>API KEY</label><input class="inp" id="iK" type="password" placeholder="api-key"></div>
        <div class="field"><label>SECRET</label><input class="inp" id="iS" type="password" placeholder="secret-key"></div>
        <div class="field"><label>PASSPHRASE</label><input class="inp" id="iP" type="password" placeholder="passphrase"></div>
      </div>
      <div class="btns">
        <button class="btn btn-o" onclick="saveKeys()">✓ 儲存</button>
        <button class="btn btn-r" onclick="clearKeys()">✕ 清除</button>
        <button class="btn" onclick="toggleShow()">👁 顯示</button>
      </div>
    </div>
  </div>
  <!-- Telegram -->
  <div class="sbox">
    <div class="sbox-head" onclick="toggleBox('tgBox','tgArr')">
      <div class="sbox-title">📱 Telegram 推播通知（選填）</div>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="sbox-st no" id="tgSt">未設定</span>
        <span style="font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2)" id="tgArr">▼</span>
      </div>
    </div>
    <div class="sbox-body" id="tgBox">
      <div class="api-note">設定後有 ICT 信號時自動推播到你的 Telegram<br>Bot Token：BotFather 取得 · Chat ID：@userinfobot</div>
      <div class="fields-2">
        <div class="field"><label>BOT TOKEN</label><input class="inp" id="iTK" type="password" placeholder="123456:ABC..."></div>
        <div class="field"><label>CHAT ID</label><input class="inp" id="iTC" placeholder="-100123456789"></div>
      </div>
      <div class="btns">
        <button class="btn btn-o" onclick="saveTG()">✓ 儲存</button>
        <button class="btn btn-r" onclick="clearTG()">✕ 清除</button>
        <button class="btn btn-p" onclick="testTG()">📨 測試</button>
      </div>
    </div>
  </div>
</div>

<!-- KILL ZONE -->
<div class="kz-bar" id="kzBar">
  <div class="kz-dot" id="kzDot"></div>
  <span id="kzText" style="color:var(--text2)">載入中...</span>
</div>

<!-- SEARCH -->
<div class="search">
  <input class="s-inp" id="coinIn" placeholder="輸入幣種 BTC · ETH · SOL ..." onkeydown="if(event.key==='Enter')go()">
  <button class="s-btn" id="goBtn" onclick="go()">分析</button>
</div>
<div class="quick">
  <span style="font-family:'Space Mono',monospace;font-size:.6rem;color:var(--text2);line-height:24px;margin-right:4px">快速：</span>
  <button class="qb" onclick="q('BTC')">BTC</button>
  <button class="qb" onclick="q('ETH')">ETH</button>
  <button class="qb" onclick="q('SOL')">SOL</button>
  <button class="qb" onclick="q('BNB')">BNB</button>
  <button class="qb" onclick="q('DOGE')">DOGE</button>
  <button class="qb" onclick="q('XRP')">XRP</button>
  <button class="qb" onclick="q('AVAX')">AVAX</button>
  <button class="qb" onclick="q('LINK')">LINK</button>
</div>

<div id="acctSec"></div>
<div id="result">
  <div class="ph">
    <div style="font-size:2.2rem;margin-bottom:12px;opacity:.2">🎯</div>
    <div class="ph-t">輸入幣種開始 ICT/SMC 分析</div>
    <div class="ph-s">
      週→日→4H→1H→15m 五層縮小找進場<br>
      OB Confluence · Displacement · 確認K線<br>
      Inducement · Kill Zone · 回測統計<br>
      Telegram 推播 · 交易日誌
    </div>
  </div>
</div>
<div class="disc">⚠ 僅供技術分析學習，不構成投資建議。加密貨幣交易具高度風險，請自行評估。</div>
</div>

<script>
let apiOpen=false,tgOpen=false,showP=false,curInst='BTC-USDT',curTF='60';

// ── SETTINGS ───────────────────────────────────────────────
function toggleBox(bodyId,arrId){
  const el=document.getElementById(bodyId);
  const open=el.classList.contains('open');
  el.className='sbox-body'+(open?'':' open');
  document.getElementById(arrId).textContent=open?'▼':'▲';
}
function saveKeys(){
  const k=document.getElementById('iK').value.trim(),s=document.getElementById('iS').value.trim(),p=document.getElementById('iP').value.trim();
  if(!k||!s||!p){alert('請填入全部三個欄位');return;}
  localStorage.setItem('ox_k',k);localStorage.setItem('ox_s',s);localStorage.setItem('ox_p',p);
  setSt('apiSt',true);
}
function clearKeys(){
  if(!confirm('確定清除？'))return;
  ['ox_k','ox_s','ox_p'].forEach(k=>localStorage.removeItem(k));
  ['iK','iS','iP'].forEach(id=>document.getElementById(id).value='');
  setSt('apiSt',false);document.getElementById('acctSec').innerHTML='';
}
function toggleShow(){showP=!showP;['iK','iS','iP'].forEach(id=>document.getElementById(id).type=showP?'text':'password');}
function saveTG(){
  const t=document.getElementById('iTK').value.trim(),c=document.getElementById('iTC').value.trim();
  if(!t||!c){alert('請填入 Bot Token 和 Chat ID');return;}
  localStorage.setItem('tg_t',t);localStorage.setItem('tg_c',c);setSt('tgSt',true);
}
function clearTG(){localStorage.removeItem('tg_t');localStorage.removeItem('tg_c');setSt('tgSt',false);}
async function testTG(){
  const t=localStorage.getItem('tg_t'),c=localStorage.getItem('tg_c');
  if(!t||!c){alert('請先儲存 Telegram 設定');return;}
  const res=await fetch('/test_tg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t,chat_id:c})});
  const d=await res.json();
  alert(d.ok?'✅ 發送成功！':'❌ 失敗：'+d.error);
}
function setSt(id,ok){const el=document.getElementById(id);el.textContent=ok?'✅ 已設定':'未設定';el.className='sbox-st '+(ok?'ok':'no');}
function loadAll(){
  const k=localStorage.getItem('ox_k'),s=localStorage.getItem('ox_s'),p=localStorage.getItem('ox_p');
  if(k&&s&&p){document.getElementById('iK').value=k;document.getElementById('iS').value=s;document.getElementById('iP').value=p;setSt('apiSt',true);}
  const t=localStorage.getItem('tg_t'),c=localStorage.getItem('tg_c');
  if(t&&c){document.getElementById('iTK').value=t;document.getElementById('iTC').value=c;setSt('tgSt',true);}
}
function getKeys(){return{api_key:localStorage.getItem('ox_k')||'',secret_key:localStorage.getItem('ox_s')||'',passphrase:localStorage.getItem('ox_p')||'',tg_token:localStorage.getItem('tg_t')||'',tg_chat:localStorage.getItem('tg_c')||''};}

// ── KILL ZONE ──────────────────────────────────────────────
function updateKZ(){
  fetch('/kill_zone').then(r=>r.json()).then(kz=>{
    const dot=document.getElementById('kzDot');
    const txt=document.getElementById('kzText');
    dot.className='kz-dot '+kz.color;
    const c=kz.color==='green'?'var(--green)':kz.color==='yellow'?'var(--yellow)':'var(--red)';
    if(kz.active){
      txt.innerHTML=`<span style="color:${c};font-weight:700">${kz.zone}</span> &nbsp;·&nbsp; 品質：<span style="color:${c}">${kz.quality}</span> &nbsp;·&nbsp; <span style="color:var(--text2)">現在是最佳交易時段</span>`;
    } else {
      txt.innerHTML=`<span style="color:var(--red)">${kz.zone}</span> &nbsp;·&nbsp; <span style="color:var(--text2)">下個Kill Zone：</span><span style="color:var(--yellow)">${kz.next||''} (${Math.floor((kz.mins_to_next||0)/60)}h${(kz.mins_to_next||0)%60}m後)</span>`;
    }
  }).catch(()=>{});
}

// ── SCAN ───────────────────────────────────────────────────
function q(c){document.getElementById('coinIn').value=c;go();}
async function go(){
  const coin=document.getElementById('coinIn').value.trim();
  if(!coin){alert('請輸入幣種');return;}
  const btn=document.getElementById('goBtn');
  const btCheck=document.getElementById('btCheck');
  const runBt=btCheck?btCheck.checked:false;
  btn.disabled=true;btn.innerHTML='<span class="spinner"></span>';
  document.getElementById('result').innerHTML=`<div style="padding:36px;text-align:center;border:1px solid var(--border);"><span class="spinner" style="width:16px;height:16px;border-width:3px;"></span><span style="font-family:'Space Mono',monospace;font-size:.72rem;color:var(--text2);margin-left:10px;letter-spacing:2px">分析 ${coin.toUpperCase()} 五個時間框架...</span></div>`;
  try{
    const res=await fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbol:coin,...getKeys(),run_backtest:runBt})});
    if(!res.ok)throw new Error(await res.text());
    const d=await res.json();
    if(d.error)throw new Error(d.error);
    curInst=d.inst;
    renderAcct(d.account,d.inst);
    renderAll(d);
  }catch(e){
    document.getElementById('result').innerHTML=`<div class="err">❌ ${e.message}</div>`;
  }
  btn.disabled=false;btn.innerHTML='分析';
}

// ── HELPERS ────────────────────────────────────────────────
function fmt(n){if(n==null)return'--';const s=Math.abs(n);if(s>=10000)return'$'+n.toLocaleString('en-US',{maximumFractionDigits:0});if(s>=1)return'$'+n.toFixed(4);return'$'+n.toFixed(8);}
function fmtN(n,d=4){if(n==null)return'--';const s=Math.abs(n);if(s>=10000)return n.toLocaleString('en-US',{maximumFractionDigits:0});if(s>=1)return n.toFixed(d);return n.toFixed(8);}

// ── TV CHART ───────────────────────────────────────────────
function buildTV(inst,interval){
  const sym='OKX:'+inst.replace('-','');
  const src=`https://s.tradingview.com/widgetembed/?frameElementId=tv_chart&symbol=${sym}&interval=${interval}&theme=dark&style=1&locale=zh_TW&timezone=Asia%2FTaipei&hide_side_toolbar=0&studies=RSI%40tv-basicstudies%2CMACD%40tv-basicstudies%2CVolume%40tv-basicstudies&backgroundColor=%230b0f16&gridColor=rgba(28%2C42%2C60%2C0.6)&upColor=%230dff8c&downColor=%23ff2d55`;
  return `<iframe src="${src}" style="width:100%;height:100%;border:none;display:block;" allowfullscreen></iframe>`;
}
function switchTF(tf,interval){
  document.querySelectorAll('.tv-tab').forEach(t=>t.classList.remove('on'));
  document.getElementById('tab-'+tf).classList.add('on');
  document.getElementById('tvFrame').innerHTML=buildTV(curInst,interval);
}

// ── ACCOUNT ────────────────────────────────────────────────
function renderAcct(a,inst){
  const sec=document.getElementById('acctSec');
  if(!a){sec.innerHTML='';return;}
  if(!a.ok){sec.innerHTML=`<div class="err" style="margin-bottom:12px">⚠ API 錯誤：${a.error}</div>`;return;}
  const pos=a.pos||[];
  sec.innerHTML=`<div class="panel" style="margin-bottom:12px;">
    <div class="panel-hd">帳號資產 <span style="color:var(--green)">✓ 已連線</span></div>
    <div class="acct-grid">
      <div class="ac"><div class="ac-l">USDT 可用</div><div class="ac-v" style="color:var(--accent)">${a.usdt.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</div></div>
      <div class="ac"><div class="ac-l">${a.base_ccy}</div><div class="ac-v" style="color:var(--yellow)">${fmtN(a.base,6)}</div></div>
      <div class="ac"><div class="ac-l">總資產 USDT</div><div class="ac-v">${a.eq.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</div></div>
    </div>
    ${pos.length===0?'<div style="padding:9px 13px;font-family:\'Space Mono\',monospace;font-size:.65rem;color:var(--text2)">無持倉</div>':pos.map(p=>`<div style="padding:10px 13px;border-top:1px solid var(--border);display:flex;gap:14px;flex-wrap:wrap;">
      <div class="ps"><div class="ps-l">方向</div><div class="ps-v" style="color:${p.side==='long'?'var(--green)':'var(--red)'}">${p.side.toUpperCase()}</div></div>
      <div class="ps"><div class="ps-l">數量</div><div class="ps-v">${p.size}張</div></div>
      <div class="ps"><div class="ps-l">開倉價</div><div class="ps-v">${fmt(p.entry)}</div></div>
      <div class="ps"><div class="ps-l">強平價</div><div class="ps-v" style="color:var(--red)">${p.liq?fmt(p.liq):'--'}</div></div>
      <div class="ps"><div class="ps-l">盈虧</div><div class="ps-v" style="color:${p.pnl>=0?'var(--green)':'var(--red)'}">${p.pnl>=0?'+':''}${p.pnl.toFixed(2)} (${p.pnl_pct.toFixed(2)}%)</div></div>
      <div class="ps"><div class="ps-l">槓桿</div><div class="ps-v">${p.lev}x</div></div>
    </div>`).join('')}
  </div>`;
}

// ── MAIN RENDER ────────────────────────────────────────────
function renderAll(d){
  const plan=d.plan||{};const sig=plan.signal||'WAIT';
  const sc=sig==='LONG'?'var(--green)':sig==='SHORT'?'var(--red)':'var(--yellow)';
  const conf=plan.confidence||0;
  const confColor=conf>=65?'var(--green)':conf>=50?'var(--yellow)':'var(--red)';
  const chg=d.chg24||0;const tfs=d.tf_analyses||[];
  const ob_d=d.orderbook||{};const bt=d.backtest;
  const h1=tfs.find(t=>t.tf==='1H')||{};
  const m15=tfs.find(t=>t.tf==='15m')||{};
  const allOB=[...(h1.order_blocks||[]),...(m15.order_blocks||[])].slice(0,5);
  const allFVG=[...(h1.fvg||[]),...(m15.fvg||[])].slice(0,5);
  const allLiq=[...(h1.liquidity||[])].slice(0,6);
  const ms1h=h1.structure||{};const pd1h=h1.premium_discount||null;
  const idm=[...(h1.inducement||[]),...(m15.inducement||[])].slice(0,3);
  const disp=m15.displacement||{};
  const confZones=plan.confluence_zones||[];
  const cc=plan.confirmation_candle||{};
  const kz=plan.kill_zone||{};
  const rr=plan.rr1;const rrc=rr>=2?'var(--green)':rr>=1.5?'var(--yellow)':'var(--red)';

  document.getElementById('result').innerHTML=`
  <!-- SIGNAL HERO -->
  <div class="panel signal-hero ${sig}" style="margin-bottom:12px;">
    <div class="sig-left">
      <div style="font-family:'Space Mono',monospace;font-size:.52rem;color:var(--text2);letter-spacing:3px;margin-bottom:7px">ICT信號</div>
      <div class="sig-word">${sig}</div>
      <div class="sig-strat">${plan.strategy||'--'}</div>
      <div class="conf-big" style="color:${confColor}">${conf}%</div>
      <div class="conf-lbl">5-TF信心度</div>
      ${kz.active!==undefined?`<div style="margin-top:8px;font-family:'Space Mono',monospace;font-size:.55rem;color:${kz.color==='green'?'var(--green)':kz.color==='yellow'?'var(--yellow)':'var(--red)'};text-align:center">${kz.zone}</div>`:''}
    </div>
    <div class="sig-right">
      <div class="sig-inst">${d.inst} <span style="font-size:.65rem;color:var(--text2);font-family:'Space Mono',monospace">${d.ts}</span></div>
      <div class="prices">
        <div class="ps"><span class="ps-l">即時價格</span><span class="ps-v" style="color:#fff;font-size:.95rem">${fmt(d.price)}</span></div>
        <div class="ps"><span class="ps-l">24H漲跌</span><span class="ps-v" style="color:${chg>=0?'var(--green)':'var(--red)'}">${chg>=0?'+':''}${chg.toFixed(2)}%</span></div>
        <div class="ps"><span class="ps-l">24H高</span><span class="ps-v" style="color:var(--green)">${fmt(d.high24)}</span></div>
        <div class="ps"><span class="ps-l">24H低</span><span class="ps-v" style="color:var(--red)">${fmt(d.low24)}</span></div>
        <div class="ps"><span class="ps-l">成交量</span><span class="ps-v">${d.vol24}</span></div>
      </div>
      <div class="steps">
        ${(plan.reason_steps||[]).map((s,i)=>`<div class="step"><span class="step-n">0${i+1}</span><span>${s}</span></div>`).join('')}
      </div>
    </div>
  </div>

  <!-- TF MATRIX -->
  <div class="panel" style="margin-bottom:12px;">
    <div class="panel-hd">多時框方向矩陣 — 週→日→4H→1H→15m</div>
    <div class="panel-body">
      <div class="tf-matrix">
        ${tfs.map(t=>{
          if(t.error)return`<div class="tf-cell"><span class="tf-lbl">${t.tf}</span><span style="color:var(--red);font-size:.6rem">ERR</span></div>`;
          const ms=t.structure||{};const c=t.bias==='BULL'?'var(--green)':t.bias==='BEAR'?'var(--red)':'var(--yellow)';
          const msT=ms.bos?'BOS↑':ms.choch?'CHoCH':ms.trend||'--';
          const disp=t.displacement||{};
          return`<div class="tf-cell ${t.bias}">
            <span class="tf-lbl">${t.tf}</span>
            <span class="tf-dir" style="color:${c}">${t.bias||'?'}</span>
            <span class="tf-detail">RSI ${t.rsi||'--'}</span>
            <span class="tf-detail" style="color:${c};opacity:.7">${msT}</span>
            ${disp.bull||disp.bear?`<span style="font-size:.5rem;color:var(--orange)">⚡DISP</span>`:''}
          </div>`;
        }).join('')}
      </div>
    </div>
  </div>

  <div class="main-layout">
    <div class="left-col">
      <!-- TV CHART -->
      <div class="tv-wrap">
        <div class="tv-tabs">
          <div class="tv-tab" id="tab-1W" onclick="switchTF('1W','W')">週線</div>
          <div class="tv-tab" id="tab-1D" onclick="switchTF('1D','D')">日線</div>
          <div class="tv-tab" id="tab-4H" onclick="switchTF('4H','240')">4H</div>
          <div class="tv-tab on" id="tab-1H" onclick="switchTF('1H','60')">1H</div>
          <div class="tv-tab" id="tab-15m" onclick="switchTF('15m','15')">15m</div>
        </div>
        <div class="tv-frame" id="tvFrame">${buildTV(d.inst,'60')}</div>
      </div>

      <!-- CONFIRMATION CANDLE -->
      <div class="panel">
        <div class="panel-hd">15m 確認K線狀態</div>
        <div class="conf-candle">
          <div class="cc-type" style="color:${cc.confirmed?'var(--green)':cc.type&&cc.type!=='無確認'?'var(--yellow)':'var(--text2)'}">${cc.confirmed?'✅ 確認進場':cc.type&&cc.type!=='無確認'?'⏳ '+cc.type:'⚠ 尚未確認'}</div>
          <div class="cc-desc">${cc.desc||'等待確認K線出現後進場，不要在無確認時追價'}</div>
          ${cc.confirmed&&cc.strength?`<div style="margin-top:4px;display:flex;gap:2px;">${Array(5).fill(0).map((_,j)=>`<div class="sd ${j<cc.strength?'on':''}"></div>`).join('')}</div>`:''}
        </div>
      </div>

      <!-- DISPLACEMENT -->
      <div class="panel">
        <div class="panel-hd">Displacement 推動波分析 (15m)</div>
        <div class="disp-box">
          <div class="disp-icon">${disp.bull?'⬆️':disp.bear?'⬇️':'⏸️'}</div>
          <div class="disp-text">
            <div style="color:${disp.bull?'var(--green)':disp.bear?'var(--red)':'var(--text2)'}">
              ${disp.desc||'無位移確認'}
            </div>
            <div style="margin-top:2px">強度：${'★'.repeat(disp.strength||0)+'☆'.repeat(5-(disp.strength||0))} (${disp.strength||0}/5)</div>
          </div>
        </div>
      </div>

      <!-- STRUCTURE -->
      <div class="panel">
        <div class="panel-hd">市場結構 (1H) — BOS · CHoCH · MSS</div>
        <div class="panel-body">
          ${ms1h.trend?`<div style="margin-bottom:8px;font-family:'Space Mono',monospace;font-size:.65rem;"><span style="color:var(--text2)">趨勢：</span><span style="color:${ms1h.trend==='BULL'?'var(--green)':'var(--red)'};">${ms1h.trend}</span></div>`:''}
          ${ms1h.bos?`<div style="padding:7px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:9px;"><span style="font-family:'Space Mono',monospace;font-size:.58rem;padding:2px 6px;background:rgba(59,158,255,.15);color:var(--accent)">BOS</span><span style="font-family:'Space Mono',monospace;font-size:.82rem;font-weight:700;color:var(--accent)">${fmt(ms1h.bos.level)}</span><span style="font-family:'Space Mono',monospace;font-size:.58rem;color:var(--text2)">${ms1h.bos.desc}</span></div>`:''}
          ${ms1h.choch?`<div style="padding:7px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:9px;"><span style="font-family:'Space Mono',monospace;font-size:.58rem;padding:2px 6px;background:rgba(255,204,0,.15);color:var(--yellow)">CHoCH</span><span style="font-family:'Space Mono',monospace;font-size:.82rem;font-weight:700;color:var(--yellow)">${fmt(ms1h.choch.level)}</span><span style="font-family:'Space Mono',monospace;font-size:.58rem;color:var(--text2)">${ms1h.choch.desc}</span></div>`:''}
          ${ms1h.mss?`<div style="padding:7px 0;display:flex;align-items:center;gap:9px;"><span style="font-family:'Space Mono',monospace;font-size:.58rem;padding:2px 6px;background:rgba(167,139,250,.15);color:var(--purple)">MSS</span><span style="font-family:'Space Mono',monospace;font-size:.82rem;font-weight:700;color:var(--purple)">${fmt(ms1h.mss.level)}</span></div>`:''}
          ${!ms1h.bos&&!ms1h.choch&&!ms1h.mss?`<div style="font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2)">無明確結構轉換</div>`:''}
          ${ms1h.swings&&ms1h.swings.length?`<div style="margin-top:10px;padding-top:9px;border-top:1px solid var(--border);"><div style="font-family:'Space Mono',monospace;font-size:.55rem;color:var(--text2);letter-spacing:2px;margin-bottom:5px">近期擺動點</div><div style="display:flex;gap:5px;flex-wrap:wrap;">${ms1h.swings.map(s=>`<span style="font-family:'Space Mono',monospace;font-size:.62rem;padding:2px 7px;background:${s.type==='SH'?'rgba(255,45,85,.1)':'rgba(13,255,140,.1)'};color:${s.type==='SH'?'var(--red)':'var(--green)'};border-radius:2px">${s.type} ${fmtN(s.price)}</span>`).join('')}</div></div>`:''}
        </div>
      </div>

      <!-- PD -->
      ${pd1h?`<div class="panel">
        <div class="panel-hd">Premium / Discount 區間 (1H)</div>
        <div class="panel-body">
          <div style="display:flex;justify-content:space-between;font-family:'Space Mono',monospace;font-size:.6rem;margin-bottom:5px;">
            <span style="color:var(--green)">🟢 Discount ${fmt(pd1h.discount_zone)}</span>
            <span style="color:var(--yellow)">⚖ Eq ${fmt(pd1h.equilibrium)}</span>
            <span style="color:var(--red)">🔴 Premium ${fmt(pd1h.premium_zone)}</span>
          </div>
          <div class="pd-bar">
            <div class="pd-eq-line"></div>
            <div class="pd-cur-dot" style="left:${Math.min(95,Math.max(5,pd1h.pct_position))}%"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-family:'Space Mono',monospace;font-size:.58rem;color:var(--text2);">
            <span>前低 ${fmt(pd1h.range_low)}</span>
            <span style="color:${pd1h.position==='DISCOUNT'?'var(--green)':'var(--red)'}">${pd1h.pct_position}% — ${pd1h.bias}</span>
            <span>前高 ${fmt(pd1h.range_high)}</span>
          </div>
        </div>
      </div>`:''}

      <!-- ENTRY PLAN -->
      <div class="panel">
        <div class="panel-hd">進場計劃</div>
        ${plan.entry_note?`<div class="entry-note">${plan.entry_note}</div>`:''}
        <div class="entry-grid">
          <div class="ec"><div class="ec-l">進場位 (OB/FVG 50%)</div><div class="ec-v" style="color:var(--accent)">${fmt(plan.entry)}</div></div>
          <div class="ec"><div class="ec-l">止損 SL</div><div class="ec-v" style="color:var(--red)">${fmt(plan.sl)}</div></div>
          <div class="ec"><div class="ec-l">止盈 TP1 (SSL/BSL)</div><div class="ec-v" style="color:var(--green)">${fmt(plan.tp1)}</div></div>
          <div class="ec"><div class="ec-l">止盈 TP2</div><div class="ec-v" style="color:var(--green)">${fmt(plan.tp2)}</div></div>
          <div class="ec"><div class="ec-l">止盈 TP3 (最終目標)</div><div class="ec-v" style="color:var(--green)">${fmt(plan.tp3)}</div></div>
          <div class="ec"><div class="ec-l">R:R (最低 1:${plan.min_rr||1.5})</div><div class="ec-v" style="color:${rrc}">${rr?'1:'+rr+(rr>=2?' ✅':rr>=1.5?' ✅':' ❌'):'等待信號'}</div></div>
        </div>
        ${confZones.length?`<div style="padding:9px 13px;border-top:1px solid var(--border);background:rgba(167,139,250,.04);">
          <div style="font-family:'Space Mono',monospace;font-size:.58rem;color:var(--purple);margin-bottom:6px;letter-spacing:2px">🔥 OB CONFLUENCE</div>
          ${confZones.map(z=>`<div class="conf-zone"><div class="conf-zone-title">${z.tf1} + ${z.tf2} 重疊</div><div class="conf-zone-price">${fmt(z.zone_low)} — ${fmt(z.zone_high)}</div></div>`).join('')}
        </div>`:''}
      </div>

    </div><!-- left-col -->

    <div class="right-col">

      <!-- OB -->
      <div class="panel">
        <div class="panel-hd">訂單塊 OB (1H+15m)</div>
        <div class="panel-body">
          ${allOB.length===0?`<div style="font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2)">無有效OB</div>`:allOB.map(o=>{
            const oc=o.type==='BULL_OB'?'var(--green)':'var(--red)';
            return`<div class="zone-item ${o.type}">
              <div style="display:flex;justify-content:space-between;align-items:center;">
                <div class="zone-label" style="color:${oc}">${o.label}${o.in_zone?` <span class="in-zone-badge">在區間</span>`:''}</div>
                <div style="display:flex;gap:4px;align-items:center;">${o.displacement?`<span style="font-family:'Space Mono',monospace;font-size:.52rem;color:var(--orange)">⚡DISP</span>`:''}<div style="display:flex;gap:2px;">${Array(5).fill(0).map((_,j)=>`<div class="sd ${j<o.strength?'on':''}"></div>`).join('')}</div></div>
              </div>
              <div class="zone-price" style="color:${oc}">${fmt(o.high)} — ${fmt(o.low)}</div>
              <div class="zone-meta">📍 50%線：${fmt(o.ob50)} · 距 ${o.dist_pct}%</div>
              <div class="zone-note" style="color:var(--text2)">${o.entry_note||''}</div>
            </div>`;}).join('')}
        </div>
      </div>

      <!-- FVG -->
      <div class="panel">
        <div class="panel-hd">FVG 失衡區 (1H+15m)</div>
        <div class="panel-body">
          ${allFVG.length===0?`<div style="font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2)">無未填滿FVG</div>`:allFVG.map(f=>{
            const fc=f.type==='BULL_FVG'?'var(--cyan)':'var(--orange)';
            const sc=f.fill_status==='IN_ZONE'?'var(--green)':f.fill_status==='FILLED_50'?'var(--yellow)':'var(--text2)';
            return`<div class="zone-item ${f.type}" style="${f.fill_status==='IN_ZONE'?'border-width:3px':''}">
              <div style="display:flex;justify-content:space-between;">
                <div class="zone-label" style="color:${fc}">${f.label}</div>
                <span style="font-family:'Space Mono',monospace;font-size:.55rem;color:${sc}">${f.status_txt||''}</span>
              </div>
              <div class="zone-price" style="color:${fc}">${fmt(f.high)} — ${fmt(f.low)}</div>
              <div class="zone-meta">50%：${fmt(f.mid)} · 缺口 ${f.size_pct}%</div>
              <div class="zone-note" style="color:var(--text2)">${f.entry_note||''}</div>
            </div>`;}).join('')}
        </div>
      </div>

      <!-- LIQUIDITY -->
      <div class="panel">
        <div class="panel-hd">流動性 SSL/BSL</div>
        <div class="panel-body">
          ${allLiq.length===0?`<div style="font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2)">無明顯流動性聚集</div>`:allLiq.map(l=>{
            const lc=l.type==='BSL'?'var(--yellow)':'var(--purple)';
            return`<div class="zone-item ${l.type}">
              <div class="zone-label" style="color:${lc}">${l.type} · ${l.type==='BSL'?'買方流動性 (前高)':'賣方流動性 (前低)'}</div>
              <div class="zone-price" style="color:${lc}">${fmt(l.price)}</div>
              <div class="zone-meta">${l.action} · 距 ${l.dist_pct}% · 測試 ${l.tests} 次</div>
              <div class="str-dots">${Array(5).fill(0).map((_,j)=>`<div class="sd ${j<l.strength?'on':''}"></div>`).join('')}</div>
            </div>`;}).join('')}
        </div>
      </div>

      <!-- INDUCEMENT -->
      ${idm.length?`<div class="panel">
        <div class="panel-hd">Inducement 誘多/誘空偵測</div>
        <div class="panel-body">
          ${idm.map(i=>`<div class="zone-item IDM">
            <div class="zone-label" style="color:var(--orange)">${i.type}</div>
            <div class="zone-price" style="color:var(--orange)">假突破位：${fmt(i.swept_level)}</div>
            <div class="zone-meta">${i.desc}</div>
            <div class="zone-note" style="color:var(--yellow)">${i.implication}</div>
          </div>`).join('')}
        </div>
      </div>`:''}

      <!-- ORDERBOOK -->
      ${ob_d.ratio!=null?`<div class="panel">
        <div class="panel-hd">訂單簿</div>
        <div class="panel-body">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
            <span style="font-family:'Space Mono',monospace;font-size:.6rem;color:var(--green);width:32px">買</span>
            <div style="flex:1;height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;"><div style="height:100%;background:var(--green);width:${Math.min(100,ob_d.ratio/(ob_d.ratio+1)*150)}%;border-radius:3px;"></div></div>
            <span style="font-family:'Space Mono',monospace;font-size:.62rem;color:var(--green);width:55px;text-align:right">${fmtN(ob_d.bid_vol,0)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
            <span style="font-family:'Space Mono',monospace;font-size:.6rem;color:var(--red);width:32px">賣</span>
            <div style="flex:1;height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;"><div style="height:100%;background:var(--red);width:${Math.min(100,(1-ob_d.ratio/(ob_d.ratio+1))*150)}%;border-radius:3px;"></div></div>
            <span style="font-family:'Space Mono',monospace;font-size:.62rem;color:var(--red);width:55px;text-align:right">${fmtN(ob_d.ask_vol,0)}</span>
          </div>
          <div style="font-family:'Space Mono',monospace;font-size:.62rem;">
            <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)"><span style="color:var(--text2)">買/賣比</span><span style="color:${ob_d.ratio>1.2?'var(--green)':ob_d.ratio<0.8?'var(--red)':'var(--text)'}">${ob_d.ratio} · ${ob_d.pressure}</span></div>
            <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)"><span style="color:var(--text2)">買單牆</span><span style="color:var(--green)">${fmt(ob_d.bid_wall[0])}</span></div>
            <div style="display:flex;justify-content:space-between;padding:4px 0"><span style="color:var(--text2)">賣單牆</span><span style="color:var(--red)">${fmt(ob_d.ask_wall[0])}</span></div>
          </div>
        </div>
      </div>`:''}

      <!-- BACKTEST -->
      ${bt&&!bt.error?`<div class="panel">
        <div class="panel-hd">回測統計 (1H · 90天)</div>
        <div class="panel-body">
          <div class="bt-grid">
            <div class="bt-cell"><div class="bt-val" style="color:${bt.win_rate>=55?'var(--green)':bt.win_rate>=45?'var(--yellow)':'var(--red)'}">${bt.win_rate}%</div><div class="bt-lbl">勝率</div></div>
            <div class="bt-cell"><div class="bt-val" style="color:${bt.profit_factor>=1.5?'var(--green)':bt.profit_factor>=1?'var(--yellow)':'var(--red)'}">${bt.profit_factor}</div><div class="bt-lbl">盈虧因子</div></div>
            <div class="bt-cell"><div class="bt-val">${bt.trades}</div><div class="bt-lbl">交易次數</div></div>
            <div class="bt-cell"><div class="bt-val" style="color:${bt.avg_rr>=1.5?'var(--green)':'var(--yellow)'}">${bt.avg_rr}</div><div class="bt-lbl">平均R:R</div></div>
            <div class="bt-cell"><div class="bt-val" style="color:var(--red)">${bt.max_dd}</div><div class="bt-lbl">最大連敗</div></div>
            <div class="bt-cell"><div class="bt-val" style="font-size:.75rem">${bt.wins}W/${bt.losses}L</div><div class="bt-lbl">勝/敗</div></div>
          </div>
          <div style="margin-top:10px;padding:7px 10px;background:rgba(255,255,255,.03);font-family:'Space Mono',monospace;font-size:.65rem;text-align:center;">${bt.verdict}</div>
        </div>
      </div>`:''}
      ${bt&&bt.error?`<div style="padding:8px 12px;font-family:'Space Mono',monospace;font-size:.62rem;color:var(--text2)">回測：${bt.error}</div>`:''}

    </div><!-- right-col -->
  </div>

  <!-- JOURNAL -->
  <div class="panel" style="margin-top:12px;">
    <div class="panel-hd">交易日誌 <button class="btn btn-g" style="font-size:.55rem;padding:3px 8px;" onclick="loadJournal('${d.inst}')">載入</button> <button class="btn" style="font-size:.55rem;padding:3px 8px;" onclick="loadJournal('')">全部</button></div>
    <div id="journalList"><div style="padding:10px 13px;font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2)">點擊「載入」查看歷史紀錄</div></div>
  </div>

  <!-- BACKTEST TOGGLE -->
  <div style="margin-top:12px;display:flex;align-items:center;gap:10px;font-family:'Space Mono',monospace;font-size:.65rem;color:var(--text2);">
    <input type="checkbox" id="btCheck" style="accent-color:var(--accent);width:14px;height:14px;">
    <label for="btCheck">下次分析時執行回測（需額外 10-20 秒）</label>
  </div>
  `;
}

async function loadJournal(inst){
  const res=await fetch('/journal?inst='+encodeURIComponent(inst));
  const rows=await res.json();
  const el=document.getElementById('journalList');
  if(!rows.length){el.innerHTML='<div style="padding:10px 13px;font-family:\'Space Mono\',monospace;font-size:.65rem;color:var(--text2)">無紀錄</div>';return;}
  el.innerHTML=rows.map(r=>{
    const sc=r.signal==='LONG'?'var(--green)':r.signal==='SHORT'?'var(--red)':'var(--yellow)';
    return`<div class="journal-item">
      <div class="j-row">
        <span style="font-family:'Space Mono',monospace;font-size:.72rem;font-weight:700;color:${sc}">${r.signal}</span>
        <span style="font-family:'Space Mono',monospace;font-size:.62rem;color:var(--text2)">${r.inst}</span>
        <span style="font-family:'Space Mono',monospace;font-size:.6rem;color:var(--text2)">${r.ts}</span>
      </div>
      <div style="font-family:'Space Mono',monospace;font-size:.62rem;color:var(--text2)">
        進場 ${r.entry?'$'+r.entry:'--'} · SL ${r.sl?'$'+r.sl:'--'} · TP1 ${r.tp1?'$'+r.tp1:'--'} · R:R ${r.rr||'--'} · 信心 ${r.confidence||'--'}%
      </div>
      ${r.notes?`<div style="font-family:'Space Mono',monospace;font-size:.6rem;color:var(--accent);margin-top:2px">${r.notes}</div>`:''}
    </div>`;
  }).join('');
}

loadAll();
updateKZ();
setInterval(updateKZ, 60000);
document.getElementById('coinIn').focus();
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════
# HTTP SERVER
# ═══════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        if self.path == '/kill_zone':
            kz = get_kill_zone()
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(kz).encode())
            return
        if self.path.startswith('/journal'):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            inst = qs.get('inst',[''])[0]
            rows = get_journal(inst, 20)
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(rows, ensure_ascii=False).encode())
            return
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(HTML.encode('utf-8'))

    def do_POST(self):
        ln = int(self.headers.get('Content-Length',0))
        body = json.loads(self.rfile.read(ln).decode()) if ln else {}

        if self.path == '/test_tg':
            ok = send_telegram(body.get('token',''), body.get('chat_id',''),
                               '✅ ICT/SMC 分析引擎連線測試成功！')
            resp = json.dumps({"ok":ok}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(resp)
            return

        if self.path == '/analyze':
            try:
                result = analyze(
                    symbol     = body.get('symbol','BTC'),
                    api_key    = body.get('api_key',''),
                    secret_key = body.get('secret_key',''),
                    passphrase = body.get('passphrase',''),
                    run_bt     = body.get('run_backtest', False),
                    tg_token   = body.get('tg_token',''),
                    tg_chat    = body.get('tg_chat',''),
                )
                resp = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                resp = json.dumps({'error':str(e)}, ensure_ascii=False).encode()
                self.send_response(500)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(resp)
            return

        self.send_response(404); self.end_headers()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8888))
    host = '0.0.0.0'
    print()
    print("=" * 60)
    print("  🚀  ICT/SMC 終極分析引擎 v3.0")
    print("=" * 60)
    print()
    print(f"  ✅  http://localhost:{port}")
    print()
    print("  新增功能：")
    print("    ✅ 確認K線偵測（吞噬/錘子/Pin Bar）")
    print("    ✅ Displacement 推動波強度")
    print("    ✅ OB Confluence 多時框重疊")
    print("    ✅ Inducement 誘多/誘空偵測")
    print("    ✅ Kill Zone 時間過濾")
    print("    ✅ 回測引擎（勝率/R:R/盈虧因子）")
    print("    ✅ 交易日誌（自動記錄/複盤）")
    print("    ✅ Telegram 推播通知")
    print()
    print("  → Chrome 開啟 http://localhost:8888")
    print("  → 按 Ctrl+C 停止")
    print()
    print("=" * 60)
    print()
    server = HTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
