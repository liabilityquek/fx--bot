"""Macro context assembler for the LLM analyst briefing.

Provides three data points per pair per cycle:
  1. Interest rate differential between the two currencies
  2. Recent forex news headlines (JB News API — same key as calendar)
  3. Upcoming high-impact economic events (EventMonitor — already running)

All methods fail silently so a data fetch failure never blocks a trade cycle.
"""

import logging
import time
from typing import Optional

import requests

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
_JB_NEWS_BASE  = 'https://www.jblanked.com/news/api'
_NEWS_ENDPOINT = f'{_JB_NEWS_BASE}/forex/news/'

# Cache: avoid calling the news endpoint on every cycle
_news_cache: dict = {}  # currency -> (timestamp, headlines_str)


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
        Results are cached per EVENT_CACHE_TTL_HOURS per currency.
        """
        api_key = settings.JB_NEWS_API_KEY
        if not api_key:
            return '(JB_NEWS_API_KEY not set)'

        cache_ttl = settings.EVENT_CACHE_TTL_HOURS * 3600
        base, quote = pair.split('_')
        headlines_by_currency = {}

        for currency in [base, quote]:
            cached = _news_cache.get(currency)
            if cached:
                ts, text = cached
                if time.time() - ts < cache_ttl:
                    headlines_by_currency[currency] = text
                    continue

            text = self._fetch_currency_news(currency, api_key)
            _news_cache[currency] = (time.time(), text)
            headlines_by_currency[currency] = text

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
