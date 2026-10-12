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

# ==================== EMA 指數移動平均線計算 ====================
def calculate_ema(closes, period):
    if len(closes) < period:
        return None
    reversed_closes = list(reversed(closes))
    k = 2 / (period + 1)
    ema = reversed_closes[0]
    for price in reversed_closes[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

# ==================== 設定區 (實盤 10-15單/日 + 1.5%SL / 2%3%4%TP) ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6132673542")

# 實盤發單頻率調校 (目標每日 10-15 筆高品質單)
MIN_OI_GROWTH_PCT = 1.5      # 5m 持倉量暴增門檻 1.5%
MIN_VOLATILITY_PCT = 0.6     # 5m 真實波幅門檻 0.6%
MIN_PRICE_CHANGE_PCT = 0.4   # 5m 實體 K 線突破門檻 0.4%

# 防重複推播冷卻機制
ALERT_COOLDOWN = {}
COOLDOWN_SECONDS = 600       # 同一標的 10 分鐘內不重複發送

# 覆盤與推播紀錄
LAST_SUNDAY_REPORT_DATE = ""
LAST_DAILY_BACKTEST_DATE = ""
TRACKED_SIGNALS = []

# ==================== Health Check 服務器 ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("⚡ 數據獵手 (實盤 1.5%SL 80%勝率版) 24H 雲端監控中".encode('utf-8'))

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

# ==================== 多時框 (Multi-Timeframe) 趨勢分析 ====================
def get_multi_tf_views(symbol):
    tf_configs = [
        ('5', '5分'),
        ('15', '15分'),
        ('60', '1小時'),
        ('240', '4小時'),
        ('D', '日線')
    ]
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

# ==================== EMA 均線與 SMC/SNR 結構深度分析 ====================
def analyze_indicators(symbol):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=5&limit=210"
    try:
        resp = requests.get(url, timeout=3.5).json()
        if resp.get("retCode") != 0 or not resp.get("result", {}).get("list"):
            return None
        
        klines = resp["result"]["list"]
        if len(klines) < 200:
            return None

        closes = [float(k[4]) for k in klines]
        c0_close = closes[0]
        c0_high = float(klines[0][2])
        c0_low = float(klines[0][3])

        c1_open = float(klines[1][1])
        c1_high = float(klines[1][2])
        c1_low = float(klines[1][3])
        c1_close = float(klines[1][4])

        c2_high = float(klines[2][2])
        c2_low = float(klines[2][3])

        # 1. EMA 20, 50, 200
        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)
        ema200 = calculate_ema(closes, 200)

        # 2. SMC: FVG
        fvg_signal = "無顯著 FVG"
        if c0_low > c2_high:
            gap_pct = round(((c0_low - c2_high) / c2_high) * 100, 2)
            fvg_signal = f"🟢 看漲 FVG (+{gap_pct}%)"
        elif c0_high < c2_low:
            gap_pct = round(((c2_low - c0_high) / c2_low) * 100, 2)
            fvg_signal = f"🔴 看跌 FVG (-{gap_pct}%)"

        # 3. SMC: OB
        ob_signal = "一般區域"
        if c0_close > c1_high and c1_close < c1_open:
            ob_signal = f"📦 突破多頭 OB ({format_price(c1_low)}~{format_price(c1_high)})"
        elif c0_close < c1_low and c1_close > c1_open:
            ob_signal = f"📦 跌破空頭 OB ({format_price(c1_low)}~{format_price(c1_high)})"

        # 4. SNR: Pivot
        past_highs = [float(k[2]) for k in klines[1:21]]
        past_lows = [float(k[3]) for k in klines[1:21]]
        resistance = max(past_highs)
        support = min(past_lows)

        snr_status = "區間內震盪"
        if c0_close >= resistance:
            snr_status = f"🚀 突破關鍵壓力 ({format_price(resistance)})"
        elif c0_close <= support:
            snr_status = f"💥 跌破關鍵支撐 ({format_price(support)})"

        return {
            "fvg": fvg_signal,
            "ob": ob_signal,
            "snr_status": snr_status,
            "support": support,
            "resistance": resistance,
            "last_price": c0_close,
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200
        }
    except Exception:
        return None

