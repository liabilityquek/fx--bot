"""Economic event monitoring and tracking.

Uses the jb-news API (jblanked.com) to fetch today's economic calendar events.
"""

import logging
import requests
from typing import List, Optional
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


# High-impact event keywords — any match upgrades impact to VERY_HIGH
_VERY_HIGH_KEYWORDS = [
    'NFP', 'Non-Farm Payroll', 'FOMC', 'Federal Reserve', 'Fed Rate',
    'CPI', 'Consumer Price Index', 'GDP', 'Gross Domestic Product',
    'Interest Rate', 'Rate Decision', 'Central Bank', 'BOE', 'ECB', 'BOJ',
    'RBA', 'SNB', 'Employment Change', 'Unemployment Rate', 'Retail Sales',
    'ISM Manufacturing', 'ISM Services', 'PCE', 'PPI', 'Trade Balance',
]

# Currency codes we monitor
_MONITORED_CURRENCIES = ['USD', 'EUR', 'GBP', 'JPY', 'CHF', 'AUD', 'NZD', 'CAD']

# jb-news impact string → EventImpact
_IMPACT_MAP = {
    'High': EventImpact.HIGH,
    'Medium': EventImpact.MEDIUM,
    'Low': EventImpact.LOW,
}

_JB_NEWS_BASE = "https://www.jblanked.com/news/api"


class EventMonitor:
    """Monitor and track economic events via the jb-news API.

    Falls back to an empty event list when the API is unavailable —
    trading continues normally (no false suspensions).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('event_monitor')

        # Cache
        self._cached_events: List[EconomicEvent] = []
        self._last_update: Optional[datetime] = None
        self._update_interval = timedelta(hours=settings.EVENT_CACHE_TTL_HOURS)

        # High-impact keywords from settings (merged with hardcoded list)
        self.high_impact_keywords: List[str] = list(
            set(settings.HIGH_IMPACT_EVENTS) | set(_VERY_HIGH_KEYWORDS)
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_upcoming_events(
        self,
        hours_ahead: int = 24,
        min_impact: EventImpact = EventImpact.MEDIUM,
        force_refresh: bool = False
    ) -> List[EconomicEvent]:
        """Get upcoming economic events for today."""
        now = datetime.now(pytz.UTC)

        if (not force_refresh
                and self._last_update
                and (now - self._last_update) < self._update_interval
                and self._cached_events):
            self.logger.debug("Using cached events")
            return self._filter_events(self._cached_events, min_impact)

        events = self._fetch_calendar_events()

        # Keep today's events: not more than 60 min in the past, within look-ahead window
        events = [e for e in events if e.minutes_until >= -60 and e.minutes_until <= hours_ahead * 60]

        self._cached_events = events
        self._last_update = now
        upcoming_count = sum(1 for e in events if e.minutes_until >= 0)
        self.logger.info(f"EventMonitor: {upcoming_count} upcoming events fetched ({len(events)} total today)")
        return self._filter_events(events, min_impact)

    def get_imminent_events(
        self,
        minutes: int = 30,
        min_impact: EventImpact = EventImpact.HIGH
    ) -> List[EconomicEvent]:
        """Get events that are imminent (within X minutes)."""
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
        """Get events affecting a specific currency pair."""
        all_events = self.get_upcoming_events(hours_ahead=hours_ahead)
        return [e for e in all_events if pair in e.affects_pairs]

    def should_suspend_trading(
        self,
        pair: Optional[str] = None,
        minutes_before: Optional[int] = None
    ) -> tuple:
        """Check if trading should be suspended due to upcoming events."""
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

    def _fetch_calendar_events(self) -> List[EconomicEvent]:
        """Fetch today's economic events from the jb-news API."""
        api_key = settings.JB_NEWS_API_KEY
        if not api_key:
            self.logger.warning("JB_NEWS_API_KEY not set — calendar events unavailable")
            return []

        url = f"{_JB_NEWS_BASE}/mql5/calendar/today/"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {api_key}",
        }

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.logger.warning(f"jb-news calendar fetch failed: {exc}")
            return []

        if not isinstance(data, list):
            self.logger.warning(f"Unexpected jb-news response format: {type(data)}")
            return []

        now = datetime.now(pytz.UTC)
        events: List[EconomicEvent] = []

        for item in data:
            try:
                event = self._parse_event(item, now)
                if event is not None:
                    events.append(event)
            except Exception as exc:
                self.logger.debug(f"Event parse error: {exc}")

        return events

    def _parse_event(self, item: dict, now: datetime) -> Optional[EconomicEvent]:
        """Parse a single jb-news calendar item into an EconomicEvent."""
        name = item.get("Name", "")
        currency = item.get("Currency", "")
        event_id = str(item.get("Event_ID", ""))
        impact_str = item.get("Impact", "Low")
        date_str = item.get("Date", "")
        actual = item.get("Actual", 0.0)
        forecast = item.get("Forecast", 0.0)
        previous = item.get("Previous", 0.0)

        if currency not in _MONITORED_CURRENCIES:
            return None

        # Parse date format: "2026.04.20 15:30:00"
        try:
            event_dt = datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S")
            event_dt = pytz.UTC.localize(event_dt)
        except ValueError:
            return None

        minutes_until = (event_dt - now).total_seconds() / 60.0

        # Map impact string → EventImpact
        impact = _IMPACT_MAP.get(impact_str, EventImpact.LOW)

        # Upgrade High → VERY_HIGH if name matches critical keywords
        if impact == EventImpact.HIGH:
            name_upper = name.upper()
            if any(kw.upper() in name_upper for kw in _VERY_HIGH_KEYWORDS):
                impact = EventImpact.VERY_HIGH

        affects_pairs = self._get_affected_pairs(currency)

        return EconomicEvent(
            event_id=event_id,
            time=event_dt,
            currency=currency,
            impact=impact,
            event_name=name,
            forecast=str(forecast),
            previous=str(previous),
            actual=str(actual),
            affects_pairs=affects_pairs,
            minutes_until=minutes_until,
        )

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
