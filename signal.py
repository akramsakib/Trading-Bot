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


def env_float(name: str, default: float) -> float:
    """Environment number that tolerates an empty or malformed value."""
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    """Environment integer that tolerates an empty or malformed value."""
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(float(raw))
    except ValueError:
        return default


MIN_CONF = env_int("MIN_CONF", 4)

# --- Scalp profile -------------------------------------------------------- #
# A scalp is opened and closed inside a few minutes, so the levels are
# fractions of the 15-minute ATR rather than multiples of it, and the spread is
# a first-class part of the maths: it is what turns a "working" scalp into a
# losing one.
ATR_SL = env_float("ATR_SL", 0.5)      # stop loss, in ATR(15m)
ATR_TP = env_float("ATR_TP", 0.75)     # take profit, in ATR(15m)
HOLD_MIN = env_int("HOLD_MIN", 5)      # intended hold, minutes
HOLD_MAX = env_int("HOLD_MAX", 16)     # hard exit, minutes
SPREAD_USD = env_float("SPREAD_USD", 0.40)   # round-trip cost, USD/oz
MIN_NET_RR = env_float("MIN_NET_RR", 1.0)    # reward:risk after costs
USE_M5 = (os.environ.get("USE_M5") or "1").strip().lower() not in ("0", "false", "off", "no")
M5_INTERVAL = "5min"
M5_OUTPUT_SIZE = 72


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

def closed_bars(raw: list[dict], now: datetime, duration: timedelta = BAR_DURATION,
                min_bars: int = 220, keep: int = 300) -> list[Bar]:
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
    complete = [(dt, bar) for dt, bar in parsed if dt + duration + CLOSE_GRACE <= now]
    if not complete:
        raise StaleDataError("no finalized candle available")
    if now - (complete[-1][0] + duration) > MAX_CLOSED_BAR_AGE:
        raise StaleDataError("latest finalized candle is too old (market may be closed)")
    if len(complete) < min_bars:
        raise DataError(f"need at least {min_bars} finalized candles, got {len(complete)}")
    return [bar for _, bar in complete[-keep:]]


def _fetch_rows(interval: str, output_size: int) -> list[dict]:
    key = os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        raise DataError("TWELVE_DATA_API_KEY not set")

    try:
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": SYMBOL, "interval": interval, "outputsize": output_size,
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
    return data["values"]


def fetch_bars() -> list[Bar]:
    return closed_bars(_fetch_rows(INTERVAL, OUTPUT_SIZE), datetime.now(timezone.utc))


def fetch_lower_tf() -> dict | None:
    """Optional 5-minute confirmation series.

    Purely supplementary: if it is unavailable the run continues on the 15m
    chart alone rather than failing, because the scalp decision is still made
    from complete, validated 15-minute data.
    """
    if not USE_M5:
        return None
    try:
        bars = closed_bars(_fetch_rows(M5_INTERVAL, M5_OUTPUT_SIZE), datetime.now(timezone.utc),
                           duration=timedelta(minutes=5), min_bars=30, keep=60)
    except (DataError, ValueError) as e:
        print(f"[WARN] 5-minute confirmation unavailable: {e}", file=sys.stderr)
        return None
    return summarise_lower_tf(bars)


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


def wilder(vals: list[float], period: int) -> list[float]:
    """Wilder's smoothing, used by ATR/ADX."""
    out = [float("nan")] * len(vals)
    if len(vals) < period:
        return out
    acc = sum(vals[:period]) / period
    out[period - 1] = acc
    for i in range(period, len(vals)):
        acc = (acc * (period - 1) + vals[i]) / period
        out[i] = acc
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line and histogram."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s for f, s in zip(ef, es)]
    sig = ema(line, signal)
    return line, sig, [m - s for m, s in zip(line, sig)]


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0):
    """Middle, upper and lower bands (population standard deviation)."""
    n = len(closes)
    mid = [float("nan")] * n
    up = [float("nan")] * n
    lo = [float("nan")] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        mid[i], up[i], lo[i] = m, m + mult * sd, m - mult * sd
    return mid, up, lo


