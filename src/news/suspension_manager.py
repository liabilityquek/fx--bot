"""Trading suspension management during news events."""

import logging
from typing import Dict, Optional, Set
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .event_monitor import EventMonitor, EconomicEvent, EventImpact
from config.settings import settings


class SuspensionReason(Enum):
    """Reasons for trading suspension."""
    HIGH_IMPACT_NEWS = "high_impact_news"
    MAJOR_EVENT = "major_event"
    MANUAL = "manual"
    VOLATILITY = "volatility"


@dataclass
class SuspensionStatus:
    """Current suspension status."""
    is_suspended: bool
    reason: Optional[SuspensionReason]
    suspended_pairs: Set[str]
    triggering_event: Optional[EconomicEvent]
    resume_time: Optional[datetime]
    message: str


class SuspensionManager:
    """Manage trading suspensions based on economic events."""
    
    def __init__(self, logger: Optional[logging.Logger] = None, on_suspension_lifted=None):
        """
        Initialize suspension manager.
        
        Args:
            logger: Logger instance
        """
        self.logger = logger or logging.getLogger('suspension_manager')
        self.event_monitor = EventMonitor(logger=logger)
        
        # Settings
        self.suspend_before_minutes = settings.NEWS_SUSPEND_BEFORE_MINUTES
        self.resume_after_minutes = settings.NEWS_RESUME_AFTER_MINUTES
        
        self._on_suspension_lifted = on_suspension_lifted  # Callable[[str], None] or None

        # State
        self.manually_suspended = False
        self.suspended_pairs: Set[str] = set()
        self.suspension_events: Dict[str, EconomicEvent] = {}
    
    def check_suspension_status(
        self,
        pair: Optional[str] = None
    ) -> SuspensionStatus:
        """
        Check if trading should be suspended.
        
        Args:
            pair: Specific pair to check (None = check general)
        
        Returns:
            SuspensionStatus with details
        """
        # Check manual suspension
        if self.manually_suspended:
            return SuspensionStatus(
                is_suspended=True,
                reason=SuspensionReason.MANUAL,
                suspended_pairs=set(settings.TRADING_PAIRS),
                triggering_event=None,
                resume_time=None,
                message="Trading manually suspended"
            )
        
        # Check event-based suspension
        should_suspend, event = self.event_monitor.should_suspend_trading(
            pair=pair,
            minutes_before=self.suspend_before_minutes
        )
        
        if should_suspend and event:
            # Calculate resume time
            resume_time = event.time + timedelta(minutes=self.resume_after_minutes)
            
            # Track suspended pairs
            if pair:
                self.suspended_pairs.add(pair)
                self.suspension_events[pair] = event
                message = f"Suspended {pair}: {event.event_name}"
            else:
                self.suspended_pairs = set(settings.TRADING_PAIRS)
                message = f"All trading suspended: {event.event_name}"
            
            return SuspensionStatus(
                is_suspended=True,
                reason=SuspensionReason.HIGH_IMPACT_NEWS,
                suspended_pairs=self.suspended_pairs.copy(),
                triggering_event=event,
                resume_time=resume_time,
                message=message
            )
        
        # Check if previously suspended pairs should resume
        self._check_resume()
        
        # Not suspended
        return SuspensionStatus(
            is_suspended=False,
            reason=None,
            suspended_pairs=set(),
            triggering_event=None,
            resume_time=None,
            message="Trading allowed"
        )
    
    def suspend_trading(
        self,
        reason: SuspensionReason = SuspensionReason.MANUAL,
        pairs: Optional[Set[str]] = None
    ):
        """
        Manually suspend trading.
        
        Args:
            reason: Suspension reason
            pairs: Specific pairs to suspend (None = all)
        """
        if reason == SuspensionReason.MANUAL:
            self.manually_suspended = True
            self.logger.warning("Trading manually suspended")
        
        if pairs:
            self.suspended_pairs.update(pairs)
            self.logger.warning(f"Suspended pairs: {', '.join(pairs)}")
        else:
            self.suspended_pairs = set(settings.TRADING_PAIRS)
            self.logger.warning("All trading suspended")
    
    def resume_trading(self, pairs: Optional[Set[str]] = None):
        """
        Resume trading.
        
        Args:
            pairs: Specific pairs to resume (None = all)
        """
        if pairs:
            self.suspended_pairs -= pairs
            self.logger.info(f"Resumed trading for: {', '.join(pairs)}")
        else:
            self.suspended_pairs.clear()
            self.manually_suspended = False
            self.suspension_events.clear()
            self.logger.info("All trading resumed")
    
    def is_pair_suspended(self, pair: str) -> bool:
        """
        Check if a specific pair is suspended.
        
        Args:
            pair: Trading pair
        
        Returns:
            True if suspended
        """
        if self.manually_suspended:
            return True
        
        if pair in self.suspended_pairs:
            return True
        
        # Check for imminent events
        status = self.check_suspension_status(pair=pair)
        return status.is_suspended
    
    def get_safe_pairs(self) -> Set[str]:
        """
        Get pairs that are safe to trade (not suspended).
        
        Returns:
            Set of safe trading pairs
        """
        all_pairs = set(settings.TRADING_PAIRS)
        safe = all_pairs - self.suspended_pairs
        
        # Double-check each pair for imminent events
        truly_safe = set()
        for pair in safe:
            if not self.is_pair_suspended(pair):
                truly_safe.add(pair)
        
        return truly_safe
    
    def should_close_positions(
        self,
        minutes_before_event: int = 5
    ) -> tuple[bool, Optional[EconomicEvent]]:
        """
        Check if open positions should be closed due to imminent event.
        
        Args:
            minutes_before_event: Close positions this many minutes before
        
        Returns:
            Tuple of (should_close, triggering_event)
        """
        imminent = self.event_monitor.get_imminent_events(
            minutes=minutes_before_event,
            min_impact=EventImpact.VERY_HIGH
        )
        
        if imminent:
            event = imminent[0]
            self.logger.critical(
                f"CLOSE POSITIONS: {event.event_name} in "
                f"{event.minutes_until:.0f} minutes!"
            )
            return True, event
        
        return False, None
    
    def _check_resume(self):
        """Check if suspended pairs should resume trading."""
        now = datetime.now()
        pairs_to_resume = set()
        
        for pair, event in list(self.suspension_events.items()):
            resume_time = event.time + timedelta(minutes=self.resume_after_minutes)
            
            if now >= resume_time:
                pairs_to_resume.add(pair)
                del self.suspension_events[pair]
        
        if pairs_to_resume:
            self.suspended_pairs -= pairs_to_resume
            # Notify callback so prediction scheduler can force-refresh affected pairs
            if self._on_suspension_lifted and pairs_to_resume:
                for pair in pairs_to_resume:
                    try:
                        self._on_suspension_lifted(pair)
                    except Exception as e:
                        self.logger.warning(f"on_suspension_lifted callback error: {e}")
            self.logger.info(
                f"Auto-resumed trading for: {', '.join(pairs_to_resume)}"
            )
    
    def get_status_summary(self, status: SuspensionStatus) -> str:
        """Get human-readable suspension status."""
        lines = ["\nTrading Suspension Status:"]
        
        if status.is_suspended:
            lines.append(f"  Status: SUSPENDED")
            lines.append(f"  Reason: {status.reason.value if status.reason else 'N/A'}")
            
            if status.suspended_pairs:
                lines.append(f"  Suspended Pairs: {', '.join(status.suspended_pairs)}")
            
            if status.triggering_event:
                event = status.triggering_event
                lines.append(
                    f"  Event: {event.event_name} ({event.currency}) "
                    f"in {event.minutes_until:.0f} min"
                )
            
            if status.resume_time:
                lines.append(f"  Resume At: {status.resume_time.strftime('%H:%M')}")
        else:
            lines.append(f"  Status: ACTIVE")
            safe_pairs = self.get_safe_pairs()
            if safe_pairs:
                lines.append(f"  Safe Pairs: {', '.join(safe_pairs)}")
        
        return "\n".join(lines)
