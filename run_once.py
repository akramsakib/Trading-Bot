"""
Single-cycle version of the signal bot, meant for GitHub Actions.

Unlike signal_bot.py (which loops forever with time.sleep), this runs ONE
check-and-notify cycle and exits — because GitHub Actions spins up a fresh
container per scheduled run rather than keeping a process alive. The
schedule itself (see .github/workflows/gold-signal.yml) is what provides
the "every 15 minutes" cadence.
"""

import json
from anthropic import Anthropic

from data_feed import get_market_snapshot, SYMBOL
from telegram_notify import send_telegram_message, format_signal_message

CLAUDE_MODEL = "claude-sonnet-4-6"

client = Anthropic()

SYSTEM_PROMPT = """You are a trading signal generator for gold (XAUUSD). \
The person you're advising will manually review and place any trade themselves \
in their own broker app — you are not executing trades. \
Respond with ONLY a single JSON object, no prose, no markdown fences, matching \
exactly this schema:

{
  "action": "buy" | "sell" | "close" | "hold",
  "lot_size": number,
  "stop_loss_pips": number,
  "take_profit_pips": number,
  "confidence": number,
  "reasoning": string
}

Rules:
- Only signal "buy" or "sell" when trend and momentum indicators reasonably agree.
- Default to "hold" when signals are mixed or unclear — do not force signals.
- lot_size is a suggestion only, keep it conservative (0.01–0.10 range) since \
this is for demo/practice trading.
- Keep reasoning to one or two sentences.
"""


def build_user_prompt(df) -> str:
    last = df.iloc[-1]
    recent = df.tail(10)[["datetime", "close", "ema20", "ema50", "rsi"]].to_dict(orient="records")

    payload = {
        "symbol": SYMBOL,
        "timeframe": "15min",
        "current_price": float(last["close"]),
        "ema20": round(float(last["ema20"]), 2),
        "ema50": round(float(last["ema50"]), 2),
        "rsi": round(float(last["rsi"]), 2) if last["rsi"] == last["rsi"] else None,
        "recent_candles": recent,
    }
    return json.dumps(payload, default=str)


def get_claude_signal(df) -> dict:
    user_prompt = build_user_prompt(df)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = response.content[0].text.strip()
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {"action": "hold", "reasoning": f"parse_error: {raw_text[:200]}"}


def main():
    df = get_market_snapshot()
    signal = get_claude_signal(df)
    print(f"[SIGNAL] {signal}")

    message = format_signal_message(SYMBOL, signal)
    send_telegram_message(message)


if __name__ == "__main__":
    main()
