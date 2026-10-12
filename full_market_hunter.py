import os
import time
import threading
import requests
import pandas as pd
import numpy as np
from flask import Flask

# --- 環境變數與參數設定 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "你的_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "你的_TELEGRAM_CHAT_ID")
RISK_USD = 100.0  # 固定 $100 風控

# 欲主動監控的標的清單（可隨時增減）
SYMBOLS = ["ETHUSDT", "BTCUSDT", "SOLUSDT"]

# 紀錄已發送的訊號，防止重複推播 (以 K 線時間戳為 key)
ALERTED_SIGNALS = {}

# --- Flask Web 服務 (維持 Render 存活) ---
app = Flask(__name__)

@app.route("/")
def health_check():
    return "Market Hunter Active Scanner is Running!"

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
    """直接向幣安 API 抓取 5 分鐘 K 線資料"""
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
    """計算 EMA 9/21/50 與動態 AVWAP"""
    # EMA 計算
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    # 動態 AVWAP 計算 (以近 50 根 K 線最高點為 Anchor)
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

def scan_market():
    """主動盤面掃描核心邏輯"""
    print("🚀 【主動盤面獵手】啟動全天候監控中...")
    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_klines(symbol)
                df = calculate_indicators(df)
                
                # 取最新 K 線與上一根 K 線
                curr = df.iloc[-1]
                prev = df.iloc[-2]
                
                avwap = curr['avwap']
                ema21 = curr['ema21']
                ema50 = curr['ema50']
                ema9 = curr['ema9']
                
                valid_support = min(ema21, avwap) if not np.isnan(avwap) else curr['close']
                
                # 1. 踩支撐：上一根低點回測支撐，且收盤未破位
                touch_support = (prev['low'] <= valid_support * 1.005) and (prev['close'] >= valid_support * 0.985)
                # 2. 順勢過濾：價格在 50 EMA 之上，或 9 > 21 EMA 多頭排列
                trend_filter = (curr['close'] > ema50) or (ema9 > ema21)
                # 3. 5m 微結構突破：當前 K 線實體突破前一根高點且為陽線
                breakout_confirm = (curr['close'] > prev['high']) and (curr['close'] > curr['open'])
                
                if touch_support and trend_filter and breakout_confirm:
                    signal_key = f"{symbol}_{curr['open_time']}"
                    
                    # 避免同一次 K 線重複發報
                    if signal_key not in ALERTED_SIGNALS:
                        ALERTED_SIGNALS[signal_key] = True
                        
                        entry_price = curr['close']
                        sl_price = min(prev['low'], valid_support * 0.995)
                        risk_per_unit = entry_price - sl_price
                        
                        if risk_per_unit > 0:
                            qty = RISK_USD / risk_per_unit
                            position_usd = qty * entry_price
                            tp1 = entry_price + (risk_per_unit * 1.5)
                            tp2 = entry_price + (risk_per_unit * 2.0)
                            tp3 = entry_price + (risk_per_unit * 3.0)
                            
                            msg = (
                                f"🎯 *【Martin 盤面主動訊號】*\n\n"
                                f"📌 *標的*: `{symbol}` (5m)\n"
                                f"💵 *現價進場*: `{entry_price:.2f}`\n"
                                f"🛑 *止損價格*: `{sl_price:.2f}` (風險固定 $100)\n"
                                f"📦 *建倉手數*: `{qty:.4f}` (`{position_usd:.1f}` USDT)\n\n"
                                f"📈 *止盈目標*:\n"
                                f"  • TP1 (1:1.5): `{tp1:.2f}`\n"
                                f"  • TP2 (1:2.0): `{tp2:.2f}`\n"
                                f"  • TP3 (1:3.0): `{tp3:.2f}`\n\n"
                                f"📊 *關鍵數據*: AVWAP `{avwap:.2f}` | 21 EMA `{ema21:.2f}`"
                            )
                            send_telegram(msg)
                            print(f"✅ 已成功推播 {symbol} 進場訊號！")
                            
        except Exception as e:
            print(f"⚠️ 掃描運行中異常: {e}")
            
        time.sleep(15)  # 每 15 秒掃描一次盤面

if __name__ == "__main__":
    # 背景啟動市場掃描器 Thread
    scanner_thread = threading.Thread(target=scan_market, daemon=True)
    scanner_thread.start()
    
    # 主執行緒運行 Web Server (適應 Render 連線需求)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
