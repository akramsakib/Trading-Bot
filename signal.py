"""
signal.py — Gold (XAU/USD) 15-minute analysis bot.

Runs every 15 minutes from GitHub Actions. Fetches the 15-min candles,
computes the indicators, scores confluence, and sends you ONE Telegram
message per run — an analysis update, with an explicit BUY/SELL/STAND ASIDE.

No LLM. Deterministic: the same candles always produce the same message.
Needs only `requests`.

Environment variables (set as GitHub Actions secrets):
    TWELVE_DATA_API_KEY   required  - free from https://twelvedata.com
    TELEGRAM_BOT_TOKEN    required  - from @BotFather
    TELEGRAM_CHAT_ID      required  - your numeric chat id
    MIN_CONF              optional  - confluence (0-5) needed to call a signal,
                                      default 4
    SYMBOL                optional  - default XAU/USD
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SYMBOL = os.environ.get("SYMBOL", "XAU/USD")
INTERVAL = "15min"
OUTPUT_SIZE = 300          # need >= 220 for EMA200 to be meaningful
MIN_CONF = int(os.environ.get("MIN_CONF", "4"))

ATR_SL = float(os.environ.get("ATR_SL", "1.5"))   # stop loss, in ATR
ATR_TP = float(os.environ.get("ATR_TP", "2.0"))   # take profit, in ATR


@dataclass
class Bar:
    dt: str
    o: float
    h: float
    l: float
    c: float


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

def fetch_bars() -> list[Bar]:
    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        raise ValueError("TWELVE_DATA_API_KEY not set")

    resp = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "outputsize": OUTPUT_SIZE,
            # ASC = oldest first. Without it the API returns newest first and
            # closes[-1] silently becomes the OLDEST bar (~12h stale).
            "order": "ASC",
            "apikey": key,
        },
        timeout=15,
    )

    if not resp.ok:
        # Never echo resp.url or resp.text: both can contain the API key, and
        # GitHub Actions logs on a public repo are readable by anyone.
        hint = ""
        if resp.status_code in (401, 403):
            hint = " — check TWELVE_DATA_API_KEY"
        elif resp.status_code == 429:
            hint = " — rate limit reached (free tier is ~800 requests/day)"
        raise ValueError(f"Twelve Data returned HTTP {resp.status_code}{hint}")

    data = resp.json()
    if "values" not in data:
        # Strip anything that might echo the request back.
        msg = str(data.get("message", data.get("code", "unknown error")))
        raise ValueError(f"Twelve Data error: {msg[:200]}")

    # Sort explicitly rather than trusting the API's ordering.
    raw = sorted(data["values"], key=lambda v: v["datetime"])
    bars = [Bar(v["datetime"], float(v["open"]), float(v["high"]),
                float(v["low"]), float(v["close"])) for v in raw]
    if len(bars) < 220:
        raise ValueError(f"need >=220 candles for EMA200, got {len(bars)}")
    return bars


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #

def ema(vals: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def rsi(vals: list[float], period: int = 14) -> list[float]:
    """Wilder RSI. Averaging only the first `period` deltas is a common bug."""
    n = len(vals)
    out = [float("nan")] * n
    if n <= period:
        return out
    gains = [max(vals[i] - vals[i - 1], 0.0) for i in range(1, n)]
    losses = [max(vals[i - 1] - vals[i], 0.0) for i in range(1, n)]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars: list[Bar], period: int = 14) -> list[float]:
    n = len(bars)
    out = [float("nan")] * n
    if n <= period:
        return out
    trs = [max(bars[i].h - bars[i].l,
               abs(bars[i].h - bars[i - 1].c),
               abs(bars[i].l - bars[i - 1].c)) for i in range(1, n)]
    a = sum(trs[:period]) / period
    out[period] = a
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out[i + 1] = a
    return out


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def analyse(bars: list[Bar]) -> dict:
    """Deterministic. Same candles in -> same dict out, every time."""
    closes = [b.c for b in bars]
    i = len(bars) - 1

    price = bars[i].c
    prev = bars[i - 1].c
    e20, e50, e200 = ema(closes, 20)[i], ema(closes, 50)[i], ema(closes, 200)[i]
    r = rsi(closes, 14)[i]
    a = atr(bars, 14)[i]
    hi20 = max(b.h for b in bars[i - 20:i])
    lo20 = min(b.l for b in bars[i - 20:i])

    # --- direction ---
    if price > hi20:
        side = 1
    elif price < lo20:
        side = -1
    elif e20 > e50 > e200:
        side = 1
    elif e20 < e50 < e200:
        side = -1
    else:
        side = 0

    # --- confluence: five independent tests ---
    reasons: list[str] = []
    if side != 0:
        if (side > 0 and price > e200) or (side < 0 and price < e200):
            reasons.append("trend vs EMA200")
        if (side > 0 and e20 > e50) or (side < 0 and e20 < e50):
            reasons.append("EMA20/50 stacked")
        if 30 < r < 70:
            reasons.append(f"RSI not exhausted ({r:.0f})")
        if (side > 0 and price > hi20) or (side < 0 and price < lo20):
            reasons.append("broke 20-bar range")
        med = sorted(x for x in atr(bars, 14)[max(0, i - 100):i] if x == x)
        if med:
            m = med[len(med) // 2]
            if 0.6 * m <= a <= 2.0 * m:
                reasons.append("volatility normal")

    conf = len(reasons)
    signal = side if conf >= MIN_CONF else 0

    out = {
        "bar_time": bars[i].dt,
        "price": price,
        "change": price - prev,
        "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi": r, "atr": a, "hi20": hi20, "lo20": lo20,
        "confidence": conf, "reasons": reasons,
        "side": signal,
        "action": "BUY" if signal > 0 else "SELL" if signal < 0 else "STAND ASIDE",
    }
    if signal != 0:
        out["entry"] = price
        out["stop_loss"] = price - signal * ATR_SL * a
        out["take_profit"] = price + signal * ATR_TP * a
        out["rr"] = ATR_TP / ATR_SL
    return out


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #

def format_message(s: dict) -> str:
    icon = {"BUY": "🟢", "SELL": "🔴"}.get(s["action"], "⚪")
    arrow = "▲" if s["change"] >= 0 else "▼"
    bar = "█" * s["confidence"] + "░" * (5 - s["confidence"])

    lines = [
        f"{icon} GOLD {SYMBOL} — {s['action']}",
        f"15m bar: {s['bar_time']} UTC",
        "",
        f"Price {s['price']:.2f}  {arrow} {s['change']:+.2f}",
        f"RSI {s['rsi']:.1f}   ATR {s['atr']:.2f}",
        f"EMA20 {s['ema20']:.2f} | EMA50 {s['ema50']:.2f} | EMA200 {s['ema200']:.2f}",
        f"20-bar range {s['lo20']:.2f} – {s['hi20']:.2f}",
        "",
        f"Confluence {s['confidence']}/5  {bar}",
    ]

    if s["reasons"]:
        lines += [f"  ✓ {x}" for x in s["reasons"]]

    if s["side"] != 0:
        lines += [
            "",
            f"Entry {s['entry']:.2f}",
            f"Stop {s['stop_loss']:.2f}   Target {s['take_profit']:.2f}   R:R {s['rr']:.1f}",
        ]
    else:
        lines += ["", f"No setup at {MIN_CONF}/5 — standing aside."]

    lines += [
        "",
        "⚠️ Rule-based monitor, not advice.",
        "Backtests show no reliable edge — verify on your own chart.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #

def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[INFO] Telegram not configured — printing the message instead.\n")
        print(text)
        return False

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=10,
    )
    if not resp.ok:
        # Log the status only — never the token.
        print(f"[ERROR] Telegram {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] fetching {SYMBOL} {INTERVAL}...")
        bars = fetch_bars()
        s = analyse(bars)

        print(f"  bar={s['bar_time']} price={s['price']:.2f} "
              f"rsi={s['rsi']:.1f} conf={s['confidence']}/5 action={s['action']}")

        ok = send_telegram(format_message(s))
        print("  ✅ sent" if ok else "  ⚠️ not sent (see above)")
        return 0 if ok else 1

    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
