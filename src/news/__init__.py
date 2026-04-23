"""News and economic calendar management module."""

from .event_monitor import (
    EventMonitor,
    EconomicEvent,
    EventImpact
)
from .suspension_manager import (
    SuspensionManager,
    SuspensionStatus,
    SuspensionReason
)
from .news_watcher import NewsWatcher

__all__ = [
    'EventMonitor',
    'EconomicEvent',
    'EventImpact',
    'SuspensionManager',
    'SuspensionStatus',
    'SuspensionReason',
    'NewsWatcher',
]
