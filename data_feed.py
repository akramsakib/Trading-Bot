"""
Market data feed — pulls recent XAUUSD (Gold) candles from Twelve Data's
free API. No broker login needed; this is just price data, independent
of VT Markets/XM.

Free tier: https://twelvedata.com (sign up, get an API key, ~800 requests/day
on the free plan — plenty for a 15-min poll cycle).
"""

import os
import requests
import pandas as pd

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
SYMBOL = "XAU/USD"
INTERVAL = "15min"
OUTPUT_SIZE = 100

BASE_URL = "https://api.twelvedata.com/time_series"


def get_market_snapshot() -> pd.DataFrame:
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY environment variable not set. "
            "Get a free key at https://twelvedata.com"
        )

    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": OUTPUT_SIZE,
        "apikey": TWELVE_DATA_API_KEY,
        "order": "ASC",
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    data = resp.json()

    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")

    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)

    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["rsi"] = compute_rsi(df["close"], 14)

    return df


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))
