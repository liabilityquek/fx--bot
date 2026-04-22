# FX Bot — Implementation Specification v2
## Option B: Quantitative Analyst (LLMAgent) + Senior Execution Trader (ReviewerAgent)
### Revisions from v1:
- Groq is always the primary LLM for both analyst and reviewer
- Anthropic is always the fallback for both analyst and reviewer
- NewsAPI replaced entirely with JB News API (already in your stack)
- Telegram commands updated to reflect new reviewer visibility

---

## 1. What Changes vs v1

| # | Change |
|---|--------|
| 1 | `LLMAgent` — Groq primary, Anthropic fallback (same as before, no change) |
| 2 | `ReviewerAgent` — **Groq primary, Anthropic fallback** (v1 had Anthropic primary) |
| 3 | `MacroContext` — replace `NewsAPI` with **JB News API** `/forex/news/` endpoint |
| 4 | `config/settings.py` — remove `NEWSAPI_KEY`, no new keys needed |
| 5 | `.env.template` — remove `NEWSAPI_KEY` entry |
| 6 | `src/monitoring/alerts.py` — add `/analyst` and `/reviewer` Telegram commands |
| 7 | `src/main.py` — wire new Telegram command callbacks |

Everything else from v1 remains identical.

---

## 2. Revised Provider Priority — Both Agents

Both `LLMAgent` (analyst) and `ReviewerAgent` (reviewer) now follow the
same provider order:

```
Priority 1 — Groq (llama-3.3-70b-versatile)
  Fast, high capacity, free tier generous.
  Used for both analyst and reviewer.

Priority 2 — Anthropic Claude Haiku (fallback)
  Activates automatically when Groq credits are exhausted
  OR when Groq returns a credit/quota error.
  Used for both analyst and reviewer.

Priority 3 — HOLD
  When both Groq and Anthropic are exhausted for the analyst:
  no trade this cycle.

  When both are exhausted for the reviewer:
  analyst vote passes through with a Telegram warning.
```

The reviewer uses a **smaller, faster Groq model** (`llama-3.1-8b-instant`)
as its primary rather than the full 70B. This keeps reviewer latency low —
the reviewer's job is logical consistency checking, not deep analysis.
The 8B model is more than capable for that task.

---

## 3. JB News API — News Endpoint

Your codebase already uses JB News API at `https://www.jblanked.com/news/api`
for the economic calendar (`/mql5/calendar/today/`). The same API provides
a forex news endpoint.

**Endpoint:** `GET https://www.jblanked.com/news/api/forex/news/`

**Authentication:** Same `Api-Key` header as the calendar endpoint.

**Response format:** JSON array of news articles. Each item contains:
```json
{
  "title": "Fed holds rates, signals caution on cuts",
  "description": "The Federal Reserve kept its benchmark...",
  "currency": "USD",
  "date": "2026.04.22 14:30:00",
  "source": "Reuters"
}
```

**Key difference from NewsAPI:** JB News is already forex-focused.
Articles are tagged by `currency`, so you can filter directly for the
currencies in the pair rather than doing keyword search. This is more
precise and requires no API key change — you already have `JB_NEWS_API_KEY`.

---

## 4. Revised File: `src/agents/macro_context.py`

Full replacement of the v1 version. JB News replaces NewsAPI entirely.
Everything else is identical to v1.

