import os
import time
import json
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from flask import Flask

# --- 環境變數與參數設定 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "你的_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "你的_TELEGRAM_CHAT_ID")
RISK_USD = 100.0  # 固定 $100 風控

SYMBOLS = ["ETHUSDT", "BTCUSDT", "SOLUSDT"]
ALERTED_SIGNALS = {}
LOG_FILE = "trade_log.json"

app = Flask(__name__)

@app.route("/")
def health_check():
    return "Market Hunter Active Scanner with SMC/SnR & Journaling is Running!"

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram Token 或 Chat ID 未設定")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"❌ Telegram 推播失敗: {e}")

def fetch_klines(symbol, interval="5m", limit=100):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    res = requests.get(url, timeout=10)
    data = res.json()
    
    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base_vol", "taker_quote_vol", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df

def calculate_indicators(df):
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    lookback = 50
    recent_df = df.tail(lookback)
    highest_idx = recent_df['high'].idxmax()
    
    anchor_df = df.loc[highest_idx:].copy()
    anchor_df['typical_price'] = (anchor_df['high'] + anchor_df['low'] + anchor_df['close']) / 3
    anchor_df['pv'] = anchor_df['typical_price'] * anchor_df['volume']
    
    cum_pv = anchor_df['pv'].cumsum()
    cum_vol = anchor_df['volume'].cumsum()
    anchor_df['avwap'] = cum_pv / cum_vol
    
    df['avwap'] = anchor_df['avwap']
    df['avwap'] = df['avwap'].ffill()
    return df

def check_smc_snr_confluence(df):
    """檢測 SMC (OB / FVG) 與 Key SnR 結構共振"""
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    confluences = []
    
    # 1. SMC FVG (Fair Value Gap 失衡區)
    if prev2['high'] < curr['low']:
        confluences.append("⚡ SMC 需求缺口 (FVG)")
        
    # 2. SMC Bullish Order Block (需求塊 OB)
    if prev['close'] < prev['open'] and curr['close'] > prev['high']:
        confluences.append("📦 SMC 機構需求塊 (OB)")
        
    # 3. Key SnR 結構支撐 (對照近 30 根 Pivot Low)
    recent_pivot_low = df['low'].tail(30).min()
    if abs(curr['low'] - recent_pivot_low) / curr['close'] <= 0.003:
        confluences.append("🎯 Key SnR 結構前高/前低支撐")
        
    if not confluences:
        return "📌 動能支撐 (未有顯著 SMC/SnR 疊加)"
    return " + ".join(confluences)

def log_trade(trade_data):
    """將發出的訊號寫入本地交易日誌，供每日損復盤使用"""
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            logs = []
    
    logs.append(trade_data)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

def generate_daily_review_template():
    """生成每日損復盤範本推播"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    msg = (
        f"📋 *【Martin 每日交易復盤卡片】* ({today_str})\n"
        f"───────────────────────\n"
        f"請針對今日觸發或執行的單子進行虧損與優化檢討：\n\n"
        f"1️⃣ *虧損單標的/單號*：`[例如：ETHUSDT - 12:30 單]`\n"
        f"2️⃣ *預估/實際虧損*：`-$100 USDT`\n"
        f"3️⃣ *止損核心原因*（請勾選）：\n"
        f"   [ ] 價格超出進場區間仍追高\n"
        f"   [ ] SMC 需求區 (OB/FVG) 被強勢跌破\n"
        f"   [ ] 順勢趨勢弱化（大盤 BTC 拖累）\n"
        f"   [ ] 假突破 / 流動性掃樓 (Liquidation Sweep)\n"
        f"4️⃣ *檢討與改善動作*：\n"
        f"   `下一次需等待 15m 結構確認，不上衝區間頂部掛單。`"
    )
    send_telegram(msg)

def scan_market():
    print("🚀 【主動盤面獵手 + SMC/SnR 驗證 + 復盤系統】啟動中...")
    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_klines(symbol)
                df = calculate_indicators(df)
                
                curr = df.iloc[-1]
                prev = df.iloc[-2]
                
                avwap = curr['avwap']
                ema21 = curr['ema21']
                ema50 = curr['ema50']
                ema9 = curr['ema9']
                
                valid_support = min(ema21, avwap) if not np.isnan(avwap) else curr['close']
                
                # 策略進場條件
                touch_support = (prev['low'] <= valid_support * 1.005) and (prev['close'] >= valid_support * 0.985)
                trend_filter = (curr['close'] > ema50) or (ema9 > ema21)
                breakout_confirm = (curr['close'] > prev['high']) and (curr['close'] > curr['open'])
                
                if touch_support and trend_filter and breakout_confirm:
                    signal_key = f"{symbol}_{curr['open_time']}"
                    
                    if signal_key not in ALERTED_SIGNALS:
                        ALERTED_SIGNALS[signal_key] = True
                        
                        # 1. 進場價格區間計算
                        entry_low = valid_support
                        entry_high = curr['close'] * 1.0015
                        
                        # 2. SMC / SnR 關鍵位驗證
                        smc_snr_tag = check_smc_snr_confluence(df)
                        
                        # 3. 風控與止損金額計算
                        sl_price = min(prev['low'], valid_support * 0.995)
                        risk_per_unit = curr['close'] - sl_price
                        
                        if risk_per_unit > 0:
                            qty = RISK_USD / risk_per_unit
                            position_usd = qty * curr['close']
                            tp1 = curr['close'] + (risk_per_unit * 1.5)
                            tp2 = curr['close'] + (risk_per_unit * 2.0)
                            tp3 = curr['close'] + (risk_per_unit * 3.0)
                            
                            trade_id = f"TRADE-{int(time.time()) % 10000}"
                            
                            msg = (
                                f"🎯 *【Martin 盤面主動訊號】* (`{trade_id}`)\n\n"
                                f"📌 *標的*: `{symbol}` (5m)\n"
                                f"🎯 *進場價格區間*: `{entry_low:.2f} ~ {entry_high:.2f}`\n"
                                f"🛑 *止損價格*: `{sl_price:.2f}` (固定風控 $100)\n"
                                f"📦 *建議手數*: `{qty:.4f}` (`{position_usd:.1f}` USDT)\n\n"
                                f"🔍 *關鍵位驗證*: \n  • {smc_snr_tag}\n\n"
                                f"📈 *止盈目標*:\n"
                                f"  • TP1 (1:1.5): `{tp1:.2f}`\n"
                                f"  • TP2 (1:2.0): `{tp2:.2f}`\n"
                                f"  • TP3 (1:3.0): `{tp3:.2f}`\n\n"
                                f"⚠️ *紀律提醒*: 現價超出 `{entry_high:.2f}` **嚴格禁止追單**！"
                            )
                            send_telegram(msg)
                            
                            # 記錄至日誌檔
                            log_trade({
                                "trade_id": trade_id,
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "symbol": symbol,
                                "entry_range": [round(entry_low, 2), round(entry_high, 2)],
                                "sl": round(sl_price, 2),
                                "smc_snr": smc_snr_tag
                            })
                            print(f"✅ 已推播 {symbol} 區間訊號，單號：{trade_id}")
                            
        except Exception as e:
            print(f"⚠️ 掃描異常: {e}")
            
        time.sleep(15)

if __name__ == "__main__":
    scanner_thread = threading.Thread(target=scan_market, daemon=True)
    scanner_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
