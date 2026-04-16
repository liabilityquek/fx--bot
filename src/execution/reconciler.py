"""Position reconciliation and state persistence.

This module handles:
- Syncing broker positions with bot state
- Detecting manual trades
- Handling orphaned positions
- State persistence to file
"""

import json
import logging
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from enum import Enum

from src.broker.base import BaseBroker, Trade, Position
from src.monitoring.logger import get_logger
from src.monitoring.alerts import AlertManager


class ReconciliationIssue(Enum):
    """Types of reconciliation issues."""
    ORPHANED_TRADE = "orphaned_trade"       # Trade at broker, not in bot
    MISSING_TRADE = "missing_trade"         # Trade in bot, not at broker
    POSITION_MISMATCH = "position_mismatch" # Units or direction don't match
    MANUAL_TRADE = "manual_trade"           # Trade detected that bot didn't place
    STALE_STATE = "stale_state"             # Bot state is outdated


@dataclass
class ReconciliationResult:
    """Result of reconciliation check."""
    is_synced: bool
    issues: List[Dict]
    broker_trades: List[str]
    bot_trades: List[str]
    orphaned: List[str]
    missing: List[str]
    manual_detected: List[str]
    timestamp: datetime
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'is_synced': self.is_synced,
            'issues': self.issues,
            'broker_trades': self.broker_trades,
            'bot_trades': self.bot_trades,
            'orphaned': self.orphaned,
            'missing': self.missing,
            'manual_detected': self.manual_detected,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class BotState:
    """Persistent bot state."""
    active_trades: Dict[str, Dict]  # trade_id -> trade info
    pending_orders: Dict[str, Dict]
    closed_trades: List[Dict]
    total_pnl: float
    last_update: datetime
    session_start: datetime
    version: str = "1.0"
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for persistence."""
        return {
            'active_trades': self.active_trades,
            'pending_orders': self.pending_orders,
            'closed_trades': self.closed_trades[-100:],  # Keep last 100
            'total_pnl': self.total_pnl,
            'last_update': self.last_update.isoformat(),
            'session_start': self.session_start.isoformat(),
            'version': self.version
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'BotState':
        """Create from dictionary."""
        return cls(
            active_trades=data.get('active_trades', {}),
            pending_orders=data.get('pending_orders', {}),
            closed_trades=data.get('closed_trades', []),
            total_pnl=data.get('total_pnl', 0.0),
            last_update=datetime.fromisoformat(data['last_update']) if 'last_update' in data else datetime.now(),
            session_start=datetime.fromisoformat(data['session_start']) if 'session_start' in data else datetime.now(),
            version=data.get('version', '1.0')
        )


class PositionReconciler:
    """Reconcile bot state with broker positions."""
    
    def __init__(
        self,
        broker: BaseBroker,
        logger: Optional[logging.Logger] = None,
        alert_manager: Optional[AlertManager] = None,
        state_file: str = "data/bot_state.json"
    ):
        """
        Initialize reconciler.
        
        Args:
            broker: Broker instance
            logger: Logger instance
            alert_manager: Alert manager for notifications
            state_file: Path to state persistence file
        """
        self.broker = broker
        self.logger = logger or get_logger('reconciler')
        self.alert_manager = alert_manager
        self.state_file = Path(state_file)
        
        # Bot state
        self.state = self._load_state()
        
        # Known manual trades (don't alert repeatedly)
        self.known_manual_trades: Set[str] = set()
    
    def _load_state(self) -> BotState:
        """Load state from file or create new."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.logger.info(f"Loaded state from {self.state_file}")
                    return BotState.from_dict(data)
            except Exception as e:
                self.logger.warning(f"Could not load state: {e}")
        
        return BotState(
            active_trades={},
            pending_orders={},
            closed_trades=[],
            total_pnl=0.0,
            last_update=datetime.now(),
            session_start=datetime.now()
        )
    
    def save_state(self):
        """Save current state to file."""
        try:
            self.state.last_update = datetime.now()
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.state_file, 'w') as f:
                json.dump(self.state.to_dict(), f, indent=2)
            
            self.logger.debug(f"State saved to {self.state_file}")
        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")
    
    def register_trade(
        self,
        trade_id: str,
        pair: str,
        side: str,
        units: int,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        strategy: str = ""
    ):
        """
        Register a trade in bot state.
        
        Args:
            trade_id: Broker trade ID
            pair: Trading pair
            side: 'buy' or 'sell'
            units: Position size
            entry_price: Entry price
            stop_loss: Stop loss price
            take_profit: Take profit price
            strategy: Strategy that created the trade
        """
        self.state.active_trades[trade_id] = {
            'trade_id': trade_id,
            'pair': pair,
            'side': side,
            'units': units,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'strategy': strategy,
            'open_time': datetime.now().isoformat()
        }
        
        self.logger.info(f"Registered trade {trade_id} in bot state")
        self.save_state()
    
    def unregister_trade(self, trade_id: str, pnl: float = 0.0, reason: str = ""):
        """
        Remove trade from active and record in closed.
        
        Args:
            trade_id: Trade ID to unregister
            pnl: Realized P/L
            reason: Reason for closure
        """
        if trade_id in self.state.active_trades:
            trade_info = self.state.active_trades.pop(trade_id)
            trade_info['close_time'] = datetime.now().isoformat()
            trade_info['pnl'] = pnl
            trade_info['close_reason'] = reason
            
            self.state.closed_trades.append(trade_info)
            self.state.total_pnl += pnl
            
            self.logger.info(f"Unregistered trade {trade_id} | P/L: ${pnl:.2f}")
            self.save_state()
    
    def reconcile(self) -> ReconciliationResult:
        """
        Reconcile bot state with broker positions.
        
        Returns:
            ReconciliationResult with details
        """
        issues = []
        orphaned = []
        missing = []
        manual_detected = []
        
        # Get broker trades
        broker_trades = self.broker.get_open_trades()
        broker_trade_ids = {t.trade_id for t in broker_trades}
        broker_trade_map = {t.trade_id: t for t in broker_trades}
        
        # Get bot trades
        bot_trade_ids = set(self.state.active_trades.keys())
        
        # Find orphaned trades (at broker, not in bot)
        orphaned_ids = broker_trade_ids - bot_trade_ids
        for trade_id in orphaned_ids:
            trade = broker_trade_map[trade_id]
            
            # Check if it's a known manual trade
            if trade_id in self.known_manual_trades:
                continue
            
            issues.append({
                'type': ReconciliationIssue.ORPHANED_TRADE.value,
                'trade_id': trade_id,
                'pair': trade.pair,
                'side': trade.side.value,
                'units': trade.units,
                'message': f"Trade {trade_id} found at broker but not in bot state"
            })
            orphaned.append(trade_id)
            manual_detected.append(trade_id)
            
            self.logger.warning(
                f"Orphaned trade detected: {trade_id} | "
                f"{trade.pair} {trade.side.value.upper()} {trade.units}"
            )
        
        # Find missing trades (in bot, not at broker - likely closed)
        missing_ids = bot_trade_ids - broker_trade_ids
        for trade_id in missing_ids:
            trade_info = self.state.active_trades[trade_id]
            
            issues.append({
                'type': ReconciliationIssue.MISSING_TRADE.value,
                'trade_id': trade_id,
                'pair': trade_info['pair'],
                'message': f"Trade {trade_id} in bot state but not at broker (likely closed)"
            })
            missing.append(trade_id)
            
            self.logger.info(
                f"Missing trade detected (closed?): {trade_id} | {trade_info['pair']}"
            )
            
            # Auto-unregister with unknown P/L
            self.unregister_trade(trade_id, pnl=0.0, reason="reconciliation_missing")
        
        # Check for position mismatches
        for trade_id in bot_trade_ids & broker_trade_ids:
            bot_trade = self.state.active_trades[trade_id]
            broker_trade = broker_trade_map[trade_id]
            
            # Check units match
            if bot_trade['units'] != broker_trade.units:
                issues.append({
                    'type': ReconciliationIssue.POSITION_MISMATCH.value,
                    'trade_id': trade_id,
                    'message': f"Units mismatch: bot={bot_trade['units']}, broker={broker_trade.units}"
                })
                
                # Update bot state to match broker
                self.state.active_trades[trade_id]['units'] = broker_trade.units
        
        is_synced = len(issues) == 0
        
        result = ReconciliationResult(
            is_synced=is_synced,
            issues=issues,
            broker_trades=list(broker_trade_ids),
            bot_trades=list(bot_trade_ids),
            orphaned=orphaned,
            missing=missing,
            manual_detected=manual_detected,
            timestamp=datetime.now()
        )
        
        # Alert if issues found
        if not is_synced and self.alert_manager:
            self.alert_manager.send_alert(
                f"Position reconciliation found {len(issues)} issue(s)",
                priority='WARNING'
            )
        
        # Save state after reconciliation
        self.save_state()
        
        return result
    
    def adopt_orphaned_trade(
        self,
        trade_id: str,
        strategy: str = "adopted"
    ) -> bool:
        """
        Adopt an orphaned trade into bot management.
        
        Args:
            trade_id: Trade ID to adopt
            strategy: Strategy name to assign
        
        Returns:
            True if adopted successfully
        """
        trades = self.broker.get_open_trades()
        
        for trade in trades:
            if trade.trade_id == trade_id:
                self.register_trade(
                    trade_id=trade_id,
                    pair=trade.pair,
                    side=trade.side.value,
                    units=trade.units,
                    entry_price=trade.entry_price,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit,
                    strategy=strategy
                )
                
                self.known_manual_trades.add(trade_id)
                self.logger.info(f"Adopted orphaned trade {trade_id}")
                return True
        
        self.logger.warning(f"Trade {trade_id} not found at broker")
        return False
    
    def ignore_orphaned_trade(self, trade_id: str):
        """
        Mark an orphaned trade as known (don't alert again).
        
        Args:
            trade_id: Trade ID to ignore
        """
        self.known_manual_trades.add(trade_id)
        self.logger.info(f"Ignoring orphaned trade {trade_id}")
    
    def get_position_summary(self) -> Dict:
        """
        Get summary of current positions.
        
        Returns:
            Position summary
        """
        broker_trades = self.broker.get_open_trades()
        positions = self.broker.get_positions()
        
        summary = {
            'total_trades': len(broker_trades),
            'total_positions': len(positions),
            'bot_managed': len(self.state.active_trades),
            'orphaned': len(set(t.trade_id for t in broker_trades) - set(self.state.active_trades.keys())),
            'total_unrealized_pnl': sum(t.unrealized_pnl for t in broker_trades),
            'session_realized_pnl': self.state.total_pnl,
            'positions': []
        }
        
        for pos in positions:
            summary['positions'].append({
                'pair': pos.pair,
                'net_units': pos.net_units,
                'direction': 'LONG' if pos.is_long else 'SHORT',
                'unrealized_pnl': pos.unrealized_pnl
            })
        
        return summary
    
    def reset_state(self):
        """Reset bot state (start fresh)."""
        self.state = BotState(
            active_trades={},
            pending_orders={},
            closed_trades=[],
            total_pnl=0.0,
            last_update=datetime.now(),
            session_start=datetime.now()
        )
        self.known_manual_trades.clear()
        self.save_state()
        self.logger.info("Bot state reset")
    
    def get_trade_history(
        self,
        limit: int = 50,
        pair: Optional[str] = None
    ) -> List[Dict]:
        """
        Get closed trade history.
        
        Args:
            limit: Maximum trades to return
            pair: Optional filter by pair
        
        Returns:
            List of closed trades
        """
        trades = self.state.closed_trades
        
        if pair:
            trades = [t for t in trades if t.get('pair') == pair]
        
        return trades[-limit:]
    
    def get_state_info(self) -> Dict:
        """
        Get current state information.
        
        Returns:
            State info dictionary
        """
        return {
            'active_trades': len(self.state.active_trades),
            'pending_orders': len(self.state.pending_orders),
            'closed_trades': len(self.state.closed_trades),
            'session_pnl': self.state.total_pnl,
            'session_start': self.state.session_start.isoformat(),
            'last_update': self.state.last_update.isoformat(),
            'state_file': str(self.state_file),
            'known_manual_trades': len(self.known_manual_trades)
        }
