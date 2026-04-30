"""HolidayGuard — blocks new trades on FX market holidays.

The FX spot market is global and never fully closes, but liquidity collapses
on major public holidays (New Year's Day, Good Friday, Christmas). Spreads
widen, slippage is unpredictable, and fills can be significantly worse than
expected.

This guard uses the NYSE calendar (XNYS) via exchange_calendars as the
standard proxy for FX market holidays. NYSE non-sessions correspond closely
to the handful of days per year where major FX liquidity genuinely dries up.

Behaviour:
  - Holiday detected  → block new trades, log WARNING, send Telegram alert
  - Not a holiday     → pass through (no-op)
  - Library missing   → fail open (log WARNING, allow trades)
  - Calendar error    → fail open (log WARNING, allow trades)

Existing open positions are NOT closed — broker-side SL/TP and the
EmergencyRiskController continue to protect them regardless.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

try:
    import exchange_calendars as xcals
    _XCALS_AVAILABLE = True
except ImportError:
    _XCALS_AVAILABLE = False


class HolidayGuard:
    """Prevent new trades from opening on FX market holidays.

    Parameters
    ----------
    logger : logging.Logger, optional
        Logger instance.
    """

    # NYSE calendar is the standard proxy for FX market holidays.
    # Covers New Year's Day, Good Friday, Christmas, and a handful of others.
    _CALENDAR_CODE = "XNYS"

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("holiday_guard")
        self._calendar = None

        if not _XCALS_AVAILABLE:
            self.logger.warning(
                "HolidayGuard: exchange_calendars not installed — "
                "holiday checks disabled. Add exchange_calendars to requirements.txt."
            )
            return

        try:
            self._calendar = xcals.get_calendar(self._CALENDAR_CODE)
            self.logger.info(
                f"HolidayGuard: loaded {self._CALENDAR_CODE} calendar OK"
            )
        except Exception as exc:
            self.logger.warning(
                f"HolidayGuard: could not load {self._CALENDAR_CODE} calendar: {exc} — "
                "holiday checks disabled"
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_holiday(self, now: Optional[datetime] = None) -> bool:
        """Return True if today is an FX market holiday.

        Fails open (returns False) if the calendar is unavailable, so a
        library error never blocks trading.
        """
        if self._calendar is None:
            return False

        today = (now or datetime.now(tz=timezone.utc)).date()
        try:
            is_session = self._calendar.is_session(today.isoformat())
            is_holiday = not is_session

            # Debug logging to understand why April 30, 2026 is flagged as holiday
            self.logger.info(
                f"HolidayGuard: checking {today.isoformat()} | "
                f"is_session={is_session} | is_holiday={is_holiday} | "
                f"calendar={self._CALENDAR_CODE}"
            )

            return is_holiday
        except Exception as exc:
            self.logger.warning(
                f"HolidayGuard: calendar check failed for {today}: {exc} — "
                "treating as non-holiday"
            )
            return False

    def is_safe_to_trade(self, now: Optional[datetime] = None) -> bool:
        """Return False if today is a market holiday (new trades should be blocked)."""
        return not self.is_holiday(now=now)

    def get_status_summary(self, now: Optional[datetime] = None) -> str:
        """Return a human-readable status string for logging."""
        if self._calendar is None:
            return "[HolidayGuard] DISABLED (exchange_calendars not available)"

        today = (now or datetime.now(tz=timezone.utc)).date()
        if self.is_holiday(now=now):
            return f"[HolidayGuard] BLOCKED — {today} is a market holiday (NYSE calendar)"
        return f"[HolidayGuard] OK — {today} is a normal trading day"