def stoch(bars: list[Bar], period: int = 14, smooth: int = 3, signal: int = 3):
    """Stochastic oscillator: raw %K, smoothed %K and %D."""
    n = len(bars)
    raw = [float("nan")] * n
    for i in range(period - 1, n):
        high = max(b.h for b in bars[i - period + 1:i + 1])
        low = min(b.l for b in bars[i - period + 1:i + 1])
        raw[i] = 50.0 if high == low else 100 * (bars[i].c - low) / (high - low)

    def smooth_series(vals, period):
        out = [float("nan")] * n
        for i in range(period - 1, n):
            window = [v for v in vals[i - period + 1:i + 1] if v == v]
            out[i] = sum(window) / len(window) if window else float("nan")
        return out

    k = smooth_series(raw, smooth)
    d = smooth_series(k, signal)
    return raw, k, d


def adx(bars: list[Bar], period: int = 14):
    """Wilder ADX with +DI/-DI. ADX < 20 means the market is ranging, not trending."""
    n = len(bars)
    adx_out = [float("nan")] * n
    if n < 2 * period:
        return adx_out, [float("nan")] * n, [float("nan")] * n
    plus_dm, minus_dm, trs = [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(1, n):
        up, down = bars[i].h - bars[i - 1].h, bars[i - 1].l - bars[i].l
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        trs[i] = max(bars[i].h - bars[i].l,
                     abs(bars[i].h - bars[i - 1].c),
                     abs(bars[i].l - bars[i - 1].c))
    atr_s = wilder(trs[1:], period)
    plus_s = wilder(plus_dm[1:], period)
    minus_s = wilder(minus_dm[1:], period)
    plus_di, minus_di = [float("nan")] * n, [float("nan")] * n
    dx = [float("nan")] * n
    offset = n - len(atr_s)
    for j in range(len(atr_s)):
        i = offset + j
        if atr_s[j] != atr_s[j] or atr_s[j] == 0:
            continue
        plus_di[i] = 100 * plus_s[j] / atr_s[j]
        minus_di[i] = 100 * minus_s[j] / atr_s[j]
        total = plus_di[i] + minus_di[i]
        dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / total if total else 0.0
    valid = [v for v in dx if v == v]
    if len(valid) >= period:
        smoothed = wilder(valid, period)
        filled = [float("nan")] * n
        idx = 0
        for i in range(n):
            if dx[i] == dx[i]:
                if idx < len(smoothed):
                    filled[i] = smoothed[idx]
                idx += 1
        adx_out = filled
    return adx_out, plus_di, minus_di


def cci(bars: list[Bar], period: int = 20) -> list[float]:
    """Commodity Channel Index on typical price."""
    n = len(bars)
    out = [float("nan")] * n
    tp = [(b.h + b.l + b.c) / 3 for b in bars]
    for i in range(period - 1, n):
        window = tp[i - period + 1:i + 1]
        m = sum(window) / period
        dev = sum(abs(x - m) for x in window) / period
        out[i] = 0.0 if dev == 0 else (tp[i] - m) / (0.015 * dev)
    return out


def williams_r(bars: list[Bar], period: int = 14) -> list[float]:
    n = len(bars)
    out = [float("nan")] * n
    for i in range(period - 1, n):
        high = max(b.h for b in bars[i - period + 1:i + 1])
        low = min(b.l for b in bars[i - period + 1:i + 1])
        out[i] = 0.0 if high == low else -100 * (high - bars[i].c) / (high - low)
    return out


def candle_shape(bar: Bar) -> dict:
    """Anatomy of one candle: body, wicks and direction, in fractions of its range."""
    rng = bar.h - bar.l
    body = abs(bar.c - bar.o)
    upper = bar.h - max(bar.o, bar.c)
    lower = min(bar.o, bar.c) - bar.l
    if rng <= 0:
        return {"dir": "flat", "body": 0.0, "upper_wick": 0.0, "lower_wick": 0.0, "range": 0.0}
    return {
        "dir": "up" if bar.c > bar.o else "down" if bar.c < bar.o else "flat",
        "body": round(body / rng, 3),
        "upper_wick": round(upper / rng, 3),
        "lower_wick": round(lower / rng, 3),
        "range": round(rng, 2),
    }


def swing_levels(bars: list[Bar], i: int, lookback: int = 80, left: int = 2, right: int = 2) -> list[dict]:
    """Recent fractal pivots, i.e. the obvious support/resistance a scalp trades against."""
    out = []
    start = max(left, i - lookback)
    for j in range(start, i - right):
        window_h = [b.h for b in bars[j - left:j + right + 1]]
        window_l = [b.l for b in bars[j - left:j + right + 1]]
        if bars[j].h == max(window_h):
            out.append({"kind": "high", "price": bars[j].h, "bars_ago": i - j})
        if bars[j].l == min(window_l):
            out.append({"kind": "low", "price": bars[j].l, "bars_ago": i - j})
    return sorted(out, key=lambda lvl: lvl["bars_ago"])[-6:]


def nearest_level(levels: list[dict], price: float, kind: str) -> float | None:
    candidates = [lvl["price"] for lvl in levels
                  if (lvl["kind"] == "high" and lvl["price"] >= price and kind == "above")
                  or (lvl["kind"] == "low" and lvl["price"] <= price and kind == "below")]
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs(p - price))


