# Gold (XAUUSD) Signal Bot — Linux, Signal-Only

Runs entirely on native Linux. No MT5, no Wine, no broker login required.
Claude analyzes gold price data and sends you a signal via Telegram; you
manually place the trade in VT Markets/XM yourself if you like it.

## What it does NOT do
- Does not log into VT Markets or XM
- Does not place any trades
- Does not need Windows/Wine/MT5 at all

## Option A: Run for free via GitHub Actions (recommended — nothing to host)

This runs the bot on GitHub's servers on a schedule. No VPS, no cost, and
your iPhone doesn't need to run anything — it just receives the Telegram
message. (iOS doesn't allow apps to run continuous background scripts, so
this cloud-schedule approach is the practical way to get "always running,
reachable anywhere" behavior.)

### Steps
1. Create a new **private** GitHub repo and push all these files to it
   (including `.github/workflows/gold-signal.yml`).
2. Go to the repo's **Settings → Secrets and variables → Actions** and add
   four repository secrets:
   - `ANTHROPIC_API_KEY`
   - `TWELVE_DATA_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   (See the "Get a free Twelve Data API key" and "Create a Telegram bot"
   sections below for how to obtain these.)
3. Go to the **Actions** tab, find "Gold Signal Bot," and click
   **Enable workflow** if prompted.
4. It'll now run automatically every ~15 minutes. To test it immediately
   without waiting, go to Actions → Gold Signal Bot → **Run workflow**
   (this is the `workflow_dispatch` trigger).
5. Telegram messages will arrive on your phone as normal — no dashboard
   needed for this mode, though `app.py`/`state.json` won't persist
   between runs here since each run is a fresh container. If you want
   history logging in this mode too, ask and I can add a step that commits
   `state.json` back to the repo after each run.

**Note on schedule timing:** GitHub Actions cron schedules are "best
effort" — during periods of high platform load, runs can be delayed by a
few minutes. Fine for this use case, just not millisecond-precise.

## Option B: Run continuously on your own Linux machine

### 1. Install dependencies
```
pip install requests pandas anthropic flask
```

### 2. Get a free Twelve Data API key (price data source)
- Sign up at https://twelvedata.com (free tier is enough for 15-min polling)
- Copy your API key

### 3. Create a Telegram bot
1. Open Telegram, search for **@BotFather**, start a chat
2. Send `/newbot`, follow the prompts (pick a name, and a username ending in `bot`)
3. BotFather replies with a token like `123456789:AAExampleTokenString` — save it
4. Send your new bot any message (e.g. "hi") so it registers your chat
5. Visit this in a browser, replacing `TOKEN`:
   `https://api.telegram.org/botTOKEN/getUpdates`
6. Find `"chat":{"id": ...}` in the response — that number is your chat ID

### 4. Set environment variables
```
export ANTHROPIC_API_KEY="your-anthropic-key"
export TWELVE_DATA_API_KEY="your-twelvedata-key"
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
```
(Add these to `~/.bashrc` so they persist across terminal sessions.)

### 5. Run it
In one terminal:
```
python signal_bot.py
```
In a second terminal, if you want the web log too:
```
python app.py
```
Open `http://<your-linux-ip>:5000` from your phone's browser for the signal
history. Telegram messages arrive automatically — no need to keep the
dashboard open.

## Files
- `data_feed.py` — pulls XAUUSD candles + indicators from Twelve Data
- `signal_bot.py` — continuous-loop version (Option B: your own machine)
- `run_once.py` — single-cycle version (Option A: GitHub Actions)
- `.github/workflows/gold-signal.yml` — the GitHub Actions schedule
- `requirements.txt` — dependencies for the GitHub Actions runner
- `telegram_notify.py` — Telegram send helper + message formatting
- `shared_state.py` — JSON log file shared with the dashboard (Option B only)
- `app.py` + `templates/dashboard.html` — optional view-only web log (Option B only)

## Notes
- Poll interval defaults to 15 minutes (`POLL_SECONDS` in `signal_bot.py`),
  matching the candle timeframe. Change both together if you adjust it.
- Since there's no execution, there's no risk filter needed here — but the
  system prompt still nudges Claude toward conservative sizing and "hold"
  when signals are unclear. Treat every signal as a suggestion to evaluate,
  not an instruction to follow blindly.
- This can run on any always-on Linux machine (a Raspberry Pi works fine)
  since there's no MT5/Wine dependency.
