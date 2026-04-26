"""Active trade management and monitoring.

This module handles:
- Monitoring open positions
- Updating trailing stops
- Checking SL/TP proximity
- Emergency position closure
"""

import json
import logging
from pathlib import Path
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from src.broker.base import BaseBroker, Trade, Position, OrderSide
from src.monitoring.logger import get_logger
from src.monitoring.alerts import AlertManager
from config.settings import settings


class TradeAction(Enum):
    """Actions that can be taken on trades."""
    NONE = "none"
    CLOSE = "close"
    MODIFY_SL = "modify_sl"
    MODIFY_TP = "modify_tp"
    TRAILING_STOP = "trailing_stop"
    EMERGENCY_CLOSE = "emergency_close"


def _market_hours_elapsed(start: datetime, end: datetime) -> float:
    """Elapsed hours between start and end, excluding FX weekends (Fri 22:00–Sun 22:00 UTC)."""
    utc = timezone.utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=utc)
    if end <= start:
        return 0.0

    # Find the Friday 22:00 UTC on or before start
    days_since_friday = (start.weekday() - 4) % 7
    first_friday = (start - timedelta(days=days_since_friday)).replace(
        hour=22, minute=0, second=0, microsecond=0
    )

    weekend_seconds = 0.0
    weekend_start = first_friday
    while weekend_start < end:
        weekend_end = weekend_start + timedelta(hours=48)  # Sun 22:00 UTC
        overlap_start = max(weekend_start, start)
        overlap_end = min(weekend_end, end)
        if overlap_end > overlap_start:
            weekend_seconds += (overlap_end - overlap_start).total_seconds()
        weekend_start += timedelta(weeks=1)

    return ((end - start).total_seconds() - weekend_seconds) / 3600


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
    break_even_triggered: bool = False
    partial_tp_triggered: bool = False
    atr_value: Optional[float] = None  # Last known ATR for dynamic trailing stop

    @property
    def age_hours(self) -> float:
        """Get trade age in market hours, excluding FX weekends."""
        return _market_hours_elapsed(self.entry_time, datetime.now(timezone.utc))


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

        # Persistent state
        self._state_file = Path(__file__).parent.parent.parent / "data" / "managed_trades.json"
        self._persisted_state: Dict[str, dict] = {}
        self._load_state()

        # Configuration
        self.trailing_stop_enabled = True
        self.trailing_stop_activation_pips = settings.TRAILING_STOP_ACTIVATION_PIPS
        self.trailing_stop_distance_pips = settings.TRAILING_STOP_DISTANCE_PIPS
        self.break_even_activation_pips = settings.BREAK_EVEN_ACTIVATION_PIPS
        self.break_even_buffer_pips = settings.BREAK_EVEN_BUFFER_PIPS
        self.max_trade_age_hours = 72.0            # Alert if trade older than this
    
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
            entry_time=trade.open_time,
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
            self._save_state()

    def update_trade_atr(self, trade_id: str, atr_value: float) -> None:
        """Update the stored ATR for a managed trade's trailing stop calculation."""
        if trade_id in self.managed_trades:
            self.managed_trades[trade_id].atr_value = atr_value

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
                # Restore from persisted state if available, else treat as unknown
                persisted = self._persisted_state.get(trade_id, {})
                managed = self.register_trade(
                    trade,
                    strategy_name=persisted.get("strategy_name", "unknown"),
                    trailing_stop=persisted.get("trailing_stop_active", False),
                    trailing_distance=persisted.get("trailing_stop_distance", 0.0),
                )
                # Restore peak/trough price tracking so trailing stop continues correctly
                if persisted:
                    managed.highest_price = persisted.get("highest_price", trade.entry_price)
                    managed.lowest_price = persisted.get("lowest_price", trade.entry_price)
                    managed.break_even_triggered = persisted.get("break_even_triggered", False)
                    managed.partial_tp_triggered = persisted.get("partial_tp_triggered", False)
                    managed.atr_value = persisted.get("atr_value", None)
                    self.logger.info(f"Restored persisted state for trade {trade_id}")
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

            # Check break-even
            self._check_break_even(managed)

            # Check partial TP
            self._check_partial_tp(managed)

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

        self._save_state()
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
        
        # Determine trailing distance: ATR-based if available, else fixed pips
        if managed.atr_value and managed.atr_value > 0:
            trail_distance = managed.atr_value * 1.5  # ATR-based distance in price
        else:
            trail_distance = managed.trailing_stop_distance * pip_size  # fallback to fixed pips

        # Calculate current profit in pips
        if trade.is_long:
            profit_pips = (trade.current_price - trade.entry_price) / pip_size
            # For long: trail below highest price
            new_sl = managed.highest_price - trail_distance
        else:
            profit_pips = (trade.entry_price - trade.current_price) / pip_size
            # For short: trail above lowest price
            new_sl = managed.lowest_price + trail_distance
        
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
    
    def _check_break_even(self, managed: ManagedTrade) -> None:
        """Move SL to break-even when profit_pips >= BREAK_EVEN_ACTIVATION_PIPS."""
        if managed.break_even_triggered:
            return
        trade = managed.trade
        pip_size = 0.01 if 'JPY' in trade.pair else 0.0001
        if trade.is_long:
            profit_pips = (trade.current_price - trade.entry_price) / pip_size
        else:
            profit_pips = (trade.entry_price - trade.current_price) / pip_size
        if profit_pips < self.break_even_activation_pips:
            return
        buffer = self.break_even_buffer_pips * pip_size
        if trade.is_long:
            new_sl = trade.entry_price + buffer
            if trade.stop_loss and new_sl <= trade.stop_loss:
                return  # Current SL already better
        else:
            new_sl = trade.entry_price - buffer
            if trade.stop_loss and new_sl >= trade.stop_loss:
                return  # Current SL already better
        success = self.broker.modify_trade(
            trade_id=trade.trade_id,
            pair=trade.pair,
            stop_loss=new_sl,
        )
        if success:
            managed.break_even_triggered = True
            self.logger.info(
                f"Break-even set for {trade.trade_id} ({trade.pair}): SL -> {new_sl:.5f}"
            )

    def _check_partial_tp(self, managed: ManagedTrade) -> None:
        """Close partial position at 1:1 RR target."""
        if not settings.PARTIAL_TP_ENABLED:
            return
        if managed.partial_tp_triggered:
            return
        if managed.initial_sl is None:
            return
        trade = managed.trade
        pip_size = 0.01 if 'JPY' in trade.pair else 0.0001
        sl_pips = abs(trade.entry_price - managed.initial_sl) / pip_size
        if sl_pips == 0:
            return
        target_pips = sl_pips * settings.PARTIAL_TP_RR_TARGET
        if trade.is_long:
            profit_pips = (trade.current_price - trade.entry_price) / pip_size
        else:
            profit_pips = (trade.entry_price - trade.current_price) / pip_size
        if profit_pips < target_pips:
            return
        units_to_close = int(abs(trade.units) * settings.PARTIAL_TP_RATIO)
        if units_to_close < 1:
            return
        success = self.broker.partial_close_trade(trade.trade_id, units_to_close)
        if success:
            managed.partial_tp_triggered = True
            managed.break_even_triggered = True  # Move to break-even simultaneously
            self.logger.info(
                f"Partial TP: {trade.pair} trade {trade.trade_id} — "
                f"closed {units_to_close} units at ~{profit_pips:.1f} pips profit"
            )
            if self.alert_manager:
                try:
                    self.alert_manager._send_telegram(
                        f"Partial TP: {trade.pair} — closed {units_to_close} units "
                        f"at {profit_pips:.1f} pips ({settings.PARTIAL_TP_RATIO*100:.0f}% of position)",
                        parse_mode=''
                    )
                except Exception:
                    pass

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
        self._save_state()
        
        return results
    
    def _save_state(self) -> None:
        """Persist managed trade metadata to disk."""
        try:
            data = {}
            for trade_id, managed in self.managed_trades.items():
                data[trade_id] = {
                    "strategy_name": managed.strategy_name,
                    "trailing_stop_active": managed.trailing_stop_active,
                    "trailing_stop_distance": managed.trailing_stop_distance,
                    "highest_price": managed.highest_price,
                    "lowest_price": managed.lowest_price,
                    "initial_sl": managed.initial_sl,
                    "initial_tp": managed.initial_tp,
                    "break_even_triggered": managed.break_even_triggered,
                    "partial_tp_triggered": managed.partial_tp_triggered,
                    "atr_value": managed.atr_value,
                }
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(data, indent=2))
            self._persisted_state = data
        except Exception as e:
            self.logger.warning(f"Could not save managed trades state: {e}")

    def _load_state(self) -> None:
        """Load persisted managed trade metadata from disk."""
        try:
            if self._state_file.exists():
                self._persisted_state = json.loads(self._state_file.read_text())
                self.logger.info(
                    f"Loaded persisted state for {len(self._persisted_state)} trade(s)"
                )
        except Exception as e:
            self.logger.warning(f"Could not load managed trades state: {e}")
            self._persisted_state = {}

    def get_managed_trade(self, trade_id: str) -> Optional[ManagedTrade]:
        """Get a managed trade by ID."""
        return self.managed_trades.get(trade_id)
    
    def list_managed_trades(self) -> List[ManagedTrade]:
        """Get list of all managed trades."""
        return list(self.managed_trades.values())
