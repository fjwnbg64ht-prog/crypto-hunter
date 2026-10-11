import os
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==================== 設定區 ====================
TELEGRAM_BOT_TOKEN = "7832628631:AAEE_1IvF5iU"  # 你的 Telegram Bot Token
TELEGRAM_CHAT_ID = "1879133649"         # 你的 Telegram Chat ID

# 策略第一拍過濾門檻
MIN_OI_GROWTH_PCT = 1.2    # 5m持倉量增長 (%)
MIN_RVOL = 2.0             # 成交量倍數 (RVOL)
MIN_VOLATILITY_PCT = 0.5   # 5m波幅 (%)

# 防重複推播冷卻機制 (幣種: 上次觸發時間)
ALERT_COOLDOWN = {}
COOLDOWN_SECONDS = 300     # 同一標的 5 分鐘內不重複發送

# ==================== Health Check 服務器 ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("⚡ 數據獵手 (旗艦穩定版) 24H 雲端監控中".encode('utf-8'))

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# ==================== Telegram 推播 (含調試日誌與安全格式) ====================
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ 未設定 Telegram Token 或 Chat ID")
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

# ==================== SMC 與 SNR 技術指標分析 ====================
def analyze_smc_snr(symbol):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=5&limit=30"
    try:
        resp = requests.get(url, timeout=3).json()
        if resp.get("retCode") != 0 or not resp.get("result", {}).get("list"):
            return None
        
        klines = resp["result"]["list"]
        if len(klines) < 20:
            return None

        c0_high = float(klines[0][2])
        c0_low = float(klines[0][3])
        c0_close = float(klines[0][4])
        
        c1_open = float(klines[1][1])
        c1_high = float(klines[1][2])
        c1_low = float(klines[1][3])
        c1_close = float(klines[1][4])
        
        c2_high = float(klines[2][2])
        c2_low = float(klines[2][3])

        # 1. SMC: FVG
        fvg_signal = "無顯著 FVG"
        if c0_low > c2_high:
            gap_pct = round(((c0_low - c2_high) / c2_high) * 100, 2)
            fvg_signal = f"🟢 看漲 FVG (+{gap_pct}%)"
        elif c0_high < c2_low:
            gap_pct = round(((c2_low - c0_high) / c2_low) * 100, 2)
            fvg_signal = f"🔴 看跌 FVG (-{gap_pct}%)"

        # 2. SMC: OB
        ob_signal = "一般區域"
        if c0_close > c1_high and c1_close < c1_open:
            ob_signal = f"📦 突破多頭 OB ({c1_low}~{c1_high})"
        elif c0_close < c1_low and c1_close > c1_open:
            ob_signal = f"📦 跌破空頭 OB ({c1_low}~{c1_high})"

        # 3. SNR: 20K Pivot
        past_highs = [float(k[2]) for k in klines[1:21]]
        past_lows = [float(k[3]) for k in klines[1:21]]
        resistance = max(past_highs)
        support = min(past_lows)

        snr_status = "區間內震盪"
        if c0_close >= resistance:
            snr_status = f"🚀 突破 20K 壓力 ({resistance})"
        elif c0_close <= support:
            snr_status = f"💥 跌破 20K 支撐 ({support})"
        else:
            near_res_pct = abs(c0_close - resistance) / c0_close * 100
            near_sup_pct = abs(c0_close - support) / c0_close * 100
            if near_res_pct < 0.3:
                snr_status = f"⚠️ 逼近壓力位 ({resistance})"
            elif near_sup_pct < 0.3:
                snr_status = f"🛡️ 逼近支撐位 ({support})"

        return {
            "fvg": fvg_signal,
            "ob": ob_signal,
            "snr_status": snr_status,
            "support": support,
            "resistance": resistance
        }
    except Exception:
        return None

