"""Kimi decision client. Never executes trades or calculates trade levels."""
from __future__ import annotations

import json
import os
import re
import sys

import requests

KIMI_URL = 'https://api.moonshot.ai/v1/chat/completions'

# Default model. Override at run time with the KIMI_MODEL environment variable.
DEFAULT_MODEL = 'kimi-k2.6'
KIMI_MODEL = DEFAULT_MODEL

# Thinking models bill reasoning tokens against max_tokens: the sum of
# reasoning_content and content must fit inside it, and Kimi recommends at
# least 16000 tokens for thinking models. 2048 starved the reasoning budget and
# truncated the answer before any JSON was emitted.
DEFAULT_MAX_TOKENS = 16384
MIN_MAX_TOKENS = 512
MAX_MAX_TOKENS = 65536
MAX_CONTENT_CHARS = 2000
MAX_REASON_CHARS = 280
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 90

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

    def __init__(self, message: str, retriable: bool = False):
        super().__init__(message)
        self.retriable = retriable


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ('0', 'false', 'off', 'no', 'disabled')


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(MIN_MAX_TOKENS, min(MAX_MAX_TOKENS, value))


def current_model() -> str:
    """Model actually used for the next call (KIMI_MODEL overrides the default)."""
    return (os.environ.get('KIMI_MODEL') or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _safe(text, limit: int = 200) -> str:
    """Flatten untrusted provider text for logs: one line, no control chars."""
    if not isinstance(text, str) or not text:
        return ''
    cleaned = ''.join(' ' if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:limit] + ('...' if len(cleaned) > limit else '')


def _debug(message: str) -> None:
    if _env_flag('KIMI_DEBUG', False):
        print(f'[kimi-debug] {message}', file=sys.stderr, flush=True)


def _provider_message(response) -> str:
    """Best-effort error text from a failed response; never returned raw."""
    try:
        body = response.json()
    except (ValueError, AttributeError):
        try:
            return _safe(getattr(response, 'text', ''), 200)
        except Exception:  # pragma: no cover - defensive
            return ''
    if isinstance(body, dict):
        error = body.get('error')
        if isinstance(error, dict):
            return _safe(error.get('message') or error.get('code') or error.get('type'), 200)
        if isinstance(error, str):
            return _safe(error, 200)
        return _safe(body.get('message'), 200)
    return ''


def _payload(snapshot: dict, thinking: bool) -> dict:
    payload = {
        'model': current_model(),
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps(snapshot, allow_nan=False)},
        ],
        'response_format': {'type': 'json_object'},
        # K2.6 supports both modes. Bound reasoning/output and request time.
        'thinking': {'type': 'enabled' if thinking else 'disabled'},
        'max_tokens': _env_int('KIMI_MAX_TOKENS', DEFAULT_MAX_TOKENS),
    }
    return payload


def _post(payload: dict, key: str, attempt: int) -> dict:
    _debug(f"attempt {attempt}: model={payload['model']} "
           f"thinking={payload['thinking']['type']} max_tokens={payload['max_tokens']}")
    try:
        response = requests.post(KIMI_URL, headers={'Authorization': f'Bearer {key}'},
                                 json=payload, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                 allow_redirects=False)
    except requests.RequestException:
        raise AIError('Kimi request failed or timed out', retriable=True) from None
    if not response.ok or 300 <= response.status_code < 400:
        detail = _provider_message(response)
        status = response.status_code
        text = f'Kimi returned HTTP {status}' + (f': {detail}' if detail else '')
        raise AIError(text, retriable=status >= 500)
    try:
        return response.json()
    except ValueError:
        raise AIError(f'Kimi returned a non-JSON HTTP {response.status_code} response') from None


def _strip_fence(text: str) -> str:
    match = re.match(r'^```[A-Za-z0-9_-]*\s*(.*?)\s*`{0,3}$', text, re.DOTALL)
    return match.group(1).strip() if match else text


def _parse(data, attempt: int):
    """Return (decision, problem). Exactly one is None-able: decision on success."""
    try:
        choice = data['choices'][0]
    except (KeyError, IndexError, TypeError):
        return None, 'response contained no choices'
    message = choice.get('message') if isinstance(choice, dict) else None
    message = message if isinstance(message, dict) else {}
    content = message.get('content')
    reasoning = message.get('reasoning_content') or ''
    finish = choice.get('finish_reason') if isinstance(choice, dict) else None
    usage = data.get('usage') if isinstance(data, dict) else None
    _debug(f"attempt {attempt}: finish_reason={finish!r} usage={usage if isinstance(usage, dict) else {}} "
           f"content_chars={len(content) if isinstance(content, str) else 'non-string'} "
           f"reasoning_chars={len(reasoning) if isinstance(reasoning, str) else 0} "
           f"content={_safe(content, 300)!r}")
    if finish is not None and finish != 'stop':
        note = f'finish_reason={finish}'
        if finish == 'length':
            note += ' (answer truncated by max_tokens; reasoning shares the budget)'
        return None, note
    if not isinstance(content, str) or not content.strip():
        return None, ('only reasoning was returned, content was empty'
                      if reasoning else 'content was empty')
    text = _strip_fence(content.strip())
    if len(text) > MAX_CONTENT_CHARS:
        return None, f'content longer than {MAX_CONTENT_CHARS} characters'
    try:
        decision = json.loads(text)
    except ValueError:
        return None, 'content was not valid JSON'
    if not isinstance(decision, dict) or set(decision) != {'action', 'reason'}:
        return None, 'JSON fields were not exactly action and reason'
    action, reason = decision['action'], decision['reason']
    if not isinstance(action, str) or action not in ('BUY', 'SELL', 'STAND ASIDE'):
        return None, 'action was not BUY, SELL or STAND ASIDE'
    if not isinstance(reason, str) or not reason.strip():
        return None, 'reason was empty'
    if len(reason) > MAX_REASON_CHARS:
        return None, f'reason longer than {MAX_REASON_CHARS} characters'
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
        return None, 'reason contained control characters'
    return {'action': action, 'reason': reason.strip()}, None


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
    # First attempt uses thinking (if enabled); a retry drops thinking, which is
    # cheaper, faster and avoids the shared reasoning/content token budget.
    attempts = [True, False] if _env_flag('KIMI_THINKING', True) else [False]
    problem = 'no usable decision'
    for index, thinking in enumerate(attempts, start=1):
        try:
            data = _post(_payload(snapshot, thinking), key, index)
        except AIError as e:
            if e.retriable and index < len(attempts):
                _debug(f'attempt {index} failed ({e}); retrying without thinking')
                problem = str(e)
                continue
            raise
        decision, failure = _parse(data, index)
        if decision is not None:
            return decision
        problem = failure
        _debug(f'attempt {index} unusable: {failure}')
    raise AIError(f'Kimi returned an incomplete or invalid decision ({problem})')
