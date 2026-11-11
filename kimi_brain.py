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

SYSTEM_PROMPT = """You are a cautious XAU/USD scalper reading a 15-minute chart, with an optional
5-minute confirmation chart. A position is held {hold_min}-{hold_max} minutes and must be
flat by the end of that window.

You receive only closed candles, indicators, a volatility and cost plan, and
locally computed strategy scores. You have no live quote, no order book, no
spread feed, no news and no account. The spread is an assumption, not a
measurement.

Decide BUY, SELL or STAND ASIDE, applying all of the following:

1. TREND: respect the EMA9/20/50/200 stack and ADX. ADX below 20 means the
   market is ranging: do not chase breakouts there, and be wary of trend entries.
2. MOMENTUM: RSI(7), RSI(14), MACD histogram, Stochastic, CCI and Williams %R
   should agree with the direction. Divergence means the move is late.
3. LOCATION: only trade at a level - support/resistance, a Bollinger extreme,
   the 20-bar range edge, or a swing pivot. Mid-range entries are poor scalps.
4. VOLATILITY: the target must be reachable inside the hold window. Compare
   target_vs_expected in the plan; above 1.0 the target needs more time than a
   {hold_max}-minute scalp has. Low ATR percentile means there is nothing to capture.
5. COST: net_rr is reward:risk after the assumed spread. If net_rr is below
   {min_rr}, or the spread dominates a small ATR, STAND ASIDE.
6. CONFIRMATION: prefer setups where at least two local strategies agree on
   direction, or one scores 4+/5 with a clear confirming candle.
7. SESSION: thin liquidity makes scalps unreliable. Be stricter when liquidity
   is low or the market is closed.
8. Most candles are not trades. If the evidence is mixed, contradicted or
   missing, STAND ASIDE.

Return only a JSON object with exactly these string/integer fields:
"action": exactly BUY, SELL or STAND ASIDE
"reason": one concise explanation of at most 280 characters naming the decisive evidence
"strategy": the playbook that best fits - trend_pullback, breakout, mean_reversion,
            momentum_macd, range_fade, or none
"hold_minutes": integer minutes you expect to hold, between {hold_min} and {hold_max}
Never claim a win probability or guaranteed profit. No extra fields."""


def system_prompt(hold_min: int, hold_max: int, min_net_rr: float) -> str:
    """Prompt text for the configured scalp profile."""
    return SYSTEM_PROMPT.format(hold_min=hold_min, hold_max=hold_max, min_rr=f"{min_net_rr:g}")


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