def session(now: datetime) -> dict:
    """Gold liquidity window in UTC. Scalps behave very differently per session."""
    weekday, hour = now.weekday(), now.hour
    closed = (weekday == 4 and hour >= 21) or weekday == 5 or (weekday == 6 and hour < 22)
    if closed:
        name, quality = "market closed (weekend)", "none"
    elif 12 <= hour < 16:
        name, quality = "London–New York overlap", "highest"
    elif 7 <= hour < 12:
        name, quality = "London", "high"
    elif 16 <= hour < 21:
        name, quality = "New York afternoon", "medium"
    elif 22 <= hour or hour < 7:
        name, quality = "Asia", "low"
    else:
        name, quality = "off-session", "low"
    return {"utc_hour": hour, "weekday": now.strftime("%a"), "session": name, "liquidity": quality}


def summarise_lower_tf(bars: list[Bar]) -> dict:
    """Compact picture of the 5-minute chart used only to time the entry."""
    closes = [b.c for b in bars]
    i = len(bars) - 1
    e9, e20 = ema(closes, 9)[i], ema(closes, 20)[i]
    return {
        "interval": M5_INTERVAL,
        "bars": len(bars),
        "last_bar_start": bars[i].dt,
        "ema9": round(e9, 2),
        "ema20": round(e20, 2),
        "micro_trend": "up" if e9 > e20 else "down",
        "rsi7": round(rsi(closes, 7)[i], 1),
        "last_3_closes": [round(b.c, 2) for b in bars[-3:]],
        "last_3_candles": [candle_shape(b) for b in bars[-3:]],
    }


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def safe_num(value, digits: int = 2):
    """JSON-safe number: NaN/inf become None so the AI payload stays serialisable."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if math.isfinite(v) else None


def percentile_rank(series: list[float], value: float, lookback: int = 100) -> float | None:
    """Where today's value sits in its own recent history, as a 0-100 rank."""
    window = [v for v in series[-(lookback + 1):-1] if v == v]
    if not window or value != value:
        return None
    return round(100 * sum(1 for v in window if v <= value) / len(window), 1)


def scalp_plan(price: float, atr_value: float, side: int, spread: float | None = None) -> dict:
    """Scalp-sized levels, cost accounting and horizon fit for one direction.

    Everything here is arithmetic on the 15-minute ATR. It is not a forecast:
    the plan says what the trade *would* have to do to beat its costs inside the
    hold window, not that it will do it.
    """
    spread = SPREAD_USD if spread is None else spread
    stop_dist = ATR_SL * atr_value
    target_dist = ATR_TP * atr_value
    stop, target = price - side * stop_dist, price + side * target_dist
    risk = stop_dist + spread          # spread widens the effective loss
    reward = target_dist - spread      # spread eats the same amount of profit
    net_rr = reward / risk if risk > 0 else 0.0
    expected_move = atr_value * math.sqrt(HOLD_MAX / 15.0)
    return {
        "side": side,
        "stop": stop,
        "target": target,
        "stop_usd": stop_dist,
        "target_usd": target_dist,
        "risk_usd": risk,
        "reward_usd": reward,
        "net_rr": net_rr,
        "expected_move_usd": expected_move,
        "target_vs_expected": (target_dist + spread) / expected_move if expected_move > 0 else None,
        "passes_cost_gate": bool(min(stop, target) > 0 and net_rr >= MIN_NET_RR),
    }


