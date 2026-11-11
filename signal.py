"""
signal.py — Gold (XAU/USD) 15-minute analysis bot.

Runs every 15 minutes from GitHub Actions. Fetches the 15-min candles,
computes the indicators, scores confluence, and sends you ONE Telegram
message per run — an analysis update, with an explicit BUY/SELL/STAND ASIDE.

Kimi selects the action; code validates the answer and calculates ATR levels.
An AI decision is not deterministic or evidence of profitability.

Environment variables (set as GitHub Actions secrets):
    MOONSHOT_API_KEY      required  - Kimi global API key
    TWELVE_DATA_API_KEY   required  - free from https://twelvedata.com
    TELEGRAM_BOT_TOKEN    required  - from @BotFather
    TELEGRAM_CHAT_ID      required  - your numeric chat id
    MIN_CONF              optional  - confluence (1-5) needed to call a signal,
                                      default 4
    SYMBOL                optional  - default XAU/USD
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

import kimi_brain

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SYMBOL = os.environ.get("SYMBOL", "XAU/USD")
INTERVAL = "15min"
OUTPUT_SIZE = 301          # retain up to 300 closed bars after excluding a live candle
BAR_DURATION = timedelta(minutes=15)
CLOSE_GRACE = timedelta(minutes=2)  # allow time for the provider to finalize a candle
MAX_CLOSED_BAR_AGE = timedelta(minutes=30)
MIN_CONF = int(os.environ.get("MIN_CONF", "4"))

ATR_SL = float(os.environ.get("ATR_SL", "1.5"))   # stop loss, in ATR
ATR_TP = float(os.environ.get("ATR_TP", "2.0"))   # take profit, in ATR


class DataError(ValueError):
    """Safe-to-display market-data failure."""


class StaleDataError(DataError):
    """No sufficiently recent completed market candle."""


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

def closed_bars(raw: list[dict], now: datetime) -> list[Bar]:
    """Validate UTC candle-start timestamps and select recent finalized bars.

    Twelve Data timestamps are treated as bar start times. A two-minute grace
    period reduces (but cannot eliminate) the risk of provider revisions.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    parsed = []
    seen = set()
    try:
        for v in raw:
            dt = datetime.fromisoformat(v["datetime"].replace("Z", "+00:00"))
            # We explicitly request UTC, whose timestamps can be naive.
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            if dt > now:
                raise DataError("market candle timestamp is in the future")
            if dt in seen:
                raise DataError("duplicate market candle timestamp")
            seen.add(dt)
            o, h, l, c = (float(v[k]) for k in ("open", "high", "low", "close"))
            if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
                raise DataError("invalid market candle prices")
            parsed.append((dt, Bar(dt.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c)))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as e:
        if isinstance(e, DataError):
            raise
        raise DataError("invalid market candle data") from None
    parsed.sort(key=lambda item: item[0])
    complete = [(dt, bar) for dt, bar in parsed if dt + BAR_DURATION + CLOSE_GRACE <= now]
    if not complete:
        raise StaleDataError("no finalized candle available")
    if now - (complete[-1][0] + BAR_DURATION) > MAX_CLOSED_BAR_AGE:
        raise StaleDataError("latest finalized candle is too old (market may be closed)")
    if len(complete) < 220:
        raise DataError(f"need at least 220 finalized candles, got {len(complete)}")
    return [bar for _, bar in complete[-300:]]


def fetch_bars() -> list[Bar]:
    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        raise DataError("TWELVE_DATA_API_KEY not set")

    try:
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": SYMBOL, "interval": INTERVAL, "outputsize": OUTPUT_SIZE,
                    "order": "DESC", "timezone": "UTC", "apikey": key},
            timeout=15,
        )
    except requests.RequestException:
        # requests exception strings can contain the API key in the URL.
        raise DataError("Twelve Data request failed") from None
    if not resp.ok:
        raise DataError(f"Twelve Data returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise DataError("Twelve Data returned invalid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("values"), list):
        # The provider's error text may echo sensitive request parameters.
        raise DataError("Twelve Data returned no candle data; check the API key and quota")
    return closed_bars(data["values"], datetime.now(timezone.utc))


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
    out[period] = (50.0 if ag == 0 else 100.0) if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = (50.0 if ag == 0 else 100.0) if al == 0 else 100 - 100 / (1 + ag / al)
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
    if not 1 <= MIN_CONF <= 5 or not math.isfinite(ATR_SL) or not math.isfinite(ATR_TP) or ATR_SL <= 0 or ATR_TP <= 0:
        raise ValueError("MIN_CONF must be 1–5 and ATR_SL/ATR_TP must be positive finite numbers")
    if len(bars) < 220:
        raise ValueError("at least 220 bars required")
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

    # --- confluence: five related checks, not independent probabilities ---
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
    signal = side if conf >= MIN_CONF and a > 0 else 0

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
        f"15m bar start: {s['bar_time']} UTC (closed)",
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
            f"Reference close {s['entry']:.2f} (not a live entry quote)",
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


