"""Kimi decision client. Never executes trades or calculates trade levels."""
from __future__ import annotations

import json
import os

import requests

KIMI_URL = 'https://api.moonshot.ai/v1/chat/completions'
KIMI_MODEL = 'kimi-k2.6'
SYSTEM_PROMPT = (
    'You are a cautious XAU/USD 15-minute market monitor. Decide BUY, SELL, '
    'or STAND ASIDE using only the supplied closed-candle data and indicators. '
    'The rule-based action is reference information, not a binding constraint. '
    'Do not assume access to live prices, news, spreads, or a trading account. '
    'Prefer STAND ASIDE when the data does not support a clear setup. '
    'Return only a JSON object with exactly two string fields: '
    '"action" (exactly BUY, SELL, or STAND ASIDE) and '
    '"reason" (one concise explanation, at most 280 characters). '
    'Do not claim a win probability, guaranteed return, or invent facts.'
)


class AIError(Exception):
    """Safe-to-log failure; no provider response or request secrets included."""


def decide(bars, indicators, symbol):
    """Call Kimi on a bounded snapshot and strictly validate the decision."""
    key = os.environ.get('MOONSHOT_API_KEY')
    if not key:
        raise AIError('MOONSHOT_API_KEY is not configured')
    snapshot = {
        'symbol': symbol,
        'interval': '15min',
        'bar_start_timezone': 'UTC',
        'last_40_closed_candles': [
            {'start': b.dt, 'open': b.o, 'high': b.h, 'low': b.l, 'close': b.c}
            for b in bars[-40:]
        ],
        'indicators': {name: indicators[name] for name in
                       ('price', 'ema20', 'ema50', 'ema200', 'rsi', 'atr', 'hi20', 'lo20')},
        'rule_based_reference': {'action': indicators['action'],
                                 'confluence_checks_passed': indicators['confidence'],
                                 'checks_passed': indicators['reasons']},
    }
    payload = {
        'model': KIMI_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps(snapshot, allow_nan=False)},
        ],
        'response_format': {'type': 'json_object'},
        # K2.6 supports both modes. Bound reasoning/output and request time.
        'thinking': {'type': 'enabled'},
        'max_tokens': 2048,
    }
    try:
        response = requests.post(KIMI_URL, headers={'Authorization': f'Bearer {key}'},
                                 json=payload, timeout=(10, 90), allow_redirects=False)
    except requests.RequestException:
        raise AIError('Kimi request failed or timed out') from None
    if not response.ok or 300 <= response.status_code < 400:
        raise AIError(f'Kimi returned HTTP {response.status_code}')
    try:
        choice = response.json()['choices'][0]
        if choice.get('finish_reason') != 'stop':
            raise ValueError('incomplete answer')
        content = choice['message']['content']
        if not isinstance(content, str) or len(content) > 2000:
            raise ValueError('invalid content')
        decision = json.loads(content)
        if not isinstance(decision, dict) or set(decision) != {'action', 'reason'}:
            raise ValueError('invalid fields')
        action, reason = decision['action'], decision['reason']
        if not isinstance(action, str) or action not in ('BUY', 'SELL', 'STAND ASIDE'):
            raise ValueError('invalid action')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 280:
            raise ValueError('invalid reason')
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
            raise ValueError('control character in reason')
        return {'action': action, 'reason': reason.strip()}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise AIError('Kimi returned an incomplete or invalid decision') from None
