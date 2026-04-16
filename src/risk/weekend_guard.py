"""Weekend guard — blocks or warns about trading near market close/open.

The FX spot market closes at approximately 22:00 UTC on Friday and
reopens at approximately 22:00 UTC on Sunday.  Holding positions over
the weekend introduces gap risk (spreads widen significantly on open)
and makes it impossible to react to news.

This module provides:
  - WeekendGuard: a lightweight risk check that can be injected into
    any pre-trade validation pipeline.
  - Helpers to detect the exact market window and compute time-to-close.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional


class MarketSession(Enum):
    """Broad FX market session states."""
    OPEN = "open"
    PRE_WEEKEND_CLOSE = "pre_weekend_close"   # Friday warning window
    WEEKEND = "weekend"
    POST_WEEKEND_OPEN = "post_weekend_open"   # Sunday/Monday re-open buffer


@dataclass
class WeekendCheckResult:
    """Result of a weekend guard evaluation."""
    allowed: bool
    session: MarketSession
    reason: str
    time_to_weekend_close: Optional[timedelta]  # None when already closed/weekend
    time_to_market_open: Optional[timedelta]    # None when market is not closed
    warnings: List[str]
    checked_at: datetime

    def to_dict(self) -> Dict:
        """Serialize to a plain dictionary."""
        return {
            "allowed": self.allowed,
            "session": self.session.value,
            "reason": self.reason,
            "time_to_weekend_close_minutes": (
                int(self.time_to_weekend_close.total_seconds() / 60)
                if self.time_to_weekend_close is not None
                else None
            ),
            "time_to_market_open_minutes": (
                int(self.time_to_market_open.total_seconds() / 60)
                if self.time_to_market_open is not None
                else None
            ),
            "warnings": self.warnings,
            "checked_at": self.checked_at.isoformat(),
        }


class WeekendGuard:
    """Prevent new trades from opening in the Friday/weekend danger window.

    The guard operates purely on UTC time and is stateless — it can be
    instantiated once and called repeatedly without side-effects.

    Parameters
    ----------
    warning_hours_before_close : float
        How many hours before the Friday 22:00 UTC close to start
        issuing warnings (default 4).
    block_hours_before_close : float
        How many hours before close to hard-block new trades (default 1).
    block_hours_after_open : float
        How many hours after the Sunday 22:00 UTC open to soft-block
        trades (gap-risk buffer, default 1).
    allow_close_only_during_weekend : bool
        If True, the bot may still close *existing* positions during
        the weekend window; only new entries are blocked (default True).
    logger : logging.Logger, optional
        Logger instance.
    """

    # Weekday constants (Python datetime: Mon=0, Fri=4, Sat=5, Sun=6)
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6

    # FX market close/open times (UTC hour)
    MARKET_CLOSE_HOUR = 22   # Friday 22:00 UTC — NY close
    MARKET_OPEN_HOUR = 22    # Sunday 22:00 UTC — Wellington/Sydney open

    def __init__(
        self,
        warning_hours_before_close: float = 4.0,
        block_hours_before_close: float = 1.0,
        block_hours_after_open: float = 1.0,
        allow_close_only_during_weekend: bool = True,
        logger: Optional[logging.Logger] = None,
    ):
        self.warning_hours_before_close = warning_hours_before_close
        self.block_hours_before_close = block_hours_before_close
        self.block_hours_after_open = block_hours_after_open
        self.allow_close_only_during_weekend = allow_close_only_during_weekend
        self.logger = logger or logging.getLogger("weekend_guard")

    # ------------------------------------------------------------------
    # Internal time helpers
    # ------------------------------------------------------------------

    def _utc_now(self) -> datetime:
        """Return current UTC time (always timezone-aware)."""
        return datetime.now(tz=timezone.utc)

    def _next_friday_close(self, now: datetime) -> datetime:
        """Return the datetime of the upcoming (or current) Friday 22:00 UTC."""
        # Days until Friday
        days_ahead = (self.FRIDAY - now.weekday()) % 7
        # If today is Friday and we are past 22:00, next occurrence is 7 days away
        if days_ahead == 0 and now.hour >= self.MARKET_CLOSE_HOUR:
            days_ahead = 7
        target = now.replace(
            hour=self.MARKET_CLOSE_HOUR, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_ahead)
        return target

    def _next_sunday_open(self, now: datetime) -> datetime:
        """Return the datetime of the upcoming Sunday 22:00 UTC market open."""
        days_ahead = (self.SUNDAY - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= self.MARKET_OPEN_HOUR:
            days_ahead = 7
        target = now.replace(
            hour=self.MARKET_OPEN_HOUR, minute=0, second=0, microsecond=0
        ) + timedelta(days=days_ahead)
        return target

    def _get_session(self, now: datetime) -> MarketSession:
        """Classify the current moment into a MarketSession."""
        weekday = now.weekday()
        hour = now.hour

        # Full weekend: Saturday all day, or Sunday before 22:00
        if weekday == self.SATURDAY:
            return MarketSession.WEEKEND
        if weekday == self.SUNDAY and hour < self.MARKET_OPEN_HOUR:
            return MarketSession.WEEKEND

        # Sunday 22:00+ — market re-opening buffer
        if weekday == self.SUNDAY and hour >= self.MARKET_OPEN_HOUR:
            return MarketSession.POST_WEEKEND_OPEN

        # Friday approaching close
        if weekday == self.FRIDAY:
            minutes_to_close = (
                (self.MARKET_CLOSE_HOUR - hour) * 60 - now.minute
            )
            warning_minutes = self.warning_hours_before_close * 60
            if 0 <= minutes_to_close <= warning_minutes:
                return MarketSession.PRE_WEEKEND_CLOSE

        return MarketSession.OPEN

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check(
        self,
        now: Optional[datetime] = None,
        is_closing_trade: bool = False,
    ) -> WeekendCheckResult:
        """
        Evaluate whether a trade is permitted at the given time.

        Args:
            now: UTC datetime to evaluate (defaults to current UTC time)
            is_closing_trade: True if the trade closes an existing position
                              (closing trades may be permitted during weekend)

        Returns:
            WeekendCheckResult with decision and diagnostics
        """
        now = (now or self._utc_now()).astimezone(timezone.utc)
        session = self._get_session(now)
        warnings: List[str] = []
        allowed = True
        reason = "Market is open. No weekend restrictions."
        time_to_close: Optional[timedelta] = None
        time_to_open: Optional[timedelta] = None

        if session == MarketSession.OPEN:
            # Still check for approaching Friday warning window
            next_close = self._next_friday_close(now)
            time_to_close = next_close - now

            warning_threshold = timedelta(hours=self.warning_hours_before_close)
            if time_to_close <= warning_threshold:
                hours_left = time_to_close.total_seconds() / 3600
                warnings.append(
                    f"Market closes in {hours_left:.1f}h (Friday 22:00 UTC). "
                    f"Consider reducing position size or avoiding new trades."
                )

        elif session == MarketSession.PRE_WEEKEND_CLOSE:
            next_close = self._next_friday_close(now)
            time_to_close = next_close - now
            hours_left = time_to_close.total_seconds() / 3600
            block_threshold = timedelta(hours=self.block_hours_before_close)

            if time_to_close <= block_threshold:
                if is_closing_trade and self.allow_close_only_during_weekend:
                    allowed = True
                    reason = (
                        f"Market closes in {hours_left:.1f}h. "
                        f"Closing trade permitted; new entries are blocked."
                    )
                    warnings.append("Pre-weekend close: new entries blocked, closing allowed.")
                else:
                    allowed = False
                    reason = (
                        f"Blocked: FX market closes in {hours_left:.1f}h. "
                        f"New trades within {self.block_hours_before_close}h of close are prohibited."
                    )
            else:
                warnings.append(
                    f"Pre-weekend warning: market closes in {hours_left:.1f}h. "
                    f"Trade with caution; gap risk increases."
                )
                reason = f"Warning: market closes in {hours_left:.1f}h."

        elif session == MarketSession.WEEKEND:
            time_to_open = self._next_sunday_open(now) - now
            hours_to_open = time_to_open.total_seconds() / 3600

            if is_closing_trade and self.allow_close_only_during_weekend:
                allowed = True
                reason = (
                    f"Weekend: market closed. Closing existing position is permitted. "
                    f"Market reopens in {hours_to_open:.1f}h."
                )
                warnings.append("Weekend: market closed. Only position closures are allowed.")
            else:
                allowed = False
                reason = (
                    f"Blocked: FX market is closed for the weekend. "
                    f"Reopens in approximately {hours_to_open:.1f}h (Sunday 22:00 UTC)."
                )

        elif session == MarketSession.POST_WEEKEND_OPEN:
            # Re-open buffer — market is technically open but spreads are wide
            buffer_threshold = timedelta(hours=self.block_hours_after_open)
            time_since_open = now - self._next_sunday_open(
                now - timedelta(days=7)  # last Sunday open
            )
            # Simpler: check how far we are past Sunday 22:00
            # Recalculate: find the most recent Sunday open
            days_since_sunday = (now.weekday() - self.SUNDAY) % 7
            last_sunday_open = now.replace(
                hour=self.MARKET_OPEN_HOUR, minute=0, second=0, microsecond=0
            ) - timedelta(days=days_since_sunday)
            time_since_open = now - last_sunday_open

            if time_since_open < buffer_threshold:
                hours_elapsed = time_since_open.total_seconds() / 3600
                hours_remaining = self.block_hours_after_open - hours_elapsed
                if is_closing_trade and self.allow_close_only_during_weekend:
                    allowed = True
                    reason = (
                        f"Post-open buffer ({hours_elapsed:.1f}h since open). "
                        f"Closing trade permitted; new entries blocked for {hours_remaining:.1f}h more."
                    )
                    warnings.append(
                        f"Gap risk window: spreads may be elevated for {hours_remaining:.1f}h more."
                    )
                else:
                    allowed = False
                    reason = (
                        f"Blocked: post-weekend gap risk window. Market opened {hours_elapsed:.1f}h ago. "
                        f"New trades blocked for {hours_remaining:.1f}h more."
                    )
            else:
                reason = "Market open. Weekend gap-risk buffer has cleared."

        result = WeekendCheckResult(
            allowed=allowed,
            session=session,
            reason=reason,
            time_to_weekend_close=time_to_close,
            time_to_market_open=time_to_open,
            warnings=warnings,
            checked_at=now,
        )

        # Log outcome
        if not allowed:
            self.logger.warning(f"WeekendGuard BLOCKED: {reason}")
        elif warnings:
            for w in warnings:
                self.logger.warning(f"WeekendGuard WARNING: {w}")
        else:
            self.logger.debug(f"WeekendGuard OK: {reason}")

        return result

    def is_safe_to_trade(
        self,
        now: Optional[datetime] = None,
        is_closing_trade: bool = False,
    ) -> bool:
        """
        Convenience method returning a simple boolean.

        Args:
            now: UTC datetime (defaults to current time)
            is_closing_trade: True for position-closing orders

        Returns:
            True if trading is permitted, False otherwise
        """
        return self.check(now=now, is_closing_trade=is_closing_trade).allowed

    def get_status_summary(self, now: Optional[datetime] = None) -> str:
        """
        Return a human-readable status string for logging or dashboards.

        Args:
            now: UTC datetime (defaults to current time)

        Returns:
            Formatted status string
        """
        result = self.check(now=now)
        status_icon = "OK" if result.allowed else "BLOCKED"
        lines = [
            f"[WeekendGuard] {status_icon}",
            f"  Session    : {result.session.value}",
            f"  Allowed    : {result.allowed}",
            f"  Reason     : {result.reason}",
            f"  Checked at : {result.checked_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]
        if result.time_to_weekend_close is not None:
            mins = int(result.time_to_weekend_close.total_seconds() / 60)
            lines.append(f"  To close   : {mins} minutes")
        if result.time_to_market_open is not None:
            mins = int(result.time_to_market_open.total_seconds() / 60)
            lines.append(f"  To open    : {mins} minutes")
        if result.warnings:
            lines.append("  Warnings   :")
            for w in result.warnings:
                lines.append(f"    - {w}")
        return "\n".join(lines)
