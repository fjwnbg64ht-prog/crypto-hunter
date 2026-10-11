import os
import time
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= 第一拍大資金捕捉參數 =================
TELEGRAM_BOT_TOKEN = "8716746659:AAEZzTH-mPNA5U7iApmL3sXazS2S1IvF5iU"
TELEGRAM_CHAT_ID = "6132673542"

MIN_24H_TURNOVER = 5_000_000   # 過濾 24H 成交額 < 500萬 USDT 的低流動性幣種
TOP_CANDIDATES_LIMIT = 50      # 取成交量前 50 名最活躍標的進行精細監控

OI_GROWTH_THRESHOLD = 1.2      # 5m OI (未平倉量) 增幅 > 1.2%
RVOL_THRESHOLD = 2.0           # 5m 成交量 > 過去20期平均 2.0 倍
PRICE_CHANGE_THRESHOLD = 0.5   # 5m 價格波幅 > 0.5%

CHECK_INTERVAL = 15            # 15 秒高頻輪詢
MAX_WORKERS = 10               # 併發線程數
# =======================================================

BYBIT_BASE = "https://api.bybit.com"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
})

# 啟動輕量背景 Web Server 應對 Render 健康檢查
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("⚡ 數據獵手 24H 雲端監控中".encode('utf-8'))

    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

def get_tw_time():
    tw_tz = timezone(timedelta(hours=8))
    return datetime.now(tw_tz).strftime('%Y-%m-%d %H:%M:%S')

def fmt_p(p):
    if p >= 100: return f"{p:.2f}"
    if p >= 1: return f"{p:.4f}"
    return f"{p:.6f}"

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        session.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"❌ Telegram 推播失敗: {e}")

def get_all_active_symbols():
    url = f"{BYBIT_BASE}/v5/market/tickers?category=linear"
    try:
        resp = session.get(url, timeout=5)
        if resp.status_code != 200: return []
        data = resp.json()
        if data.get('retCode') != 0: return []
        
        ticker_list = data.get('result', {}).get('list', [])
        valid_items = [
            item for item in ticker_list
            if isinstance(item, dict)
            and item.get('symbol', '').endswith('USDT')
            and float(item.get('turnover24h', 0)) >= MIN_24H_TURNOVER
        ]
        
        valid_items.sort(key=lambda x: float(x.get('turnover24h', 0)), reverse=True)
        return [item['symbol'] for item in valid_items[:TOP_CANDIDATES_LIMIT]]
    except Exception as e:
        print(f"❌ 獲取全市場清單失敗: {e}")
        return []

def analyze_symbol(symbol):
    try:
        kline_url = f"{BYBIT_BASE}/v5/market/kline?category=linear&symbol={symbol}&interval=5&limit=21"
        resp = session.get(kline_url, timeout=3)
        if resp.status_code != 200: return None
        
        klines = resp.json().get('result', {}).get('list', [])
        if not isinstance(klines, list) or len(klines) < 21: return None
        
        vols = [float(k[5]) for k in klines[1:]]
        curr_vol = float(klines[0][5])
        avg_vol = sum(vols) / len(vols) if len(vols) > 0 else 1
        rvol = curr_vol / avg_vol if avg_vol > 0 else 0
        
        open_p = float(klines[0][1])
        close_p = float(klines[0][4])
        price_change = ((close_p - open_p) / open_p) * 100

        if rvol < RVOL_THRESHOLD or abs(price_change) < PRICE_CHANGE_THRESHOLD:
            return None

        oi_url = f"{BYBIT_BASE}/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime=5min&limit=2"
        oi_resp = session.get(oi_url, timeout=3)
        if oi_resp.status_code != 200: return None
        
        oi_list = oi_resp.json().get('result', {}).get('list', [])
        if not isinstance(oi_list, list) or len(oi_list) < 2: return None
        
        curr_oi = float(oi_list[0]['openInterest'])
        prev_oi = float(oi_list[1]['openInterest'])
        if prev_oi <= 0: return None
        oi_change = ((curr_oi - prev_oi) / prev_oi) * 100

        if oi_change >= OI_GROWTH_THRESHOLD:
            if price_change > 0:
                direction_tag = "🟢 偏多確認 (LONG)"
                sl = close_p * (1 - 0.015)
                tp1 = close_p * (1 + 0.020)
                tp2 = close_p * (1 + 0.030)
                tp3 = close_p * (1 + 0.040)
            else:
                direction_tag = "🔴 偏空確認 (SHORT)"
                sl = close_p * (1 + 0.015)
                tp1 = close_p * (1 - 0.020)
                tp2 = close_p * (1 - 0.030)
                tp3 = close_p * (1 - 0.040)

            highs = [float(k[2]) for k in klines[1:]]
            lows = [float(k[3]) for k in klines[1:]]

            return {
                'symbol': symbol,
                'direction': direction_tag,
                'entry_price': close_p,
                'price_change': price_change,
                'rvol': rvol,
                'oi_change': oi_change,
                'sl': sl,
                'tp1': tp1,
                'tp2': tp2,
                'tp3': tp3,
                'resistance': max(highs),
                'support': min(lows)
            }
    except:
        pass
    return None