# ==================== 三階止盈實盤自動覆盤引擎 ====================
def update_signal_outcomes():
    now_ts = time.time()
    for sig in TRACKED_SIGNALS:
        if sig["status"] == "EXPIRED" or sig["status"] == "LOSS":
            continue
        
        if (now_ts - sig["created_at"]) > 43200 and sig["status"] == "PENDING":
            sig["status"] = "EXPIRED"
            continue

        try:
            symbol = sig["symbol"]
            url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
            res = requests.get(url, timeout=3).json()
            ticker_data = res.get("result", {}).get("list", [{}])[0]
            if not ticker_data:
                continue

            last_price = float(ticker_data.get("lastPrice", 0))

            if sig["direction"] == "LONG":
                if last_price >= sig["tp3"]:
                    sig["status"] = "WIN_TP3"
                elif last_price >= sig["tp2"] and sig["status"] != "WIN_TP3":
                    sig["status"] = "WIN_TP2"
                elif last_price >= sig["tp1"] and sig["status"] not in ["WIN_TP2", "WIN_TP3"]:
                    sig["status"] = "WIN_TP1"
                elif last_price <= sig["sl"] and sig["status"] == "PENDING":
                    sig["status"] = "LOSS"
            else: # SHORT
                if last_price <= sig["tp3"]:
                    sig["status"] = "WIN_TP3"
                elif last_price <= sig["tp2"] and sig["status"] != "WIN_TP3":
                    sig["status"] = "WIN_TP2"
                elif last_price <= sig["tp1"] and sig["status"] not in ["WIN_TP2", "WIN_TP3"]:
                    sig["status"] = "WIN_TP1"
                elif last_price >= sig["sl"] and sig["status"] == "PENDING":
                    sig["status"] = "LOSS"

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

        msg = f"""📈 <b>【數據獵手 24H 實盤策略覆盤戰報】</b>

📅 統計日期：<code>{today_date_str}</code>

📊 <b>【實盤績效與盈虧比】</b>
• 總發單訊號數：<b>{total_cnt} 筆</b> (日目標 10-15 筆)
• TP1 達標 (2%)：<b>{tp1_cnt} 筆</b> ({tp1_cnt/total_cnt*100 if total_cnt else 0:.1f}%)
• TP2 達標 (3%)：<b>{tp2_cnt} 筆</b> ({tp2_cnt/total_cnt*100 if total_cnt else 0:.1f}%)
• TP3 滿盈 (4%)：<b>{tp3_cnt} 筆</b> ({tp3_cnt/total_cnt*100 if total_cnt else 0:.1f}%)
• SL 觸及止損 (-1.5%)：<b>{loss_cnt} 筆</b>
• 進行中 / 持倉：<b>{pending_cnt} 筆</b>

🎯 <b>【實盤勝率評估】</b>
• 策略勝率 (TP1 命中率)：<b>{win_rate:.1f}%</b> (目標 80%)

💡 <i>嚴格執行 1.5% 止損與 2%/3%/4% 分段止盈，獲利期望值極高！</i>"""

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
                indi = analyze_indicators(symbol)
                if not indi: continue

                report_msg = f"""📊 <b>【每週日主流幣合約週報】</b>

📌 <b>標的</b>：<code>{symbol}</code> ({name} 永續合約)
當前價格：<b>{format_price(indi['last_price'])} USDT</b>

🎯 <b>【關鍵結構與均線】</b>
• 強壓力位：<code>{format_price(indi['resistance'])}</code> | 強支撐位：<code>{format_price(indi['support'])}</code>
• 5m EMA 200：<code>{format_price(indi['ema200'])}</code>
• SMC FVG：{indi['fvg']} | SMC OB：{indi['ob']}

🌐 <b>【大時框趨勢】</b>
• 1小時：{tf_views.get('1小時', 'N/A')} | 4小時：{tf_views.get('4小時', 'N/A')} | 日線：{tf_views.get('日線', 'N/A')}"""

                send_telegram_alert(report_msg)
                time.sleep(2)
            except Exception:
                pass
        LAST_SUNDAY_REPORT_DATE = today_date_str

