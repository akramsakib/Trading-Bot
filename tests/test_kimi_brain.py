import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import kimi_brain as ai
from test_signal import bot


def answer(action='BUY', reason='The short trend is rising.'):
    return {'choices': [{'finish_reason': 'stop',
                         'message': {'content': json.dumps({'action': action, 'reason': reason})}}]}


class KimiTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.bars = [bot.Bar((now - timedelta(minutes=15 * (299-i) + 18)).strftime('%Y-%m-%d %H:%M:%S'),
                             100 + i/100, 102 + i/100, 99 + i/100, 101 + i/100)
                     for i in range(300)]
        self.info = bot.analyse(self.bars)

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_valid_response_and_request_contract(self, post):
        post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value=answer()))
        result = ai.decide(self.bars, self.info, 'XAU/USD')
        self.assertEqual(result['action'], 'BUY')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.moonshot.ai/v1/chat/completions')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['model'], 'kimi-k2.6')
        self.assertEqual(len(json.loads(kwargs['json']['messages'][1]['content'])['last_40_closed_candles']), 40)
        self.assertNotIn('test-secret', json.dumps(kwargs['json']))

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_invalid_answers_fail_closed(self, post):
        bad = [answer('HOLD'), answer('BUY', ''), answer('BUY', 'x'*281),
               answer('SELL', 'one\ntwo'), {'choices': []},
               {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
               {'choices': [{'finish_reason': 'stop', 'message': {'content': 'not json'}}]},
               {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"action":"BUY","reason":"x","extra":1}'}}]}]
        for response in bad:
            post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value=response))
            with self.subTest(response=response), self.assertRaises(ai.AIError):
                ai.decide(self.bars, self.info, 'XAU/USD')

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_api_errors_do_not_expose_secrets(self, post):
        post.side_effect = ai.requests.RequestException('test-secret')
        with self.assertRaises(ai.AIError) as e:
            ai.decide(self.bars, self.info, 'XAU/USD')
        self.assertNotIn('test-secret', str(e.exception))
        post.side_effect = None
        post.return_value = Mock(ok=False, status_code=401)
        with self.assertRaises(ai.AIError):
            ai.decide(self.bars, self.info, 'XAU/USD')
        post.return_value = Mock(ok=True, status_code=302)
        with self.assertRaises(ai.AIError):
            ai.decide(self.bars, self.info, 'XAU/USD')

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(ai.requests, 'post')
    def test_missing_key(self, post):
        with self.assertRaises(ai.AIError):
            ai.decide(self.bars, self.info, 'XAU/USD')
        post.assert_not_called()

    def test_ai_action_overrides_rule_and_levels_are_local(self):
        self.info['action'] = 'STAND ASIDE'
        result = bot.format_ai_message(self.info, {'action': 'BUY', 'reason': 'Trend.'})
        self.assertIn('— BUY', result)
        self.assertIn('Rule reference: STAND ASIDE', result)
        self.assertIn(f"Illustrative stop {self.info['price'] - bot.ATR_SL * self.info['atr']:.2f}", result)
        result = bot.format_ai_message(self.info, {'action': 'SELL', 'reason': 'Trend.'})
        self.assertIn(f"Illustrative stop {self.info['price'] + bot.ATR_SL * self.info['atr']:.2f}", result)
        self.assertNotIn('Illustrative stop', bot.format_ai_message(self.info, {'action': 'STAND ASIDE', 'reason': 'Unclear.'}))

    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(ai, 'decide', side_effect=ai.AIError('unavailable'))
    def test_main_fail_closed(self, decide, send):
        with patch.object(bot, 'fetch_bars', return_value=self.bars):
            self.assertEqual(bot.main(), 1)
        self.assertIn('STAND ASIDE', send.call_args.args[0])
        self.assertNotIn('— BUY', send.call_args.args[0])

    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(ai, 'decide', return_value={'action': 'BUY', 'reason': 'Trend.'})
    def test_main_ai_can_decide_buy(self, decide, send):
        with patch.object(bot, 'fetch_bars', return_value=self.bars):
            self.assertEqual(bot.main(), 0)
        self.assertIn('— BUY', send.call_args.args[0])

    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(ai, 'decide', return_value={'action': 'BUY', 'reason': 'Trend.'})
    def test_main_rejects_data_that_becomes_stale(self, decide, send):
        self.bars[-1].dt = (datetime.now(timezone.utc) - timedelta(minutes=46)).strftime('%Y-%m-%d %H:%M:%S')
        with patch.object(bot, 'fetch_bars', return_value=self.bars):
            self.assertEqual(bot.main(), 1)
        self.assertIn('STAND ASIDE', send.call_args.args[0])

if __name__ == '__main__':
    unittest.main()
