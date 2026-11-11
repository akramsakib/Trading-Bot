import importlib.util
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('gold_bot', Path(__file__).resolve().parents[1] / 'signal.py')
bot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bot
spec.loader.exec_module(bot)


class CandleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 15, 12, 3, tzinfo=timezone.utc)
        start = self.now.replace(hour=12, minute=0) - timedelta(minutes=15 * 300)
        self.rows = [{'datetime': (start + timedelta(minutes=15*i)).strftime('%Y-%m-%d %H:%M:%S'),
                      'open': '100', 'high': '102', 'low': '99', 'close': '101'}
                     for i in range(301)]

    def test_closed_candle_and_grace(self):
        bars = bot.closed_bars(list(reversed(self.rows)), self.now)
        self.assertEqual(len(bars), 300)
        self.assertEqual(bars[-1].dt, '2026-06-15 11:45:00')
        # At 12:01 the previous bar has not yet passed the 2-minute grace.
        earlier = bot.closed_bars(self.rows[:-1], self.now.replace(minute=1))
        self.assertEqual(earlier[-1].dt, '2026-06-15 11:30:00')

    def test_stale_or_empty_feed(self):
        with self.assertRaises(bot.StaleDataError):
            bot.closed_bars(self.rows[:-4], self.now)
        with self.assertRaises(bot.StaleDataError):
            bot.closed_bars([], self.now)

    def test_duplicate_invalid_future_insufficient(self):
        for rows in (self.rows + [self.rows[-1]],
                     self.rows[:-1] + [{**self.rows[-1], 'high': 'nan'}],
                     self.rows[:-1] + [{**self.rows[-1], 'datetime': '2026-06-15 12:15:00'}],
                     self.rows[-10:]):
            with self.subTest(rows=rows[-1]), self.assertRaises(bot.DataError):
                bot.closed_bars(rows, self.now)

    def test_offset_timestamp_converted_to_utc(self):
        self.rows[-2]['datetime'] = '2026-06-15T19:45:00+08:00'
        self.assertEqual(bot.closed_bars(self.rows, self.now)[-1].dt, '2026-06-15 11:45:00')

    @patch.object(bot.requests, 'get')
    @patch.dict(os.environ, {'TWELVE_DATA_API_KEY': 'testsecret'})
    def test_fetch_requests_utc_latest_and_redacts_error(self, get):
        get.side_effect = bot.requests.RequestException('testsecret')
        with self.assertRaises(bot.DataError) as e:
            bot.fetch_bars()
        self.assertNotIn('testsecret', str(e.exception))
        self.assertEqual(get.call_args.kwargs['params']['timezone'], 'UTC')
        self.assertEqual(get.call_args.kwargs['params']['order'], 'DESC')


class AnalysisTests(unittest.TestCase):
    def test_rsi_flat_and_trends(self):
        self.assertEqual(bot.rsi([10.0] * 30)[-1], 50.0)
        self.assertEqual(bot.rsi(list(range(30)))[-1], 100.0)
        self.assertEqual(bot.rsi(list(range(30, 0, -1)))[-1], 0.0)

    def test_invalid_configuration_fails_closed(self):
        bars = [bot.Bar(str(i), 100, 101, 99, 100) for i in range(300)]
        for value in (0, 6):
            with patch.object(bot, 'MIN_CONF', value), self.assertRaises(ValueError):
                bot.analyse(bars)
        for value in (0, -1, math.nan):
            with patch.object(bot, 'ATR_SL', value), self.assertRaises(ValueError):
                bot.analyse(bars)

    def test_flat_prices_do_not_create_signal(self):
        bars = [bot.Bar(str(i), 100, 100, 100, 100) for i in range(300)]
        with patch.object(bot, 'MIN_CONF', 1):
            self.assertEqual(bot.analyse(bars)['action'], 'STAND ASIDE')

    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(bot, 'fetch_bars', side_effect=bot.StaleDataError('old data'))
    def test_stale_data_sends_stand_aside(self, fetch, send):
        self.assertEqual(bot.main(), 0)
        self.assertIn('STAND ASIDE', send.call_args.args[0])
        self.assertNotIn('BUY', send.call_args.args[0])

    @patch.object(bot.requests, 'post')
    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'secret', 'TELEGRAM_CHAT_ID': '123'})
    def test_telegram_rejection_is_failure(self, post):
        post.return_value = Mock(ok=True, json=Mock(return_value={'ok': False}))
        self.assertFalse(bot.send_telegram('hello'))
        post.side_effect = bot.requests.RequestException('secret')
        self.assertFalse(bot.send_telegram('hello'))


if __name__ == '__main__':
    unittest.main()
