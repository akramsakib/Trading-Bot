#!/usr/bin/env python3
"""Reproduce a simple closed-bar, next-open backtest of the current bot.

Input: CSV with datetime,open,high,low,close columns, ascending 15-minute bars.
Assumes prices are midpoints and a constant full spread in USD/oz. See output
and research report for execution assumptions and limitations.
"""
import argparse
import csv
import importlib.util
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gold_signal_bot', ROOT / 'signal.py')
bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)
# Fix the documented defaults so environment variables cannot change the test.
bot.MIN_CONF, bot.ATR_SL, bot.ATR_TP = 4, 1.5, 2.0


def load(path):
    bars, times = [], []
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            t = datetime.fromisoformat(row['datetime'].replace('Z', '+00:00'))
            o, h, l, c = (float(row[k]) for k in ('open', 'high', 'low', 'close'))
            if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
                raise ValueError(f'invalid OHLC at {t}')
            if times and t <= times[-1]:
                raise ValueError(f'non-increasing timestamp: {t}')
            times.append(t)
            bars.append(bot.Bar(row['datetime'], o, h, l, c))
    if len(bars) < 301:
        raise ValueError('need at least 301 bars')
    return bars, times


def run(bars, times, spread):
    # Compute indicators using exactly the bot's last 300 candles on each bar.
    # Signals are available at bar close. An order is entered at the next bar's
    # open when flat. Existing stop/target levels are the bot's original levels.
    signals = [None] * 299 + [bot.analyse(bars[i-299:i+1]) for i in range(299, len(bars))]
    counts = Counter(s['action'] for s in signals if s)
    trades, position = [], None
    pending = signals[299] if signals[299]['side'] else None
    invalid = ambiguous = busy_signals = 0
    half = spread / 2
    for i in range(300, len(bars)):
        b = bars[i]
        if pending is not None:
            s = pending
            side = s['side']
            entry = b.o + side * half
            # A next-open execution outside the original bracket isn't viable.
            if not (s['stop_loss'] < entry < s['take_profit'] if side == 1 else s['take_profit'] < entry < s['stop_loss']):
                invalid += 1
            else:
                position = {'side': side, 'entry': entry, 'stop': s['stop_loss'],
                            'target': s['take_profit'], 'entry_i': i,
                            'entry_time': b.dt}
            pending = None
        if position:
            p = position
            side = p['side']
            # Long exits transact at bid; short exits at ask.
            op = b.o - side * half
            low = b.l - side * half
            high = b.h - side * half
            stop_hit = low <= p['stop'] if side == 1 else high >= p['stop']
            target_hit = high >= p['target'] if side == 1 else low <= p['target']
            exit_price = outcome = None
            if (op <= p['stop'] if side == 1 else op >= p['stop']):
                exit_price, outcome = op, 'stop_gap'
            elif (op >= p['target'] if side == 1 else op <= p['target']):
                exit_price, outcome = p['target'], 'target_gap'  # no favorable gap improvement
            elif stop_hit and target_hit:
                ambiguous += 1
                exit_price, outcome = p['stop'], 'stop_ambiguous'
            elif stop_hit:
                exit_price, outcome = p['stop'], 'stop'
            elif target_hit:
                exit_price, outcome = p['target'], 'target'
            if exit_price is not None:
                pnl = side * (exit_price - p['entry'])
                trades.append({**p, 'exit_time': b.dt, 'exit_i': i, 'exit': exit_price,
                               'pnl': pnl, 'r': pnl / abs(p['entry'] - p['stop']), 'outcome': outcome})
                position = None
        s = signals[i]
        if s['side']:
            if position:
                busy_signals += 1
            elif i < len(bars) - 1:
                pending = s
    pnls = [t['pnl'] for t in trades]
    rs = [t['r'] for t in trades]
    pos = sum(x for x in pnls if x > 0)
    neg = -sum(x for x in pnls if x < 0)
    equity = peak = dd = 0.0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    months = defaultdict(list)
    for t in trades:
        months[t['exit_time'][:7]].append(t['pnl'])
    return {
        'spread_usd_per_oz': spread, 'signal_counts': dict(counts),
        'closed_trades': len(trades), 'wins': sum(x > 0 for x in pnls),
        'losses': sum(x < 0 for x in pnls),
        'win_rate': sum(x > 0 for x in pnls) / len(pnls) if pnls else None,
        'total_pnl_usd_per_oz': sum(pnls),
        'average_pnl_usd_per_oz': statistics.mean(pnls) if pnls else None,
        'average_R': statistics.mean(rs) if rs else None,
        'profit_factor': pos / neg if neg else None,
        'max_drawdown_usd_per_oz': dd,
        'outcomes': dict(Counter(t['outcome'] for t in trades)),
        'ambiguous_both_hit_stop_first': ambiguous,
        'signals_ignored_while_open': busy_signals,
        'orders_skipped_invalid_bracket_at_open': invalid,
        'open_trade_at_end': bool(position),
        'monthly_closed_pnl_usd_per_oz': {k: {'trades': len(v), 'pnl': sum(v)} for k, v in sorted(months.items())},
        'first_entry': trades[0]['entry_time'] if trades else None,
        'last_exit_bar': trades[-1]['exit_time'] if trades else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv')
    parser.add_argument('--spread', type=float, nargs='+', default=[0, 0.4])
    args = parser.parse_args()
    if any(s < 0 for s in args.spread):
        parser.error('spread must be nonnegative')
    bars, times = load(args.csv)
    gaps = [b-a for a, b in zip(times, times[1:]) if b-a > timedelta(minutes=15)]
    print(json.dumps({'data': {'bars': len(bars), 'first': bars[0].dt, 'last': bars[-1].dt,
                              'gaps_over_15_minutes': len(gaps),
                              'max_gap_hours': max((g.total_seconds()/3600 for g in gaps), default=0)},
                      'results': [run(bars, times, s) for s in args.spread]}, indent=2))

if __name__ == '__main__':
    main()
