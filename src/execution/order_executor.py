"""Order execution with retry logic and slippage tracking.

This module handles the actual order placement with:
- Market and limit order support
- Retry logic with exponential backoff
- Order confirmation
- Slippage tracking and analysis
"""

import logging
import time
import uuid
import threading
from collections import deque
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.broker.base import BaseBroker, OrderSide
from src.monitoring.logger import get_logger


class OrderType(Enum):
    """Order types."""
    MARKET = "market"
    LIMIT = "limit"


class ExecutionStatus(Enum):
    """Order execution status."""
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


@dataclass
class OrderRequest:
    """Order request specification."""
    pair: str
    side: OrderSide
    units: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy_name: str = ""
    signal_id: str = ""
    expected_price: Optional[float] = None  # For slippage tracking
    max_slippage_pips: float = 5.0
    
    def __post_init__(self):
        if not self.signal_id:
            self.signal_id = uuid.uuid4().hex


@dataclass
class ExecutionResult:
    """Result of order execution."""
    success: bool
    trade_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_units: int = 0
    status: ExecutionStatus = ExecutionStatus.PENDING
    slippage_pips: float = 0.0
    retry_count: int = 0
    error_message: str = ""
    execution_time_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __repr__(self) -> str:
        if self.success:
            return (
                f"ExecutionResult(SUCCESS | Trade: {self.trade_id} | "
                f"Price: {self.fill_price} | Slippage: {self.slippage_pips:.1f} pips)"
            )
        return f"ExecutionResult(FAILED | {self.error_message})"


@dataclass
class SlippageStats:
    """Slippage statistics."""
    total_orders: int = 0
    total_slippage_pips: float = 0.0
    positive_slippage_count: int = 0  # In our favor
    negative_slippage_count: int = 0  # Against us
    max_slippage: float = 0.0
    min_slippage: float = 0.0
    
    @property
    def average_slippage(self) -> float:
        """Get average slippage per order."""
        if self.total_orders == 0:
            return 0.0
        return self.total_slippage_pips / self.total_orders