def _payload(snapshot: dict, thinking: bool, prompt: str) -> dict:
    payload = {
        'model': current_model(),
        'messages': [
            {'role': 'system', 'content': prompt},
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


def _parse(data, attempt: int, bounds=(5, 16)):
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
    allowed = {'action', 'reason', 'strategy', 'hold_minutes'}
    if not isinstance(decision, dict) or not {'action', 'reason'} <= set(decision) <= allowed:
        return None, 'JSON fields were not exactly action and reason (plus optional strategy/hold_minutes)'
    action, reason = decision['action'], decision['reason']
    if not isinstance(action, str) or action not in ('BUY', 'SELL', 'STAND ASIDE'):
        return None, 'action was not BUY, SELL or STAND ASIDE'
    if not isinstance(reason, str) or not reason.strip():
        return None, 'reason was empty'
    if len(reason) > MAX_REASON_CHARS:
        return None, f'reason longer than {MAX_REASON_CHARS} characters'
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
        return None, 'reason contained control characters'
    return {'action': action, 'reason': reason.strip(),
            'strategy': _strategy(decision.get('strategy')),
            'hold_minutes': _hold(decision.get('hold_minutes'), bounds)}, None


def _strategy(value):
    """Optional label: kept only if it is short, printable text."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        _debug('dropped malformed strategy field')
        return None
    text = ''.join(' ' if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in value).strip()
    return text or None


def _hold(value, bounds):
    """Optional hold estimate: kept only if it is an in-window integer."""
    low, high = bounds
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _debug(f'dropped hold_minutes outside {low}-{high}')
        return None
    return value


def _num(value, digits: int = 2):
    return _safe_number(value, digits)


def _safe_number(value, digits: int = 2):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if v == v and abs(v) != float('inf') else None


def build_snapshot(bars, a: dict, symbol: str, lower_tf=None) -> dict:
    """Bounded, secrets-free view of the market for one scalp decision."""
    ind = a.get('indicators_full', {})
    plans = a.get('plans') or {}
    regime = ('trending' if (ind.get('adx') or 0) >= 25 else
              'transitional' if (ind.get('adx') or 0) >= 20 else 'ranging/choppy')
    return {
        'symbol': symbol,
        'trading_style': 'scalp',
        'chart_interval': '15min',
        'hold_horizon_minutes': [a.get('hold_min', 5), a.get('hold_max', 16)],
        'bar_start_timezone': 'UTC',
        'last_40_closed_candles': [
            {'start': b.dt, 'open': b.o, 'high': b.h, 'low': b.l, 'close': b.c}
            for b in bars[-40:]
        ],
        'last_4_candle_shapes': a.get('candle_shapes', [])[-4:],
        'trend': {
            'price': _num(a.get('price')),
            'ema9': _num(ind.get('ema9')), 'ema20': _num(ind.get('ema20')),
            'ema50': _num(ind.get('ema50')), 'ema200': _num(ind.get('ema200')),
            'ema_stack': ('bullish' if ind.get('ema9', 0) > ind.get('ema20', 0) > ind.get('ema50', 0)
                          else 'bearish' if ind.get('ema9', 0) < ind.get('ema20', 0) < ind.get('ema50', 0)
                          else 'mixed'),
            'adx': _num(ind.get('adx'), 1),
            'plus_di': _num(ind.get('plus_di'), 1), 'minus_di': _num(ind.get('minus_di'), 1),
            'regime': regime,
        },
        'momentum': {
            'rsi7': _num(ind.get('rsi7'), 1), 'rsi14': _num(ind.get('rsi14'), 1),
            'macd': _num(ind.get('macd')), 'macd_signal': _num(ind.get('macd_signal')),
            'macd_hist': _num(ind.get('macd')), 'macd_hist_prev_bars': [
                _num(ind.get('macd_hist_1_ago')), _num(ind.get('macd_hist_2_ago'))],
            'stoch_k': _num(ind.get('stoch_k'), 1), 'stoch_d': _num(ind.get('stoch_d'), 1),
            'cci': _num(ind.get('cci'), 1), 'williams_r': _num(ind.get('williams_r'), 1),
        },
        'volatility': {
            'atr_15m': _num(a.get('atr')),
            'atr_percentile_100bars': _num(ind.get('atr_percentile'), 1),
            'atr_pct_of_price': _num(ind.get('atr_pct_of_price'), 3),
            'expected_move_usd_at_hold_max': _num(
                (plans.get('BUY') or {}).get('expected_move_usd')),
        },
        'levels': {
            'bar_20_high': _num(a.get('hi20')), 'bar_20_low': _num(a.get('lo20')),
            'bollinger_upper': _num(a.get('bb_up')), 'bollinger_lower': _num(a.get('bb_lo')),
            'bollinger_pct_b': _num(a.get('bb_pctb'), 3),
            'nearest_resistance': _num(a.get('nearest_resistance')),
            'nearest_support': _num(a.get('nearest_support')),
            'recent_swing_levels': [{'kind': lvl['kind'], 'price': _num(lvl['price'])}
                                    for lvl in (a.get('levels') or [])],
        },
        'session': a.get('session', {}),
        'cost_and_plan': {
            'assumed_spread_usd_per_oz': _num(a.get('spread')),
            'hold_minutes': [a.get('hold_min', 5), a.get('hold_max', 16)],
            'min_net_reward_risk_required': _num(a.get('min_net_rr'), 2),
            'plan_if_buy': _plan(plans.get('BUY')),
            'plan_if_sell': _plan(plans.get('SELL')),
        },
        'local_strategy_scores': a.get('strategies', []),
        'rule_based_reference': {'action': a.get('action'),
                                 'confluence_checks_passed': a.get('confidence'),
                                 'checks_passed': a.get('reasons', [])},
        **({'lower_timeframe_5min': lower_tf} if lower_tf else {}),
    }


def _plan(plan: dict | None) -> dict | None:
    if not plan:
        return None
    return {
        'stop': _num(plan.get('stop')),
        'target': _num(plan.get('target')),
        'risk_usd': _num(plan.get('risk_usd')),
        'reward_usd': _num(plan.get('reward_usd')),
        'net_reward_risk_after_spread': _num(plan.get('net_rr')),
        'target_vs_expected_move': _num(plan.get('target_vs_expected'), 2),
        'passes_cost_gate': plan.get('passes_cost_gate'),
    }


def decide(bars, analysis: dict, symbol: str, lower_tf=None):
    """Call Kimi on a bounded snapshot and strictly validate the decision."""
    key = os.environ.get('MOONSHOT_API_KEY')
    if not key:
        raise AIError('MOONSHOT_API_KEY is not configured')
    snapshot = build_snapshot(bars, analysis, symbol, lower_tf)
    prompt = system_prompt(analysis.get('hold_min', 5), analysis.get('hold_max', 16),
                           analysis.get('min_net_rr', 1.0))
    bounds = (analysis.get('hold_min', 5), analysis.get('hold_max', 16))
    # First attempt uses thinking (if enabled); a retry drops thinking, which is
    # cheaper, faster and avoids the shared reasoning/content token budget.
    attempts = [True, False] if _env_flag('KIMI_THINKING', True) else [False]
    problem = 'no usable decision'
    for index, thinking in enumerate(attempts, start=1):
        try:
            data = _post(_payload(snapshot, thinking, prompt), key, index)
        except AIError as e:
            if e.retriable and index < len(attempts):
                _debug(f'attempt {index} failed ({e}); retrying without thinking')
                problem = str(e)
                continue
            raise
        decision, failure = _parse(data, index, bounds)
        if decision is not None:
            return decision
        problem = failure
        _debug(f'attempt {index} unusable: {failure}')
    raise AIError(f'Kimi returned an incomplete or invalid decision ({problem})')
