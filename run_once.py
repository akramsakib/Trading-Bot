import os
import sys
from datetime import datetime
import json
import requests
import anthropic

# ====== DATA FETCH ======
def get_gold_data():
    api_key = os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        raise ValueError("TWELVE_DATA_API_KEY not set")
    
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": "XAU/USD",
        "interval": "15min",
        "outputsize": 48,
        "apikey": api_key
    }
    
    response = requests.get(url, params=params)
    data = response.json()
    
    if "values" not in data or len(data["values"]) < 20:
        raise ValueError(f"Invalid response: {data}")
    
    values = data["values"]
    closes = [float(v["close"]) for v in values]
    
    # Calculate EMA(20)
    def ema(prices, period=20):
        multiplier = 2 / (period + 1)
        ema_values = [prices[0]]
        for price in prices[1:]:
            ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
        return ema_values
    
    # Calculate RSI(14)
    def rsi(prices, period=14):
        gains = []
        losses = []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i-1]
            gains.append(diff if diff > 0 else 0)
            losses.append(-diff if diff < 0 else 0)
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    current_price = closes[-1]
    ema20 = ema(closes)[-1]
    rsi14 = rsi(closes)
    
    return {
        "price": current_price,
        "ema20": ema20,
        "rsi14": rsi14,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    }

# ====== CLAUDE SIGNAL ======
def get_signal(data):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    
    client = anthropic.Anthropic(api_key=api_key)
    
    prompt = f"""Analyze this gold (XAUUSD) data and provide a trading signal:

Current Price: ${data['price']:.2f}
EMA(20): ${data['ema20']:.2f}
RSI(14): {data['rsi14']:.1f}

Provide a signal in this exact format:
ACTION: [BUY/SELL/HOLD]
ENTRY: [price]
STOP LOSS: [price]
TAKE PROFIT: [price]
REASONING: [brief explanation]"""
    
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return response.content[0].text

# ====== TELEGRAM NOTIFY ======
def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN or CHAT_ID not set")
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        raise ValueError(f"Telegram error: {response.text}")
    return True

# ====== MAIN ======
def main():
    try:
        print("Fetching XAUUSD data...")
        data = get_gold_data()
        print(f"Price: ${data['price']:.2f}")
        print(f"EMA20: ${data['ema20']:.2f}")
        print(f"RSI14: {data['rsi14']:.1f}")
        
        print("Calling Claude API...")
        signal = get_signal(data)
        print(signal)
        
        print("Sending to Telegram...")
        send_telegram(f"📊 GOLD SIGNAL\n{signal}")
        
        print("✅ Done!")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
