from flask import Flask, request, jsonify, send_from_directory
import json, urllib.request
import hmac, hashlib, base64
from datetime import datetime, timezone

app = Flask(__name__)
OKX_BASE = "https://www.okx.com"



# ═══════════════════════════════════════════════════════════════════════════
# OKX DATA
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
    headers = {"OK-ACCESS-KEY": ak, "OK-ACCESS-SIGN": sig,
               "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": pp,
               "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(OKX_BASE + path, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode())
    if d.get("code") != "0":
        raise Exception(f"OKX私有: {d.get('msg')}")
    return d["data"]

def norm(sym):
    s = sym.strip().upper()
    if "-" in s: return s
    if s.endswith("USDT"): return s[:-4] + "-USDT"
    return s + "-USDT"

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
# 純數學指標
# ═══════════════════════════════════════════════════════════════════════════

def ema(arr, p):
    k = 2/(p+1); r = [arr[0]]
    for v in arr[1:]: r.append(v*k + r[-1]*(1-k))
    return r

def rsi(closes, p=14):
    if len(closes) < p+2: return 50.0
    g = l = 0.0
    for i in range(len(closes)-p, len(closes)):
        d = closes[i]-closes[i-1]
        if d>0: g+=d
        else: l-=d
    ag,al = g/p, l/p
    return round(100-100/(1+ag/max(al,1e-9)), 2)

def atr(candles, p=14):
    trs = []
    for i,c in enumerate(candles):
        if i==0: trs.append(c["h"]-c["l"])
        else: trs.append(max(c["h"]-c["l"],abs(c["h"]-candles[i-1]["c"]),abs(c["l"]-candles[i-1]["c"])))
    a = sum(trs[:p])/p
    for tr in trs[p:]: a=(a*(p-1)+tr)/p
    return a

def macd(closes):
    e12=ema(closes,12); e26=ema(closes,26)
    ml=[a-b for a,b in zip(e12,e26)]
    sp=ema(ml[25:],9); sf=[None]*25+sp
    hist=[(ml[i]-sf[i]) if sf[i] is not None else 0 for i in range(len(ml))]
    golden = sf[-1] is not None and sf[-2] is not None and ml[-1]>sf[-1] and ml[-2]<=sf[-2]
    dead   = sf[-1] is not None and sf[-2] is not None and ml[-1]<=sf[-1] and ml[-2]>sf[-2]
    bull   = ml[-1]>(sf[-1] or 0)
    return {"hist":round(hist[-1],4),"bull":bull,"golden":golden,"dead":dead}

# ═══════════════════════════════════════════════════════════════════════════
# ICT / SMC 核心偵測
# ═══════════════════════════════════════════════════════════════════════════

def detect_swing_points(candles, left=3, right=3):
    """偵測 Swing High / Swing Low"""
    swings = []
    n = len(candles)
    for i in range(left, n-right):
        h,l = candles[i]["h"], candles[i]["l"]
        is_sh = all(h>=candles[i+k]["h"] for k in range(-left,0)) and \
                all(h>=candles[i+k]["h"] for k in range(1,right+1))
        is_sl = all(l<=candles[i+k]["l"] for k in range(-left,0)) and \
                all(l<=candles[i+k]["l"] for k in range(1,right+1))
        if is_sh: swings.append({"type":"SH","price":h,"idx":i,"t":candles[i]["t"]})
        if is_sl: swings.append({"type":"SL","price":l,"idx":i,"t":candles[i]["t"]})
    return sorted(swings, key=lambda x: x["idx"])

def detect_market_structure(candles, cur_price):
    swings = detect_swing_points(candles, 3, 3)
    if len(swings) < 4:
        return {"trend":"UNKNOWN","last_bos":None,"choch":None,"mss":None,"swings":[]}

    # 找最近的 SH 和 SL 序列
    recent = swings[-10:]
    shs = [s for s in recent if s["type"]=="SH"]
    sls = [s for s in recent if s["type"]=="SL"]

    trend = "UNKNOWN"
    bos = None
    choch = None
    mss_point = None

    # 判斷趨勢：HH/HL = 多頭，LH/LL = 空頭
    if len(shs)>=2 and len(sls)>=2:
        last_sh = shs[-1]; prev_sh = shs[-2]
        last_sl = sls[-1]; prev_sl = sls[-2]

        if last_sh["price"] > prev_sh["price"] and last_sl["price"] > prev_sl["price"]:
            trend = "BULL"  # HH + HL
        elif last_sh["price"] < prev_sh["price"] and last_sl["price"] < prev_sl["price"]:
            trend = "BEAR"  # LH + LL
        else:
            trend = "RANGING"

        # BOS：突破最近 SH (多頭) 或跌破最近 SL (空頭)
        if trend == "BULL" and cur_price > last_sh["price"]:
            bos = {"direction":"UP","level":last_sh["price"],"desc":"突破前高 BOS ↑"}
        elif trend == "BEAR" and cur_price < last_sl["price"]:
            bos = {"direction":"DOWN","level":last_sl["price"],"desc":"跌破前低 BOS ↓"}

        # CHoCH：多頭趨勢中跌破前 SL，或空頭趨勢中突破前 SH
        if trend == "BULL" and cur_price < last_sl["price"]:
            choch = {"level":last_sl["price"],"desc":"跌破前低 CHoCH ⚠ 潛在反轉"}
        elif trend == "BEAR" and cur_price > last_sh["price"]:
            choch = {"level":last_sh["price"],"desc":"突破前高 CHoCH ⚠ 潛在反轉"}

        # MSS：CHoCH 後再確認一次
        if choch and len(shs)>=3 and len(sls)>=3:
            mss_point = {"level":choch["level"],"desc":"MSS 市場結構轉換確認"}

    return {
        "trend": trend,
        "bos": bos,
        "choch": choch,
        "mss": mss_point,
        "swings": [{"type":s["type"],"price":s["price"],"t":s["t"]} for s in swings[-6:]],
        "last_sh": shs[-1] if shs else None,
        "last_sl": sls[-1] if sls else None,
    }

def detect_order_blocks(candles, cur_price, direction="both"):
    obs = []
    n = len(candles)
    for i in range(2, n-2):
        c = candles[i]
        body = abs(c["c"]-c["o"])
        avg_body = sum(abs(candles[j]["c"]-candles[j]["o"]) for j in range(max(0,i-10),i)) / min(10,i)

        # 多頭 OB：陰線後跟著強勢上漲，且突破前高
        if c["c"] < c["o"] and body > avg_body*0.8:
            # 接下來幾根要強勢上漲
            next_candles = candles[i+1:min(i+4,n)]
            if next_candles and max(nc["c"] for nc in next_candles) > c["h"]*1.001:
                if c["l"] < cur_price < c["h"]*1.05:  # 價格在 OB 附近或上方
                    obs.append({
                        "type":"BULL_OB",
                        "high":c["h"],"low":c["l"],
                        "mid":(c["h"]+c["l"])/2,
                        "t":c["t"],
                        "strength": min(5, int(body/avg_body*2)+1),
                        "dist_pct": round((cur_price-c["l"])/cur_price*100,2),
                        "label":"多頭訂單塊 🟢"
                    })

        # 空頭 OB：陽線後跟著強勢下跌
        if c["c"] > c["o"] and body > avg_body*0.8:
            next_candles = candles[i+1:min(i+4,n)]
            if next_candles and min(nc["c"] for nc in next_candles) < c["l"]*0.999:
                if c["l"]*0.95 < cur_price < c["h"]:
                    obs.append({
                        "type":"BEAR_OB",
                        "high":c["h"],"low":c["l"],
                        "mid":(c["h"]+c["l"])/2,
                        "t":c["t"],
                        "strength": min(5, int(body/avg_body*2)+1),
                        "dist_pct": round((c["h"]-cur_price)/cur_price*100,2),
                        "label":"空頭訂單塊 🔴"
                    })

    # 按距離排序，只取最近的
    bull_obs = sorted([o for o in obs if o["type"]=="BULL_OB"], key=lambda x: x["dist_pct"])[:3]
    bear_obs = sorted([o for o in obs if o["type"]=="BEAR_OB"], key=lambda x: x["dist_pct"])[:3]
    return bull_obs + bear_obs

def detect_fvg(candles, cur_price):
    fvgs = []
    n = len(candles)
    for i in range(1, n-1):
        prev = candles[i-1]; curr = candles[i]; nxt = candles[i+1]

        # 多頭 FVG
        if prev["h"] < nxt["l"]:
            gap_size = nxt["l"] - prev["h"]
            mid = (prev["h"] + nxt["l"]) / 2
            if gap_size/curr["c"] > 0.001:  # 缺口至少 0.1%
                filled = cur_price <= nxt["l"] and cur_price >= prev["h"]
                fvgs.append({
                    "type":"BULL_FVG",
                    "high":nxt["l"],"low":prev["h"],"mid":mid,
                    "size_pct":round(gap_size/curr["c"]*100,3),
                    "t":curr["t"],"filled":filled,
                    "label":"多頭FVG 失衡區 🟦",
                    "above_price": cur_price < prev["h"],
                    "dist_pct": round(abs(cur_price-mid)/cur_price*100,2)
                })

        # 空頭 FVG
        if prev["l"] > nxt["h"]:
            gap_size = prev["l"] - nxt["h"]
            mid = (prev["l"] + nxt["h"]) / 2
            if gap_size/curr["c"] > 0.001:
                filled = cur_price >= nxt["h"] and cur_price <= prev["l"]
                fvgs.append({
                    "type":"BEAR_FVG",
                    "high":prev["l"],"low":nxt["h"],"mid":mid,
                    "size_pct":round(gap_size/curr["c"]*100,3),
                    "t":curr["t"],"filled":filled,
                    "label":"空頭FVG 失衡區 🟧",
                    "above_price": cur_price < nxt["h"],
                    "dist_pct": round(abs(cur_price-mid)/cur_price*100,2)
                })

    # 過濾已填滿的，按距離排序
    active = [f for f in fvgs if not f["filled"]]
    bull_fvg = sorted([f for f in active if f["type"]=="BULL_FVG"], key=lambda x: x["dist_pct"])[:3]
    bear_fvg = sorted([f for f in active if f["type"]=="BEAR_FVG"], key=lambda x: x["dist_pct"])[:3]
    return bull_fvg + bear_fvg

def detect_liquidity(candles, cur_price):
    liq_points = []
    n = len(candles)

    # 找所有明顯高低點
    for i in range(3, n-3):
        h = candles[i]["h"]; l = candles[i]["l"]
        # 等於 swing high/low 的邏輯
        is_sh = all(h>=candles[i+k]["h"] for k in [-3,-2,-1,1,2,3])
        is_sl = all(l<=candles[i+k]["l"] for k in [-3,-2,-1,1,2,3])

        if is_sh and h > cur_price:
            # 計算同價位有多少次測試（測試次數越多，流動性越高）
            tests = sum(1 for j in range(n) if abs(candles[j]["h"]-h)/h < 0.003)
            liq_points.append({
                "type":"BSL",
                "price":h,
                "tests":tests,
                "strength":min(5,tests),
                "dist_pct":round((h-cur_price)/cur_price*100,2),
                "label":f"BSL 買方流動性 (前高) — 目標/獵殺點",
                "action":"空方目標 / 多方止損區"
            })

        if is_sl and l < cur_price:
            tests = sum(1 for j in range(n) if abs(candles[j]["l"]-l)/l < 0.003)
            liq_points.append({
                "type":"SSL",
                "price":l,
                "tests":tests,
                "strength":min(5,tests),
                "dist_pct":round((cur_price-l)/cur_price*100,2),
                "label":f"SSL 賣方流動性 (前低) — 目標/獵殺點",
                "action":"多方目標 / 空方止損區"
            })

    # 合併相近點位，排序
    bsl = sorted([p for p in liq_points if p["type"]=="BSL"], key=lambda x: x["price"])[:4]
    ssl = sorted([p for p in liq_points if p["type"]=="SSL"], key=lambda x: -x["price"])[:4]
    return bsl + ssl

def detect_premium_discount(candles, cur_price):
    swings = detect_swing_points(candles, 3, 3)
    if len(swings) < 2:
        return None
    shs = [s for s in swings if s["type"]=="SH"]
    sls = [s for s in swings if s["type"]=="SL"]
    if not shs or not sls:
        return None

    range_high = shs[-1]["price"]
    range_low  = sls[-1]["price"]
    equilibrium = (range_high + range_low) / 2
    premium_zone = equilibrium + (range_high-range_low)*0.25   # 上75%
    discount_zone = equilibrium - (range_high-range_low)*0.25  # 下25%

    position = "PREMIUM" if cur_price > equilibrium else "DISCOUNT"
    pct_position = round((cur_price-range_low)/(range_high-range_low)*100, 1) if range_high!=range_low else 50

    return {
        "range_high": round(range_high, 4),
        "range_low":  round(range_low, 4),
        "equilibrium": round(equilibrium, 4),
        "premium_zone": round(premium_zone, 4),
        "discount_zone": round(discount_zone, 4),
        "position": position,
        "pct_position": pct_position,
        "bias": "空方區域 — 尋找做空機會" if position=="PREMIUM" else "多方區域 — 尋找做多機會"
    }

# ═══════════════════════════════════════════════════════════════════════════
# 多時間框架分析（5層）
# ═══════════════════════════════════════════════════════════════════════════

def analyze_tf(candles, cur_price, tf_name):
    """單一時間框架完整分析"""
    if len(candles) < 30:
        return {"tf":tf_name,"error":"數據不足"}
    closes = [c["c"] for c in candles]
    e20 = ema(closes,20); e50 = ema(closes,50)
    trend = "BULL" if e20[-1]>e50[-1] else "BEAR"
    rsi_val = rsi(closes)
    atr_val = atr(candles)
    macd_r  = macd(closes)
    ms      = detect_market_structure(candles, cur_price)
    pd      = detect_premium_discount(candles, cur_price)
    ob      = detect_order_blocks(candles, cur_price)
    fvg     = detect_fvg(candles, cur_price)
    liq     = detect_liquidity(candles, cur_price)

    # 方向偏差
    bias = "BULL" if trend=="BULL" and ms["trend"] in ["BULL","UNKNOWN"] else \
           "BEAR" if trend=="BEAR" and ms["trend"] in ["BEAR","UNKNOWN"] else \
           ms["trend"]

    return {
        "tf": tf_name,
        "trend": trend,
        "bias": bias,
        "rsi": rsi_val,
        "atr": round(atr_val,4),
        "ema20": round(e20[-1],4),
        "ema50": round(e50[-1],4),
        "macd": macd_r,
        "structure": ms,
        "premium_discount": pd,
        "order_blocks": ob[:4],
        "fvg": fvg[:4],
        "liquidity": liq[:6],
    }

def multi_tf_bias(tf_analyses):
    """彙整5個時間框架的方向偏差，輸出最終交易偏向"""
    weights = {"1W":5,"1D":4,"4H":3,"1H":2,"15m":1}
    bull=0; bear=0
    for tf_data in tf_analyses:
        tf = tf_data.get("tf","")
        w  = weights.get(tf,1)
        b  = tf_data.get("bias","UNKNOWN")
        if b=="BULL": bull+=w
        elif b=="BEAR": bear+=w

    total = bull+bear
    if total==0: return "NEUTRAL",50
    bull_pct = round(bull/total*100)
    if bull_pct>=65: return "BULL",bull_pct
    if bull_pct<=35: return "BEAR",100-bull_pct
    return "NEUTRAL",max(bull_pct,100-bull_pct)

def generate_trade_plan(tf_analyses, cur_price, ticker):
    """根據多時框分析生成交易計劃"""
    overall_bias, confidence = multi_tf_bias(tf_analyses)

    # 從 15m 取得精確進場
    entry_tf = next((t for t in tf_analyses if t["tf"]=="15m"), None)
    h1_tf    = next((t for t in tf_analyses if t["tf"]=="1H"), None)
    h4_tf    = next((t for t in tf_analyses if t["tf"]=="4H"), None)

    signal = "WAIT"
    entry = sl = tp1 = tp2 = tp3 = None
    reason_steps = []

    if overall_bias == "BULL" and confidence >= 60:
        signal = "LONG"
        reason_steps = [
            f"週/日/4H 多頭趨勢一致",
            f"尋找回調至折扣區 (Discount) 進場",
            f"在15m 訂單塊或FVG支撐做多",
        ]
        # 進場點：15m 最近多頭 OB 或 FVG
        if entry_tf:
            bull_obs = [o for o in entry_tf.get("order_blocks",[]) if o["type"]=="BULL_OB"]
            bull_fvg = [f for f in entry_tf.get("fvg",[]) if f["type"]=="BULL_FVG"]
            if bull_obs:
                entry = round(bull_obs[0]["high"],4)
                sl    = round(bull_obs[0]["low"]*0.999,4)
            elif bull_fvg:
                entry = round(bull_fvg[0]["high"],4)
                sl    = round(bull_fvg[0]["low"]*0.999,4)
            else:
                entry = round(cur_price,4)
                atr_e = entry_tf.get("atr",cur_price*0.01)
                sl    = round(entry - atr_e*1.5,4)

        # 止盈：SSL/BSL
        if h1_tf:
            bsl = [l for l in h1_tf.get("liquidity",[]) if l["type"]=="BSL"]
            if bsl:
                tp1 = round(bsl[0]["price"],4)
                tp2 = round(bsl[1]["price"],4) if len(bsl)>1 else round(tp1*1.02,4)
                tp3 = round(bsl[-1]["price"],4) if len(bsl)>2 else round(tp2*1.02,4)

    elif overall_bias == "BEAR" and confidence >= 60:
        signal = "SHORT"
        reason_steps = [
            f"週/日/4H 空頭趨勢一致",
            f"尋找反彈至溢價區 (Premium) 做空",
            f"在15m 訂單塊或FVG壓力做空",
        ]
        if entry_tf:
            bear_obs = [o for o in entry_tf.get("order_blocks",[]) if o["type"]=="BEAR_OB"]
            bear_fvg = [f for f in entry_tf.get("fvg",[]) if f["type"]=="BEAR_FVG"]
            if bear_obs:
                entry = round(bear_obs[0]["low"],4)
                sl    = round(bear_obs[0]["high"]*1.001,4)
            elif bear_fvg:
                entry = round(bear_fvg[0]["low"],4)
                sl    = round(bear_fvg[0]["high"]*1.001,4)
            else:
                entry = round(cur_price,4)
                atr_e = entry_tf.get("atr",cur_price*0.01)
                sl    = round(entry + atr_e*1.5,4)

        if h1_tf:
            ssl = [l for l in h1_tf.get("liquidity",[]) if l["type"]=="SSL"]
            if ssl:
                tp1 = round(ssl[0]["price"],4)
                tp2 = round(ssl[1]["price"],4) if len(ssl)>1 else round(tp1*0.98,4)
                tp3 = round(ssl[-1]["price"],4) if len(ssl)>2 else round(tp2*0.98,4)

    # ── 止盈計算與 R:R 驗證 ─────────────────────────────────────────────────
    MIN_RR = 1.5   # 最低可接受 R:R

    if entry and sl:
        risk = abs(entry - sl)
        if risk == 0:
            entry = sl = tp1 = tp2 = tp3 = None
            rr1 = rr2 = None
        else:
            # 確保止盈方向正確
            is_long = signal == "LONG"

            # 過濾掉方向錯誤的 TP（多單止盈必須 > 進場，空單必須 <）
            if tp1 and ((is_long and tp1 <= entry) or (not is_long and tp1 >= entry)):
                tp1 = None
            if tp2 and ((is_long and tp2 <= entry) or (not is_long and tp2 >= entry)):
                tp2 = None
            if tp3 and ((is_long and tp3 <= entry) or (not is_long and tp3 >= entry)):
                tp3 = None

            # 確保 TP 達到最低 R:R 1.5
            min_tp1_dist = risk * MIN_RR
            if not tp1 or abs(tp1 - entry) < min_tp1_dist:
                tp1 = round(entry + min_tp1_dist if is_long else entry - min_tp1_dist, 4)
            if not tp2 or abs(tp2 - entry) < risk * 2:
                tp2 = round(entry + risk * 2.5 if is_long else entry - risk * 2.5, 4)
            if not tp3 or abs(tp3 - entry) < risk * 3:
                tp3 = round(entry + risk * 4.0 if is_long else entry - risk * 4.0, 4)

            rr1 = round(abs(tp1 - entry) / risk, 2)
            rr2 = round(abs(tp2 - entry) / risk, 2)

            # R:R 不足 1.0 → 放棄這次信號
            if rr1 < 1.0:
                signal = "WAIT"
                entry = sl = tp1 = tp2 = tp3 = None
                rr1 = rr2 = None
                reason_steps = ["R:R 比值不足 1:1，等待更好進場點"]
    else:
        rr1 = rr2 = None

    return {
        "signal": signal,
        "overall_bias": overall_bias,
        "confidence": confidence,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr1": rr1,
        "rr2": rr2,
        "min_rr": MIN_RR,
        "reason_steps": reason_steps,
    }

# ═══════════════════════════════════════════════════════════════════════════
# 主分析入口
# ═══════════════════════════════════════════════════════════════════════════

def analyze(symbol, api_key="", secret_key="", passphrase=""):
    inst = norm(symbol)
    ticker = get_ticker(inst)
    cur = ticker["price"]

    # 抓5個時間框架
    tf_configs = [
        ("1W", "1W", 52),
        ("1D", "1D", 90),
        ("4H", "4H", 120),
        ("1H", "1H", 168),
        ("15m","15m",96),
    ]
    tf_analyses = []
    for tf_name, bar, limit in tf_configs:
        try:
            candles = get_candles(inst, bar, limit)
            tf_analyses.append(analyze_tf(candles, cur, tf_name))
        except Exception as e:
            tf_analyses.append({"tf":tf_name,"error":str(e)})

    # 流動性訂單簿
    try:
        bids, asks = get_books(inst)
        bid_vol = sum(b[1] for b in bids); ask_vol = sum(a[1] for a in asks)
        spread = asks[0][0]-bids[0][0] if bids and asks else 0
        liq_ratio = round(bid_vol/ask_vol,3) if ask_vol else 1
        bid_wall = max(bids,key=lambda x:x[1]) if bids else [0,0]
        ask_wall = max(asks,key=lambda x:x[1]) if asks else [0,0]
        orderbook = {"bid_vol":round(bid_vol,2),"ask_vol":round(ask_vol,2),
                     "ratio":liq_ratio,"spread":round(spread,6),
                     "bid_wall":bid_wall,"ask_wall":ask_wall,
                     "pressure":"買壓強" if liq_ratio>1.2 else ("賣壓強" if liq_ratio<0.8 else "均衡")}
    except:
        orderbook = {}

    plan = generate_trade_plan(tf_analyses, cur, ticker)

    # 帳號（選填）
    account = None
    if api_key and secret_key and passphrase:
        try:
            bal = okx_private("/api/v5/account/balance", api_key, secret_key, passphrase)
            details = bal[0].get("details",[])
            base_ccy = inst.split("-")[0]
            usdt = next((d["availBal"] for d in details if d["ccy"]=="USDT"),"0")
            base = next((d["availBal"] for d in details if d["ccy"]==base_ccy),"0")
            eq   = bal[0].get("totalEq","0")
            try:
                pos_data = okx_private(f"/api/v5/account/positions?instId={inst}-SWAP",api_key,secret_key,passphrase)
            except: pos_data=[]
            positions=[]
            for p in pos_data:
                if float(p.get("pos",0))!=0:
                    positions.append({"side":p.get("posSide","--"),"size":p.get("pos","0"),
                        "entry":float(p.get("avgPx",0)),"liq":float(p.get("liqPx",0)) if p.get("liqPx") else None,
                        "pnl":float(p.get("upl",0)),"pnl_pct":float(p.get("uplRatio",0))*100,"lev":p.get("lever","--")})
            account={"ok":True,"usdt":float(usdt),"base":float(base),"base_ccy":base_ccy,"eq":float(eq),"pos":positions}
        except Exception as e:
            account={"ok":False,"error":str(e)}

    chg = round((cur-ticker["open24"])/ticker["open24"]*100,2)
    v24 = ticker["vol24"]
    vol_str = f"{v24/1e9:.2f}B" if v24>=1e9 else f"{v24/1e6:.1f}M"

    return {
        "inst": inst,
        "ts": datetime.now().strftime("%m/%d %H:%M:%S"),
        "price": cur, "chg24": chg,
        "high24": ticker["high24"], "low24": ticker["low24"],
        "vol24": vol_str,
        "tf_analyses": tf_analyses,
        "plan": plan,
        "orderbook": orderbook,
        "account": account,
    }

# ═══════════════════════════════════════════════════════════════════════════
# HTML 前端
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/analyze', methods=['POST'])
def analyze_route():
    try:
        body = request.get_json() or {}
        result = analyze(
            symbol=body.get('symbol', 'BTC'),
            api_key=body.get('api_key', ''),
            secret_key=body.get('secret_key', ''),
            passphrase=body.get('passphrase', ''),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8888))
    app.run(host='0.0.0.0', port=port)