def run_formal_hunter():
    threading.Thread(target=start_health_server, daemon=True).start()
    
    print("⚡ 數據獵手 [Render Web 實戰版] 已啟動...")
    print("⚙️ 風控目標：SL 1.5% | TP1 2.0% | TP2 3.0% | TP3 4.0%")
    print(f"📌 第一拍門檻：5m OI > {OI_GROWTH_THRESHOLD}% | RVOL > {RVOL_THRESHOLD}x | 5m 波幅 > {PRICE_CHANGE_THRESHOLD}%\n")
    
    while True:
        symbols = get_all_active_symbols()
        now_str = get_tw_time()
        print(f"[{now_str}] 🔍 正在併發掃描熱門標的 {len(symbols)} 個...")
        
        triggered_results = []
        if symbols:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_symbol = {executor.submit(analyze_symbol, s): s for s in symbols}
                for future in as_completed(future_to_symbol):
                    res = future.result()
                    if res: triggered_results.append(res)
        
        if triggered_results:
            print(f"\n🎯 [{now_str}] 發現 {len(triggered_results)} 個【第一拍大資金流入】標的！")
            triggered_results.sort(key=lambda x: x['oi_change'], reverse=True)
            
            for item in triggered_results:
                alert_msg = (
                    f"⚡ **[數據獵手 - 第一拍大資金入場]** ⚡\n"
                    f"📌 **標的**: `{item['symbol']}`\n"
                    f"🧭 **訊號方向**: `{item['direction']}`\n"
                    f"💵 **建議進場價**: `{fmt_p(item['entry_price'])}` USDT\n"
                    f"----------------------------\n"
                    f"📈 **5m OI 增幅**: `{item['oi_change']:+.2f}%` 🚨 (主力第一拍進場)\n"
                    f"📊 **5m 爆量倍數**: `{item['rvol']:.2f}x` RVOL\n"
                    f"⚡ **5m 價格波幅**: `{item['price_change']:+.2f}%`\n"
                    f"----------------------------\n"
                    f"🎯 **止盈規劃 (TP)**:\n"
                    f"  • TP1 (2.0%): `{fmt_p(item['tp1'])}`\n"
                    f"  • TP2 (3.0%): `{fmt_p(item['tp2'])}`\n"
                    f"  • TP3 (4.0%): `{fmt_p(item['tp3'])}` \n\n"
                    f"🛡 **止損規劃 (SL)**:\n"
                    f"  • SL  (1.5%): `{fmt_p(item['sl'])}`\n\n"
                    f"📍 **5m/20期 結構位**:\n"
                    f"  • 近期壓力 (R): `{fmt_p(item['resistance'])}`\n"
                    f"  • 近期支撐 (S): `{fmt_p(item['support'])}`\n"
                    f"----------------------------\n"
                    f"🔗 [Bybit 交易](https://www.bybit.com/trade/usdt/{item['symbol']})"
                )
                send_telegram_alert(alert_msg)
        else:
            print(f"💤 [{now_str}] 掃描完畢，暫無大資金第一拍進場標的。")
            
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_formal_hunter()
