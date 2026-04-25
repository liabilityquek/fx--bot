"""Execution engine module for live trading."""

from .engine import TradingEngine

from .order_executor import (
    OrderExecutor,
    OrderRequest,
    OrderType,
    ExecutionStatus,
    ExecutionResult,
    SlippageStats
)

from .trade_manager import (
    TradeManager,
    ManagedTrade,
    TradeAction,
    TradeManagementResult
)

__all__ = [
    # Engine
    'TradingEngine',

    # Order Executor
    'OrderExecutor',
    'OrderRequest',
    'OrderType',
    'ExecutionStatus',
    'ExecutionResult',
    'SlippageStats',

    # Trade Manager
    'TradeManager',
    'ManagedTrade',
    'TradeAction',
    'TradeManagementResult',
]