```python
"""Macro context assembler for the LLM analyst briefing.

Provides three data points per pair per cycle:
  1. Interest rate differential between the two currencies
  2. Recent forex news headlines (JB News API — same key as calendar)
  3. Upcoming high-impact economic events (EventMonitor — already running)

All methods fail silently so a data fetch failure never blocks a trade cycle.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
import pytz

from config.settings import settings

# ---------------------------------------------------------------------------
# Central bank policy rates
# Update manually after each major central bank meeting.
# These change roughly once every 6-12 weeks.
# ---------------------------------------------------------------------------
CENTRAL_BANK_RATES = {
    'USD': 4.50,   # Fed Funds Rate
    'EUR': 2.40,   # ECB deposit rate
    'GBP': 4.50,   # BOE bank rate
    'JPY': 0.50,   # BOJ policy rate
    'CHF': 0.25,   # SNB policy rate
    'AUD': 4.10,   # RBA cash rate
}

# ---------------------------------------------------------------------------
# JB News API
# ---------------------------------------------------------------------------
_JB_NEWS_BASE    = 'https://www.jblanked.com/news/api'
_NEWS_ENDPOINT   = f'{_JB_NEWS_BASE}/forex/news/'

# Cache: avoid calling the news endpoint on every cycle
_news_cache: dict = {}           # currency -> (timestamp, headlines_str)
_NEWS_CACHE_TTL_SECONDS = 3600   # 1 hour — same as event cache


class MacroContext:
    """Assembles macro briefing data for a currency pair."""

    def __init__(
        self,
        event_monitor=None,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or logging.getLogger('macro_context')
        self._event_monitor = event_monitor

    def build(self, pair: str) -> dict:
        """
        Build a complete macro context dict for the given pair.

        Returns:
            {
                'rate_differential': str,
                'carry_bias':        str,
                'recent_news':       str,
                'upcoming_events':   str,
            }

        Never raises — returns empty strings on any failure.
        """
        result = {
            'rate_differential': '',
            'carry_bias':        '',
            'recent_news':       '',
            'upcoming_events':   '',
        }

        try:
            result.update(self._get_rate_differential(pair))
        except Exception as exc:
            self.logger.debug(f'MacroContext: rate diff failed for {pair}: {exc}')

        try:
            result['recent_news'] = self._get_news(pair)
        except Exception as exc:
            self.logger.debug(f'MacroContext: news fetch failed for {pair}: {exc}')

        try:
            result['upcoming_events'] = self._get_events(pair)
        except Exception as exc:
            self.logger.debug(f'MacroContext: event fetch failed for {pair}: {exc}')

        return result

    # ------------------------------------------------------------------
    # Rate differential
    # ------------------------------------------------------------------

    def _get_rate_differential(self, pair: str) -> dict:
        base, quote = pair.split('_')
        base_rate  = CENTRAL_BANK_RATES.get(base)
        quote_rate = CENTRAL_BANK_RATES.get(quote)

        if base_rate is None or quote_rate is None:
            return {}

        diff = base_rate - quote_rate

        if diff > 0:
            carry_bias = f'{base} has yield advantage ({diff:+.2f}%)'
        elif diff < 0:
            carry_bias = f'{quote} has yield advantage ({abs(diff):.2f}%)'
        else:
            carry_bias = 'Rates equal — no carry bias'

        return {
            'rate_differential': (
                f'{base} {base_rate:.2f}% vs {quote} {quote_rate:.2f}% '
                f'(differential {diff:+.2f}%)'
            ),
            'carry_bias': carry_bias,
        }

    # ------------------------------------------------------------------
    # JB News API — forex news headlines
    # ------------------------------------------------------------------

    def _get_news(self, pair: str) -> str:
        """
        Fetch recent forex news for both currencies in the pair
        using the JB News API.

        Uses the same JB_NEWS_API_KEY as the economic calendar.
        Results are cached for 1 hour per currency.
        """
        api_key = settings.JB_NEWS_API_KEY
        if not api_key:
            return '(JB_NEWS_API_KEY not set)'

        base, quote = pair.split('_')
        headlines_by_currency = {}

        for currency in [base, quote]:
            cached = _news_cache.get(currency)
            if cached:
                ts, text = cached
                if time.time() - ts < _NEWS_CACHE_TTL_SECONDS:
                    headlines_by_currency[currency] = text
                    continue

            text = self._fetch_currency_news(currency, api_key)
            _news_cache[currency] = (time.time(), text)
            headlines_by_currency[currency] = text

        # Combine both currencies' news
        sections = []
        for currency, text in headlines_by_currency.items():
            if text and text not in ('(no recent news)', '(news fetch failed)'):
                sections.append(f'  [{currency}]\n{text}')

        return '\n'.join(sections) if sections else '  (no recent forex news)'

    def _fetch_currency_news(self, currency: str, api_key: str) -> str:
        """Fetch news headlines for a single currency from JB News API."""
        headers = {
            'Content-Type':  'application/json',
            'Authorization': f'Api-Key {api_key}',
        }
        params = {'currency': currency}

        try:
            resp = requests.get(
                _NEWS_ENDPOINT,
                headers=headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.logger.debug(f'JB News fetch failed for {currency}: {exc}')
            return '(news fetch failed)'

        if not isinstance(data, list) or not data:
            return '(no recent news)'

        # Take the 5 most recent articles
        lines = []
        for item in data[:5]:
            title  = item.get('title', '')
            source = item.get('source', '')
            if title:
                lines.append(
                    f'  - {title}'
                    + (f' [{source}]' if source else '')
                )

        return '\n'.join(lines) if lines else '(no recent news)'

    # ------------------------------------------------------------------
    # Upcoming events from EventMonitor
    # ------------------------------------------------------------------

    def _get_events(self, pair: str) -> str:
        if self._event_monitor is None:
            return '(event monitor not connected)'

        try:
            events = self._event_monitor.get_events_for_pair(
                pair, hours_ahead=8
            )
        except Exception:
            return '(event fetch failed)'

        if not events:
            return '  None in next 8 hours'

        lines = []
        for e in sorted(events, key=lambda x: x.minutes_until):
            if e.minutes_until < 0:
                continue
            h = int(e.minutes_until // 60)
            m = int(e.minutes_until % 60)
            countdown = f'{h}h {m}m' if h > 0 else f'{m}m'
            lines.append(
                f'  [{countdown}] {e.currency} — {e.event_name} '
                f'({e.impact.value.upper()})'
            )

        return '\n'.join(lines) if lines else '  None in next 8 hours'
```

