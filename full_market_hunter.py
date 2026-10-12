import os
import time
import json
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

def clean_symbol(symbol_str):
    """ 清理 TradingView 傳來的 ticker 名稱 (例如 BYBIT:BTCUSDT.P -> BTCUSDT) """
    s = symbol_str.upper()
    if ":" in s:
        s = s.split(":")[-1]
    if "." in s:
        s = s.split(".")[0]
    return s

# ==================== 設定區 (TradingView Webhook + $100 風控) ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6132673542")

ALERT_COOLDOWN = {}
COOLDOWN_SECONDS = 300       # 同一標的 5 分鐘內不重複發送

LAST_SUNDAY_REPORT_DATE = ""
LAST_DAILY_BACKTEST_DATE = ""
TRACKED_SIGNALS = []

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
            print(f"❌ Telegram 發送失敗: {res_json.get('description')}")
        else:
            print("✅ Telegram Webhook 警報成功推播！")
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

# ==================== 處理來自 TradingView 的 Webhook 警報 ====================
def process_tradingview_alert(data):
    try:
        raw_symbol = str(data.get("symbol", ""))
        symbol = clean_symbol(raw_symbol)
        if not symbol: return

        now_time = time.time()
        if symbol in ALERT_COOLDOWN and (now_time - ALERT_COOLDOWN[symbol]) < COOLDOWN_SECONDS:
            print(f"⏳ [{symbol}] 冷卻中，跳過重複 Webhook 推播")
            return

        entry_price = float(data.get("price", 0))
        if entry_price <= 0:
            # 若 TV 沒傳 price，向 Bybit 查詢最新價
            url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
            res = requests.get(url, timeout=3).json()
            entry_price = float(res["result"]["list"][0]["lastPrice"])

        ALERT_COOLDOWN[symbol] = now_time

        # 風控參數 (-2.0% SL, +2%/+3%/+4% TP)
        sl_val = entry_price * 0.980
        tp1_val = entry_price * 1.020
        tp2_val = entry_price * 1.030
        tp3_val = entry_price * 1.040

        # 精算 $10,000 帳戶固定虧損 $100 (1.0% 風控比例) 的建倉金額
        account_risk_100 = 100.0
        per_unit_risk = abs(entry_price - sl_val)
        position_units = account_risk_100 / per_unit_risk if per_unit_risk > 0 else 0
        position_value = position_units * entry_price

        # 多時框觀點
        tf_views = get_multi_tf_views(symbol)

        reasons = [
            f"1. <b>📡 TradingView 訊號觸發</b>：AVWAP + 5m 微結構突破觸發",
            f"2. <b>🎯 觸發價格</b>：<code>{format_price(entry_price)} USDT</code>",
            f"3. <b>🛡️ 嚴格風控</b>：單筆固定虧損 $100 (1.0% Risk)"
        ]
        reason_str = "\n".join([f"• {r}" for r in reasons])

        # 加入 24H 戰報追蹤引擎
        TRACKED_SIGNALS.append({
            "symbol": symbol,
            "direction": "LONG",
            "entry_price": entry_price,
            "sl": sl_val,
            "tp1": tp1_val,
            "tp2": tp2_val,
            "tp3": tp3_val,
            "created_at": now_time,
            "status": "PENDING",
            "notified_tp": 0
        })

        msg = f"""🚨 <b>【📈 TradingView AVWAP 微結構觸發告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
方向：<b>🟢 多頭回調買入 (Bullish)</b>
當前進場價：<b>{format_price(entry_price)} USDT</b>

💡 <b>【訊號摘要】</b>
{reason_str}

🌐 <b>【多時框趨勢觀點】</b>
• 5m: {tf_views.get('5分', 'N/A')} | 15m: {tf_views.get('15分', 'N/A')} | 1h: {tf_views.get('1小時', 'N/A')} | 4h: {tf_views.get('4小時', 'N/A')}

🛡️ <b>【實盤風控與 $100 倉位建議】</b>
• <b>止損 (SL -2.0%)</b>：<code>{format_price(sl_val)}</code>
• <b>目標 1 (TP1 +2.0%)</b>：<code>{format_price(tp1_val)}</code> (保本平半倉)
• <b>目標 2 (TP2 +3.0%)</b>：<code>{format_price(tp2_val)}</code>
• <b>目標 3 (TP3 +4.0%)</b>：<code>{format_price(tp3_val)}</code> (滿盈離場)
• <b>$10k 帳戶建倉參考</b>：約投入 <b>{position_value:.1f} USDT</b> (固定風控 $100 / 1.0%)"""

        print(f"🔥 [TradingView Webhook 觸發] {symbol} @ {entry_price} | 風控 $100")
        send_telegram_alert(msg)

    except Exception as e:
        print(f"❌ 處理 TradingView 警報時發生錯誤: {e}")

