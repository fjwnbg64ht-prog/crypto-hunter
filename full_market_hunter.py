import os
import time
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

# 精簡為 4 大高流動性主流標的 (避免暴險過度分散)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

ALERTED_SIGNALS = {}

app = Flask(__name__)

@app.route("/")
def health_check():
    return "Market Hunter Clean & High-Performance Scanner is Running!"

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
    """即時抓取幣安 L2 訂單簿 (Order Book Depth)，計算買賣牆資金比例"""
    try:
        url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit={limit}"
        res = requests.get(url, timeout=5)
        data = res.json()
        
        bids = data.get("bids", []) # 買單 [[price, qty], ...]
        asks = data.get("asks", []) # 賣單 [[price, qty], ...]
        
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
        print(f"⚠️ 訂單簿抓取失敗: {e}")
        return "📖 訂單簿數據暫無法獲取", 50.0

def calculate_indicators(df):
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['vol_ma20'] = df['volume'].rolling(window=20).mean()
    
    # 爆量柱錨點 AVWAP
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

def check_smc_snr_confluence(df, ob_tag, vol_ratio):
    """檢測 SMC (OB / FVG) 與 Key SnR 結構，標註 (+ / -) 符號"""
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    confluences = []
    reasons = []
    
    # 1. AVWAP 爆量機構持倉支撐
    confluences.append("🔥 爆量機構持倉 AVWAP 支撐 (+)")
    reasons.append("價格回踩近 50 根最大成交量 K 線所拉出的機構持倉 AVWAP 成本線。")
    
    # 2. 突破放量倍數
    if vol_ratio >= 1.2:
        confluences.append(f"💥 {vol_ratio:.1f}x 放量動能突破 (+)")
        reasons.append(f"5m 突破 K 線成交量達到近 20 均量的 {vol_ratio:.1f} 倍，屬實體資金推動。")
    else:
        reasons.append("5m 結構性實體突破前高。")
        
    # 3. 訂單簿盤口
    confluences.append(ob_tag)
    
    # 4. SMC Bullish FVG
    if prev2['high'] < curr['low']:
        confluences.append("⚡ SMC 需求缺口 (FVG) (+)")
        reasons.append("下方疊加 5m SMC 看漲失衡需求缺口 (FVG)。")
        
    # 5. SMC Bullish Order Block
    if prev['close'] < prev['open'] and curr['close'] > prev['high']:
        confluences.append("📦 SMC 機構需求塊 (OB) (+)")
        reasons.append("當前支撐位位處機構吸收籌碼的 Order Block (OB) 需求區內。")
        
    # 6. Key SnR 結構支撐
    recent_pivot_low = df['low'].tail(30).min()
    if abs(curr['low'] - recent_pivot_low) / curr['close'] <= 0.003:
        confluences.append("🎯 Key SnR 結構支撐 (+)")
        reasons.append("觸及近 30 根 K 線的波段結構前低 Key SnR 關鍵支撐。")
        
    # 7. 上方結構阻力
    recent_pivot_high = df['high'].tail(30).max()
    if abs(curr['high'] - recent_pivot_high) / curr['close'] <= 0.003:
        confluences.append("⚠️ 近期 Key SnR 結構阻力 (-)")
        
    confluence_str = " | \n  • ".join(confluences)
    reason_str = "\n  • ".join(reasons)
    
    return confluence_str, reason_str

def scan_market():
    print(f"🚀 【高精準 AVWAP + SMC/SnR 輕量獵手】已啟動！監控 {len(SYMBOLS)} 隻主流標的中...")
    while True:
        try:
            for symbol in SYMBOLS:
                df = fetch_klines(symbol)
                df = calculate_indicators(df)
                
                curr = df.iloc[-1]
                prev = df.iloc[-2]
                
                avwap = curr['avwap']
                avwap_prev = prev['avwap']
                ema21 = curr['ema21']
                ema50 = curr['ema50']
                ema9 = curr['ema9']
                
                valid_support = min(ema21, avwap) if not np.isnan(avwap) else curr['close']
                
                # 策略進場條件
                touch_support = (prev['low'] <= valid_support * 1.005) and (prev['close'] >= valid_support * 0.985)
                avwap_slope_ok = (avwap >= avwap_prev)
                trend_filter = (curr['close'] > ema50) or (ema9 > ema21)
                
                vol_ratio = (curr['volume'] / curr['vol_ma20']) if curr['vol_ma20'] > 0 else 1.0
                vol_expansion = vol_ratio >= 1.2
                breakout_confirm = (curr['close'] > prev['high']) and (curr['close'] > curr['open']) and vol_expansion
                
                if touch_support and avwap_slope_ok and trend_filter and breakout_confirm:
                    signal_key = f"{symbol}_{curr['open_time']}"
                    
                    if signal_key not in ALERTED_SIGNALS:
                        ALERTED_SIGNALS[signal_key] = True
                        
                        # 1. 抓取訂單簿深度 (Order Book Depth)
                        ob_tag, buyer_ratio = fetch_order_book_depth(symbol)
                        
                        # 2. 計算進場區間
                        entry_low = valid_support
                        entry_high = curr['close'] * 1.0015
                        
                        # 3. 生成關鍵位驗證與進場理由分析
                        smc_snr_tag, trade_reasons = check_smc_snr_confluence(df, ob_tag, vol_ratio)
                        
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
                                f"🎯 *【Martin 高精準進場訊號】* (`{trade_id}`)\n\n"
                                f"📌 *標的*: `{symbol}` (5m)\n"
                                f"🎯 *進場價格區間*: `{entry_low:.4f} ~ {entry_high:.4f}`\n"
                                f"🛑 *止損價格*: `{sl_price:.4f}` (固定風控 $100)\n"
                                f"📦 *建議手數*: `{qty:.2f}` (`{position_usd:.1f}` USDT)\n\n"
                                f"💡 *進場理由與邏輯分析*:\n"
                                f"  • {trade_reasons}\n\n"
                                f"🔍 *關鍵位與盤口驗證 (+/-)*: \n"
                                f"  • {smc_snr_tag}\n\n"
                                f"📈 *止盈目標*:\n"
                                f"  • TP1 (1:1.5): `{tp1:.4f}`\n"
                                f"  • TP2 (1:2.0): `{tp2:.4f}`\n"
                                f"  • TP3 (1:3.0): `{tp3:.4f}`\n\n"
                                f"⚠️ *紀律提醒*: 現價超出 `{entry_high:.4f}` **嚴格禁止追單**！"
                            )
                            send_telegram(msg)
                            print(f"✅ 已推播 {symbol} 訊號，單號：{trade_id}")
                            
        except Exception as e:
            print(f"⚠️ 掃描異常: {e}")
            
        time.sleep(15)

if __name__ == "__main__":
    scanner_thread = threading.Thread(target=scan_market, daemon=True)
    scanner_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
