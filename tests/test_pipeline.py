"""Exercise the complete workflow with simulated external services."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from test_signal import bot


class PipelineTest(unittest.TestCase):
    @patch.dict(os.environ, {'TWELVE_DATA_API_KEY': 'test-data-key',
                             'MOONSHOT_API_KEY': 'test-kimi-key',
                             'TELEGRAM_BOT_TOKEN': 'test-telegram-token',
                             'TELEGRAM_CHAT_ID': '123'}, clear=True)
    @patch.object(bot.requests, 'get')
    @patch.object(bot.requests, 'post')
    def test_full_pipeline_with_simulated_providers(self, post, get):
        now = datetime.now(timezone.utc)
        latest_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        rows = []
        for i in range(301):
            dt = latest_start - timedelta(minutes=15 * i)
            price = 3000 + (300 - i) * 0.1
            rows.append({'datetime': dt.strftime('%Y-%m-%d %H:%M:%S'),
                         'open': str(price), 'high': str(price + 2),
                         'low': str(price - 2), 'close': str(price + 1)})
        get.return_value = Mock(ok=True, status_code=200, json=Mock(return_value={'values': rows}))

        def simulate_post(url, **kwargs):
            if url == bot.kimi_brain.KIMI_URL:
                payload = kwargs['json']
                self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-kimi-key')
                snapshot = json.loads(payload['messages'][1]['content'])
                self.assertEqual(snapshot['symbol'], 'XAU/USD')
                self.assertEqual(len(snapshot['last_40_closed_candles']), 40)
                last = datetime.fromisoformat(snapshot['last_40_closed_candles'][-1]['start']).replace(tzinfo=timezone.utc)
                self.assertLessEqual(last + timedelta(minutes=17), datetime.now(timezone.utc))
                self.assertNotIn('test-data-key', json.dumps(snapshot))
                return Mock(ok=True, status_code=200, json=Mock(return_value={
                    'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                        'action': 'SELL', 'reason': 'Simulated analysis for a pipeline test.'})}}]}))
            self.assertEqual(url, 'https://api.telegram.org/bottest-telegram-token/sendMessage')
            text = kwargs['json']['text']
            self.assertIn('— SELL', text)
            self.assertIn('AI decision: kimi-k2.6', text)
            self.assertIn('Illustrative stop', text)
            self.assertNotIn('test-kimi-key', text)
            return Mock(ok=True, status_code=200, json=Mock(return_value={'ok': True}))

        post.side_effect = simulate_post
        self.assertEqual(bot.main(), 0)
        self.assertEqual(post.call_count, 2)
        get.assert_called_once()


if __name__ == '__main__':
    unittest.main()