def score_strategies(bars: list[Bar], ind: dict) -> list[dict]:
    """Five classic short-term playbooks, scored locally.

    These are mechanical readings of the same candles, not evidence of an edge;
    they exist so the AI sees the setups spelled out instead of guessing.
    """
    i, price, a = len(bars) - 1, ind["price"], ind["atr"]
    e9, e20, e50 = ind["ema9"], ind["ema20"], ind["ema50"]
    r7, r14 = ind["rsi7"], ind["rsi14"]
    adx_v, pdi, mdi = ind["adx"], ind["plus_di"], ind["minus_di"]
    hist, sig = ind["macd_hist"], ind["macd_signal"]
    bb_up, bb_lo, pctb = ind["bb_up"], ind["bb_lo"], ind["bb_pctb"]
    k, d = ind["stoch_k"], ind["stoch_d"]
    atr_pctile = ind["atr_percentile"]
    last = bars[i]
    shape = candle_shape(last)
    out: list[dict] = []

    def add(name, direction, score, note):
        out.append({"name": name, "direction": "BUY" if direction > 0 else "SELL" if direction < 0 else "none",
                    "score": int(score), "note": note})

    # 1. Trend pullback continuation
    if e20 > e50 and e9 > e20:
        direction, why = 1, "uptrend stack"
    elif e20 < e50 and e9 < e20:
        direction, why = -1, "downtrend stack"
    else:
        direction, why = 0, "no EMA stack"
    near = abs(price - e20) <= 0.75 * a
    touched = any(min(bars[j].l, bars[j].h) <= e20 <= max(bars[j].l, bars[j].h) for j in (i - 1, i - 2))
    score = 0
    if direction:
        score = 1 + (near or touched) + (adx_v >= 20) + \
            ((r7 < 45 and direction > 0) or (r7 > 55 and direction < 0)) + \
            ((hist > 0) if direction > 0 else (hist < 0))
    add("trend_pullback", direction, score,
        f"{why}; price {'at' if near else 'away from'} EMA20, ADX {adx_v:.0f}")

    # 2. Breakout of the 20-bar range
    if price > ind["hi20"]:
        direction = 1
    elif price < ind["lo20"]:
        direction = -1
    else:
        direction = 0
    expanding = (atr_pctile or 0) >= 55 or (last.h - last.l) >= 1.2 * a
    clean_close = False
    if direction and last.h > last.l:
        clean_close = ((last.c - last.l) / (last.h - last.l) > 0.6 if direction > 0
                       else (last.h - last.c) / (last.h - last.l) > 0.6)
    score = 0
    if direction:
        score = 2 + expanding + (adx_v >= 18 or (pdi > mdi if direction > 0 else mdi > pdi)) + clean_close
    add("breakout", direction, score,
        f"{'close beyond' if direction else 'inside'} 20-bar "
        f"{'high' if direction > 0 else 'low' if direction < 0 else 'range'}, ATR {atr_pctile or 0:.0f}th pct")

    # 3. Mean reversion at a Bollinger extreme
    if pctb is not None and pctb >= 0.98 and r7 >= 70:
        direction = -1
    elif pctb is not None and pctb <= 0.02 and r7 <= 30:
        direction = 1
    else:
        direction = 0
    rejection = (shape["upper_wick"] >= 0.35) if direction < 0 else (shape["lower_wick"] >= 0.35)
    extended = abs(price - e20) >= 1.5 * a
    score = 0
    if direction:
        score = 2 + rejection + extended + (adx_v < 30)
    label = f"%B {pctb:.2f}" if pctb is not None else "%B n/a"
    add("mean_reversion", direction, score,
        f"{label}, RSI7 {r7:.0f}, {'rejection wick' if rejection else 'no rejection wick'}")

    # 4. MACD momentum shift
    flips = [ind[f"macd_hist_{n}_ago"] for n in (1, 2)]
    if hist > 0 and flips[0] <= 0:
        direction = 1
    elif hist < 0 and flips[0] >= 0:
        direction = -1
    elif hist > 0 and flips[1] <= 0:
        direction = 1
    elif hist < 0 and flips[1] >= 0:
        direction = -1
    else:
        direction = 0
    score = 0
    if direction:
        score = 2 + (adx_v >= 18) + ((e9 > e20) if direction > 0 else (e9 < e20)) + \
            ((r14 > 50) if direction > 0 else (r14 < 50)) + \
            ((last.c > last.o) if direction > 0 else (last.c < last.o))
    add("momentum_macd", direction, score,
        f"hist {hist:+.2f} vs signal {sig:+.2f}{', fresh cross' if direction else ', no fresh cross'}")

    # 5. Range fade (only meaningful when the market is not trending)
    if adx_v < 22:
        span = ind["hi20"] - ind["lo20"]
        pos = (price - ind["lo20"]) / span if span > 0 else 0.5
        direction = -1 if pos >= 0.85 else 1 if pos <= 0.15 else 0
        if direction:
            score = 1 + (adx_v < 15) + ((k >= 80) if direction < 0 else (k <= 20)) + \
                ((ind["cci"] >= 100) if direction < 0 else (ind["cci"] <= -100))
            add("range_fade", direction, score,
                f"ADX {adx_v:.0f} (chop), price at {pos * 100:.0f}% of the 20-bar range")
        else:
            add("range_fade", 0, 0, f"ADX {adx_v:.0f} (chop), mid-range — no fade")
    else:
        add("range_fade", 0, 0, f"ADX {adx_v:.0f} — trending, fading is unsafe")

    return sorted(out, key=lambda item: -item["score"])


