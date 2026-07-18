"""
Sends formatted signal messages to your phone via a Telegram bot.

Setup (one-time):
1. Open Telegram, search for @BotFather, start a chat.
2. Send /newbot, follow the prompts (choose a name and a username ending in 'bot').
3. BotFather gives you a token like: 123456789:AAExampleTokenString
4. Send your new bot any message (e.g. "hi") so it can find your chat.
5. Visit this URL in a browser (replace TOKEN):
   https://api.telegram.org/botTOKEN/getUpdates
   Look for "chat":{"id": ...} in the response — that number is your CHAT_ID.
6. Set both as environment variables:
   export TELEGRAM_BOT_TOKEN="123456789:AAExampleTokenString"
   export TELEGRAM_CHAT_ID="987654321"
"""

import os
import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("[WARN] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping notification.")
        print(text)
        return

    url = API_URL.format(token=BOT_TOKEN)
    resp = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }, timeout=10)

    if not resp.ok:
        print(f"[WARN] Telegram send failed: {resp.status_code} {resp.text}")


def format_signal_message(symbol: str, signal: dict) -> str:
    action = signal.get("action", "hold").upper()
    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪", "CLOSE": "🟡"}.get(action, "⚪")

    lines = [f"{emoji} *{symbol}* — *{action}*"]

    if action in ("BUY", "SELL"):
        lines.append(f"Size: {signal.get('lot_size', '—')} lots")
        lines.append(f"SL: {signal.get('stop_loss_pips', '—')} pips")
        lines.append(f"TP: {signal.get('take_profit_pips', '—')} pips")

    lines.append(f"Confidence: {signal.get('confidence', '—')}")
    if signal.get("reasoning"):
        lines.append(f"_{signal['reasoning']}_")

    return "\n".join(lines)
