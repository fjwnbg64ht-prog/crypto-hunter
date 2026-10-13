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

# 12 隻熱門主流與高波動標的
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", 
    "DOGEUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT", 
    "NEARUSDT", "LINKUSDT", "SUIUSDT", "APTUSDT"
]

ALERTED_SIGNALS = {}
LOG_FILE = "trade_log.json"

app = Flask(__name__)

@app.route("/")
def health_check():
    return "Market Hunter Active Scanner with High-Precision AVWAP & SMC/SnR is Running!"

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
    # EMA 計算
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    # 20 周期成交量均線 (用於爆量突破驗證)
    df['vol_ma20'] = df['volume'].rolling(window=20).mean()
    
    # --- 精準優化 1：爆量柱錨點 AVWAP (Volume Spike Anchor) ---
    lookback = 50
    recent_df = df.tail(lookback)
    max_vol_idx = recent_df['volume'].idxmax() # 找出近 50 根內成交量最大的 K 線作為機構錨點
    
    anchor_df = df.loc[max_vol_idx:].copy()
    anchor_df['typical_price'] = (anchor_df['high'] + anchor_df['low'] + anchor_df['close']) / 3
    anchor_df['pv'] = anchor_df['typical_price'] * anchor_df['volume']
    
    cum_pv = anchor_df['pv'].cumsum()
    cum_vol = anchor_df['volume'].cumsum()
    anchor_df['avwap'] = cum_pv / cum_vol
    
    # --- 精準優化 3：AVWAP 折價通道 (-1 倍標準差) ---
    anchor_df['variance'] = ((anchor_df['typical_price'] - anchor_df['avwap']) ** 2) * anchor_df['volume']
    anchor_df['std'] = np.sqrt(anchor_df['variance'].cumsum() / cum_vol)
    anchor_df['avwap_lower'] = anchor_df['avwap'] - anchor_df['std']
    
    df['avwap'] = anchor_df['avwap'].reindex(df.index).ffill()
    df['avwap_lower'] = anchor_df['avwap_lower'].reindex(df.index).ffill()
    return df

def check_smc_snr_confluence(df):
    """檢測 SMC (OB / FVG) 與 Key SnR 結構，標註 (+ / -) 符號"""
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    confluences = []
    
    # 爆量機構 AVWAP 支撐
    confluences.append("🔥 爆量機構持倉 AVWAP 支撐 (+)")
    
    # 放量倍數標記
    if curr['vol_ma20'] > 0:
        vol_ratio = curr['volume'] / curr['vol_ma20']
        if vol_ratio >= 1.2:
            confluences.append(f"💥 {vol_ratio:.1f}x 放量動能突破 (>= 1.2x MA20) (+)")
        
    # 1. SMC Bullish FVG (看漲需求缺口 +)
    if prev2['high'] < curr['low']:
        confluences.append("⚡ SMC 需求缺口 (FVG) (+)")
        
    # 2. SMC Bullish Order Block (看漲機構需求塊 +)
    if prev['close'] < prev['open'] and curr['close'] > prev['high']:
        confluences.append("📦 SMC 機構需求塊 (OB) (+)")
        
    # 3. Key SnR 結構支撐 (+)
    recent_pivot_low = df['low'].tail(30).min()
    if abs(curr['low'] - recent_pivot_low) / curr['close'] <= 0.003:
        confluences.append("🎯 Key SnR 結構支撐 (+)")
        
    # 4. 上方結構阻力 (-)
    recent_pivot_high = df['high'].tail(30).max()
    if abs(curr['high'] - recent_pivot_high) / curr['close'] <= 0.003:
        confluences.append("⚠️ 近期 Key SnR 結構阻力 (-)")
        
    return " | \n  • ".join(confluences)

def log_trade(trade_data):
    """將發出的訊號寫入本地交易日誌"""
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

def scan_market():
    print(f"🚀 【高精準 AVWAP + SMC 獵手】已啟動！監控 {len(SYMBOLS)} 隻標的中...")
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
                
                # --- 高精準策略條件 ---
                # 1. 精準踩踏支撐或折價區
                touch_support = (prev['low'] <= valid_support * 1.005) and (prev['close'] >= valid_support * 0.985)
                
                # 2. 精準優化：AVWAP 斜率過濾 (必須向上或平緩向上)
                avwap_slope_ok = (avwap >= avwap_prev)
                
                # 3. 順勢趨勢過濾
                trend_filter = (curr['close'] > ema50) or (ema9 > ema21)
                
                # 4. 精準優化：5m 微結構突破 + 成交量放大 (>= 1.2 倍 MA20 均量)
                vol_expansion = (curr['volume'] >= curr['vol_ma20'] * 1.2) if curr['vol_ma20'] > 0 else True
                breakout_confirm = (curr['close'] > prev['high']) and (curr['close'] > curr['open']) and vol_expansion
                
                # 綜合進場判斷
                if touch_support and avwap_slope_ok and trend_filter and breakout_confirm:
                    signal_key = f"{symbol}_{curr['open_time']}"
                    
                    if signal_key not in ALERTED_SIGNALS:
                        ALERTED_SIGNALS[signal_key] = True
                        
                        # 進場區間計算
                        entry_low = valid_support
                        entry_high = curr['close'] * 1.0015
                        
                        # SMC / SnR 關鍵位驗證
                        smc_snr_tag = check_smc_snr_confluence(df)
                        
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
                                f"🎯 *【Martin 高精準 AVWAP 訊號】* (`{trade_id}`)\n\n"
                                f"📌 *標的*: `{symbol}` (5m)\n"
                                f"🎯 *進場價格區間*: `{entry_low:.4f} ~ {entry_high:.4f}`\n"
                                f"🛑 *止損價格*: `{sl_price:.4f}` (固定風控 $100)\n"
                                f"📦 *建議手數*: `{qty:.2f}` (`{position_usd:.1f}` USDT)\n\n"
                                f"🔍 *關鍵位驗證 (+/-)*: \n  • {smc_snr_tag}\n\n"
                                f"📈 *止盈目標*:\n"
                                f"  • TP1 (1:1.5): `{tp1:.4f}`\n"
                                f"  • TP2 (1:2.0): `{tp2:.4f}`\n"
                                f"  • TP3 (1:3.0): `{tp3:.4f}`\n\n"
                                f"⚠️ *紀律提醒*: 現價超出 `{entry_high:.4f}` **嚴格禁止追單**！"
                            )
                            send_telegram(msg)
                            
                            log_trade({
                                "trade_id": trade_id,
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "symbol": symbol,
                                "entry_range": [round(entry_low, 4), round(entry_high, 4)],
                                "sl": round(sl_price, 4),
                                "smc_snr": smc_snr_tag
                            })
                            print(f"✅ 已推播 {symbol} 精準訊號，單號：{trade_id}")
                            
        except Exception as e:
            print(f"⚠️ 掃描異常: {e}")
            
        time.sleep(15)

if __name__ == "__main__":
    scanner_thread = threading.Thread(target=scan_market, daemon=True)
    scanner_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
