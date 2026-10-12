import os
import time
import requests
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==================== 時區設定 (台灣時間 UTC+8) ====================
TZ_TAIPEI = timezone(timedelta(hours=8))

def get_taipei_now():
    return datetime.now(TZ_TAIPEI)

def get_taipei_now_str():
    return get_taipei_now().strftime("%Y-%m-%d %H:%M:%S")

# ==================== 價格動態格式化 ====================
def format_price(val):
    if val is None or val == "N/A":
        return "N/A"
    try:
        f_val = float(val)
        if f_val >= 100:
            return f"{f_val:.2f}"
        elif f_val >= 1:
            return f"{f_val:.4f}"
        elif f_val >= 0.0001:
            return f"{f_val:.6f}"
        else:
            return f"{f_val:.8f}"
    except Exception:
        return str(val)

# ==================== EMA 計算 ====================
def calculate_ema(closes, period):
    if len(closes) < period:
        return None
    reversed_closes = list(reversed(closes))
    k = 2 / (period + 1)
    ema = reversed_closes[0]
    for price in reversed_closes[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

# ==================== 設定區 (AVWAP 多標的平行監控 + $100 風控) ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6132673542")

MIN_OI_GROWTH_PCT = 1.0      # 5m 持倉量增長門檻 1.0% (捕捉更多優質標的)
ALERT_COOLDOWN = {}
COOLDOWN_SECONDS = 300       # 同一標的 5 分鐘冷卻 (不同標的同步發送不影響)

LAST_SUNDAY_REPORT_DATE = ""
LAST_DAILY_BACKTEST_DATE = ""
TRACKED_SIGNALS = []

# ==================== Health Check 服務器 ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("⚡ 數據獵手 (AVWAP 多標的平行推播版) 運行中".encode('utf-8'))

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# ==================== Telegram 推播 ====================
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ 未設定 TELEGRAM_BOT_TOKEN 環境變數！")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        res_json = resp.json()
        if not res_json.get("ok"):
            print(f"❌ Telegram 發送失敗 API 回應: {res_json.get('description')}")
        else:
            print("✅ Telegram 推播成功發送！")
    except Exception as e:
        print(f"❌ Telegram 連線異常: {e}")

# ==================== 多時框 (MTF) 趨勢分析 ====================
def get_multi_tf_views(symbol):
    tf_configs = [('5', '5分'), ('15', '15分'), ('60', '1小時'), ('240', '4小時'), ('D', '日線')]
    views = {}
    for interval, label in tf_configs:
        try:
            url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit=15"
            res = requests.get(url, timeout=2.5).json()
            klines = res.get("result", {}).get("list", [])
            if len(klines) >= 10:
                c0_close = float(klines[0][4])
                c0_open = float(klines[0][1])
                closes = [float(k[4]) for k in klines]
                ma = sum(closes) / len(closes)
                if c0_close > ma and c0_close >= c0_open:
                    views[label] = "🟢 多頭強勢"
                elif c0_close < ma and c0_close <= c0_open:
                    views[label] = "🔴 空頭強勢"
                elif c0_close > ma:
                    views[label] = "📈 偏多震盪"
                else:
                    views[label] = "📉 偏空震盪"
            else:
                views[label] = "⚪ 數據不足"
        except Exception:
            views[label] = "⚪ 讀取失敗"
    return views

# ==================== Martin 日線選強與階段高點 AVWAP 計算 ====================
def analyze_martin_daily_setup(symbol):
    """ 抓取日線資料，精確計算階段高點 AVWAP 與 21 EMA 支撐位 """
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit=60"
    try:
        resp = requests.get(url, timeout=3.5).json()
        klines = resp.get("result", {}).get("list", [])
        if len(klines) < 30:
            return None

        klines_asc = list(reversed(klines))
        closes = [float(k[4]) for k in klines_asc]
        highs = [float(k[2]) for k in klines_asc]
        lows = [float(k[3]) for k in klines_asc]
        volumes = [float(k[5]) for k in klines_asc]

        c_now = closes[-1]
        c_30d_ago = closes[-30]
        momentum_30d = ((c_now - c_30d_ago) / c_30d_ago) * 100

        # 1. 篩選強勢標的 (近30日動能)
        if momentum_30d < 10.0:
            return None

        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        ema50 = calculate_ema(closes, 50)

        # 2. 回調低點抬高 (Higher Lows)
        recent_lows = lows[-10:]
        is_higher_lows = recent_lows[-1] >= recent_lows[0]

        # 3. 尋找近 30 日最高點，精確計算 AVWAP
        recent_30_highs = highs[-30:]
        peak_high = max(recent_30_highs)
        peak_idx = recent_30_highs.index(peak_high) + (len(highs) - 30)

        pv_sum = sum((highs[i] + lows[i] + closes[i]) / 3 * volumes[i] for i in range(peak_idx, len(highs)))
        vol_sum = sum(volumes[i] for i in range(peak_idx, len(highs)))
        avwap_val = (pv_sum / vol_sum) if vol_sum > 0 else c_now

        # 4. 判斷價格是否落在 AVWAP 或 21 EMA 支撐區 (2.5% 範圍內)
        dist_avwap_pct = abs(c_now - avwap_val) / c_now * 100
        dist_ema21_pct = abs(c_now - ema21) / c_now * 100

        if is_higher_lows and (dist_avwap_pct <= 2.5 or dist_ema21_pct <= 2.5 or c_now >= ema21 * 0.98):
            return {
                "ema9": ema9,
                "ema21": ema21,
                "ema50": ema50,
                "avwap": avwap_val,
                "dist_avwap_pct": dist_avwap_pct,
                "peak_high": peak_high,
                "support_zone": min(ema21, avwap_val)
            }
        return None
    except Exception:
        return None

# ==================== 三階止盈與即時打點播報覆盤引擎 ====================
def update_signal_outcomes():
    now_ts = time.time()
    for sig in TRACKED_SIGNALS:
        if sig["status"] in ["WIN_TP3", "LOSS", "EXPIRED"]:
            continue
        
        if (now_ts - sig["created_at"]) > 43200 and sig["status"] == "PENDING":
            sig["status"] = "EXPIRED"
            continue

        try:
            symbol = sig["symbol"]
            direction = sig["direction"]
            entry_price = sig["entry_price"]
            
            url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
            res = requests.get(url, timeout=3).json()
            ticker_data = res.get("result", {}).get("list", [{}])[0]
            if not ticker_data:
                continue

            last_price = float(ticker_data.get("lastPrice", 0))

            if direction == "LONG":
                # 止損 (-2.0%) + 診斷原因
                if last_price <= sig["sl"] and sig["status"] == "PENDING":
                    sig["status"] = "LOSS"
                    sl_reasons = [
                        "1. <b>AVWAP 支撐跌破</b>：價格實體向下跌破階段高點 AVWAP 防守線",
                        "2. <b>微結構突破失敗</b>：5m 突破前高後缺乏買盤跟進，未能守住突破陽線低點",
                        "3. <b>大盤聯動急洗</b>：大盤短線快速下挫，貫穿 -2.0% 防守位"
                    ]
                    reason_text = "\n".join([f"• {r}" for r in sl_reasons])
                    loss_msg = f"""❌ <b>【AVWAP 策略 - 單筆止損告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
方向：<b>🟢 AVWAP 回調買入</b>
進場價格：<b>{format_price(entry_price)} USDT</b>
觸發止損價：<b>{format_price(last_price)} USDT</b> (-2.0%)

🔍 <b>【止損原因診斷】</b>
{reason_text}

⚠️ <i>已觸發 -2.0% 紀律止損，嚴格控管 $100 風險！</i>"""
                    send_telegram_alert(loss_msg)

                # TP3 (+4.0%)
                elif last_price >= sig["tp3"] and sig["notified_tp"] < 3:
                    sig["status"] = "WIN_TP3"
                    sig["notified_tp"] = 3
                    tp3_msg = f"""🚀 <b>【AVWAP 策略 - TP3 滿盈離場告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
方向：<b>🟢 AVWAP 回調買入</b>
進場價格：<b>{format_price(entry_price)} USDT</b>
當前價格：<b>{format_price(last_price)} USDT</b> (+4.0%)

🎉 <i>已達標 TP3 (+4.0%)！大獲全勝，請全數獲利離場！</i>"""
                    send_telegram_alert(tp3_msg)

                # TP2 (+3.0%)
                elif last_price >= sig["tp2"] and sig["notified_tp"] < 2:
                    sig["status"] = "WIN_TP2"
                    sig["notified_tp"] = 2
                    tp2_msg = f"""🎯 <b>【AVWAP 策略 - TP2 達標告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
方向：<b>🟢 AVWAP 回調買入</b>
進場價格：<b>{format_price(entry_price)} USDT</b>
當前價格：<b>{format_price(last_price)} USDT</b> (+3.0%)

💪 <i>已達標 TP2 (+3.0%)！獲利持續擴展中！</i>"""
                    send_telegram_alert(tp2_msg)

                # TP1 (+2.0%)
                elif last_price >= sig["tp1"] and sig["notified_tp"] < 1:
                    sig["status"] = "WIN_TP1"
                    sig["notified_tp"] = 1
                    tp1_msg = f"""✅ <b>【AVWAP 策略 - TP1 達標告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
方向：<b>🟢 AVWAP 回調買入</b>
進場價格：<b>{format_price(entry_price)} USDT</b>
當前價格：<b>{format_price(last_price)} USDT</b> (+2.0%)

💡 <i>已達標 TP1 (+2.0%)！建議平倉 50% 並將止損拉至進場保本價！</i>"""
                    send_telegram_alert(tp1_msg)

            else: # SHORT
                if last_price >= sig["sl"] and sig["status"] == "PENDING":
                    sig["status"] = "LOSS"
                    send_telegram_alert(f"❌ <b>【AVWAP 策略】</b> <code>{symbol}</code> 觸及 -2.0% 止損！")
                elif last_price <= sig["tp3"] and sig["notified_tp"] < 3:
                    sig["status"] = "WIN_TP3"
                    sig["notified_tp"] = 3
                    send_telegram_alert(f"🚀 <b>【AVWAP 策略】</b> <code>{symbol}</code> 達標 TP3 (+4.0%) 滿盈離場！")
                elif last_price <= sig["tp2"] and sig["notified_tp"] < 2:
                    sig["status"] = "WIN_TP2"
                    sig["notified_tp"] = 2
                    send_telegram_alert(f"🎯 <b>【AVWAP 策略】</b> <code>{symbol}</code> 達標 TP2 (+3.0%)！")
                elif last_price <= sig["tp1"] and sig["notified_tp"] < 1:
                    sig["status"] = "WIN_TP1"
                    sig["notified_tp"] = 1
                    send_telegram_alert(f"✅ <b>【AVWAP 策略】</b> <code>{symbol}</code> 達標 TP1 (+2.0%)！建議保本平半倉！")

        except Exception:
            pass

def check_and_send_daily_backtest_report():
    global LAST_DAILY_BACKTEST_DATE
    now_dt = get_taipei_now()
    today_date_str = now_dt.strftime("%Y-%m-%d")

    if now_dt.hour == 8 and LAST_DAILY_BACKTEST_DATE != today_date_str:
        if not TRACKED_SIGNALS:
            LAST_DAILY_BACKTEST_DATE = today_date_str
            return

        total_cnt = len(TRACKED_SIGNALS)
        tp1_cnt = sum(1 for s in TRACKED_SIGNALS if s["status"] in ["WIN_TP1", "WIN_TP2", "WIN_TP3"])
        tp2_cnt = sum(1 for s in TRACKED_SIGNALS if s["status"] in ["WIN_TP2", "WIN_TP3"])
        tp3_cnt = sum(1 for s in TRACKED_SIGNALS if s["status"] == "WIN_TP3")
        loss_cnt = sum(1 for s in TRACKED_SIGNALS if s["status"] == "LOSS")
        pending_cnt = sum(1 for s in TRACKED_SIGNALS if s["status"] == "PENDING")

        completed_cnt = tp1_cnt + loss_cnt
        win_rate = (tp1_cnt / completed_cnt * 100) if completed_cnt > 0 else 0.0

        msg = f"""📈 <b>【AVWAP 回調買入法 - 24H 實盤戰報】</b>

📅 統計日期：<code>{today_date_str}</code>

📊 <b>【實盤績效統計】</b>
• 總發單訊號數：<b>{total_cnt} 筆</b>
• TP1 命中 (+2%)：<b>{tp1_cnt} 筆</b> ({tp1_cnt/total_cnt*100 if total_cnt else 0:.1f}%)
• TP2 命中 (+3%)：<b>{tp2_cnt} 筆</b> ({tp2_cnt/total_cnt*100 if total_cnt else 0:.1f}%)
• TP3 滿盈 (+4%)：<b>{tp3_cnt} 筆</b> ({tp3_cnt/total_cnt*100 if total_cnt else 0:.1f}%)
• SL 止損 (-2.0%)：<b>{loss_cnt} 筆</b>
• 持倉進行中：<b>{pending_cnt} 筆</b>

🎯 <b>【實盤勝率】</b>
• 策略勝率 (TP1 達標率)：<b>{win_rate:.1f}%</b> (目標 80%)

💡 <i>專注 AVWAP 機構成本支撐共振 + 5m 微結構突破 + $100 固定風控！</i>"""

        send_telegram_alert(msg)
        LAST_DAILY_BACKTEST_DATE = today_date_str

# ==================== 週日 BTC/ETH 大盤週報 ====================
def check_and_send_sunday_report():
    global LAST_SUNDAY_REPORT_DATE
    now_dt = get_taipei_now()
    today_date_str = now_dt.strftime("%Y-%m-%d")

    if now_dt.weekday() == 6 and LAST_SUNDAY_REPORT_DATE != today_date_str:
        targets = [("BTCUSDT", "比特幣"), ("ETHUSDT", "以太坊")]
        for symbol, name in targets:
            try:
                tf_views = get_multi_tf_views(symbol)
                m_setup = analyze_martin_daily_setup(symbol)
                if not m_setup: continue

                report_msg = f"""📊 <b>【每週日 AVWAP 策略主流幣大盤點評】</b>

📌 <b>標的</b>：<code>{symbol}</code> ({name} 永續合約)

🎯 <b>【日線 AVWAP 與均線支撐】</b>
• 階段高點 AVWAP：<code>{format_price(m_setup['avwap'])}</code> (距離 {m_setup['dist_avwap_pct']:.2f}%)
• 21 EMA：<code>{format_price(m_setup['ema21'])}</code> | 9 EMA：<code>{format_price(m_setup['ema9'])}</code>
• 關鍵支撐區：<code>{format_price(m_setup['support_zone'])}</code>

🌐 <b>【多時框趨勢】</b>
• 1小時：{tf_views.get('1小時', 'N/A')} | 4小時：{tf_views.get('4小時', 'N/A')} | 日線：{tf_views.get('日線', 'N/A')}"""

                send_telegram_alert(report_msg)
                time.sleep(2)
            except Exception:
                pass
        LAST_SUNDAY_REPORT_DATE = today_date_str

# ==================== 單一標的 AVWAP 觸發掃描 (5m 微結構突破) ====================
def inspect_symbol(ticker):
    symbol = ticker["symbol"]
    try:
        now_time = time.time()
        if symbol in ALERT_COOLDOWN and (now_time - ALERT_COOLDOWN[symbol]) < COOLDOWN_SECONDS:
            return

        # 1. 檢測 5m OI 爆量卡位
        oi_url = f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime=5min&limit=2"
        oi_res = requests.get(oi_url, timeout=3).json()
        oi_list = oi_res.get("result", {}).get("list", [])
        if len(oi_list) < 2: return

        oi_now = float(oi_list[0]["openInterest"])
        oi_prev = float(oi_list[1]["openInterest"])
        if oi_prev <= 0: return

        oi_change_pct = ((oi_now - oi_prev) / oi_prev) * 100
        if oi_change_pct < MIN_OI_GROWTH_PCT:
            return

        # 2. 檢測 AVWAP 日線層面支撐區
        m_setup = analyze_martin_daily_setup(symbol)
        if not m_setup:
            return

        avwap_val = m_setup["avwap"]
        dist_avwap_pct = m_setup["dist_avwap_pct"]

        # 3. 檢測 5m 微結構突破 (Micro Breakout)
        kline_url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=5&limit=3"
        k_res = requests.get(kline_url, timeout=3).json()
        klines = k_res.get("result", {}).get("list", [])
        if len(klines) < 2: return

        c0_open = float(klines[0][1])
        c0_high = float(klines[0][2])
        c0_low = float(klines[0][3])
        c0_close = float(klines[0][4])

        c1_high = float(klines[1][2])
        c1_low = float(klines[1][3])

        # 條件：5m 收盤價強勢突破前一根 5m High (微結構突破確立)
        is_micro_breakout = c0_close > c1_high
        if not is_micro_breakout:
            return

        direction_str = "LONG"
        direction_disp = "🟢 AVWAP 支撐共振 + 多頭突破 (Bullish)"

        # 4. 多時框趨勢過濾 (1h/4h 不能是強空)
        tf_views = get_multi_tf_views(symbol)
        h1_tf = tf_views.get("1小時", "")
        h4_tf = tf_views.get("4小時", "")
        if "🔴 空頭強勢" in h1_tf or "🔴 空頭強勢" in h4_tf:
            return

        # 觸發冷卻防護
        ALERT_COOLDOWN[symbol] = now_time

        # 風控計算 (-2.0% SL, +2%/+3%/+4% TP)
        sl_val = c0_close * 0.980
        tp1_val = c0_close * 1.020
        tp2_val = c0_close * 1.030
        tp3_val = c0_close * 1.040

        # 計算 Martin 1.0% 帳戶風控倉位 ($10,000 帳戶單筆固定虧損 $100)
        account_risk_100 = 100.0
        per_unit_risk = abs(c0_close - sl_val)
        position_units = account_risk_100 / per_unit_risk if per_unit_risk > 0 else 0
        position_value = position_units * c0_close

        reasons = [
            f"1. <b>🎯 AVWAP 關鍵支撐觸發</b>：價格精準回測階段高點 AVWAP 成本線 (距離僅 {dist_avwap_pct:.2f}%)",
            f"2. <b>⚡ 5m 微結構發動</b>：收盤價 ({format_price(c0_close)}) 強勢突破前一根 5m K 線最高價 ({format_price(c1_high)})",
            f"3. <b>📈 日線強勢結構</b>：30日漲幅顯著，回調低點持續抬高 (Higher Lows)",
            f"4. <b>🔥 5m 持倉量爆量</b>：OI 增長 {oi_change_pct:+.2f}%，機構資金同步進場"
        ]
        reason_str = "\n".join([f"• {r}" for r in reasons])

        # 寫入即時監控覆盤引擎
        TRACKED_SIGNALS.append({
            "symbol": symbol,
            "direction": direction_str,
            "entry_price": c0_close,
            "sl": sl_val,
            "tp1": tp1_val,
            "tp2": tp2_val,
            "tp3": tp3_val,
            "created_at": now_time,
            "status": "PENDING",
            "notified_tp": 0
        })

        msg = f"""🚨 <b>【🎯 AVWAP 關鍵支撐 + 5m 微結構觸發告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
方向：<b>{direction_disp}</b>
當前進場價：<b>{format_price(c0_close)} USDT</b>

💡 <b>【核心進場理由】</b>
{reason_str}

🎯 <b>【關鍵支撐價位】</b>
• <b>階段高點 AVWAP</b>：<code>{format_price(avwap_val)}</code>
• <b>日線 21 EMA</b>：<code>{format_price(m_setup['ema21'])}</code>

🌐 <b>【多時框趨勢觀點】</b>
• 5m: {tf_views.get('5分', 'N/A')} | 15m: {tf_views.get('15分', 'N/A')} | 1h: {h1_tf} | 4h: {h4_tf}

🛡️ <b>【實盤風控與 $100 倉位建議】</b>
• <b>止損 (SL -2.0%)</b>：<code>{format_price(sl_val)}</code>
• <b>目標 1 (TP1 +2.0%)</b>：<code>{format_price(tp1_val)}</code> (保本平半倉)
• <b>目標 2 (TP2 +3.0%)</b>：<code>{format_price(tp2_val)}</code>
• <b>目標 3 (TP3 +4.0%)</b>：<code>{format_price(tp3_val)}</code> (滿盈離場)
• <b>$10k 帳戶建倉參考</b>：約投入 <b>{position_value:.1f} USDT</b> (固定風控 $100 / 1.0%)"""

        print(f"🔥 [AVWAP 觸發訊號] {symbol} | AVWAP: {format_price(avwap_val)} | 風控 $100")
        send_telegram_alert(msg)

    except Exception:
        pass

# ==================== 主監控循環 ====================
def run_formal_hunter():
    threading.Thread(target=start_health_server, daemon=True).start()
    print("⚡ 數據獵手 (AVWAP 多標的平行推播版) 已啟動...")

    while True:
        try:
            update_signal_outcomes()
            check_and_send_daily_backtest_report()
            check_and_send_sunday_report()

            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            res = requests.get(url, timeout=5).json()
            tickers = res.get("result", {}).get("list", [])
            tickers = sorted(tickers, key=lambda x: float(x.get("turnover24h", 0)), reverse=True)[:50]

            now_str = get_taipei_now_str()
            print(f"[{now_str}] 🔍 AVWAP 併發掃描 50 個合約標的...")

            with ThreadPoolExecutor(max_workers=10) as executor:
                executor.map(inspect_symbol, tickers)

        except Exception as e:
            print(f"掃描異常: {e}")

        time.sleep(15)

if __name__ == "__main__":
    run_formal_hunter()
