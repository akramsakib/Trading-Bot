import io
import json
import math
import os
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import kimi_brain as ai
from test_signal import bot


def answer(action='BUY', reason='The short trend is rising.'):
    return {'choices': [{'finish_reason': 'stop',
                         'message': {'content': json.dumps({'action': action, 'reason': reason})}}]}


def body(action='BUY', reason='The short trend is rising.', finish='stop', reasoning=''):
    """Raw provider payload; optionally simulating a thinking-model response."""
    return {'choices': [{'finish_reason': finish,
                         'message': {'content': json.dumps({'action': action, 'reason': reason}),
                                     'reasoning_content': reasoning}}]}


def truncated():
    """What a starved max_tokens budget looks like: reasoning, no content."""
    return {'choices': [{'finish_reason': 'length',
                         'message': {'content': '', 'reasoning_content': 'thinking...'}}]}


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
        # Reasoning tokens share this budget; Kimi recommends >= 16000.
        self.assertGreaterEqual(kwargs['json']['max_tokens'], 16000)
        self.assertEqual(kwargs['json']['thinking'], {'type': 'enabled'})
        self.assertEqual(len(json.loads(kwargs['json']['messages'][1]['content'])['last_40_closed_candles']), 40)
        self.assertNotIn('test-secret', json.dumps(kwargs['json']))

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_invalid_answers_fail_closed(self, post):
        # Extra fields, synonyms and casing are accepted now; these are not.
        bad = [answer('MAYBE'), answer('BUY', ''), answer('BUY', 'x'*281),
               answer('SELL', 'one\ntwo'), {'choices': []},
               {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
               {'choices': [{'finish_reason': 'stop', 'message': {'content': 'not json'}}]},
               {'choices': [{'finish_reason': 'stop',
                             'message': {'content': '{"action": 5, "reason": "x"}'}}]}]
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
        decision = {'action': 'BUY', 'reason': 'Trend.', 'strategy': 'trend_pullback', 'hold_minutes': 12}
        result = bot.format_ai_message(self.info, decision)
        self.assertIn('— BUY', result)
        self.assertIn('Rule reference: STAND ASIDE', result)
        # Levels are calculated locally from ATR, never taken from the AI.
        self.assertIn(f"Stop {self.info['price'] - bot.ATR_SL * self.info['atr']:.2f}", result)
        self.assertIn(f"Target {self.info['price'] + bot.ATR_TP * self.info['atr']:.2f}", result)
        self.assertIn('Playbook: trend_pullback', result)
        self.assertIn('12m', result)
        result = bot.format_ai_message(self.info, {'action': 'SELL', 'reason': 'Trend.',
                                                   'strategy': None, 'hold_minutes': None})
        self.assertIn(f"Stop {self.info['price'] + bot.ATR_SL * self.info['atr']:.2f}", result)
        self.assertNotIn('Stop ', bot.format_ai_message(self.info, {'action': 'STAND ASIDE',
                                                                    'reason': 'Unclear.',
                                                                    'strategy': None,
                                                                    'hold_minutes': None}))

    def test_scalp_message_shows_session_cost_and_horizon(self):
        result = bot.format_ai_message(self.info, {'action': 'BUY', 'reason': 'Trend.',
                                                   'strategy': 'breakout', 'hold_minutes': 9})
        self.assertIn(f"scalp {bot.HOLD_MIN}–{bot.HOLD_MAX}m", result)
        self.assertIn('Session', result)
        self.assertIn('ADX', result)
        self.assertIn('Net R:R', result)
        self.assertIn('spread', result)
        self.assertNotIn('nan', result.lower().replace('stand', ''))

    def test_cost_gate_blocks_a_spread_dominated_scalp(self):
        """With a large spread and a small ATR there is nothing worth scalping."""
        cheap = bot.scalp_plan(100.0, 8.0, 1, spread=0.4)
        self.assertTrue(cheap['passes_cost_gate'])
        self.assertGreater(cheap['net_rr'], 1.0)
        dead = bot.scalp_plan(100.0, 1.0, 1, spread=0.4)
        self.assertFalse(dead['passes_cost_gate'])
        self.assertLess(dead['net_rr'], bot.MIN_NET_RR)
        # The plan scales with volatility, so a bigger ATR re-opens the trade.
        self.assertTrue(bot.scalp_plan(100.0, 1.0, 1, spread=0.0)['passes_cost_gate'])


class ScalpPlanAndCostTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.bars = [bot.Bar((now - timedelta(minutes=15 * (299-i) + 18)).strftime('%Y-%m-%d %H:%M:%S'),
                             100 + i/100, 102 + i/100, 99 + i/100, 101 + i/100)
                     for i in range(300)]
        self.info = bot.analyse(self.bars)

    def test_snapshot_macd_histogram_is_not_the_macd_line(self):
        snap = bot.kimi_brain.build_snapshot(self.bars, self.info, 'XAU/USD')
        momentum = snap['momentum']
        self.assertAlmostEqual(momentum['macd_hist'],
                               round(momentum['macd'] - momentum['macd_signal'], 2), places=1)
        self.assertIsNotNone(momentum['macd_hist_prev_bars'][0])

    def test_scalp_levels_are_fractions_of_atr(self):
        plan = bot.scalp_plan(self.info['price'], self.info['atr'], 1)
        self.assertAlmostEqual(plan['stop'], self.info['price'] - bot.ATR_SL * self.info['atr'])
        self.assertAlmostEqual(plan['target'], self.info['price'] + bot.ATR_TP * self.info['atr'])
        self.assertLess(plan['target_usd'], self.info['atr'])  # a scalp, not a swing
        self.assertLessEqual(plan['stop_usd'], self.info['atr'])

    def test_invalid_scalp_configuration_fails_closed(self):
        bars = [bot.Bar(str(i), 100, 101, 99, 100) for i in range(300)]
        for name, value in (('HOLD_MIN', 0), ('HOLD_MAX', 0), ('SPREAD_USD', -1),
                            ('MIN_NET_RR', 0), ('SPREAD_USD', math.nan)):
            with patch.object(bot, name, value), self.assertRaises(ValueError):
                bot.analyse(bars)
        with patch.object(bot, 'HOLD_MIN', 30), self.assertRaises(ValueError):
            bot.analyse(bars)  # HOLD_MIN > HOLD_MAX

    def test_strategy_scores_and_indicators_are_populated(self):
        for key in ('ema9', 'ema200', 'rsi7', 'macd', 'macd_hist', 'bb_up', 'bb_lo',
                    'stoch_k', 'adx', 'cci', 'williams_r', 'atr_percentile'):
            self.assertIn(key, self.info)
        for key in ('session', 'candle_shapes', 'levels', 'strategies', 'plans'):
            self.assertIn(key, self.info)
        self.assertEqual(len(self.info['strategies']), 5)
        for item in self.info['strategies']:
            self.assertIn(item['direction'], ('BUY', 'SELL', 'none'))
            self.assertIn(item['name'], ('trend_pullback', 'breakout', 'mean_reversion',
                                         'momentum_macd', 'range_fade'))
            self.assertIsInstance(item['score'], int)
        # Every value handed to the AI must be JSON-serialisable (no NaN/inf).
        json.dumps(bot.kimi_brain.build_snapshot(self.bars, self.info, 'XAU/USD'),
                   allow_nan=False)

    def test_candle_shape_and_swing_levels(self):
        bar = bot.Bar('2026-01-01 00:00:00', 100, 103, 99, 102)
        shape = bot.candle_shape(bar)
        self.assertEqual(shape['dir'], 'up')
        self.assertAlmostEqual(shape['body'] + shape['upper_wick'] + shape['lower_wick'], 1.0, places=2)
        levels = bot.swing_levels(self.bars, len(self.bars) - 1)
        self.assertTrue(all(lvl['kind'] in ('high', 'low') for lvl in levels))

    def test_session_labels(self):
        self.assertEqual(bot.session(datetime(2026, 6, 15, 13, tzinfo=timezone.utc))['liquidity'], 'highest')
        self.assertEqual(bot.session(datetime(2026, 6, 15, 3, tzinfo=timezone.utc))['liquidity'], 'low')
        self.assertEqual(bot.session(datetime(2026, 6, 13, 12, tzinfo=timezone.utc))['liquidity'], 'none')

    def test_lower_timeframe_summary_is_optional(self):
        with patch.dict(os.environ, {'TWELVE_DATA_API_KEY': 'k'}):
            with patch.object(bot.requests, 'get', side_effect=bot.DataError('no data')):
                self.assertIsNone(bot.fetch_lower_tf())
        with patch.dict(os.environ, {'USE_M5': '0'}):
            self.assertIsNone(bot.fetch_lower_tf())

    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(ai, 'decide', side_effect=ai.AIError('unavailable'))
    def test_main_fail_closed(self, decide, send):
        with patch.object(bot, 'fetch_bars', return_value=self.bars):
            self.assertEqual(bot.main(), 1)
        self.assertIn('STAND ASIDE', send.call_args.args[0])
        self.assertNotIn('— BUY', send.call_args.args[0])

    @patch.object(bot, 'SPREAD_USD', 0.0)
    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(ai, 'decide', return_value={'action': 'BUY', 'reason': 'Trend.',
                                              'strategy': 'trend_pullback', 'hold_minutes': 10})
    def test_main_ai_can_decide_buy(self, decide, send):
        with patch.object(bot, 'fetch_bars', return_value=self.bars):
            self.assertEqual(bot.main(), 0)
        self.assertIn('— BUY', send.call_args.args[0])
        self.assertIn('Net R:R', send.call_args.args[0])

    @patch.object(bot, 'SPREAD_USD', 5.0)   # absurdly wide: nothing can beat it
    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(ai, 'decide', return_value={'action': 'BUY', 'reason': 'Trend.',
                                              'strategy': None, 'hold_minutes': None})
    def test_main_cost_gate_sends_stand_aside_without_failing(self, decide, send):
        with patch.object(bot, 'fetch_bars', return_value=self.bars):
            self.assertEqual(bot.main(), 0)
        text = send.call_args.args[0]
        self.assertIn('STAND ASIDE', text)
        self.assertNotIn('— BUY', text)
        self.assertIn('Net R:R', text)

    @patch.object(bot, 'send_telegram', return_value=True)
    @patch.object(ai, 'decide', return_value={'action': 'BUY', 'reason': 'Trend.'})
    def test_main_rejects_data_that_becomes_stale(self, decide, send):
        self.bars[-1].dt = (datetime.now(timezone.utc) - timedelta(minutes=46)).strftime('%Y-%m-%d %H:%M:%S')
        with patch.object(bot, 'fetch_bars', return_value=self.bars):
            self.assertEqual(bot.main(), 1)
        self.assertIn('STAND ASIDE', send.call_args.args[0])

    # ------------------------------------------------------------------ #
    # Kimi request/response handling
    # ------------------------------------------------------------------ #

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_truncated_thinking_answer_retries_without_thinking(self, post):
        post.side_effect = [Mock(ok=True, status_code=200, json=Mock(return_value=truncated())),
                            Mock(ok=True, status_code=200, json=Mock(return_value=answer('SELL')))]
        self.assertEqual(ai.decide(self.bars, self.info, 'XAU/USD')['action'], 'SELL')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs['json']['thinking'], {'type': 'enabled'})
        self.assertEqual(post.call_args_list[1].kwargs['json']['thinking'], {'type': 'disabled'})

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret', 'KIMI_THINKING': 'off'})
    @patch.object(ai.requests, 'post')
    def test_thinking_disabled_makes_one_attempt(self, post):
        post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value=truncated()))
        with self.assertRaises(ai.AIError) as e:
            ai.decide(self.bars, self.info, 'XAU/USD')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})
        self.assertIn('truncated', str(e.exception))

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret', 'KIMI_MODEL': 'kimi-k3',
                             'KIMI_MAX_TOKENS': '8192'})
    @patch.object(ai.requests, 'post')
    def test_env_overrides_model_and_token_budget(self, post):
        post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value=answer()))
        ai.decide(self.bars, self.info, 'XAU/USD')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'kimi-k3')
        self.assertEqual(payload['max_tokens'], 8192)
        self.assertEqual(ai.current_model(), 'kimi-k3')

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_code_fenced_json_is_accepted(self, post):
        fenced = {'choices': [{'finish_reason': 'stop',
                               'message': {'content': '```json\n{"action": "BUY", "reason": "Uptrend."}\n```'}}]}
        post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value=fenced))
        self.assertEqual(ai.decide(self.bars, self.info, 'XAU/USD')['action'], 'BUY')

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_http_error_reports_provider_message(self, post):
        post.return_value = Mock(ok=False, status_code=400, text='',
                                 json=Mock(return_value={'error': {'message': 'unknown model'}}))
        with self.assertRaises(ai.AIError) as e:
            ai.decide(self.bars, self.info, 'XAU/USD')
        self.assertIn('HTTP 400', str(e.exception))
        self.assertIn('unknown model', str(e.exception))
        self.assertEqual(post.call_count, 1)  # 4xx is not retried

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_server_error_is_retried(self, post):
        post.side_effect = [Mock(ok=False, status_code=500, text='',
                                 json=Mock(return_value={'error': {'message': 'upstream failure'}})),
                            Mock(ok=True, status_code=200, json=Mock(return_value=answer('SELL')))]
        self.assertEqual(ai.decide(self.bars, self.info, 'XAU/USD')['action'], 'SELL')
        self.assertEqual(post.call_count, 2)

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_timeout_is_retried_without_thinking(self, post):
        post.side_effect = [ai.requests.RequestException('timeout'),
                            Mock(ok=True, status_code=200, json=Mock(return_value=answer('STAND ASIDE')))]
        self.assertEqual(ai.decide(self.bars, self.info, 'XAU/USD')['action'], 'STAND ASIDE')
        self.assertEqual(post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret', 'KIMI_DEBUG': '1'})
    @patch.object(ai.requests, 'post')
    def test_debug_output_is_useful_and_leaks_nothing(self, post):
        post.return_value = Mock(ok=True, status_code=200,
                                 json=Mock(return_value=body(reasoning='step one', finish='stop')))
        stream = io.StringIO()
        with redirect_stderr(stream):
            ai.decide(self.bars, self.info, 'XAU/USD')
        text = stream.getvalue()
        self.assertIn('finish_reason', text)
        self.assertIn('max_tokens', text)
        self.assertNotIn('test-secret', text)
        self.assertNotIn('Bearer', text)

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_harmless_model_variation_is_still_read(self, post):
        """A usable verdict must not be thrown away over formatting."""
        cases = [
            ('extra fields ignored', {'action': 'BUY', 'reason': 'Trend.', 'confidence': 'high',
                                      'entry': 4415.3, 'stop_loss': 4411.7}, 'BUY'),
            ('lowercase action', {'action': 'buy', 'reason': 'Trend.'}, 'BUY'),
            ('padded and underscored', {'action': ' stand_aside ', 'reason': 'Chop.'}, 'STAND ASIDE'),
            ('synonym hold', {'action': 'HOLD', 'reason': 'Chop.'}, 'STAND ASIDE'),
            ('numeric string hold', {'action': 'SELL', 'reason': 'Trend.', 'hold_minutes': '12'}, 'SELL'),
            ('float hold', {'action': 'SELL', 'reason': 'Trend.', 'hold_minutes': 12.0}, 'SELL'),
            ('nested object', {'decision': {'action': 'BUY', 'reason': 'Trend.'}}, 'BUY'),
            ('prose around json', None, 'BUY'),
        ]
        for label, payload, expected in cases:
            if payload is None:
                content = 'Sure! Here is my decision:\n{"action": "BUY", "reason": "Trend."}\nHope that helps.'
            else:
                content = json.dumps(payload)
            post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value={
                'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}))
            with self.subTest(label):
                self.assertEqual(ai.decide(self.bars, self.info, 'XAU/USD')['action'], expected)

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_optional_fields_are_kept_when_valid(self, post):
        post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value={
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
                {'action': 'BUY', 'reason': 'Trend.', 'strategy': 'breakout',
                 'hold_minutes': 11, 'confidence': 0.7})}}]}))
        decision = ai.decide(self.bars, self.info, 'XAU/USD')
        self.assertEqual(decision['strategy'], 'breakout')
        self.assertEqual(decision['hold_minutes'], 11)
        self.assertNotIn('confidence', decision)

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_rate_limit_is_retried(self, post):
        post.side_effect = [Mock(ok=False, status_code=429, text='',
                                 json=Mock(return_value={'error': {'message': 'rate limited'}})),
                            Mock(ok=True, status_code=200, json=Mock(return_value=answer('BUY')))]
        self.assertEqual(ai.decide(self.bars, self.info, 'XAU/USD')['action'], 'BUY')
        self.assertEqual(post.call_count, 2)

    def test_failure_cause_is_short_and_printable(self):
        cause = bot.fmt_cause(ai.AIError('Kimi returned HTTP 429: rate limited\nfor now'))
        self.assertEqual(cause, 'Kimi returned HTTP 429: rate limited for now')
        self.assertNotIn('\n', cause)
        self.assertLessEqual(len(cause), 120)
        self.assertEqual(bot.fmt_cause(ai.AIError('x' * 500))[-3:], '...')

    @patch.dict(os.environ, {'MOONSHOT_API_KEY': 'test-secret'})
    @patch.object(ai.requests, 'post')
    def test_failure_detail_names_the_cause(self, post):
        cases = [
            (truncated(), 'truncated'),
            ({'choices': [{'finish_reason': 'stop', 'message': {'content': '',
                                                                'reasoning_content': 'thinking'}}]},
             'only reasoning'),
            ({'choices': [{'finish_reason': 'stop', 'message': {'content': '{"action":"BUY"}'}}]},
             'reason was empty or missing'),
            ({'choices': [{'finish_reason': 'stop',
                           'message': {'content': '{"action":"MAYBE","reason":"x"}'}}]},
             'action was not BUY'),
            ({'choices': []}, 'no choices'),
        ]
        for response, expected in cases:
            post.return_value = Mock(ok=True, status_code=200, json=Mock(return_value=response))
            with self.subTest(expected=expected), self.assertRaises(ai.AIError) as e:
                ai.decide(self.bars, self.info, 'XAU/USD')
            self.assertIn(expected, str(e.exception))


if __name__ == '__main__':
    unittest.main()
