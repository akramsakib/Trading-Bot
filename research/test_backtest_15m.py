import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('backtest', Path(__file__).with_name('backtest_15m.py'))
bt = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bt
spec.loader.exec_module(bt)


class ExecutionTests(unittest.TestCase):
    def scenario(self, last_bars, spread=0):
        times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15*i)
                 for i in range(300 + len(last_bars))]
        bars = [bt.bot.Bar(t.isoformat(), 100, 101, 99, 100) for t in times]
        for i, (o, h, l, c) in enumerate(last_bars, 300):
            bars[i] = bt.bot.Bar(times[i].isoformat(), o, h, l, c)
        def signal(window):
            if window[-1].dt == bars[299].dt:
                return {'side': 1, 'action': 'BUY', 'stop_loss': 95, 'take_profit': 105}
            return {'side': 0, 'action': 'STAND ASIDE'}
        with patch.object(bt.bot, 'analyse', side_effect=signal):
            return bt.run(bars, times, spread)

    def test_same_bar_ambiguity_stop_first_with_spread(self):
        result = self.scenario([(100, 106, 94, 100)], 0.4)
        self.assertEqual(result['closed_trades'], 1)
        self.assertEqual(result['ambiguous_both_hit_stop_first'], 1)
        self.assertAlmostEqual(result['total_pnl_usd_per_oz'], -5.2)

    def test_gap_through_stop_fills_at_open(self):
        result = self.scenario([(100, 101, 99, 100), (93, 94, 92, 93)])
        self.assertEqual(result['outcomes']['stop_gap'], 1)
        self.assertAlmostEqual(result['total_pnl_usd_per_oz'], -7)

    def test_invalid_bracket_skipped(self):
        result = self.scenario([(106, 107, 105, 106)])
        self.assertEqual(result['orders_skipped_invalid_bracket_at_open'], 1)
        self.assertEqual(result['closed_trades'], 0)

if __name__ == '__main__':
    unittest.main()
