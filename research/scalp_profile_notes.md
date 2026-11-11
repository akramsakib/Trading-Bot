# Scalp profile notes

How the **deterministic** part of the scalp layer (indicators, strategy scores,
ATR sizing, cost gate) behaves on one historical sample.

**This is not a backtest of the AI.** Kimi's decisions are not modelled here,
no trades are simulated, and nothing below says the strategy makes money.

## Sample

| | |
|---|---|
| Source | `getdata-finance/xauusd-15m-ohlcv-metals-historical-data` @ `ae63d12` |
| File | `XAUUSD_15m.csv` |
| Bars | 11,989 (2026-03-26 → 2026-09-25), 15-minute, UTC bar starts |
| Price level | ~$4,285/oz at the end of the sample |
| Provider caveat | Not Twelve Data; midpoint convention unverified |

The backtest in `backtest_report.md` uses the same file, but it evaluates the
**old rule-based strategy only**.

## What the sizing produces

With the shipped defaults (`ATR_SL=0.5`, `ATR_TP=0.75`, `SPREAD_USD=0.40`,
`MIN_NET_RR=1.0`, `HOLD_MAX=16`):

| Metric | Value |
|---|---|
| ATR(15m) — median / p10 / p90 | $8.73 / $6.45 / $13.24 |
| Stop / target at the median ATR | ~$4.4 / ~$6.5 |
| Net reward:risk after the assumed spread — median | 1.29 |
| Bars passing the cost gate | 99.9% |
| Target as a multiple of the typical 16-minute move — median | 0.77× |
| Bars where a playbook scores ≥ 4/5 | 36.5% |
| Bars where ≥ 2 playbooks score ≥ 3/5 | 22.9% |

Read that as: the sizing is *selective but not silent*. Roughly a third of bars
show a mechanically strong setup, about a quarter show agreement between
playbooks, and the target normally fits inside the hold window.

## Interpretation and limits

- ATR dominates everything. The gate blocks trades when
  `ATR < 8 × SPREAD_USD` (about $3.20 at a $0.40 spread), i.e. in dead hours.
  At this sample's ATR the gate almost never fires, which is the intended
  behaviour — it is a guard, not a filter.
- The spread assumption is the most important number in the configuration.
  Retail XAU/USD spreads vary widely; at $1.00 the same settings would reject
  every setup with ATR below $8.
- `target_vs_expected` scales the target against `ATR × √(hold/15)`, a
  diffusive estimate. Moves are not normally distributed and news events break
  the assumption entirely.
- Sessions are evaluated from the wall clock at run time, so these historical
  bars do not exercise the session logic.
- The AI still decides the direction; none of the above predicts whether its
  calls are right.

## Reproduce

```bash
pip install requests
python - <<'PY'
import csv, sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('gold_bot', 'signal.py')
bot = importlib.util.module_from_spec(spec); sys.modules[spec.name] = bot
spec.loader.exec_module(bot)
rows = list(csv.DictReader(open('XAUUSD_15m.csv')))
bars = [bot.Bar(r['datetime'].replace('T', ' ')[:19], float(r['open']), float(r['high']),
                float(r['low']), float(r['close'])) for r in rows]
for end in range(300, len(bars)):
    s = bot.analyse(bars[end - 300:end])
    plan = s['plans']['BUY']
    # inspect s['strategies'], plan['net_rr'], plan['target_vs_expected']
PY
```