# ==================== 單一標的掃描 ====================
def inspect_symbol(ticker):
    symbol = ticker["symbol"]
    try:
        now_time = time.time()
        # 檢查冷卻時間
        if symbol in ALERT_COOLDOWN and (now_time - ALERT_COOLDOWN[symbol]) < COOLDOWN_SECONDS:
            return

        oi_url = f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime=5min&limit=2"
        vol_url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"

        oi_res = requests.get(oi_url, timeout=3).json()
        vol_res = requests.get(vol_url, timeout=3).json()

        oi_list = oi_res.get("result", {}).get("list", [])
        ticker_data = vol_res.get("result", {}).get("list", [{}])[0]

        if len(oi_list) < 2 or not ticker_data:
            return

        oi_now = float(oi_list[0]["openInterest"])
        oi_prev = float(oi_list[1]["openInterest"])
        oi_change_pct = ((oi_now - oi_prev) / oi_prev) * 100 if oi_prev > 0 else 0

        last_price = float(ticker_data.get("lastPrice", 0))
        high_price = float(ticker_data.get("highPrice24h", last_price))
        low_price = float(ticker_data.get("lowPrice24h", last_price))
        turnover_24h = float(ticker_data.get("turnover24h", 0))

        volatility_5m = ((high_price - low_price) / last_price) * 100 if last_price > 0 else 0

        # 第一拍條件過濾
        if abs(oi_change_pct) >= MIN_OI_GROWTH_PCT and volatility_5m >= MIN_VOLATILITY_PCT:
            is_bullish = oi_change_pct > 0
            direction = "🟢 主力開多 (Bullish)" if is_bullish else "🔴 主力開空 (Bearish)"
            
            # 更新冷卻時間
            ALERT_COOLDOWN[symbol] = now_time

            # SMC & SNR 分析
            smc_snr = analyze_smc_snr(symbol)
            fvg_text = smc_snr["fvg"] if smc_snr else "無"
            ob_text = smc_snr["ob"] if smc_snr else "無"
            snr_text = smc_snr["snr_status"] if smc_snr else "無"
            sup_val = smc_snr["support"] if smc_snr else "N/A"
            res_val = smc_snr["resistance"] if smc_snr else "N/A"

            # 多時框觀點
            tf_views = get_multi_tf_views(symbol)

            # 整理進場理由
            reasons = [f"1. 5m OI 爆量卡位 {oi_change_pct:+.2f}%，主力資金入場"]
            if "看漲 FVG" in fvg_text or "看跌 FVG" in fvg_text:
                reasons.append(f"2. SMC 留下失衡區 ({fvg_text})")
            if "突破" in snr_text or "跌破" in snr_text:
                reasons.append(f"3. SNR 突破關鍵結構 ({snr_text})")
            
            higher_tf_bull = "多頭" in tf_views.get("1小時", "") or "多頭" in tf_views.get("4小時", "")
            if is_bullish and higher_tf_bull:
                reasons.append("4. 順應 1h/4h 大時框多頭主趨勢")
            elif not is_bullish and not higher_tf_bull:
                reasons.append("4. 順應 1h/4h 大時框空頭主趨勢")
            
            reason_str = "\n".join([f"• {r}" for r in reasons])

            # 風控建議
            sl = round(last_price * 0.985, 4) if is_bullish else round(last_price * 1.015, 4)
            tp1 = round(last_price * 1.020, 4) if is_bullish else round(last_price * 0.980, 4)
            tp2 = round(last_price * 1.030, 4) if is_bullish else round(last_price * 0.970, 4)

            # 簡潔乾淨的 HTML 訊息
            msg = f"""🚨 <b>【主力第一拍 + 多時框進場告警】</b>

📌 <b>標的</b>：<code>{symbol}</code>
方向：<b>{direction}</b>
當前價格：<b>{last_price}</b>

💡 <b>【核心進場理由】</b>
{reason_str}

🌐 <b>【多時框趨勢觀點】</b>
• 5 分鐘：{tf_views.get('5分', 'N/A')}
• 15 分鐘：{tf_views.get('15分', 'N/A')}
• 1 小時：{tf_views.get('1小時', 'N/A')}
• 4 小時：{tf_views.get('4小時', 'N/A')}
• 日線：{tf_views.get('日線', 'N/A')}

🎯 <b>【SMC & SNR 結構】</b>
• SMC FVG 失衡區：{fvg_text}
• SMC OB 訂單塊：{ob_text}
• SNR 支撐壓力：{snr_text}
• 阻力 / 支撐：<code>{res_val}</code> / <code>{sup_val}</code>

🛡️ <b>【風控參考】</b>
• 止損 (SL)：<code>{sl}</code>
• 目標 1 (TP1)：<code>{tp1}</code> | 目標 2 (TP2)：<code>{tp2}</code>"""

            print(f"🔥 [訊號觸發] {symbol} | OI: {oi_change_pct:.2f}% | 開始發送 Telegram...")
            send_telegram_alert(msg)

    except Exception:
        pass

# ==================== 主監控循環 ====================
def run_formal_hunter():
    threading.Thread(target=start_health_server, daemon=True).start()
    print("⚡ 數據獵手 [旗艦穩定版] 已啟動...")

    while True:
        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            res = requests.get(url, timeout=5).json()
            tickers = res.get("result", {}).get("list", [])

            tickers = sorted(tickers, key=lambda x: float(x.get("turnover24h", 0)), reverse=True)[:50]

            now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            print(f"[{now_str}] 🔍 併發掃描 50 個標的 (多時框 + SMC + SNR)...")

            with ThreadPoolExecutor(max_workers=10) as executor:
                executor.map(inspect_symbol, tickers)

        except Exception as e:
            print(f"掃描異常: {e}")

        time.sleep(15)

if __name__ == "__main__":
    run_formal_hunter()