def analyse(bars: list[Bar]) -> dict:
    """Deterministic. Same candles in -> same dict out, every time."""
    if not 1 <= MIN_CONF <= 5 or not math.isfinite(ATR_SL) or not math.isfinite(ATR_TP) or ATR_SL <= 0 or ATR_TP <= 0:
        raise ValueError("MIN_CONF must be 1–5 and ATR_SL/ATR_TP must be positive finite numbers")
    if not (0 < HOLD_MIN <= HOLD_MAX <= 240):
        raise ValueError("HOLD_MIN/HOLD_MAX must satisfy 0 < HOLD_MIN <= HOLD_MAX <= 240 minutes")
    if not math.isfinite(SPREAD_USD) or SPREAD_USD < 0:
        raise ValueError("SPREAD_USD must be a non-negative finite number")
    if not math.isfinite(MIN_NET_RR) or MIN_NET_RR <= 0:
        raise ValueError("MIN_NET_RR must be a positive finite number")
    if len(bars) < 220:
        raise ValueError("at least 220 bars required")
    closes = [b.c for b in bars]
    i = len(bars) - 1

    price = bars[i].c
    prev = bars[i - 1].c
    e9, e20, e50, e200 = (ema(closes, p)[i] for p in (9, 20, 50, 200))
    r, r7 = rsi(closes, 14)[i], rsi(closes, 7)[i]
    a = atr(bars, 14)[i]
    atr_series = atr(bars, 14)
    hi20 = max(b.h for b in bars[i - 20:i])
    lo20 = min(b.l for b in bars[i - 20:i])
    macd_line, macd_sig, macd_hist = macd(closes)
    bb_mid, bb_up, bb_lo = bollinger(closes)
    _, stoch_k, stoch_d = stoch(bars)
    adx_v, pdi, mdi = adx(bars, 14)
    cci_v, wr = cci(bars, 20), williams_r(bars, 14)
    levels = swing_levels(bars, i)

    ind = {
        "price": price,
        "ema9": e9, "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi14": r, "rsi7": r7,
        "atr": a, "atr_percentile": percentile_rank(atr_series, a) or 0.0,
        "atr_pct_of_price": 100 * a / price if price else float("nan"),
        "hi20": hi20, "lo20": lo20,
        "macd": macd_line[i], "macd_signal": macd_sig[i], "macd_hist": macd_hist[i],
        "macd_hist_1_ago": macd_hist[i - 1], "macd_hist_2_ago": macd_hist[i - 2],
        "bb_mid": bb_mid[i], "bb_up": bb_up[i], "bb_lo": bb_lo[i],
        "bb_pctb": (price - bb_lo[i]) / (bb_up[i] - bb_lo[i]) if bb_up[i] > bb_lo[i] else None,
        "stoch_k": stoch_k[i], "stoch_d": stoch_d[i],
        "adx": adx_v[i], "plus_di": pdi[i], "minus_di": mdi[i],
        "cci": cci_v[i], "williams_r": wr[i],
    }

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
        med = sorted(x for x in atr_series[max(0, i - 100):i] if x == x)
        if med:
            m = med[len(med) // 2]
            if 0.6 * m <= a <= 2.0 * m:
                reasons.append("volatility normal")

    conf = len(reasons)
    signal = side if conf >= MIN_CONF and a > 0 else 0
    strategies = score_strategies(bars, ind) if a > 0 else []
    plans = {"BUY": scalp_plan(price, a, 1), "SELL": scalp_plan(price, a, -1)} if a > 0 else {}
    plan = plans.get("BUY" if signal > 0 else "SELL") if signal else None
    now = datetime.now(timezone.utc)

    out = {
        "bar_time": bars[i].dt,
        "price": price,
        "change": price - prev,
        "ema9": e9, "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi": r, "rsi7": r7, "atr": a, "hi20": hi20, "lo20": lo20,
        "confidence": conf, "reasons": reasons,
        "side": signal,
        "action": "BUY" if signal > 0 else "SELL" if signal < 0 else "STAND ASIDE",
        # --- scalp additions ---
        "macd": macd_line[i], "macd_signal": macd_sig[i], "macd_hist": macd_hist[i],
        "bb_up": bb_up[i], "bb_lo": bb_lo[i], "bb_pctb": ind["bb_pctb"],
        "stoch_k": stoch_k[i], "stoch_d": stoch_d[i],
        "adx": adx_v[i], "cci": cci_v[i], "williams_r": wr[i],
        "atr_percentile": ind["atr_percentile"],
        "session": session(now),
        "candle_shapes": [candle_shape(b) for b in bars[-4:]],
        "levels": levels,
        "nearest_resistance": nearest_level(levels, price, "above"),
        "nearest_support": nearest_level(levels, price, "below"),
        "strategies": strategies,
        "plan": plan,
        "plans": plans,
        "hold_min": HOLD_MIN,
        "hold_max": HOLD_MAX,
        "spread": SPREAD_USD,
        "min_net_rr": MIN_NET_RR,
        "indicators_full": ind,
    }
    if signal != 0:
        out["entry"] = price
        out["stop_loss"] = price - signal * ATR_SL * a
        out["take_profit"] = price + signal * ATR_TP * a
        out["rr"] = ATR_TP / ATR_SL
        out["plan"] = plan
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


def fmt(value, spec: str = ".2f", fallback: str = "n/a") -> str:
    """Format a number for Telegram; NaN/inf/None become n/a."""
    v = safe_num(value, 4)
    return format(v, spec) if v is not None else fallback


def format_ai_message(s: dict, decision: dict) -> str:
    """AI decides direction; levels, costs and the horizon are calculated locally."""
    action = decision["action"]
    icon = {"BUY": "🟢", "SELL": "🔴"}.get(action, "⚪")
    lines = [
        f"{icon} GOLD {SYMBOL} — {action} · scalp {s['hold_min']}–{s['hold_max']}m",
        f"AI decision: {kimi_brain.current_model()}",
        f"15m bar start: {s['bar_time']} UTC (closed)",
    ]
    if action in ("BUY", "SELL"):
        plan = (s.get("plans") or {}).get(action) or scalp_plan(s["price"], s["atr"],
                                                                1 if action == "BUY" else -1)
        hold = decision.get("hold_minutes")
        lines += [
            "",
            f"Reference close {s['price']:.2f} (not a live quote)",
            f"Stop {plan['stop']:.2f} (-{plan['stop_usd']:.2f})   Target {plan['target']:.2f} (+{plan['target_usd']:.2f})",
            f"Net R:R {plan['net_rr']:.2f} after {s['spread']:.2f} spread",
            f"Target = {fmt(plan['target_vs_expected'])}× the typical {s['hold_max']}m move ({fmt(plan['expected_move_usd'])} USD)",
            f"Flat by {s['hold_max']} min" + (f" · AI expects {hold}m" if hold else ""),
        ]
    lines += [
        "",
        f"Session {s['session']['session']} (liquidity {s['session']['liquidity']})",
        f"ADX {fmt(s['adx'], '.0f')} · ATR {fmt(s['atr'])} ({fmt(s['atr_percentile'], '.0f')}th pct) · "
        f"RSI7 {fmt(s['rsi7'], '.0f')} / RSI14 {fmt(s['rsi'], '.0f')}",
        f"MACD hist {fmt(s['macd_hist'], '+.2f')} · Stoch {fmt(s['stoch_k'], '.0f')}/{fmt(s['stoch_d'], '.0f')} · "
        f"CCI {fmt(s['cci'], '.0f')} · %B {fmt(s['bb_pctb'])}",
        f"EMA9 {fmt(s['ema9'])} | EMA20 {fmt(s['ema20'])} | EMA50 {fmt(s['ema50'])} | EMA200 {fmt(s['ema200'])}",
        f"20-bar range {fmt(s['lo20'])} – {fmt(s['hi20'])}",
    ]
    if decision.get("strategy"):
        lines.append(f"Playbook: {decision['strategy']}")
    top = [x for x in (s.get("strategies") or []) if x["score"] >= 3][:2]
    if top:
        lines.append("Local read: " + "; ".join(f"{x['name']} {x['direction']} {x['score']}/5" for x in top))
    lines += [
        f"AI reason: {decision['reason']}",
        f"Rule reference: {s['action']} ({s['confidence']}/5 checks)",
        "",
        "⚠️ Experimental AI monitor, not advice. Costs are assumptions, not measurements.",
    ]
    return "\n".join(lines)


def format_cost_blocked_message(s: dict, decision: dict, plan: dict) -> str:
    """The setup exists, but the spread eats the scalp: a deliberate no-trade."""
    return "\n".join([
        f"⚪ GOLD {SYMBOL} — STAND ASIDE · scalp {s['hold_min']}–{s['hold_max']}m",
        f"AI wanted {decision['action']}, but the costs do not fit the horizon.",
        f"15m bar start: {s['bar_time']} UTC (closed)",
        "",
        f"ATR {fmt(s['atr'])} · target {fmt(plan['target_usd'])} · risk {fmt(plan['risk_usd'])}",
        f"Net R:R {plan['net_rr']:.2f} < {s['min_net_rr']:.2f} after the assumed {s['spread']:.2f} spread",
        f"Session {s['session']['session']} (liquidity {s['session']['liquidity']})",
        "",
        "Wait for more volatility, a tighter spread, or a longer hold window.",
        "⚠️ Experimental AI monitor, not advice. No proven edge.",
    ])


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
        lower_tf = fetch_lower_tf()
        try:
            if not all(math.isfinite(s[k]) for k in ("price", "atr", "ema20", "ema50", "ema200", "rsi", "hi20", "lo20")):
                raise kimi_brain.AIError("market indicators are not finite")
            if s["atr"] <= 0:
                raise kimi_brain.AIError("ATR is zero; cannot calculate risk levels")
            decision = kimi_brain.decide(bars, s, SYMBOL, lower_tf)
            plan = None
            if decision["action"] != "STAND ASIDE":
                plan = (s.get("plans") or {}).get(decision["action"])
                if plan is None:
                    raise kimi_brain.AIError("cannot calculate valid displayed risk levels")
                if (not math.isfinite(plan["stop"]) or not math.isfinite(plan["target"])
                        or min(plan["stop"], plan["target"]) <= 0
                        or round(plan["stop"], 2) == round(s["price"], 2)
                        or round(plan["target"], 2) == round(s["price"], 2)):
                    raise kimi_brain.AIError("cannot calculate valid displayed risk levels")
            # A slow AI response must not turn a once-fresh candle into a stale signal.
            bar_start = datetime.fromisoformat(s["bar_time"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - (bar_start + BAR_DURATION) > MAX_CLOSED_BAR_AGE:
                raise kimi_brain.AIError("market data became stale while waiting for AI")
        except kimi_brain.AIError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            send_telegram(f"⚪ {SYMBOL} — STAND ASIDE\nAI unavailable or invalid; no trading signal generated. Check workflow logs.")
            return 1

        strategy = decision.get("strategy") or "-"
        print(f"  bar={s['bar_time']} price={s['price']:.2f} adx={fmt(s['adx'], '.0f')} "
              f"atr={s['atr']:.2f} rule_conf={s['confidence']}/5 "
              f"ai_action={decision['action']} playbook={strategy} "
              f"m5={'yes' if lower_tf else 'no'}")

        # Cost gate: a scalp whose target cannot beat the spread inside the hold
        # window is a no-trade, not a signal.
        if plan is not None and not plan["passes_cost_gate"]:
            print(f"  STAND ASIDE: net R:R {plan['net_rr']:.2f} < {s['min_net_rr']:.2f} "
                  f"after a {s['spread']:.2f} spread on ATR {s['atr']:.2f}")
            ok = send_telegram(format_cost_blocked_message(s, decision, plan))
            print("  ✅ sent" if ok else "  ⚠️ not sent (see above)")
            return 0 if ok else 1

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
