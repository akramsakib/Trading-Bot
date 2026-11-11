# XAU/USD 15-minute signal backtest

Run on 2026-11-11 with `MIN_CONF=4`, `ATR_SL=1.5` and `ATR_TP=2.0`.
The results were re-run after the candle-safety changes and were unchanged.
The historical simulation already used completed bars and does not exercise
the live-fetch freshness checks. This is a research simulation, not evidence
that the bot is profitable or a recommendation to trade.

## Data

The [public 15-minute sample](https://github.com/getdata-finance/xauusd-15m-ohlcv-metals-historical-data/blob/ae63d12c79bbbdd2856157bca8f45a2c27ce9557/XAUUSD_15m.csv)
contains 11,989 bars from 2026-03-26 02:30 UTC through 2026-09-25 20:30 UTC.
SHA-256: `b23257db4b38cbffe431b98449f1aef523cecc232f659e79781aeeff8f2bb4c9`.
There are 134 gaps longer than 15 minutes; the largest is 54.75 hours.
These include market closures and were not individually investigated. The
sample's source and price construction have not been independently verified;
it is not the Twelve Data feed used by the bot.

## Results

| Metric | Zero spread | Assumed $0.40/oz spread |
| --- | ---: | ---: |
| Closed trades | 1,178 | 1,172 |
| Winners / losers | 515 / 663 | 509 / 663 |
| Win rate | 43.72% | 43.43% |
| Net P&L for one ounce per trade | +$243.87 | -$79.35 |
| Mean P&L per trade per ounce | +$0.207 | -$0.068 |
| Profit factor | 1.025 | 0.992 |
| Maximum closed-trade drawdown per ounce | $431.06 | $522.55 |

Profit factor is total winning P&L divided by absolute losing P&L. Drawdown is
the largest peak-to-trough decline in cumulative closed-trade P&L, not an
account percentage or mark-to-market drawdown. One position was allowed at a
time; raw signals while in a trade were ignored. The spread is an illustrative
constant, not a measured broker spread. Trade paths differ between scenarios.
Neither scenario includes commissions, extra slippage, financing, or latency.

With the $0.40 spread, monthly P&L per ounce was March +$114.09 (partial),
April -$239.37, May +$49.69, June +$154.60, July -$224.77, August +$111.31,
and September -$44.91 (partial).

## Execution assumptions

- Run the actual `analyse()` on each rolling 300-bar window. Only completed
  bars are used, and trades start at the following available bar's open. This
  assumes zero execution delay at that open, which GitHub Actions cannot ensure.
- The bot's stop and target, computed from the signal-bar close, remain fixed.
  If the next opening entry quote lies outside that bracket, skip the trade.
- Treat the sample's OHLC as midpoint prices: apply half the assumed spread
  at entry and at exit, including when testing stop/target hits. The underlying
  data's real bid/ask construction is unknown, so this is an approximation.
- A stop gapped through fills at the opening quote; a target gapped through
  fills at the target (no favorable gap improvement). If stop and target both
  occur within one bar and their order is unknown, assume the stop first
  (three such exits in each scenario). An open position at the sample end is
  excluded from closed-trade statistics; none remained in these runs.
- A signal is ignored while a position is open; a new signal can be taken at
  the next open after an existing trade exits. This is an additional portfolio
  rule; the live bot sends signals, it does not manage positions or fills.

The result is highly sensitive to assumptions and is one six-month sample,
not an out-of-sample validation. It does not test actual Telegram delivery,
live candle freshness, the Twelve Data feed, a broker's bid/ask, or a user's
execution. The results do not establish a reliable trading edge.

## Reproduce

```bash
python -m pip install -r requirements.txt
gh api 'repos/getdata-finance/xauusd-15m-ohlcv-metals-historical-data/contents/XAUUSD_15m.csv?ref=ae63d12c79bbbdd2856157bca8f45a2c27ce9557' \
  -H 'Accept: application/vnd.github.raw+json' > /tmp/xau15.csv
sha256sum /tmp/xau15.csv
python research/backtest_15m.py /tmp/xau15.csv
python -m unittest discover -s research -p 'test_*.py' -v
```
