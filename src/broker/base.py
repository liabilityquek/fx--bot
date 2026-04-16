"""Base broker interface - abstract class for all broker implementations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict
from enum import Enum


class OrderSide(Enum):
    """Order side enumeration."""
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    """Order status enumeration."""
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Trade:
    """Trade representation."""
    trade_id: str
    pair: str
    side: OrderSide
    units: int
    entry_price: float
    current_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unrealized_pnl: float = 0.0
    open_time: Optional[datetime] = None
    
    @property
    def is_long(self) -> bool:
        """Check if trade is long."""
        return self.side == OrderSide.BUY
    
    @property
    def is_short(self) -> bool:
        """Check if trade is short."""
        return self.side == OrderSide.SELL


@dataclass
class Position:
    """Position representation (aggregate of trades in same pair)."""
    pair: str
    net_units: int  # Positive = long, negative = short
    average_price: float
    unrealized_pnl: float
    trades: List[Trade]
    
    @property
    def is_long(self) -> bool:
        """Check if position is net long."""
        return self.net_units > 0
    
    @property
    def is_short(self) -> bool:
        """Check if position is net short."""
        return self.net_units < 0
    
    @property
    def is_flat(self) -> bool:
        """Check if position is flat (no exposure)."""
        return self.net_units == 0


@dataclass
class AccountInfo:
    """Account information."""
    account_id: str
    balance: float
    nav: float  # Net Asset Value
    margin_used: float
    margin_available: float
    unrealized_pnl: float
    open_trade_count: int
    currency: str = "USD"


class BaseBroker(ABC):
    """Abstract base class for broker implementations."""
    
    @abstractmethod
    def connect(self) -> bool:
        """
        Establish connection to broker API.
        
        Returns:
            True if connection successful, False otherwise
        """
        pass
    
    @abstractmethod
    def get_account_info(self) -> Optional[AccountInfo]:
        """
        Get current account information.
        
        Returns:
            AccountInfo object or None if failed
        """
        pass
    
    @abstractmethod
    def get_current_price(self, pair: str) -> Optional[Dict[str, float]]:
        """
        Get current bid/ask prices for a pair.
        
        Args:
            pair: Trading pair (e.g., 'EUR_USD')
        
        Returns:
            Dictionary with 'bid' and 'ask' prices, or None if failed
        """
        pass
    
    @abstractmethod
    def get_open_trades(self) -> List[Trade]:
        """
        Get all open trades.
        
        Returns:
            List of Trade objects
        """
        pass
    
    @abstractmethod
    def get_positions(self) -> List[Position]:
        """
        Get all open positions.
        
        Returns:
            List of Position objects
        """
        pass
    
    @abstractmethod
    def get_position(self, pair: str) -> Optional[Position]:
        """
        Get position for specific pair.
        
        Args:
            pair: Trading pair
        
        Returns:
            Position object or None if no position
        """
        pass
    
    @abstractmethod
    def place_market_order(
        self,
        pair: str,
        side: OrderSide,
        units: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> Optional[str]:
        """
        Place a market order.
        
        Args:
            pair: Trading pair
            side: Order side (BUY or SELL)
            units: Number of units (absolute value)
            stop_loss: Stop loss price (optional)
            take_profit: Take profit price (optional)
        
        Returns:
            Trade ID if successful, None otherwise
        """
        pass
    
    @abstractmethod
    def close_trade(self, trade_id: str) -> bool:
        """
        Close a specific trade.
        
        Args:
            trade_id: Trade ID to close
        
        Returns:
            True if closed successfully, False otherwise
        """
        pass
    
    @abstractmethod
    def close_position(self, pair: str) -> bool:
        """
        Close entire position for a pair.
        
        Args:
            pair: Trading pair
        
        Returns:
            True if closed successfully, False otherwise
        """
        pass
    
    @abstractmethod
    def modify_trade(
        self,
        trade_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> bool:
        """
        Modify stop loss and/or take profit for a trade.
        
        Args:
            trade_id: Trade ID
            stop_loss: New stop loss price
            take_profit: New take profit price
        
        Returns:
            True if modified successfully, False otherwise
        """
        pass
    
    def has_open_position(self, pair: str) -> bool:
        """
        Check if there's an open position for a pair.
        
        Args:
            pair: Trading pair
        
        Returns:
            True if position exists, False otherwise
        """
        position = self.get_position(pair)
        return position is not None and not position.is_flat
