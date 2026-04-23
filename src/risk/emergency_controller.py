"""Emergency Risk Controller for critical safety operations.

This module provides emergency risk management features including:
- Emergency shutdown (close all positions)
- Automatic position closure when risk limits breached
- Stop-loss override and modification
- Account protection mechanisms (max drawdown, daily loss limits)
- Circuit breaker functionality
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, IntEnum

from config.settings import settings


class EmergencyLevel(IntEnum):
    """Emergency severity levels."""
    NONE = 0
    WARNING = 1     # Approaching limits
    CRITICAL = 2    # Limits breached
    PANIC = 3       # Immediate shutdown required


class ShutdownReason(Enum):
    """Reasons for emergency shutdown."""
    MANUAL = "manual"  # User-triggered
    EXPOSURE_BREACH = "exposure_breach"  # Over 10% exposure
    DRAWDOWN_LIMIT = "drawdown_limit"  # Max drawdown exceeded
    DAILY_LOSS_LIMIT = "daily_loss_limit"  # Max daily loss exceeded
    MARGIN_CALL = "margin_call"  # Insufficient margin
    NEWS_EVENT = "news_event"  # Major news event
    SYSTEM_ERROR = "system_error"  # Technical failure


@dataclass
class EmergencyStatus:
    """Current emergency status."""
    level: EmergencyLevel
    active_alerts: List[str]
    positions_at_risk: int
    recommended_action: str
    requires_shutdown: bool
    shutdown_reason: Optional[ShutdownReason]


@dataclass
class ShutdownReport:
    """Report from emergency shutdown."""
    success: bool
    positions_closed: int
    orders_cancelled: int
    reason: ShutdownReason
    timestamp: datetime
    total_loss_usd: float
    errors: List[str]


class EmergencyRiskController:
    """Emergency risk management and position protection."""
    
    def __init__(
        self,
        max_drawdown_percent: float = 0.20,  # 20% max drawdown
        max_daily_loss_percent: float = 0.05,  # 5% max daily loss
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize emergency risk controller.
        
        Args:
            max_drawdown_percent: Maximum drawdown before emergency stop (0.20 = 20%)
            max_daily_loss_percent: Maximum daily loss before emergency stop (0.05 = 5%)
            logger: Logger instance
        """
        self.logger = logger or logging.getLogger('emergency_controller')
        
        # Risk limits from settings
        self.max_total_exposure = settings.MAX_TOTAL_EXPOSURE
        self.max_drawdown = max_drawdown_percent
        self.max_daily_loss = max_daily_loss_percent
        
        # State tracking
        self.emergency_active = False
        self.daily_pnl_start_balance = None
        self.daily_pnl_reset_date = None
        self.circuit_breaker_triggered = False
        self.shutdown_history: List[ShutdownReport] = []
    
    def check_emergency_conditions(
        self,
        account_balance: float,
        initial_balance: float,
        open_positions: List[Dict],
        current_exposure_percent: float,
        unrealized_pnl: float
    ) -> EmergencyStatus:
        """
        Check if emergency conditions exist.
        
        Args:
            account_balance: Current account balance
            initial_balance: Starting account balance (for drawdown calc)
            open_positions: List of open positions
            current_exposure_percent: Current total exposure percentage
            unrealized_pnl: Current unrealized profit/loss
        
        Returns:
            EmergencyStatus with recommended actions
        """
        alerts = []
        level = EmergencyLevel.NONE
        requires_shutdown = False
        shutdown_reason = None
        positions_at_risk = 0
        
        # Check 1: Exposure breach
        max_exposure_percent = self.max_total_exposure * 100
        
        if current_exposure_percent > max_exposure_percent * 1.5:
            # Severe breach (>150% of limit)
            level = EmergencyLevel.PANIC
            requires_shutdown = True
            shutdown_reason = ShutdownReason.EXPOSURE_BREACH
            alerts.append(
                f"🚨 CRITICAL: Exposure at {current_exposure_percent:.1f}% "
                f"(limit {max_exposure_percent:.0f}%) - SHUTDOWN REQUIRED"
            )
        elif current_exposure_percent > max_exposure_percent:
            # Breach but not severe
            level = EmergencyLevel.CRITICAL
            alerts.append(
                f"⚠️ EXPOSURE BREACH: {current_exposure_percent:.1f}% > "
                f"{max_exposure_percent:.0f}% - Close positions"
            )
            positions_at_risk = len(open_positions)
        elif current_exposure_percent > max_exposure_percent * 0.9:
            # Approaching limit
            level = EmergencyLevel.WARNING
            alerts.append(
                f"⚠️ Exposure high: {current_exposure_percent:.1f}% "
                f"(limit {max_exposure_percent:.0f}%)"
            )
        
        # Check 2: Drawdown limit
        if initial_balance > 0:
            current_drawdown = (initial_balance - account_balance) / initial_balance
            
            if current_drawdown >= self.max_drawdown:
                level = EmergencyLevel.PANIC
                requires_shutdown = True
                shutdown_reason = ShutdownReason.DRAWDOWN_LIMIT
                alerts.append(
                    f"🚨 MAX DRAWDOWN EXCEEDED: {current_drawdown*100:.1f}% "
                    f"(limit {self.max_drawdown*100:.0f}%) - EMERGENCY STOP"
                )
            elif current_drawdown >= self.max_drawdown * 0.8:
                if level < EmergencyLevel.CRITICAL:
                    level = EmergencyLevel.CRITICAL
                alerts.append(
                    f"⚠️ High drawdown: {current_drawdown*100:.1f}% "
                    f"(limit {self.max_drawdown*100:.0f}%)"
                )
        
        # Check 3: Daily loss limit
        daily_loss = self._calculate_daily_loss(account_balance)
        
        if daily_loss is not None and daily_loss >= self.max_daily_loss:
            level = EmergencyLevel.PANIC
            requires_shutdown = True
            shutdown_reason = ShutdownReason.DAILY_LOSS_LIMIT
            alerts.append(
                f"🚨 DAILY LOSS LIMIT: {daily_loss*100:.1f}% "
                f"(limit {self.max_daily_loss*100:.0f}%) - STOP TRADING"
            )
        
        # Check 4: Margin issues
        if account_balance <= 0:
            level = EmergencyLevel.PANIC
            requires_shutdown = True
            shutdown_reason = ShutdownReason.MARGIN_CALL
            alerts.append("🚨 MARGIN CALL: Account balance <= 0")
        
        # Check 5: Large unrealized losses
        if unrealized_pnl < -(account_balance * 0.15):  # 15% unrealized loss
            if level < EmergencyLevel.CRITICAL:
                level = EmergencyLevel.CRITICAL
            alerts.append(
                f"⚠️ Large unrealized loss: ${unrealized_pnl:,.2f} "
                f"({(unrealized_pnl / account_balance * 100):.1f}%)"
            )
            positions_at_risk = len(open_positions)
        
        # Check 6: Too many open positions (potential over-leverage)
        if len(open_positions) > 10:
            if level < EmergencyLevel.WARNING:
                level = EmergencyLevel.WARNING
            alerts.append(f"⚠️ High position count: {len(open_positions)}")
        
        # Determine recommended action
        if requires_shutdown:
            recommended_action = "EMERGENCY SHUTDOWN - Close all positions immediately"
        elif level == EmergencyLevel.CRITICAL:
            recommended_action = "Close positions to reduce exposure/risk"
        elif level == EmergencyLevel.WARNING:
            recommended_action = "Monitor closely, tighten stop losses"
        else:
            recommended_action = "Continue normal operations"
        
        status = EmergencyStatus(
            level=level,
            active_alerts=alerts,
            positions_at_risk=positions_at_risk,
            recommended_action=recommended_action,
            requires_shutdown=requires_shutdown,
            shutdown_reason=shutdown_reason
        )
        
        # Log alerts
        for alert in alerts:
            if level == EmergencyLevel.PANIC:
                self.logger.critical(alert)
            elif level == EmergencyLevel.CRITICAL:
                self.logger.error(alert)
            elif level == EmergencyLevel.WARNING:
                self.logger.warning(alert)
        
        return status
    
    def execute_emergency_shutdown(
        self,
        broker_client,
        reason: ShutdownReason,
        force: bool = False
    ) -> ShutdownReport:
        """
        Execute emergency shutdown - close all positions and cancel orders.
        
        Args:
            broker_client: Broker client with close_all/cancel methods
            reason: Reason for shutdown
            force: Force shutdown even if circuit breaker active
        
        Returns:
            ShutdownReport with results
        """
        if self.circuit_breaker_triggered and not force:
            self.logger.error(
                "Circuit breaker active - shutdown already in progress. "
                "Use force=True to override."
            )
            return ShutdownReport(
                success=False,
                positions_closed=0,
                orders_cancelled=0,
                reason=reason,
                timestamp=datetime.now(),
                total_loss_usd=0.0,
                errors=["Circuit breaker active - shutdown already executed"]
            )
        
        self.logger.critical(
            f"🚨 EMERGENCY SHUTDOWN INITIATED - Reason: {reason.value}"
        )
        
        self.emergency_active = True
        self.circuit_breaker_triggered = True
        
        errors = []
        positions_closed = 0
        orders_cancelled = 0
        total_loss = 0.0
        
        try:
            # Step 1: Get all open positions
            open_positions = broker_client.get_positions()
            self.logger.info(f"Found {len(open_positions)} open positions to close")
            
            # Step 2: Close all positions
            for position in open_positions:
                try:
                    instrument = position.pair

                    # Get unrealized P/L
                    unrealized_pl = position.unrealized_pnl
                    total_loss += unrealized_pl

                    # Close position
                    result = broker_client.close_position(instrument)
                    positions_closed += 1
                    
                    self.logger.warning(
                        f"Closed position: {instrument} "
                        f"(P/L: ${unrealized_pl:,.2f})"
                    )
                    
                except Exception as e:
                    error_msg = f"Failed to close {instrument}: {e}"
                    errors.append(error_msg)
                    self.logger.error(error_msg)
            
            # Step 3: Cancel all pending orders (if broker supports)
            try:
                if hasattr(broker_client, 'cancel_all_orders'):
                    cancelled = broker_client.cancel_all_orders()
                    orders_cancelled = len(cancelled) if isinstance(cancelled, list) else 0
                    self.logger.info(f"Cancelled {orders_cancelled} pending orders")
            except Exception as e:
                error_msg = f"Failed to cancel orders: {e}"
                errors.append(error_msg)
                self.logger.error(error_msg)
            
            success = len(errors) == 0 or positions_closed > 0
            
            report = ShutdownReport(
                success=success,
                positions_closed=positions_closed,
                orders_cancelled=orders_cancelled,
                reason=reason,
                timestamp=datetime.now(),
                total_loss_usd=total_loss,
                errors=errors
            )
            
            self.shutdown_history.append(report)
            
            if success:
                self.logger.critical(
                    f"✅ Emergency shutdown complete: "
                    f"{positions_closed} positions closed, "
                    f"Total realized loss: ${total_loss:,.2f}"
                )
            else:
                self.logger.critical(
                    f"⚠️ Emergency shutdown completed with errors: "
                    f"{len(errors)} errors"
                )
            
            return report
            
        except Exception as e:
            error_msg = f"Emergency shutdown failed: {e}"
            self.logger.critical(error_msg)
            
            return ShutdownReport(
                success=False,
                positions_closed=positions_closed,
                orders_cancelled=orders_cancelled,
                reason=reason,
                timestamp=datetime.now(),
                total_loss_usd=total_loss,
                errors=[error_msg] + errors
            )
    
    def tighten_stop_losses(
        self,
        broker_client,
        open_positions: List[Dict],
        new_stop_pips: int,
        only_losing: bool = True
    ) -> Tuple[int, List[str]]:
        """
        Tighten stop losses on existing positions.
        
        Args:
            broker_client: Broker client
            open_positions: List of open positions
            new_stop_pips: New stop loss distance in pips
            only_losing: Only modify losing positions
        
        Returns:
            Tuple of (positions_modified, errors)
        """
        self.logger.warning(
            f"Tightening stop losses to {new_stop_pips} pips "
            f"({'losing positions only' if only_losing else 'all positions'})"
        )
        
        modified = 0
        errors = []
        
        for position in open_positions:
            try:
                instrument = position.pair
                unrealized_pl = position.unrealized_pnl

                # Skip winning positions if only_losing=True
                if only_losing and unrealized_pl >= 0:
                    continue

                # Get current price
                current_price = broker_client.get_current_price(instrument)

                # Look up pip value for this specific pair
                from config.pairs import PAIR_INFO
                pair_info = PAIR_INFO.get(instrument, {})
                pip_value = pair_info.get('pip_value', 0.0001)

                if position.is_long:
                    # Long position - stop below current price
                    new_stop = current_price['bid'] - (new_stop_pips * pip_value)
                elif position.is_short:
                    # Short position - stop above current price
                    new_stop = current_price['ask'] + (new_stop_pips * pip_value)
                else:
                    continue

                # Modify each trade within the position
                for trade in position.trades:
                    result = broker_client.modify_trade(
                        trade_id=trade.trade_id,
                        pair=instrument,
                        stop_loss=new_stop
                    )

                modified += 1
                self.logger.info(
                    f"Tightened stop loss for {instrument}: "
                    f"New SL @ {new_stop:.5f}"
                )
                
            except Exception as e:
                error_msg = f"Failed to modify {instrument}: {e}"
                errors.append(error_msg)
                self.logger.error(error_msg)
        
        self.logger.warning(
            f"Stop loss tightening complete: {modified} positions modified"
        )
        
        return modified, errors
    
    def close_worst_positions(
        self,
        broker_client,
        open_positions: List[Dict],
        count: int = 1
    ) -> Tuple[int, float]:
        """
        Close the worst-performing positions.
        
        Args:
            broker_client: Broker client
            open_positions: List of open positions
            count: Number of positions to close
        
        Returns:
            Tuple of (positions_closed, total_loss)
        """
        if not open_positions:
            return 0, 0.0
        
        # Sort by unrealized P/L (worst first)
        sorted_positions = sorted(
            open_positions,
            key=lambda p: p.unrealized_pnl
        )

        positions_to_close = sorted_positions[:count]

        self.logger.warning(
            f"Closing {len(positions_to_close)} worst positions"
        )

        closed = 0
        total_loss = 0.0

        for position in positions_to_close:
            try:
                instrument = position.pair
                unrealized_pl = position.unrealized_pnl
                
                broker_client.close_position(instrument)
                
                closed += 1
                total_loss += unrealized_pl
                
                self.logger.warning(
                    f"Closed worst position: {instrument} "
                    f"(Loss: ${unrealized_pl:,.2f})"
                )
                
            except Exception as e:
                self.logger.error(f"Failed to close {instrument}: {e}")
        
        return closed, total_loss
    
    def reset_circuit_breaker(self):
        """Reset circuit breaker (use with caution!)."""
        self.logger.warning("Circuit breaker RESET - trading can resume")
        self.circuit_breaker_triggered = False
        self.emergency_active = False
    
    def _calculate_daily_loss(self, current_balance: float) -> Optional[float]:
        """Calculate daily loss percentage."""
        today = datetime.now().date()
        
        # Reset daily tracking at start of new day
        if self.daily_pnl_reset_date != today:
            self.daily_pnl_start_balance = current_balance
            self.daily_pnl_reset_date = today
            return None  # No data yet
        
        if self.daily_pnl_start_balance is None:
            self.daily_pnl_start_balance = current_balance
            return None
        
        loss = (self.daily_pnl_start_balance - current_balance) / self.daily_pnl_start_balance
        
        return max(0, loss)  # Only track losses, not gains
    
    def get_status_summary(self, status: EmergencyStatus) -> str:
        """Get human-readable emergency status summary."""
        lines = [
            f"\n🚨 Emergency Risk Status",
            f"  Level: {status.level.name}",
            f"  Positions at Risk: {status.positions_at_risk}",
            f"  Shutdown Required: {'YES ⚠️' if status.requires_shutdown else 'No'}",
        ]
        
        if status.shutdown_reason:
            lines.append(f"  Shutdown Reason: {status.shutdown_reason.value}")
        
        if status.active_alerts:
            lines.append(f"\n  Active Alerts:")
            for alert in status.active_alerts:
                lines.append(f"    {alert}")
        
        lines.append(f"\n  Recommended Action:")
        lines.append(f"    {status.recommended_action}")
        
        return "\n".join(lines)