def format_ai_message(s: dict, decision: dict) -> str:
    """AI decides direction; ATR levels are calculated locally, not by AI."""
    action = decision["action"]
    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    lines = [
        f"{icon} GOLD {SYMBOL} — {action}",
        f"AI decision: {kimi_brain.KIMI_MODEL}",
        f"15m bar start: {s['bar_time']} UTC (closed)",
        f"Reference close {s['price']:.2f} (not a live quote)",
        f"RSI {s['rsi']:.1f}   ATR {s['atr']:.2f}",
        f"EMA20 {s['ema20']:.2f} | EMA50 {s['ema50']:.2f} | EMA200 {s['ema200']:.2f}",
        f"20-bar range {s['lo20']:.2f} – {s['hi20']:.2f}",
        f"AI reason: {decision['reason']}",
        f"Rule reference: {s['action']} ({s['confidence']}/5 checks)",
    ]
    if action in ("BUY", "SELL"):
        side = 1 if action == "BUY" else -1
        stop = s["price"] - side * ATR_SL * s["atr"]
        target = s["price"] + side * ATR_TP * s["atr"]
        lines += [f"Illustrative stop {stop:.2f}   Target {target:.2f}   R:R {ATR_TP / ATR_SL:.1f}"]
    lines += ["", "⚠️ Experimental AI monitor, not advice. No proven edge; verify independently."]
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

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.RequestException:
        print("[ERROR] Telegram request failed", file=sys.stderr)
        return False
    if not resp.ok:
        # Log the status only — never the token.
        print(f"[ERROR] Telegram HTTP {resp.status_code}", file=sys.stderr)
        return False
    try:
        if resp.json().get("ok") is not True:
            print("[ERROR] Telegram rejected the message", file=sys.stderr)
            return False
    except (ValueError, AttributeError):
        print("[ERROR] Telegram returned an invalid response", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] fetching {SYMBOL} {INTERVAL}...")
        try:
            bars = fetch_bars()
        except StaleDataError as e:
            # Market closures and stale feeds must never produce BUY/SELL.
            print(f"  STAND ASIDE: {e}")
            ok = send_telegram(f"⚪ {SYMBOL} — STAND ASIDE\n{e}. No signal generated.")
            return 0 if ok else 1
        s = analyse(bars)
        try:
            if not all(math.isfinite(s[k]) for k in ("price", "atr", "ema20", "ema50", "ema200", "rsi", "hi20", "lo20")):
                raise kimi_brain.AIError("market indicators are not finite")
            if s["atr"] <= 0:
                raise kimi_brain.AIError("ATR is zero; cannot calculate risk levels")
            decision = kimi_brain.decide(bars, s, SYMBOL)
            if decision["action"] != "STAND ASIDE":
                side = 1 if decision["action"] == "BUY" else -1
                stop = s["price"] - side * ATR_SL * s["atr"]
                target = s["price"] + side * ATR_TP * s["atr"]
                if (not math.isfinite(stop) or not math.isfinite(target) or min(stop, target) <= 0
                        or round(stop, 2) == round(s["price"], 2)
                        or round(target, 2) == round(s["price"], 2)):
                    raise kimi_brain.AIError("cannot calculate valid displayed risk levels")
            # A slow AI response must not turn a once-fresh candle into a stale signal.
            bar_start = datetime.fromisoformat(s["bar_time"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - (bar_start + BAR_DURATION) > MAX_CLOSED_BAR_AGE:
                raise kimi_brain.AIError("market data became stale while waiting for AI")
        except kimi_brain.AIError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            send_telegram(f"⚪ {SYMBOL} — STAND ASIDE\nAI unavailable or invalid; no trading signal generated. Check workflow logs.")
            return 1

        print(f"  bar={s['bar_time']} price={s['price']:.2f} "
              f"rsi={s['rsi']:.1f} rule_conf={s['confidence']}/5 ai_action={decision['action']}")

        ok = send_telegram(format_ai_message(s, decision))
        print("  ✅ sent" if ok else "  ⚠️ not sent (see above)")
        return 0 if ok else 1

    except Exception as e:
        # Never echo arbitrary exception text: request URLs can contain secrets.
        message = str(e) if isinstance(e, DataError) else "unexpected error; check configuration and input data"
        print(f"❌ Error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