# ==================== Health Check & Webhook 接收服務器 ====================
class WebhookAndHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("⚡ 數據獵手 (TradingView Webhook 接收端) 運行中".encode('utf-8'))

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            # 解析 TradingView 傳來的 JSON Payload
            data = json.loads(post_data.decode('utf-8'))
            print(f"📩 收到 Webhook Payload: {data}")
            
            # 在背景線程處理，不阻塞 HTTP 回應
            threading.Thread(target=process_tradingview_alert, args=(data,)).start()

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), WebhookAndHealthHandler)
    server.serve_forever()

# ==================== 三階止盈與覆盤引擎 ====================
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
            entry_price = sig["entry_price"]
            
            url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
            res = requests.get(url, timeout=3).json()
            ticker_data = res.get("result", {}).get("list", [{}])[0]
            if not ticker_data: continue

            last_price = float(ticker_data.get("lastPrice", 0))

            # SL (-2.0%)
            if last_price <= sig["sl"] and sig["status"] == "PENDING":
                sig["status"] = "LOSS"
                loss_msg = f"""❌ <b>【TradingView 策略 - 單筆止損告警】</b>

📌 <b>標的</b>：<code>{symbol}</code> (永續合約)
進場價格：<b>{format_price(entry_price)} USDT</b>
觸發止損價：<b>{format_price(last_price)} USDT</b> (-2.0%)

⚠️ <i>已觸發 -2.0% 紀律止損，嚴格控管 $100 風險！</i>"""
                send_telegram_alert(loss_msg)

            # TP3 (+4.0%)
            elif last_price >= sig["tp3"] and sig["notified_tp"] < 3:
                sig["status"] = "WIN_TP3"
                sig["notified_tp"] = 3
                send_telegram_alert(f"🚀 <b>【TradingView 策略】</b> <code>{symbol}</code> 達標 TP3 (+4.0%) 滿盈離場！")

            # TP2 (+3.0%)
            elif last_price >= sig["tp2"] and sig["notified_tp"] < 2:
                sig["status"] = "WIN_TP2"
                sig["notified_tp"] = 2
                send_telegram_alert(f"🎯 <b>【TradingView 策略】</b> <code>{symbol}</code> 達標 TP2 (+3.0%)！")

            # TP1 (+2.0%)
            elif last_price >= sig["tp1"] and sig["notified_tp"] < 1:
                sig["status"] = "WIN_TP1"
                sig["notified_tp"] = 1
                send_telegram_alert(f"✅ <b>【TradingView 策略】</b> <code>{symbol}</code> 達標 TP1 (+2.0%)！建議保本平半倉！")

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

        msg = f"""📈 <b>【TradingView AVWAP 策略 - 24H 實盤戰報】</b>

📅 統計日期：<code>{today_date_str}</code>

📊 <b>【實盤績效統計】</b>
• 總接收訊號數：<b>{total_cnt} 筆</b>
• TP1 命中 (+2%)：<b>{tp1_cnt} 筆</b>
• TP2 命中 (+3%)：<b>{tp2_cnt} 筆</b>
• TP3 滿盈 (+4%)：<b>{tp3_cnt} 筆</b>
• SL 止損 (-2.0%)：<b>{loss_cnt} 筆</b>
• 持倉進行中：<b>{pending_cnt} 筆</b>

🎯 <b>【實盤勝率】</b>
• 策略勝率 (TP1 達標率)：<b>{win_rate:.1f}%</b>

💡 <i>專注 TradingView 精準訊號 + 0.5%/1.0% 風控算手數！</i>"""

        send_telegram_alert(msg)
        LAST_DAILY_BACKTEST_DATE = today_date_str

# ==================== 主服務循環 ====================
def run_formal_hunter():
    threading.Thread(target=start_health_server, daemon=True).start()
    print("⚡ 數據獵手 (TradingView Webhook 接收端) 已啟動...")

    while True:
        try:
            # 輪詢更新持倉與止盈止損狀況
            update_signal_outcomes()
            check_and_send_daily_backtest_report()
        except Exception as e:
            print(f"覆盤循環異常: {e}")

        time.sleep(10)

if __name__ == "__main__":
    run_formal_hunter()