# ==================== 單一標的高勝率嚴格篩選 ====================
def inspect_symbol(ticker):
    symbol = ticker["symbol"]
    try:
        now_time = time.time()
        if symbol in ALERT_COOLDOWN and (now_time - ALERT_COOLDOWN[symbol]) < COOLDOWN_SECONDS:
            return

        # 1. 抓取 OI 數據
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

        # 2. 技術指標與 EMA 均線
        indi = analyze_indicators(symbol)
        if not indi: return

        c0_close = indi["last_price"]
        ema20 = indi["ema20"]
        ema50 = indi["ema50"]
        ema200 = indi["ema200"]

        if not (ema20 and ema50 and ema200): return

        # 3. 5m K 線體積與方向判斷
        kline_url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=5&limit=2"
        k_res = requests.get(kline_url, timeout=3).json()
        klines = k_res.get("result", {}).get("list", [])
        if not klines: return

        c0_open = float(klines[0][1])
        c0_high = float(klines[0][2])
        c0_low = float(klines[0][3])

        price_change_5m = ((c0_close - c0_open) / c0_open) * 100
        volatility_5m = ((c0_high - c0_low) / c0_open) * 100

        if volatility_5m < MIN_VOLATILITY_PCT: return

        if price_change_5m >= MIN_PRICE_CHANGE_PCT:
            is_bullish = True
            direction_str = "LONG"
            direction_disp = "🟢 主力合約開多 (Bullish)"
        elif price_change_5m <= -MIN_PRICE_CHANGE_PCT:
            is_bullish = False
            direction_str = "SHORT"
            direction_disp = "🔴 主力合約開空 (Bearish)"
        else:
            return

        # 【核心過濾 1：EMA 均線趨勢對齊】
        if is_bullish:
            if c0_close < ema200 or ema20 < ema50: return
        else:
            if c0_close > ema200 or ema20 > ema50: return

        # 【核心過濾 2：SMC / SNR 結構打破】
        fvg_text = indi["fvg"]
        ob_text = indi["ob"]
        snr_text = indi["snr_status"]

        has_fvg = ("看漲 FVG" in fvg_text) if is_bullish else ("看跌 FVG" in fvg_text)
        has_ob = ("突破多頭 OB" in ob_text) if is_bullish else ("跌破空頭 OB" in ob_text)
        has_snr = ("突破關鍵壓力" in snr_text) if is_bullish else ("跌破關鍵支撐" in snr_text)

        if not (has_fvg or has_ob or has_snr): return

        # 【核心過濾 3：1h/4h 大時框趨勢過濾】
        tf_views = get_multi_tf_views(symbol)
        h1_tf = tf_views.get("1小時", "")
        h4_tf = tf_views.get("4小時", "")

        if is_bullish:
            if "🔴 空頭強勢" in h1_tf or "🔴 空頭強勢" in h4_tf: return
        else:
            if "🟢 多頭強勢" in h1_tf or "🟢 多頭強勢" in h4_tf: return

        # 觸發冷卻防護
        ALERT_COOLDOWN[symbol] = now_time

        reasons = [
            f"1. 5m OI 暴增 {oi_change_pct:+.2f}%，機構大資金進場",
            f"2. 5m K 線強勢突破 {price_change_5m:+.2f}%",
            f"3. 順應 EMA 20/50/200 多空均線排列"
        ]
        if has_fvg: reasons.append(f"4. SMC 失衡區觸發 ({fvg_text})")
        if has_ob: reasons.append(f"5. SMC 訂單塊突破 ({ob_text})")
        if has_snr: reasons.append(f"6. SNR 結構打破 ({snr_text})")

        reason_str = "\n".join([f"• {r}" for r in reasons])

        # 【實盤精準風控：固定 1.5% 止損 | 2%、3%、4% 止盈】
        sl_val = c0_close * 0.985 if is_bullish else c0_close * 1.015
        tp1_val = c0_close * 1.020 if is_bullish else c0_close * 0.980
        tp2_val = c0_close * 1.030 if is_bullish else c0_close * 0.970
        tp3_val = c0_close * 1.040 if is_bullish else c0_close * 0.960

        # 將實盤訊號寫入自動覆盤
        TRACKED_SIGNALS.append({
            "symbol": symbol,
            "direction": direction_str,
            "entry_price": c0_close,
            "sl": sl_val,
            "tp1": tp1_val,
            "tp2": tp2_val,
            "tp3": tp3_val,
            "created_at": now_time,
            "status": "PENDING"
        })

        msg = f"""🚨 <b>【實盤第一拍 + 80% 勝率模組告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
方向：<b>{direction_disp}</b>
當前進場價：<b>{format_price(c0_close)} USDT</b>

💡 <b>【核心進場理由】</b>
{reason_str}

📈 <b>【EMA 均線結構】</b>
• 5m EMA 20 / 50 / 200：<code>{format_price(ema20)}</code> / <code>{format_price(ema50)}</code> / <code>{format_price(ema200)}</code>

🌐 <b>【多時框趨勢觀點】</b>
• 5m: {tf_views.get('5分', 'N/A')} | 15m: {tf_views.get('15分', 'N/A')} | 1h: {h1_tf} | 4h: {h4_tf}

🎯 <b>【SMC & SNR 結構】</b>
• FVG: {fvg_text} | OB: {ob_text} | 狀態: {snr_text}

🛡️ <b>【實盤風控點位】</b>
• <b>止損 (SL -1.5%)</b>：<code>{format_price(sl_val)}</code>
• <b>目標 1 (TP1 +2%)</b>：<code>{format_price(tp1_val)}</code> (保本平半倉)
• <b>目標 2 (TP2 +3%)</b>：<code>{format_price(tp2_val)}</code>
• <b>目標 3 (TP3 +4%)</b>：<code>{format_price(tp3_val)}</code> (滿盈離場)"""

        print(f"🔥 [實盤訊號發送] {symbol} | OI: {oi_change_pct:+.2f}% | SL -1.5%")
        send_telegram_alert(msg)

    except Exception:
        pass

# ==================== 主監控循環 ====================
def run_formal_hunter():
    threading.Thread(target=start_health_server, daemon=True).start()
    print("⚡ 數據獵手 [實盤 1.5%SL 80%勝率版] 已啟動...")

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
            print(f"[{now_str}] 🔍 併發掃描 50 個永續合約標的...")

            with ThreadPoolExecutor(max_workers=10) as executor:
                executor.map(inspect_symbol, tickers)

        except Exception as e:
            print(f"掃描異常: {e}")

        time.sleep(15)

if __name__ == "__main__":
    run_formal_hunter()
