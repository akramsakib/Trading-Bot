# Gold (XAU/USD) 15-Minute Signal Bot

Analyses the 15-minute gold chart every 15 minutes and sends you one Telegram
message per run — a full analysis update with an explicit **BUY / SELL / STAND
ASIDE**, its confluence score, and the reasons behind it.

Runs entirely on GitHub Actions. No server, no VPS, no broker login, no MT5.
No LLM — the analysis is deterministic, so the same candles always produce the
same message.

---

## What you get on Telegram

Every 15 minutes:

```
🔴 GOLD XAU/USD — SELL
15m bar: 2026-09-08 17:31:06 UTC

Price 4439.60  ▲ +0.30
RSI 46.8   ATR 8.54
EMA20 4441.16 | EMA50 4448.02 | EMA200 4469.07
20-bar range 4425.10 – 4457.90

Confluence 4/5  ████░
  ✓ trend vs EMA200
  ✓ EMA20/50 stacked
  ✓ RSI not exhausted (47)
  ✓ volatility normal

Entry 4439.60
Stop 4452.42   Target 4422.51   R:R 1.3

⚠️ Rule-based monitor, not advice.
```

When nothing lines up, you still get the update — it just says **STAND ASIDE**
and shows which of the five tests failed.

---

## How the analysis works

Direction comes from a 20-bar range break, falling back to the EMA stack.
Then five **independent** tests are scored:

| # | Test | Passes when |
|---|---|---|
| 1 | trend vs EMA200 | price is on the trend side of the 200 EMA |
| 2 | EMA20/50 stacked | the short EMAs agree with the direction |
| 3 | RSI not exhausted | RSI(14) is between 30 and 70 |
| 4 | broke 20-bar range | close is outside the last 20 bars' high/low |
| 5 | volatility normal | ATR is 0.6–2.0× its recent median |

A **BUY/SELL** is only called when the score reaches `MIN_CONF` (default **4**).
Below that you get STAND ASIDE. Stop loss is 1.5×ATR, target 2.0×ATR.

---

## Setup

### 1. Get a Twelve Data API key (free)

1. Sign up at <https://twelvedata.com>
2. Copy your API key. The free tier is enough for a 15-minute poll.

### 2. Create a Telegram bot

1. In Telegram, search for **@BotFather** and start a chat.
2. Send `/newbot`, then pick a name and a username ending in `bot`.
3. BotFather replies with a token like `123456789:AAExampleTokenString`. Keep it.
4. **Send your new bot any message** (e.g. "hi") — it can't message you until you do.
5. Open this in a browser, replacing `TOKEN`:
   `https://api.telegram.org/botTOKEN/getUpdates`
6. Find `"chat":{"id":123456789}` in the response. That number is your chat ID.

### 3. Push this repo

```bash
git clone https://github.com/akramsakib/Trading-Bot.git
cd Trading-Bot
git remote set-url origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 4. Add the three secrets

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `TWELVE_DATA_API_KEY` | your Twelve Data key |
| `TELEGRAM_BOT_TOKEN` | the BotFather token |
| `TELEGRAM_CHAT_ID` | your numeric chat ID |

> Put these in **Secrets**, never in the code or the workflow file.

### 5. Turn the schedule on

Go to the **Actions** tab → **Gold 15m Signal** → **I understand my workflows,
go ahead and enable them** if prompted.

To test immediately instead of waiting: click **Run workflow** (that's the
`workflow_dispatch` trigger). You should get a Telegram message within a minute.

After that it runs automatically at :00, :15, :30 and :45 every hour.

---

## Tuning

Change these in `.github/workflows/gold-signal.yml` under `env`:

| Variable | Default | Effect |
|---|---|---|
| `MIN_CONF` | `4` | `5` = fewer, stricter signals. `3` = more, noisier. |

Or edit `signal.py`:

| Setting | Default | Meaning |
|---|---|---|
| `ATR_SL` | `1.5` | stop loss distance, in ATR |
| `ATR_TP` | `2.0` | target distance, in ATR |
| `SYMBOL` | `XAU/USD` | any Twelve Data symbol |

### Running it locally

```bash
pip install -r requirements.txt
export TWELVE_DATA_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python signal.py
```

Leave the Telegram variables unset and it prints the message to your terminal
instead of sending it — useful for testing.

---

## Known limitations — read this

**This is a monitor, not a profitable system.** The backtests in this project's
history found no reliable edge: on 2.4 years of hourly gold, buy-and-hold beat
every rule tested, and a random-entry baseline was profitable in the same
number of periods as the best rule. The confluence filter cuts noise
dramatically; it does not create an edge. **Verify every signal on your own
chart before acting on it.**

**Prices will not exactly match MT5 / VT Markets.** Expect a permanent offset
of roughly $0.15–$0.40:

- MT5 charts show the **bid**; Twelve Data's XAU/USD is a mid/last aggregation.
- They are **different data providers** with different liquidity sources.
- Candle close times depend on your broker's server timezone (often GMT+2/+3).
- Polling every 15 minutes means you are always up to a quarter-hour behind.

If you need your broker's exact price, the data source has to be your broker.

**GitHub Actions cron is best-effort.** During high platform load runs can be
delayed by a few minutes. Fine here, but not second-precise.

**Rate limits.** 96 messages a day to one chat is well inside Telegram's limits.
Twelve Data's free tier allows it, but if you shorten the interval you will hit
the ~800 requests/day cap.

---

## Files

| File | Purpose |
|---|---|
| `signal.py` | the whole bot: fetch, indicators, confluence, Telegram |
| `.github/workflows/gold-signal.yml` | the every-15-minutes schedule |
| `requirements.txt` | just `requests` |

---

## Security

Secrets live in GitHub Actions, never in the repo. The code logs Telegram
failure status codes but never the token. `.gitignore` blocks `.env`, `*.pem`,
`*.key` and `credentials.json`.

## License

MIT — see `LICENSE`.