class OrderExecutor:
    """Execute orders with retry logic and slippage tracking."""
    
    def __init__(
        self,
        broker: BaseBroker,
        logger: Optional[logging.Logger] = None,
        max_retries: int = 3,
        initial_retry_delay: float = 1.0,
        max_retry_delay: float = 30.0,
        backoff_multiplier: float = 2.0
    ):
        """
        Initialize order executor.
        
        Args:
            broker: Broker instance for order placement
            logger: Logger instance
            max_retries: Maximum retry attempts
            initial_retry_delay: Initial delay between retries (seconds)
            max_retry_delay: Maximum retry delay (seconds)
            backoff_multiplier: Exponential backoff multiplier
        """
        self.broker = broker
        self.logger = logger or get_logger('order_executor')
        self.max_retries = max_retries
        self.initial_retry_delay = initial_retry_delay
        self.max_retry_delay = max_retry_delay
        self.backoff_multiplier = backoff_multiplier
        
        # Slippage tracking
        self.slippage_stats = SlippageStats()
        self.execution_history: deque = deque(maxlen=500)

        # Rate limiting
        self._rate_limit_lock = threading.Lock()
        self._order_timestamps: deque = deque()
        self._max_orders_per_minute: int = 10

        # Circuit breaker
        self._circuit_open: bool = False
        self._circuit_open_time: Optional[datetime] = None
        self._consecutive_failures: int = 0
        self._circuit_failure_threshold: int = 5
        self._circuit_cooldown_seconds: float = 60.0
    
    def _check_rate_limit(self) -> bool:
        """
        Check whether the current order rate is within allowed limits.

        Returns:
            True if order may proceed, False if rate limit is exceeded.
        """
        with self._rate_limit_lock:
            now = time.time()
            # Remove timestamps older than 60 seconds
            while self._order_timestamps and now - self._order_timestamps[0] > 60.0:
                self._order_timestamps.popleft()
            if len(self._order_timestamps) >= self._max_orders_per_minute:
                return False
            return True

    def _record_order_attempt(self, success: bool):
        """
        Record an order attempt for rate limiting and circuit breaker tracking.

        Args:
            success: True if the order was filled, False otherwise.
        """
        with self._rate_limit_lock:
            self._order_timestamps.append(time.time())
            if success:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._circuit_failure_threshold:
                    self._circuit_open = True
                    self._circuit_open_time = datetime.now()
                    self.logger.error(
                        f"Circuit breaker opened after {self._consecutive_failures} "
                        f"consecutive failures."
                    )

    def reset_circuit_breaker(self):
        """Manually reset the circuit breaker to allow orders again."""
        with self._rate_limit_lock:
            self._circuit_open = False
            self._circuit_open_time = None
            self._consecutive_failures = 0
        self.logger.info("Circuit breaker reset.")

    def execute_market_order(
        self,
        request: OrderRequest
    ) -> ExecutionResult:
        """
        Execute a market order with retry logic.
        
        Args:
            request: Order request specification
        
        Returns:
            ExecutionResult with execution details
        """
        # Circuit breaker check
        if self._circuit_open:
            elapsed = (datetime.now() - self._circuit_open_time).total_seconds()
            if elapsed < self._circuit_cooldown_seconds:
                msg = (
                    f"Circuit breaker is open. Retry in "
                    f"{self._circuit_cooldown_seconds - elapsed:.0f}s."
                )
                self.logger.warning(msg)
                return ExecutionResult(
                    success=False,
                    status=ExecutionStatus.FAILED,
                    error_message=msg
                )
            else:
                self.reset_circuit_breaker()

        # Rate limit check
        if not self._check_rate_limit():
            msg = "Order rate limit exceeded. Too many orders per minute."
            self.logger.warning(msg)
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.FAILED,
                error_message=msg
            )

        start_time = time.time()
        retry_count = 0
        current_delay = self.initial_retry_delay
        last_error = ""
        seen_signal_ids = set()

        self.logger.info(
            f"Executing market order: {request.side.value.upper()} "
            f"{request.units:,} {request.pair}"
        )

        while retry_count <= self.max_retries:
            try:
                # Idempotency check on retry: skip if signal already submitted
                if retry_count > 0 and request.signal_id in seen_signal_ids:
                    self.logger.warning(
                        f"Duplicate signal_id detected on retry: {request.signal_id}. Skipping."
                    )
                    break
                seen_signal_ids.add(request.signal_id)

                # Get current price before execution for slippage calculation
                if request.expected_price is None:
                    price_data = self.broker.get_current_price(request.pair)
                    if price_data:
                        if request.side == OrderSide.BUY:
                            request.expected_price = price_data['ask']
                        else:
                            request.expected_price = price_data['bid']
                
                # Place the order
                trade_id = self.broker.place_market_order(
                    pair=request.pair,
                    side=request.side,
                    units=request.units,
                    stop_loss=request.stop_loss,
                    take_profit=request.take_profit
                )
                
                if trade_id:
                    # Get fill price for slippage calculation
                    fill_price = self._get_fill_price(request.pair, trade_id)
                    slippage = self._calculate_slippage(
                        request.expected_price,
                        fill_price,
                        request.side,
                        request.pair
                    )
                    
                    execution_time = (time.time() - start_time) * 1000
                    
                    result = ExecutionResult(
                        success=True,
                        trade_id=trade_id,
                        fill_price=fill_price,
                        filled_units=request.units,
                        status=ExecutionStatus.FILLED,
                        slippage_pips=slippage,
                        retry_count=retry_count,
                        execution_time_ms=execution_time
                    )
                    
                    # Update slippage stats
                    self._update_slippage_stats(slippage)
                    
                    # Log success
                    self.logger.info(
                        f"✅ Order filled: {request.pair} {request.side.value.upper()} "
                        f"{request.units:,} @ {fill_price} | "
                        f"Slippage: {slippage:+.1f} pips | "
                        f"Time: {execution_time:.0f}ms"
                    )

                    self._record_order_attempt(True)
                    self.execution_history.append(result)
                    return result

                # Order failed but no exception
                last_error = "Order rejected by broker (no trade ID returned)"
                self.logger.warning(f"Order attempt {retry_count + 1} failed: {last_error}")
                self._record_order_attempt(False)

            except Exception as e:
                last_error = str(e)
                self.logger.error(f"Order attempt {retry_count + 1} error: {e}")
                self._record_order_attempt(False)
            
            # Retry logic
            retry_count += 1
            if retry_count <= self.max_retries:
                self.logger.info(
                    f"Retrying in {current_delay:.1f}s "
                    f"(attempt {retry_count + 1}/{self.max_retries + 1})"
                )
                time.sleep(current_delay)
                current_delay = min(
                    current_delay * self.backoff_multiplier,
                    self.max_retry_delay
                )
        
        # All retries exhausted
        execution_time = (time.time() - start_time) * 1000
        result = ExecutionResult(
            success=False,
            status=ExecutionStatus.FAILED,
            retry_count=retry_count - 1,
            error_message=last_error,
            execution_time_ms=execution_time
        )
        
        self.logger.error(
            f"❌ Order failed after {retry_count} attempts: {request.pair} "
            f"{request.side.value.upper()} {request.units:,} | Error: {last_error}"
        )
        
        self.execution_history.append(result)
        return result
    
    def execute_limit_order(
        self,
        request: OrderRequest
    ) -> ExecutionResult:
        """Place a pending GTC limit order at the specified price."""
        if request.limit_price is None:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.FAILED,
                error_message="Limit price required for limit orders"
            )

        start_time = time.time()
        self.logger.info(
            f"Placing limit order: {request.side.value.upper()} "
            f"{request.units:,} {request.pair} @ {request.limit_price}"
        )

        try:
            order_id = self.broker.place_limit_order(
                pair=request.pair,
                side=request.side,
                units=request.units,
                price=request.limit_price,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.FAILED,
                error_message=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )

        execution_time = (time.time() - start_time) * 1000

        if order_id:
            self._record_order_attempt(True)
            result = ExecutionResult(
                success=True,
                trade_id=order_id,
                status=ExecutionStatus.PENDING,
                execution_time_ms=execution_time,
            )
            self.execution_history.append(result)
            self.logger.info(
                f"Limit order accepted: {request.pair} | Order ID: {order_id} | "
                f"Time: {execution_time:.0f}ms"
            )
            return result

        self._record_order_attempt(False)
        result = ExecutionResult(
            success=False,
            status=ExecutionStatus.FAILED,
            error_message="Broker rejected limit order (no order ID returned)",
            execution_time_ms=execution_time,
        )
        self.execution_history.append(result)
        return result
    
    def execute(self, request: OrderRequest) -> ExecutionResult:
        """
        Execute an order based on type.
        
        Args:
            request: Order request
        
        Returns:
            ExecutionResult
        """
        if request.order_type == OrderType.MARKET:
            return self.execute_market_order(request)
        elif request.order_type == OrderType.LIMIT:
            return self.execute_limit_order(request)
        else:
            return ExecutionResult(
                success=False,
                status=ExecutionStatus.FAILED,
                error_message=f"Unknown order type: {request.order_type}"
            )
    
    def _get_fill_price(
        self,
        pair: str,
        trade_id: str
    ) -> Optional[float]:
        """
        Get the fill price for a trade.
        
        Args:
            pair: Trading pair
            trade_id: Trade ID
        
        Returns:
            Fill price or None
        """
        try:
            trades = self.broker.get_open_trades()
            for trade in trades:
                if trade.trade_id == trade_id:
                    return trade.entry_price
            
            # Trade might not be in open trades if already closed
            # Fall back to current price
            price_data = self.broker.get_current_price(pair)
            if price_data:
                return (price_data['bid'] + price_data['ask']) / 2
            
        except Exception as e:
            self.logger.warning(f"Could not get fill price: {e}")
        
        return None
    
    def _calculate_slippage(
        self,
        expected_price: Optional[float],
        fill_price: Optional[float],
        side: OrderSide,
        pair: str
    ) -> float:
        """
        Calculate slippage in pips.
        
        Positive slippage = in our favor (got better price)
        Negative slippage = against us (got worse price)
        
        Args:
            expected_price: Expected fill price
            fill_price: Actual fill price
            side: Order side
            pair: Trading pair
        
        Returns:
            Slippage in pips
        """
        if expected_price is None or fill_price is None:
            return 0.0
        
        # Determine pip size
        if 'JPY' in pair:
            pip_size = 0.01
        else:
            pip_size = 0.0001
        
        price_diff = fill_price - expected_price
        slippage_pips = price_diff / pip_size
        
        # Adjust sign based on side
        # For BUY: negative diff (lower fill) is positive slippage
        # For SELL: positive diff (higher fill) is positive slippage
        if side == OrderSide.BUY:
            slippage_pips = -slippage_pips
        
        return slippage_pips
    
    def _update_slippage_stats(self, slippage: float):
        """
        Update slippage statistics.
        
        Args:
            slippage: Slippage in pips
        """
        self.slippage_stats.total_orders += 1
        self.slippage_stats.total_slippage_pips += slippage
        
        if slippage > 0:
            self.slippage_stats.positive_slippage_count += 1
        elif slippage < 0:
            self.slippage_stats.negative_slippage_count += 1
        
        self.slippage_stats.max_slippage = max(
            self.slippage_stats.max_slippage, slippage
        )
        self.slippage_stats.min_slippage = min(
            self.slippage_stats.min_slippage, slippage
        )
    
    def get_slippage_report(self) -> Dict:
        """
        Get slippage statistics report.
        
        Returns:
            Dictionary with slippage stats
        """
        stats = self.slippage_stats
        return {
            'total_orders': stats.total_orders,
            'average_slippage_pips': stats.average_slippage,
            'total_slippage_pips': stats.total_slippage_pips,
            'positive_slippage_count': stats.positive_slippage_count,
            'negative_slippage_count': stats.negative_slippage_count,
            'max_slippage_pips': stats.max_slippage,
            'min_slippage_pips': stats.min_slippage
        }
    
    def get_execution_history(
        self,
        limit: int = 50,
        success_only: bool = False
    ) -> List[ExecutionResult]:
        """
        Get recent execution history.
        
        Args:
            limit: Maximum number of results
            success_only: Only return successful executions
        
        Returns:
            List of ExecutionResult
        """
        history = self.execution_history
        
        if success_only:
            history = [r for r in history if r.success]
        
        return history[-limit:]
    
    def reset_stats(self):
        """Reset slippage statistics and execution history."""
        self.slippage_stats = SlippageStats()
        self.execution_history.clear()
        self.logger.info("Execution statistics reset")