---

## 5. Revised File: `src/agents/reviewer_agent.py`

Only the provider priority changes. Groq is now primary, Anthropic is fallback.
The reviewer uses `llama-3.1-8b-instant` (Groq's smaller model) as its
primary — fast enough for consistency checking, cheaper, lower latency.

Replace the full file with this:

```python
"""ReviewerAgent — Senior FX Execution Trader.

Role: 20-year institutional execution trader with final authority on whether
a trade recommendation is executed right now.

Provider priority (same as LLMAgent):
  1. Groq llama-3.1-8b-instant (primary — fast, sufficient for review task)
  2. Anthropic Claude Haiku (fallback on Groq credit exhaustion)
  3. Pass-through with Telegram warning when both are down

The reviewer uses a smaller Groq model than the analyst deliberately —
the review task (logical consistency check) does not require the same
reasoning depth as the analysis task.
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from openai import OpenAI

from .base import AgentVote, Signal

_MIN_CALL_SPACING_SECONDS = 5
_last_reviewer_call: float = 0.0

_CREDIT_EXHAUSTION_KEYWORDS = (
    'quota', 'credit', 'billing', 'insufficient',
    'payment', 'exceeded your', 'out of tokens', 'balance',
)


def _is_credit_exhausted(exc: Exception) -> bool:
    msg = str(exc).lower()
    if hasattr(exc, 'status_code') and getattr(exc, 'status_code', None) == 402:
        return True
    return any(kw in msg for kw in _CREDIT_EXHAUSTION_KEYWORDS)


class ReviewVerdict(Enum):
    APPROVED = 'APPROVED'
    ADJUSTED = 'ADJUSTED'
    REJECTED = 'REJECTED'


@dataclass
class ReviewResult:
    verdict: ReviewVerdict
    adjusted_confidence: float
    reason: str
    reviewer_available: bool


# ---------------------------------------------------------------------------
# Role and system prompt
# ---------------------------------------------------------------------------

_REVIEWER_SYSTEM_PROMPT = """You are a senior FX execution trader with 20 years of institutional
trading experience across G10 currency pairs.

You will receive:
1. A complete market briefing (technical indicators, macro context, recent news, upcoming events)
2. A trade recommendation from a quantitative analyst, including their reasoning

Your job is to make the final execution decision: should this trade be placed RIGHT NOW?

You are NOT re-analysing the market from scratch.
You are reviewing whether the analyst's recommendation is sound enough to execute given:
  - Is the analyst's reasoning logically consistent with the data shown?
  - Are there upcoming events that make this a bad time to enter?
  - Is the confidence level appropriate given the signal strength?
  - Would a professional trader execute this right now?

Rules you never break:
  1. Never approve a new position within 30 minutes of a VERY HIGH impact event
     (NFP, FOMC, rate decisions, GDP) for either currency in the pair.
  2. If the analyst's reasoning directly contradicts the data shown, REJECT.
  3. If confidence is clearly inflated vs the actual signal quality, ADJUST it down.
  4. You cannot change the direction (BUY to SELL or SELL to BUY).
     If you disagree with direction entirely, REJECT and let the next cycle decide.
  5. If technical signals are mixed but analyst still voted directionally,
     ADJUST confidence to reflect that uncertainty.

Respond with valid JSON only — no markdown, no preamble:
{"verdict": "APPROVED|ADJUSTED|REJECTED", "adjusted_confidence": 0.0-1.0, "reason": "max 150 chars"}

If APPROVED: set adjusted_confidence equal to the original analyst confidence.
If REJECTED: set adjusted_confidence to 0.0.
Speak like a trader in your reason — be direct and specific."""


class ReviewerAgent:
    """
    Senior FX execution trader.
    Provider priority: Groq (primary) → Anthropic (fallback).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('ReviewerAgent')

        # Groq — primary
        self._groq_client: Optional[OpenAI] = None
        self._groq_model:     str  = ''
        self._groq_exhausted: bool = False

        # Anthropic — fallback
        self._anthropic_client = None
        self._anthropic_model:     str  = ''
        self._anthropic_exhausted: bool = False

        self._init_groq()
        self._init_anthropic()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_groq(self) -> None:
        """
        Groq primary — uses llama-3.1-8b-instant for the reviewer.
        Smaller and faster than the analyst's 70B model; sufficient
        for logical consistency checking.
        """
        try:
            from config.settings import settings
            if not settings.GROQ_API_KEY:
                self.logger.warning('ReviewerAgent: GROQ_API_KEY not set — Groq disabled')
                self._groq_exhausted = True
                return
            self._groq_client = OpenAI(
                api_key=settings.GROQ_API_KEY,
                base_url='https://api.groq.com/openai/v1',
            )
            self._groq_model = settings.REVIEWER_LLM_MODEL
            self.logger.info(
                f'ReviewerAgent: Groq ready ({self._groq_model})'
            )
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent: Groq init failed: {exc}')
            self._groq_exhausted = True

    def _init_anthropic(self) -> None:
        """Anthropic fallback — activates when Groq credits are exhausted."""
        try:
            from config.settings import settings
            if not settings.ANTHROPIC_API_KEY:
                self.logger.info(
                    'ReviewerAgent: ANTHROPIC_API_KEY not set — Anthropic fallback disabled'
                )
                self._anthropic_exhausted = True
                return
            import anthropic as _sdk
            self._anthropic_client = _sdk.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY
            )
            self._anthropic_model = settings.ANTHROPIC_LLM_MODEL
            self.logger.info(
                f'ReviewerAgent: Anthropic fallback ready ({self._anthropic_model})'
            )
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent: Anthropic init failed: {exc}')
            self._anthropic_exhausted = True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return not (self._groq_exhausted and self._anthropic_exhausted)

    @property
    def both_exhausted(self) -> bool:
        return self._groq_exhausted and self._anthropic_exhausted

    @property
    def active_provider(self) -> str:
        if not self._groq_exhausted:
            return 'groq'
        if not self._anthropic_exhausted:
            return 'anthropic'
        return 'none'

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def review(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: dict,
        analyst_vote: AgentVote,
    ) -> ReviewResult:
        """
        Review the analyst's recommendation.
        Always returns ReviewResult — never raises.
        If unavailable, passes through analyst vote with a warning flag.
        """
        if self.both_exhausted:
            self.logger.warning(
                'ReviewerAgent: both providers exhausted — passing through'
            )
            return ReviewResult(
                verdict=ReviewVerdict.APPROVED,
                adjusted_confidence=analyst_vote.confidence,
                reason='Reviewer providers exhausted — passing through',
                reviewer_available=False,
            )

        try:
            return self._review(pair, candles, price, indicators, analyst_vote)
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent review failed for {pair}: {exc}')
            return ReviewResult(
                verdict=ReviewVerdict.APPROVED,
                adjusted_confidence=analyst_vote.confidence,
                reason=f'Reviewer error — passing through',
                reviewer_available=False,
            )

    # ------------------------------------------------------------------
    # Internal logic
    # ------------------------------------------------------------------

    def _review(
        self, pair, candles, price, indicators, analyst_vote
    ) -> ReviewResult:
        global _last_reviewer_call
        elapsed = time.time() - _last_reviewer_call
        if elapsed < _MIN_CALL_SPACING_SECONDS:
            time.sleep(_MIN_CALL_SPACING_SECONDS - elapsed)

        user_msg = _build_review_message(
            pair, candles, price, indicators, analyst_vote
        )
        _last_reviewer_call = time.time()

        # Try Groq first
        if not self._groq_exhausted and self._groq_client is not None:
            try:
                return self._call_groq(user_msg, analyst_vote)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        'ReviewerAgent: Groq credits exhausted — switching to Anthropic'
                    )
                    self._groq_exhausted = True
                else:
                    self.logger.warning(
                        f'ReviewerAgent: Groq transient error for {pair}: {exc}'
                    )
                    return ReviewResult(
                        verdict=ReviewVerdict.APPROVED,
                        adjusted_confidence=analyst_vote.confidence,
                        reason='Reviewer transient error — passing through',
                        reviewer_available=False,
                    )

        # Try Anthropic fallback
        if not self._anthropic_exhausted and self._anthropic_client is not None:
            try:
                return self._call_anthropic(user_msg, analyst_vote)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        'ReviewerAgent: Anthropic exhausted — both providers down'
                    )
                    self._anthropic_exhausted = True
                else:
                    self.logger.warning(
                        f'ReviewerAgent: Anthropic transient error for {pair}: {exc}'
                    )
                    return ReviewResult(
                        verdict=ReviewVerdict.APPROVED,
                        adjusted_confidence=analyst_vote.confidence,
                        reason='Reviewer transient error — passing through',
                        reviewer_available=False,
                    )

        return ReviewResult(
            verdict=ReviewVerdict.APPROVED,
            adjusted_confidence=analyst_vote.confidence,
            reason='All reviewer providers exhausted — passing through',
            reviewer_available=False,
        )

    def _call_groq(
        self, user_msg: str, analyst_vote: AgentVote
    ) -> ReviewResult:
        response = self._groq_client.chat.completions.create(
            model=self._groq_model,
            max_tokens=200,
            messages=[
                {'role': 'system', 'content': _REVIEWER_SYSTEM_PROMPT},
                {'role': 'user',   'content': user_msg},
            ],
        )
        return _parse_review_response(
            response.choices[0].message.content.strip(), analyst_vote
        )

    def _call_anthropic(
        self, user_msg: str, analyst_vote: AgentVote
    ) -> ReviewResult:
        response = self._anthropic_client.messages.create(
            model=self._anthropic_model,
            max_tokens=200,
            system=_REVIEWER_SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': user_msg}],
        )
        return _parse_review_response(
            response.content[0].text.strip(), analyst_vote
        )


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_review_message(
    pair: str,
    candles: List[Dict],
    price: float,
    indicators: dict,
    analyst_vote: AgentVote,
) -> str:
    from .llm_agent import _build_analyst_message
    briefing = _build_analyst_message(pair, candles, price, indicators)

    return (
        f'{briefing}\n\n'
        f'=== ANALYST RECOMMENDATION ===\n'
        f'Vote:       {analyst_vote.signal.value}\n'
        f'Confidence: {analyst_vote.confidence:.2f}\n'
        f'Reasoning:  {analyst_vote.reasoning}\n\n'
        f'Review this recommendation. Should we execute this trade right now?'
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_review_response(
    raw: str, analyst_vote: AgentVote
) -> ReviewResult:
    text = raw.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(lines[1:-1]) if len(lines) > 2 else text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return ReviewResult(
                    ReviewVerdict.APPROVED,
                    analyst_vote.confidence,
                    'JSON parse error — passing through',
                    True,
                )
        else:
            return ReviewResult(
                ReviewVerdict.APPROVED,
                analyst_vote.confidence,
                'JSON parse error — passing through',
                True,
            )

    verdict_str = data.get('verdict', 'APPROVED').upper()
    try:
        verdict = ReviewVerdict[verdict_str]
    except KeyError:
        verdict = ReviewVerdict.APPROVED

    try:
        adj_conf = float(
            data.get('adjusted_confidence', analyst_vote.confidence)
        )
        adj_conf = max(0.0, min(1.0, adj_conf))
    except (TypeError, ValueError):
        adj_conf = analyst_vote.confidence

    reason = str(data.get('reason', ''))[:150]

    return ReviewResult(
        verdict=verdict,
        adjusted_confidence=round(adj_conf, 4),
        reason=reason,
        reviewer_available=True,
    )
```

---

## 6. Revised File: `config/settings.py`

**Remove** `NEWSAPI_KEY` (was added in v1 — never needed now).

**Change** `REVIEWER_LLM_MODEL` default to use Groq's smaller model:

Find and replace these lines in the `GROQ / LLM AGENT` section:

```python
    # Reviewer agent model
    # Uses Groq's smaller model for fast consistency checking.
    # Falls back to Anthropic Claude Haiku if Groq is exhausted.
    REVIEWER_LLM_MODEL: str = os.getenv(
        'REVIEWER_LLM_MODEL', 'llama-3.1-8b-instant'
    )
```

The full `GROQ / LLM AGENT` block should look like this after the change:

```python
    # ==========================================
    # GROQ / LLM AGENT
    # ==========================================
    GROQ_API_KEY: str = os.getenv('GROQ_API_KEY', '')
    LLM_MODEL:    str = os.getenv('LLM_MODEL', 'llama-3.3-70b-versatile')
    LLM_AGENT_WEIGHT: float = float(os.getenv('LLM_AGENT_WEIGHT', '1.5'))

    # Anthropic — fallback for both analyst and reviewer
    ANTHROPIC_API_KEY:   str = os.getenv('ANTHROPIC_API_KEY', '')
    ANTHROPIC_LLM_MODEL: str = os.getenv(
        'ANTHROPIC_LLM_MODEL', 'claude-haiku-4-5-20251001'
    )

    # Reviewer model — Groq small model (fast, sufficient for review task)
    # Anthropic fallback uses ANTHROPIC_LLM_MODEL above
    REVIEWER_LLM_MODEL: str = os.getenv(
        'REVIEWER_LLM_MODEL', 'llama-3.1-8b-instant'
    )
```

---

## 7. Revised File: `.env.template`

Remove the `NEWSAPI_KEY` block. Update the `REVIEWER_LLM_MODEL` default.
The `LLM STRATEGIST` section should look like this:

```
# ==========================================
# LLM AGENTS
# ==========================================

# Groq API — PRIMARY for both analyst and reviewer
# Get key: https://console.groq.com
GROQ_API_KEY=your_groq_api_key_here

# Groq model for analyst (quantitative FX analyst)
LLM_MODEL=llama-3.3-70b-versatile

# Groq model for reviewer (senior execution trader)
# Uses smaller/faster model — sufficient for consistency checking
REVIEWER_LLM_MODEL=llama-3.1-8b-instant

# Anthropic Claude — FALLBACK for both analyst and reviewer
# Activates automatically when Groq credits are exhausted
# Get key: https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY=your_anthropic_api_key_here
ANTHROPIC_LLM_MODEL=claude-haiku-4-5-20251001
```

---

## 8. New Telegram Commands

Two new commands are added: `/analyst` and `/reviewer`.

The existing commands are unchanged:

| Command | What it does |
|---------|-------------|
| `/stop` | Activate kill switch — halt all trading |
| `/resume` | Deactivate kill switch — resume trading |
| `/status` | Current bot status, open positions, P/L |
| `/calendar` | Upcoming economic events today |
| `/calhistory` | Past economic events today |
| `/logs` | Last 30 log lines for today |
| `/credits` | LLM provider credit status |
| `/help` | Show all commands |

Two new commands:

| Command | What it does |
|---------|-------------|
| `/analyst` | Show last analyst decision per pair (signal, confidence, reasoning) |
| `/reviewer` | Show last reviewer verdict per pair (APPROVED/ADJUSTED/REJECTED + reason) |

These are useful for debugging — you can see exactly what the analyst
decided and what the reviewer did with it, without waiting for a trade
to fire.

---

## 9. Changes to `src/monitoring/alerts.py`

### 9.1 Add new alert method for reviewer unavailability

Add this method to the `AlertManager` class after `alert_llm_credits_exhausted`:

```python
    def alert_reviewer_unavailable(self, pair: str, reason: str):
        """Send alert when ReviewerAgent is unavailable — analyst vote passing through."""
        message = (
            f'⚠️ *Reviewer Unavailable*\n\n'
            f'*Pair:* `{pair}`\n'
            f'*Reason:* {reason}\n'
            f'Analyst vote is passing through unreviewed this cycle.'
        )
        self.send_alert(message, 'WARNING')
```

### 9.2 Add `/analyst` and `/reviewer` command handlers

**Step 1:** Add `get_analyst_fn` and `get_reviewer_fn` parameters to
`start_command_poller()`. Find the method signature and add two parameters:

```python
    def start_command_poller(
        self,
        kill_switch=None,
        get_status_fn=None,
        get_calendar_fn=None,
        get_calhistory_fn=None,
        get_credits_fn=None,
        get_analyst_fn=None,      # ← ADD THIS
        get_reviewer_fn=None,     # ← ADD THIS
        poll_interval_seconds: int = 10,
    ) -> None:
```

Store the new callbacks alongside the existing ones:

```python
        self._get_analyst_fn  = get_analyst_fn
        self._get_reviewer_fn = get_reviewer_fn
```

**Step 2:** In `_check_commands()`, add two new elif branches.
Find the `/credits` elif and add after it:

```python
                elif text == '/analyst':
                    self._handle_analyst()
                elif text == '/reviewer':
                    self._handle_reviewer()
```

**Step 3:** Update `/help` response to include new commands:

```python
                elif text == '/help':
                    self._send_telegram(
                        '🤖 *FX Bot Commands*\n\n'
                        '/stop — activate kill switch\n'
                        '/resume — deactivate kill switch\n'
                        '/status — current bot status and positions\n'
                        '/calendar — upcoming economic events today\n'
                        '/calhistory — past economic events today\n'
                        '/logs — today\'s bot log entries\n'
                        '/credits — LLM provider credit status\n'
                        '/analyst — last analyst decision per pair\n'
                        '/reviewer — last reviewer verdict per pair\n'
                        '/help — show this message'
                    )
```

**Step 4:** Add the two handler methods at the end of the class:

```python
    def _handle_analyst(self) -> None:
        """Reply with the last analyst decision for each pair."""
        fn = getattr(self, '_get_analyst_fn', None)
        if not fn:
            self._send_telegram(
                '🧠 *Analyst*\n\nAnalyst history not available.',
                parse_mode=''
            )
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode='')
        except Exception as exc:
            self._send_telegram(
                f'🧠 *Analyst*\n\nFailed to fetch analyst history: {exc}',
                parse_mode=''
            )

    def _handle_reviewer(self) -> None:
        """Reply with the last reviewer verdict for each pair."""
        fn = getattr(self, '_get_reviewer_fn', None)
        if not fn:
            self._send_telegram(
                '🔍 *Reviewer*\n\nReviewer history not available.',
                parse_mode=''
            )
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode='')
        except Exception as exc:
            self._send_telegram(
                f'🔍 *Reviewer*\n\nFailed to fetch reviewer history: {exc}',
                parse_mode=''
            )
```

---

## 10. Changes to `src/voting/engine.py`

Store the last decision per pair so Telegram can retrieve it.
Add a `_last_results` dict and two getter methods.

**Step 1:** In `__init__`, add:

```python
        self._last_results: dict = {}   # pair -> VoteResult
```

**Step 2:** At the end of `run_vote()`, before returning, store the result:

```python
        self._last_results[pair] = result
        return result
```

**Step 3:** Add two new getter methods:

```python
    def get_analyst_summary(self) -> str:
        """Return last analyst decision per pair — used by /analyst command."""
        if not self._last_results:
            return '🧠 Analyst History\n\nNo decisions yet this session.'

        lines = ['🧠 Analyst History\n']
        for pair, result in self._last_results.items():
            llm_vote = result.agent_votes[0] if result.agent_votes else None
            if llm_vote:
                lines.append(
                    f'{pair}\n'
                    f'  Signal:     {llm_vote.signal.value}\n'
                    f'  Confidence: {llm_vote.confidence:.2f}\n'
                    f'  Reasoning:  {llm_vote.reasoning}'
                )
            else:
                lines.append(f'{pair}\n  No analyst data')
        return '\n\n'.join(lines)

    def get_reviewer_summary(self) -> str:
        """Return last reviewer verdict per pair — used by /reviewer command."""
        if not self._last_results:
            return '🔍 Reviewer History\n\nNo decisions yet this session.'

        lines = ['🔍 Reviewer History\n']
        for pair, result in self._last_results.items():
            verdict  = getattr(result, 'reviewer_verdict', 'N/A')
            reason   = getattr(result, 'reviewer_reason',  'N/A')
            adj_conf = result.consensus_score

            verdict_badge = {
                'APPROVED':    '✅ APPROVED',
                'ADJUSTED':    '⚠️  ADJUSTED',
                'REJECTED':    '❌ REJECTED',
                'SKIPPED':     '— SKIPPED',
                'UNAVAILABLE': '⚠️  REVIEWER DOWN',
            }.get(verdict, verdict)

            lines.append(
                f'{pair}\n'
                f'  Verdict:    {verdict_badge}\n'
                f'  Confidence: {adj_conf:.2f}\n'
                f'  Reason:     {reason}'
            )
        return '\n\n'.join(lines)
```

---

## 11. Changes to `src/main.py`

**Change 1:** Pass `event_monitor` into `VotingEngine` (same as v1):

```python
    event_monitor = EventMonitor(logger)
    voting_engine = VotingEngine(
        logger,
        alert_manager=alert_manager,
        event_monitor=event_monitor,
    )
```

**Change 2:** Wire the two new Telegram callbacks into `start_command_poller()`.
Find the existing call and add the two new parameters:

```python
    alert_manager.start_command_poller(
        kill_switch=kill_switch,
        get_status_fn=engine.get_status,
        get_calendar_fn=_get_calendar_text,
        get_calhistory_fn=_get_calhistory_text,
        get_credits_fn=voting_engine.get_llm_provider_status,
        get_analyst_fn=voting_engine.get_analyst_summary,      # ← ADD
        get_reviewer_fn=voting_engine.get_reviewer_summary,    # ← ADD
    )
```

**Change 3:** In `_process_pair()` inside `TradingEngine`, add the reviewer
unavailability alert. Find the block in `execution/engine.py` where the
reviewer result is checked and add:

```python
        if not review.reviewer_available and self._alert_manager is not None:
            try:
                self._alert_manager.alert_reviewer_unavailable(
                    pair, review.reason
                )
            except Exception:
                pass
```

---

## 12. Summary of All Provider States

| Analyst Groq | Analyst Anthropic | Outcome |
|:---:|:---:|---------|
| ✅ Active | Any | Analyst uses Groq |
| ❌ Exhausted | ✅ Active | Analyst switches to Anthropic |
| ❌ Exhausted | ❌ Exhausted | **HOLD — no trade** |

| Reviewer Groq | Reviewer Anthropic | Outcome |
|:---:|:---:|---------|
| ✅ Active | Any | Reviewer uses Groq (8B model) |
| ❌ Exhausted | ✅ Active | Reviewer switches to Anthropic |
| ❌ Exhausted | ❌ Exhausted | Pass-through + Telegram warning |

Note: Groq has **shared** credit across both agents. If the analyst
exhausts Groq credits, the reviewer's Groq will also be exhausted.
In practice this means: if Groq goes down, both agents fall back to
Anthropic simultaneously. This is expected and handled correctly.

---

## 13. Files Changed in v2 vs v1

| File | v1 | v2 |
|------|----|----|
| `src/agents/macro_context.py` | NewsAPI | JB News API |
| `src/agents/reviewer_agent.py` | Anthropic primary, Groq fallback | **Groq primary, Anthropic fallback** |
| `src/monitoring/alerts.py` | 8 commands | **10 commands** + new alert method |
| `src/voting/engine.py` | No history storage | **Stores last result per pair** + 2 getter methods |
| `src/main.py` | 5 Telegram callbacks | **7 Telegram callbacks** |
| `config/settings.py` | Had NEWSAPI_KEY | **Removed NEWSAPI_KEY**, REVIEWER_LLM_MODEL default changed |
| `.env.template` | Had NEWSAPI_KEY | **Removed NEWSAPI_KEY**, updated LLM section |

All other files from v1 are **unchanged**.

---

## 14. Implementation Order

1. Update `config/settings.py` — change `REVIEWER_LLM_MODEL` default, remove `NEWSAPI_KEY`
2. Update `.env.template` — update LLM section, remove `NEWSAPI_KEY`
3. Update your `.env` file — add `REVIEWER_LLM_MODEL=llama-3.1-8b-instant`
4. Replace `src/agents/macro_context.py` — JB News version
5. Replace `src/agents/reviewer_agent.py` — Groq primary version
6. Update `src/voting/engine.py` — add `_last_results` + getter methods
7. Update `src/monitoring/alerts.py` — new alert method + 2 new commands
8. Update `src/main.py` — event_monitor wiring + 2 new Telegram callbacks
9. Run `python src/main.py --test` to verify all components load correctly
