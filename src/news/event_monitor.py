"""Economic event monitoring and tracking.

Uses Firecrawl to search for upcoming high-impact economic events.
Replaces the previous investpy/NewsAPI-based implementation.
"""

import logging
import re
from typing import List, Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import pytz

from config.settings import settings


class EventImpact(Enum):
    """Event impact levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass
class EconomicEvent:
    """Economic event data."""
    event_id: str
    time: datetime
    currency: str
    impact: EventImpact
    event_name: str
    forecast: str
    previous: str
    actual: str
    affects_pairs: List[str]
    minutes_until: float

    def is_imminent(self, minutes: int = 30) -> bool:
        """Check if event is within X minutes."""
        return 0 <= self.minutes_until <= minutes

    def is_past(self) -> bool:
        """Check if event has passed."""
        return self.minutes_until < 0


# High-impact event keywords — any match → VERY_HIGH impact
_VERY_HIGH_KEYWORDS = [
    'NFP', 'Non-Farm Payroll', 'FOMC', 'Federal Reserve', 'Fed Rate',
    'CPI', 'Consumer Price Index', 'GDP', 'Gross Domestic Product',
    'Interest Rate', 'Rate Decision', 'Central Bank', 'BOE', 'ECB', 'BOJ',
    'RBA', 'SNB', 'Employment Change', 'Unemployment Rate', 'Retail Sales',
    'ISM Manufacturing', 'ISM Services', 'PCE', 'PPI', 'Trade Balance',
]

# Currency codes we monitor
_MONITORED_CURRENCIES = ['USD', 'EUR', 'GBP', 'JPY', 'CHF', 'AUD', 'NZD', 'CAD']


class EventMonitor:
    """Monitor and track economic events continuously.

    Uses Firecrawl to search for upcoming high-impact economic events.
    Falls back to an empty event list when Firecrawl is unavailable —
    trading continues normally (no false suspensions).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize event monitor.

        Args:
            logger: Logger instance
        """
        self.logger = logger or logging.getLogger('event_monitor')

        # Lazy-import FirecrawlSource to avoid circular imports at module level
        self._firecrawl = None

        # Cache
        self._cached_events: List[EconomicEvent] = []
        self._last_update: Optional[datetime] = None
        self._update_interval = timedelta(minutes=15)

        # High-impact keywords from settings (merged with hardcoded list)
        self.high_impact_keywords: List[str] = list(
            set(settings.HIGH_IMPACT_EVENTS) | set(_VERY_HIGH_KEYWORDS)
        )

    # ------------------------------------------------------------------
    # Public interface (unchanged — SuspensionManager depends on these)
    # ------------------------------------------------------------------

    def get_upcoming_events(
        self,
        hours_ahead: int = 24,
        min_impact: EventImpact = EventImpact.MEDIUM,
        force_refresh: bool = False
    ) -> List[EconomicEvent]:
        """
        Get upcoming economic events.

        Args:
            hours_ahead: Look ahead this many hours
            min_impact: Minimum impact level to include
            force_refresh: Force refresh from Firecrawl

        Returns:
            List of economic events
        """
        now = datetime.now(pytz.UTC)

        if (not force_refresh
                and self._last_update
                and (now - self._last_update) < self._update_interval
                and self._cached_events):
            self.logger.debug("Using cached events")
            return self._filter_events(self._cached_events, min_impact)

        events = self._fetch_calendar_events()

        # Filter to requested window
        cutoff = now + timedelta(hours=hours_ahead)
        events = [e for e in events if e.minutes_until <= hours_ahead * 60]

        self._cached_events = events
        self._last_update = now
        self.logger.info(f"EventMonitor: {len(events)} upcoming events fetched")
        return self._filter_events(events, min_impact)

    def get_imminent_events(
        self,
        minutes: int = 30,
        min_impact: EventImpact = EventImpact.HIGH
    ) -> List[EconomicEvent]:
        """
        Get events that are imminent (within X minutes).

        Args:
            minutes: Look ahead this many minutes
            min_impact: Minimum impact level

        Returns:
            List of imminent events
        """
        all_events = self.get_upcoming_events(hours_ahead=2)

        imminent = [
            event for event in all_events
            if event.is_imminent(minutes)
            and self._impact_level(event.impact) >= self._impact_level(min_impact)
        ]

        if imminent:
            self.logger.warning(f"Found {len(imminent)} imminent high-impact events!")

        return imminent

    def get_events_for_pair(
        self,
        pair: str,
        hours_ahead: int = 24
    ) -> List[EconomicEvent]:
        """
        Get events affecting a specific currency pair.

        Args:
            pair: Trading pair (e.g., 'EUR_USD')
            hours_ahead: Look ahead hours

        Returns:
            List of relevant events
        """
        all_events = self.get_upcoming_events(hours_ahead=hours_ahead)
        return [e for e in all_events if pair in e.affects_pairs]

    def should_suspend_trading(
        self,
        pair: Optional[str] = None,
        minutes_before: Optional[int] = None
    ) -> tuple:
        """
        Check if trading should be suspended due to upcoming events.

        Args:
            pair: Specific pair to check (None = check all)
            minutes_before: Suspension window in minutes

        Returns:
            Tuple of (should_suspend, triggering_event)
        """
        if minutes_before is None:
            minutes_before = settings.NEWS_SUSPEND_BEFORE_MINUTES

        imminent = self.get_imminent_events(
            minutes=minutes_before,
            min_impact=EventImpact.HIGH
        )

        if not imminent:
            return False, None

        if pair:
            for event in imminent:
                if pair in event.affects_pairs:
                    self.logger.warning(
                        f"Suspension triggered for {pair}: "
                        f"{event.event_name} in {event.minutes_until:.0f} min"
                    )
                    return True, event
            return False, None

        # General suspension
        event = imminent[0]
        self.logger.warning(
            f"General trading suspension: {event.event_name} "
            f"in {event.minutes_until:.0f} min"
        )
        return True, event

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_firecrawl(self):
        """Lazy-load FirecrawlSource to avoid circular imports."""
        if self._firecrawl is None:
            try:
                from src.dataflows.firecrawl_source import FirecrawlSource
                self._firecrawl = FirecrawlSource(logger=self.logger)
            except Exception as exc:
                self.logger.warning(f"Could not load FirecrawlSource: {exc}")
        return self._firecrawl

    def _fetch_calendar_events(self) -> List[EconomicEvent]:
        """
        Fetch upcoming economic events via Firecrawl search.

        Returns:
            List of EconomicEvent objects (empty on failure)
        """
        firecrawl = self._get_firecrawl()
        if firecrawl is None:
            return []

        try:
            articles = firecrawl.search_fx_news(
                query="forex economic calendar high impact events this week NFP FOMC CPI",
                limit=5
            )
        except Exception as exc:
            self.logger.warning(f"Firecrawl calendar fetch failed: {exc}")
            return []

        events: List[EconomicEvent] = []
        now = datetime.now(pytz.UTC)

        for i, article in enumerate(articles):
            try:
                text = f"{article.title} {article.content}"
                parsed = self._parse_events_from_text(text, now, base_id=f"fc_{i}")
                events.extend(parsed)
            except Exception as exc:
                self.logger.debug(f"Event parse error for article {i}: {exc}")

        # Deduplicate by event name + currency
        seen = set()
        deduped = []
        for e in events:
            key = (e.event_name.lower()[:30], e.currency)
            if key not in seen:
                seen.add(key)
                deduped.append(e)

        return deduped

    def _parse_events_from_text(
        self,
        text: str,
        now: datetime,
        base_id: str
    ) -> List[EconomicEvent]:
        """
        Parse economic events from scraped markdown text.

        Looks for known high-impact event names and associated currency codes.
        Since exact timestamps are rarely available in news text, defaults
        minutes_until to 60 (conservative — will trigger suspension if matched).

        Args:
            text: Scraped article text
            now: Current UTC time
            base_id: ID prefix for generated events

        Returns:
            List of EconomicEvent objects
        """
        events = []
        text_upper = text.upper()

        for idx, keyword in enumerate(self.high_impact_keywords):
            if keyword.upper() not in text_upper:
                continue

            # Find which currencies are mentioned near this keyword
            # Search a 200-char window around the first occurrence
            pos = text_upper.find(keyword.upper())
            window = text_upper[max(0, pos - 100):pos + 200]

            currencies_found = [c for c in _MONITORED_CURRENCIES if c in window]
            if not currencies_found:
                # Try the full text
                currencies_found = [c for c in _MONITORED_CURRENCIES if c in text_upper]
            if not currencies_found:
                currencies_found = ['USD']  # fallback

            for currency in currencies_found:
                # Assign impact
                impact = self._classify_impact(keyword)

                # Conservative default: 60 min until (triggers suspension window)
                minutes_until = 60.0

                # Try to extract a more specific time hint (e.g. "in 2 hours", "tomorrow")
                minutes_until = self._extract_time_hint(window, minutes_until)

                affects_pairs = self._get_affected_pairs(currency)

                event = EconomicEvent(
                    event_id=f"{base_id}_{idx}_{currency}",
                    time=now + timedelta(minutes=minutes_until),
                    currency=currency,
                    impact=impact,
                    event_name=keyword,
                    forecast='',
                    previous='',
                    actual='',
                    affects_pairs=affects_pairs,
                    minutes_until=minutes_until
                )
                events.append(event)
                break  # One event per keyword match (first currency wins)

        return events

    def _classify_impact(self, keyword: str) -> EventImpact:
        """Map keyword to impact level."""
        kw_upper = keyword.upper()
        if any(k.upper() in kw_upper or kw_upper in k.upper()
               for k in _VERY_HIGH_KEYWORDS):
            return EventImpact.VERY_HIGH
        return EventImpact.HIGH

    def _extract_time_hint(self, window: str, default_minutes: float) -> float:
        """
        Try to parse a rough time hint from text window.

        Patterns matched: "in X hours", "in X minutes", "tomorrow", "today"
        Returns default_minutes when no pattern matched.
        """
        window_lower = window.lower()

        # "in X hours"
        m = re.search(r'in\s+(\d+)\s+hours?', window_lower)
        if m:
            return float(m.group(1)) * 60

        # "in X minutes"
        m = re.search(r'in\s+(\d+)\s+min', window_lower)
        if m:
            return float(m.group(1))

        # "tomorrow" — roughly 24h
        if 'tomorrow' in window_lower:
            return 24 * 60

        # "this week" — roughly 3 days
        if 'this week' in window_lower:
            return 3 * 24 * 60

        return default_minutes

    def _get_affected_pairs(self, currency: str) -> List[str]:
        """Get trading pairs affected by a currency."""
        return [
            pair for pair in settings.TRADING_PAIRS
            if currency in pair.split('_')
        ]

    def _filter_events(
        self,
        events: List[EconomicEvent],
        min_impact: EventImpact
    ) -> List[EconomicEvent]:
        """Filter events by minimum impact level."""
        min_level = self._impact_level(min_impact)
        return [e for e in events if self._impact_level(e.impact) >= min_level]

    def _impact_level(self, impact: EventImpact) -> int:
        """Convert impact to numeric level for comparison."""
        return {
            EventImpact.LOW: 1,
            EventImpact.MEDIUM: 2,
            EventImpact.HIGH: 3,
            EventImpact.VERY_HIGH: 4,
        }.get(impact, 0)

    def get_event_summary(self, events: List[EconomicEvent]) -> str:
        """Get human-readable event summary."""
        if not events:
            return "No upcoming events"

        lines = [f"\nUpcoming Events ({len(events)}):"]

        for event in sorted(events, key=lambda e: e.minutes_until):
            time_str = f"{int(event.minutes_until)}min" if event.minutes_until >= 0 else "PAST"
            lines.append(
                f"  [{time_str}] {event.currency}: {event.event_name} "
                f"({event.impact.value})"
            )

        return "\n".join(lines)
