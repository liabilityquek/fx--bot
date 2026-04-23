"""Active trade management and monitoring.

This module handles:
- Monitoring open positions
- Updating trailing stops
- Checking SL/TP proximity
- Handling partial closes
- Emergency position closure
"""

import logging
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from src.broker.base import BaseBroker, Trade, Position, OrderSide
from src.monitoring.logger import get_logger
from src.monitoring.alerts import AlertManager


class TradeAction(Enum):
    """Actions that can be taken on trades."""
    NONE = "none"
    CLOSE = "close"
    PARTIAL_CLOSE = "partial_close"
    MODIFY_SL = "modify_sl"
    MODIFY_TP = "modify_tp"
    TRAILING_STOP = "trailing_stop"
    EMERGENCY_CLOSE = "emergency_close"


@dataclass
class ManagedTrade:
    """Extended trade information for management."""
    trade: Trade
    strategy_name: str = ""
    entry_time: datetime = field(default_factory=datetime.now)
    initial_sl: Optional[float] = None
    initial_tp: Optional[float] = None
    trailing_stop_active: bool = False
    trailing_stop_distance: float = 0.0
    highest_price: float = 0.0  # For long trailing
    lowest_price: float = 0.0   # For short trailing
    partial_closes: List[Dict] = field(default_factory=list)
    
    @property
    def age_hours(self) -> float:
        """Get trade age in hours."""
        delta = datetime.now() - self.entry_time
        return delta.total_seconds() / 3600


@dataclass
class TradeManagementResult:
    """Result of trade management action."""
    trade_id: str
    action: TradeAction
    success: bool
    details: str = ""
    new_sl: Optional[float] = None
    new_tp: Optional[float] = None
    pnl: float = 0.0


