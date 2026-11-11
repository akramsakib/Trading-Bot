# Gold (XAU/USD) 15-Minute AI Signal Monitor

This experimental GitHub Actions bot fetches 15-minute gold candles, asks
**Kimi K2.6** to choose BUY, SELL, or STAND ASIDE, and sends the result to
Telegram. It does **not** place trades or connect to a broker.

**The AI strategy has not been backtested or proven profitable.** AI can make
mistakes, invent explanations, or change its answer for the same data. Paper
trade and independently verify signals before considering any real-money use.

## How it works

1. Twelve Data supplies candles. The bot requests UTC, assumes timestamps are
   candle start times, and uses only candles at least two minutes past closing.
2. Code calculates RSI, ATR, EMA20/50/200, a prior 20-bar range and the original
   rule-based reference. Kimi receives those values plus the last 40 closed
   OHLC candles. It has no live quote, news, account information, or tools.
3. Kimi chooses the final action. It can disagree with the rule-based reference,
   including choosing BUY or SELL when the rules say STAND ASIDE. The rule
   score is **not** a probability. `MIN_CONF` affects only the reference, not
   the AI's decision.
4. For BUY/SELL, the code (not the AI) calculates illustrative stop and target
   levels at 1.5×ATR and 2×ATR from the last close. These are not executable
   quotes; spreads, slippage, and delayed delivery change real risk and reward.
5. Invalid or incomplete AI output, a missing key, timeouts, or data that
   becomes stale while waiting result in STAND ASIDE rather than falling back
   to a rule-based trade. The run fails so the problem is visible in Actions.
   A stale feed or market closure also produces STAND ASIDE without calling AI;
   expect repeated messages during weekends. Other market-data errors fail the
   run without a signal. Telegram delivery failures also fail the run.

The bot validates the AI response format, **not the truth or profitability**
of its reasoning. Only market candles and indicators are sent to Moonshot AI;
API keys and Telegram details are not included in the AI prompt.

## Setup

1. Get a [Twelve Data API key](https://twelvedata.com) and a **Moonshot/Kimi
   Global API key** from the [Kimi API platform](https://platform.moonshot.ai/).
   This integration uses `https://api.moonshot.ai/v1` and `kimi-k2.6` in
   thinking mode. An Arena API key is **not** used or interchangeable with a
   Moonshot key. Make sure your account has access and sufficient API credit.
   Note that Moonshot plans to retire the K2 series by the end of December
   2026; `kimi-k3` and `kimi-k3.1-pro` are the current replacements.
2. Create a Telegram bot with @BotFather and send it a message. Obtain its chat
   ID through Telegram's `getUpdates` endpoint. Keep the bot token private.
3. In the GitHub repository, open **Settings → Secrets and variables → Actions
   → New repository secret**, and add:

   | Name | Value |
   |---|---|
   | `MOONSHOT_API_KEY` | Kimi Global API key |
   | `TWELVE_DATA_API_KEY` | Twelve Data API key |
   | `TELEGRAM_BOT_TOKEN` | Telegram bot token |
   | `TELEGRAM_CHAT_ID` | Telegram chat ID |

   Never paste keys into source code, commits, public logs, or chat.
4. Deploy these changes to the repository's default branch. GitHub scheduled
   workflows use that branch's workflow file. Enable **Gold 15m Signal** in the
   Actions tab if it is disabled. For a manual check, use **Run workflow** and
   select the branch containing these changes. Start with paper trading.

The schedule is :03, :18, :33 and :48 UTC each hour. GitHub Actions can delay
or skip runs; the bot can also send repeated signals for the same candle. Its
2-minute candle-finalization grace does not guarantee the provider will not
revise data. A candle older than 30 minutes after close is considered stale.

## Cost and limitations

A run with fresh market data makes **one paid Kimi request**, potentially 96
requests per day. Thinking mode is on by default and reasoning tokens share the
`max_tokens` budget (16,384 by default), so a slow or verbose answer can cost
more than a non-thinking call. Check Moonshot's current pricing, quotas, and
your account's spend controls before enabling the schedule. AI requests have a
90-second read timeout, and one retry without thinking is allowed on a timeout,
a 5xx error, or an unusable answer. No Kimi call is made for stale market data.

Prices may differ from your broker's bid/ask and candle timestamps. Stop/target
levels, execution assumptions, and a historical reference close are not actual
fills. The workflow has no knowledge of existing positions and cannot manage
risk in a brokerage account.

## Local use and tests

After `pip install -r requirements.txt`, set the four environment variables
above securely and run `python signal.py`. Without Telegram settings, messages
are printed but the process exits unsuccessfully rather than claiming delivery.
Tests do not call external APIs:

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s research -p 'test_*.py' -v
```

The prior [historical backtest](research/backtest_report.md) evaluates **only
the old deterministic rules**, not Kimi. It cannot establish the performance
of the new AI strategy.

## If the Kimi step fails

The run logs `[kimi-debug]` lines (model, thinking mode, `max_tokens`, HTTP
status, `finish_reason`, token usage) with secrets removed. The error text
names the cause, for example:

| Log line | Meaning |
|---|---|
| `finish_reason=length (answer truncated by max_tokens)` | Reasoning used the whole token budget — raise `KIMI_MAX_TOKENS`. |
| `only reasoning was returned, content was empty` | Same cause; the retry without thinking also failed. |
| `Kimi returned HTTP 401` / `403` | Key missing, wrong, or out of credit — check `MOONSHOT_API_KEY`. |
| `Kimi returned HTTP 404: ...` | Model name not available to your account. |
| `Kimi request failed or timed out` | Network or a slow thinking response; one retry is attempted. |

Optional repository variables (**Settings → Secrets and variables → Actions →
Variables**, or workflow `env`) tune the call without a code change:

| Variable | Default | Effect |
|---|---|---|
| `KIMI_MODEL` | `kimi-k2.6` | Any model your key can call, e.g. `kimi-k3`. |
| `KIMI_MAX_TOKENS` | `16384` | Reasoning + answer budget (512–65536). |
| `KIMI_THINKING` | on | `off` disables reasoning: faster, cheaper, less deliberate. |
| `KIMI_DEBUG` | `0` | `1` prints the sanitised diagnostics above. |

## Files

- `signal.py`: data validation, indicators, local risk levels and Telegram.
- `kimi_brain.py`: Kimi request and strict response validation.
- `.github/workflows/gold-signal.yml`: schedule and secret wiring.
- `tests/`: mocked API and safety checks.
- `research/`: original rule-based backtest and its assumptions.

MIT licensed; see `LICENSE`.
