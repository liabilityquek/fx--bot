"""News and economic calendar management module.

This module provides:
- Continuous economic event monitoring
- Automatic trading suspension logic
- News impact analysis
- Event-based position management
"""

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
from .news_filter import (
    NewsFilter,
    NewsRelevance
)

__all__ = [
    'EventMonitor',
    'EconomicEvent',
    'EventImpact',
    'SuspensionManager',
    'SuspensionStatus',
    'SuspensionReason',
    'NewsFilter',
    'NewsRelevance',
]