class TradeManager:
    """Manage active trades with monitoring and adjustments."""
    
    def __init__(
        self,
        broker: BaseBroker,
        logger: Optional[logging.Logger] = None,
        alert_manager: Optional[AlertManager] = None
    ):
        """
        Initialize trade manager.
        
        Args:
            broker: Broker instance
            logger: Logger instance
            alert_manager: Alert manager for notifications
        """
        self.broker = broker
        self.logger = logger or get_logger('trade_manager')
        self.alert_manager = alert_manager
        
        # Managed trades tracking
        self.managed_trades: Dict[str, ManagedTrade] = {}
        
        # Configuration
        self.trailing_stop_enabled = True
        self.trailing_stop_activation_pips = 20.0  # Activate after X pips profit
        self.trailing_stop_distance_pips = 15.0    # Trail by X pips
        self.max_trade_age_hours = 72.0            # Alert if trade older than this
        self.partial_close_levels = [0.5, 0.75]    # Close 50% at 50% of TP, etc.
    
    def register_trade(
        self,
        trade: Trade,
        strategy_name: str = "",
        trailing_stop: bool = False,
        trailing_distance: float = 0.0
    ) -> ManagedTrade:
        """
        Register a new trade for management.
        
        Args:
            trade: Trade to manage
            strategy_name: Name of strategy that created the trade
            trailing_stop: Enable trailing stop
            trailing_distance: Trailing stop distance in pips
        
        Returns:
            ManagedTrade instance
        """
        managed = ManagedTrade(
            trade=trade,
            strategy_name=strategy_name,
            initial_sl=trade.stop_loss,
            initial_tp=trade.take_profit,
            trailing_stop_active=trailing_stop,
            trailing_stop_distance=trailing_distance or self.trailing_stop_distance_pips,
            highest_price=trade.entry_price,
            lowest_price=trade.entry_price
        )
        
        self.managed_trades[trade.trade_id] = managed
        
        self.logger.info(
            f"Registered trade {trade.trade_id}: {trade.pair} "
            f"{trade.side.value.upper()} {trade.units:,} units"
        )
        
        return managed
    
    def unregister_trade(self, trade_id: str):
        """Remove trade from management."""
        if trade_id in self.managed_trades:
            del self.managed_trades[trade_id]
            self.logger.info(f"Unregistered trade {trade_id}")
    
    def sync_trades(self) -> Dict[str, str]:
        """
        Synchronize managed trades with broker.
        
        Returns:
            Dict of trade_id -> status (added, removed, synced)
        """
        result = {}
        broker_trades = {t.trade_id: t for t in self.broker.get_open_trades()}
        
        # Check for closed trades (in managed but not in broker)
        closed_ids = set(self.managed_trades.keys()) - set(broker_trades.keys())
        for trade_id in closed_ids:
            self.logger.info(f"Trade {trade_id} closed (no longer at broker)")
            self.unregister_trade(trade_id)
            result[trade_id] = "removed"
        
        # Update existing trades
        for trade_id, trade in broker_trades.items():
            if trade_id in self.managed_trades:
                # Update trade data
                self.managed_trades[trade_id].trade = trade
                result[trade_id] = "synced"
            else:
                # New trade found at broker (manual or from another system)
                self.register_trade(trade, strategy_name="unknown")
                result[trade_id] = "added"
        
        return result
    
    def update_all_trades(self) -> List[TradeManagementResult]:
        """
        Update all managed trades.
        
        Returns:
            List of management actions taken
        """
        results = []
        
        # Sync with broker first
        self.sync_trades()
        
        for trade_id, managed in list(self.managed_trades.items()):
            # Update price tracking for trailing stops
            self._update_price_tracking(managed)
            
            # Check trailing stop
            if managed.trailing_stop_active:
                result = self._check_trailing_stop(managed)
                if result and result.action != TradeAction.NONE:
                    results.append(result)
            
            # Check trade age
            if managed.age_hours > self.max_trade_age_hours:
                self.logger.warning(
                    f"Trade {trade_id} is {managed.age_hours:.1f} hours old"
                )
                if self.alert_manager:
                    self.alert_manager.send_alert(
                        f"Old trade alert: {managed.trade.pair} open for "
                        f"{managed.age_hours:.0f} hours",
                        priority='WARNING'
                    )
        
        return results
    
    def _update_price_tracking(self, managed: ManagedTrade):
        """Update highest/lowest price for trailing stop."""
        current_price = managed.trade.current_price
        
        if current_price > managed.highest_price:
            managed.highest_price = current_price
        if current_price < managed.lowest_price or managed.lowest_price == 0:
            managed.lowest_price = current_price
    
    def _check_trailing_stop(
        self,
        managed: ManagedTrade
    ) -> Optional[TradeManagementResult]:
        """
        Check and update trailing stop.
        
        Args:
            managed: Managed trade
        
        Returns:
            TradeManagementResult if action taken
        """
        trade = managed.trade
        
        # Determine pip size
        pip_size = 0.01 if 'JPY' in trade.pair else 0.0001
        
        # Calculate current profit in pips
        if trade.is_long:
            profit_pips = (trade.current_price - trade.entry_price) / pip_size
            # For long: trail below highest price
            new_sl = managed.highest_price - (managed.trailing_stop_distance * pip_size)
        else:
            profit_pips = (trade.entry_price - trade.current_price) / pip_size
            # For short: trail above lowest price
            new_sl = managed.lowest_price + (managed.trailing_stop_distance * pip_size)
        
        # Only activate trailing stop after minimum profit
        if profit_pips < self.trailing_stop_activation_pips:
            return None
        
        # Check if new SL is better than current
        current_sl = trade.stop_loss
        should_update = False
        
        if current_sl is None:
            should_update = True
        elif trade.is_long and new_sl > current_sl:
            should_update = True
        elif trade.is_short and new_sl < current_sl:
            should_update = True
        
        if should_update:
            success = self.broker.modify_trade(
                trade_id=trade.trade_id,
                pair=trade.pair,
                stop_loss=new_sl
            )
            
            if success:
                self.logger.info(
                    f"Trailing stop updated for {trade.trade_id}: "
                    f"{current_sl} -> {new_sl:.5f}"
                )
                return TradeManagementResult(
                    trade_id=trade.trade_id,
                    action=TradeAction.TRAILING_STOP,
                    success=True,
                    details=f"SL moved from {current_sl} to {new_sl:.5f}",
                    new_sl=new_sl
                )
            else:
                self.logger.error(
                    f"Failed to update trailing stop for {trade.trade_id}"
                )
                return TradeManagementResult(
                    trade_id=trade.trade_id,
                    action=TradeAction.TRAILING_STOP,
                    success=False,
                    details="Broker rejected modification"
                )
        
        return None
    
    def close_trade(
        self,
        trade_id: str,
        reason: str = "manual"
    ) -> TradeManagementResult:
        """
        Close a specific trade.
        
        Args:
            trade_id: Trade ID to close
            reason: Reason for closure
        
        Returns:
            TradeManagementResult
        """
        managed = self.managed_trades.get(trade_id)
        
        if not managed:
            self.logger.warning(f"Trade {trade_id} not in managed trades")
        
        success = self.broker.close_trade(trade_id)
        
        if success:
            pnl = managed.trade.unrealized_pnl if managed else 0.0
            
            self.logger.info(
                f"✅ Trade {trade_id} closed | Reason: {reason} | "
                f"P/L: ${pnl:.2f}"
            )
            
            if self.alert_manager and managed:
                self.alert_manager.alert_trade_closed(
                    pair=managed.trade.pair,
                    pnl=pnl,
                    reason=reason
                )
            
            self.unregister_trade(trade_id)
            
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.CLOSE,
                success=True,
                details=f"Closed: {reason}",
                pnl=pnl
            )
        else:
            self.logger.error(f"❌ Failed to close trade {trade_id}")
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.CLOSE,
                success=False,
                details="Broker rejected close request"
            )
    
    def close_all_trades(
        self,
        reason: str = "close_all",
        pairs: Optional[Set[str]] = None
    ) -> List[TradeManagementResult]:
        """
        Close all managed trades.
        
        Args:
            reason: Reason for closure
            pairs: Optional set of pairs to close (None = all)
        
        Returns:
            List of results
        """
        results = []
        
        for trade_id, managed in list(self.managed_trades.items()):
            if pairs is None or managed.trade.pair in pairs:
                result = self.close_trade(trade_id, reason=reason)
                results.append(result)
        
        return results
    
    def emergency_close_all(
        self,
        reason: str = "emergency"
    ) -> List[TradeManagementResult]:
        """
        Emergency close all positions.
        
        Args:
            reason: Emergency reason
        
        Returns:
            List of results
        """
        self.logger.critical(f"🚨 EMERGENCY CLOSE ALL: {reason}")
        
        if self.alert_manager:
            self.alert_manager.send_alert(
                f"🚨 EMERGENCY: Closing all positions - {reason}",
                priority='CRITICAL'
            )
        
        results = []
        
        # Close via broker positions (more reliable than individual trades)
        positions = self.broker.get_positions()
        
        for position in positions:
            success = self.broker.close_position(position.pair)
            
            results.append(TradeManagementResult(
                trade_id=f"position_{position.pair}",
                action=TradeAction.EMERGENCY_CLOSE,
                success=success,
                details=f"Emergency close {position.pair}",
                pnl=position.unrealized_pnl
            ))
            
            if success:
                self.logger.info(f"Emergency closed {position.pair}")
            else:
                self.logger.error(f"Failed to emergency close {position.pair}")
        
        # Clear managed trades
        self.managed_trades.clear()
        
        return results
    
    def modify_stop_loss(
        self,
        trade_id: str,
        pair: str,
        new_sl: float
    ) -> TradeManagementResult:
        """
        Modify stop loss for a trade.

        Args:
            trade_id: Trade ID
            pair: Instrument pair (e.g. 'USD_CHF') for price formatting
            new_sl: New stop loss price

        Returns:
            TradeManagementResult
        """
        success = self.broker.modify_trade(trade_id, pair=pair, stop_loss=new_sl)
        
        if success:
            self.logger.info(f"Modified SL for {trade_id}: {new_sl}")
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.MODIFY_SL,
                success=True,
                new_sl=new_sl
            )
        else:
            self.logger.error(f"Failed to modify SL for {trade_id}")
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.MODIFY_SL,
                success=False,
                details="Broker rejected modification"
            )
    
    def modify_take_profit(
        self,
        trade_id: str,
        pair: str,
        new_tp: float
    ) -> TradeManagementResult:
        """
        Modify take profit for a trade.

        Args:
            trade_id: Trade ID
            pair: Instrument pair (e.g. 'USD_CHF') for price formatting
            new_tp: New take profit price

        Returns:
            TradeManagementResult
        """
        success = self.broker.modify_trade(trade_id, pair=pair, take_profit=new_tp)
        
        if success:
            self.logger.info(f"Modified TP for {trade_id}: {new_tp}")
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.MODIFY_TP,
                success=True,
                new_tp=new_tp
            )
        else:
            self.logger.error(f"Failed to modify TP for {trade_id}")
            return TradeManagementResult(
                trade_id=trade_id,
                action=TradeAction.MODIFY_TP,
                success=False,
                details="Broker rejected modification"
            )
    
    def get_managed_trade(self, trade_id: str) -> Optional[ManagedTrade]:
        """Get a managed trade by ID."""
        return self.managed_trades.get(trade_id)
    
    def list_managed_trades(self) -> List[ManagedTrade]:
        """Get list of all managed trades."""
        return list(self.managed_trades.values())
