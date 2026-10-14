import os
import time
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from flask import Flask

# --- 環境變數與參數設定 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "你的_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "你的_TELEGRAM_CHAT_ID")
RISK_USD = 100.0  # 固定 $100 風控

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

ALERTED_SIGNALS = {}
TRACKED_TRADES = []       # 追蹤中的單子
DAILY_REPORT_SENT = False  # 避免 8 點重複推播

app = Flask(__name__)

@app.route("/")
def health_check():
    return "Market Hunter with Auto 8:00 AM PnL Review is Running!"

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

def fetch_order_book_depth(symbol, limit=20):
    try:
        url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit={limit}"
        res = requests.get(url, timeout=5)
        data = res.json()
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        total_bid_usdt = sum(float(b[0]) * float(b[1]) for b in bids)
        total_ask_usdt = sum(float(a[0]) * float(a[1]) for a in asks)
        total_depth = total_bid_usdt + total_ask_usdt
        
        if total_depth == 0:
            return "⚖️ 盤口掛單量平穩 (1:1)", 50.0
            
        buyer_ratio = (total_bid_usdt / total_depth) * 100
        
        if buyer_ratio >= 55.0:
            ob_tag = f"📖 訂單簿買盤厚度優勢 (買牆 {buyer_ratio:.1f}%) (+)"
        elif buyer_ratio <= 45.0:
            ob_tag = f"⚠️ 訂單簿上方賣壓牆壓制 (賣牆 {100-buyer_ratio:.1f}%) (-)"
        else:
            ob_tag = f"⚖️ 訂單簿買賣多空均衡 (買牆 {buyer_ratio:.1f}%)"
            
        return ob_tag, buyer_ratio
    except Exception as e:
        return "📖 訂單簿數據暫無法獲取", 50.0

def calculate_indicators(df):
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['vol_ma20'] = df['volume'].rolling(window=20).mean()
    
    lookback = 50
    recent_df = df.tail(lookback)
    max_vol_idx = recent_df['volume'].idxmax()
    
    anchor_df = df.loc[max_vol_idx:].copy()
    anchor_df['typical_price'] = (anchor_df['high'] + anchor_df['low'] + anchor_df['close']) / 3
    anchor_df['pv'] = anchor_df['typical_price'] * anchor_df['volume']
    
    cum_pv = anchor_df['pv'].cumsum()
    cum_vol = anchor_df['volume'].cumsum()
    anchor_df['avwap'] = cum_pv / cum_vol
    
    df['avwap'] = anchor_df['avwap'].reindex(df.index).ffill()
    return df

def find_structural_levels(df, curr_price, valid_support):
    recent_swing_low = df['low'].tail(15).min()
    sl_structural = min(recent_swing_low, valid_support) * 0.9995
    
    pivot_near = df['high'].tail(20).max()
    pivot_mid = df['high'].tail(50).max()
    pivot_far = df['high'].tail(100).max()
    
    risk_unit = curr_price - sl_structural
    if risk_unit <= 0:
