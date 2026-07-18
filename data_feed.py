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

    # Built as a plain string (not passed via the `params=` dict) so the
    # "/" in "XAU/USD" stays literal instead of being percent-encoded to
    # "%2F", which Twelve Data's API was rejecting as an invalid symbol.
    url = (
        f"{BASE_URL}?symbol={SYMBOL}"
        f"&interval={INTERVAL}"
        f"&outputsize={OUTPUT_SIZE}"
        f"&apikey={TWELVE_DATA_API_KEY}"
        f"&order=ASC"
    )
    resp = requests.get(url, timeout=15)
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
