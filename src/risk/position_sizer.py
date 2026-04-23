"""Position sizing calculator for FX trading.

This module calculates position sizes based on account balance, risk parameters,
and various sizing methods (percent-risk, Kelly criterion).
"""

import logging
from enum import Enum
from typing import Optional
from dataclasses import dataclass

from config.settings import settings
from config.pairs import PAIR_INFO


class PositionSizingMethod(Enum):
    """Position sizing methods."""
    PERCENT_RISK = "percent_risk"  # Risk-based percentage
    KELLY = "kelly"  # Kelly criterion


@dataclass
class PositionSizeResult:
    """Result of position size calculation."""
    units: int
    risk_amount: float
    risk_percent: float
    method: PositionSizingMethod
    leverage_used: float
    pip_value: float
    notes: str = ""


class PositionSizer:
    """Calculate position sizes based on risk parameters."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize position sizer.
        
        Args:
            logger: Logger instance (optional)
        """
        self.logger = logger or logging.getLogger('position_sizer')
        self.max_leverage = settings.MAX_LEVERAGE
        self.max_risk_per_trade = settings.MAX_RISK_PER_TRADE
    
    def calculate(
        self,
        pair: str,
        account_balance: float,
        stop_loss_pips: int,
        risk_percent: Optional[float] = None,
        method: PositionSizingMethod = PositionSizingMethod.PERCENT_RISK,
        kelly_win_rate: Optional[float] = None,
        kelly_avg_win: Optional[float] = None,
        kelly_avg_loss: Optional[float] = None,
        current_price: Optional[float] = None
    ) -> Optional[PositionSizeResult]:
        """
        Calculate position size based on specified method.

        Args:
            pair: Trading pair (e.g., 'EUR_USD')
            account_balance: Current account balance in USD
            stop_loss_pips: Stop loss distance in pips
            risk_percent: Risk percentage (default: from settings)
            method: Position sizing method
            kelly_win_rate: Win rate for Kelly criterion (0-1)
            kelly_avg_win: Average win amount
            kelly_avg_loss: Average loss amount
            current_price: Live market price (used for accurate pip value on USD_JPY, USD_CHF)

        Returns:
            PositionSizeResult or None if calculation fails
        """
        # Validate pair
        if pair not in PAIR_INFO:
            self.logger.error(f"Unknown trading pair: {pair}")
            return None
        
        # Validate account balance
        if account_balance <= 0:
            self.logger.error(f"Invalid account balance: ${account_balance}")
            return None
        
        # Validate stop loss
        if stop_loss_pips <= 0:
            self.logger.error(f"Invalid stop loss pips: {stop_loss_pips}")
            return None
        
        # Use default risk percent if not provided
        if risk_percent is None:
            risk_percent = self.max_risk_per_trade
        
        # Enforce maximum risk per trade
        if risk_percent > self.max_risk_per_trade:
            self.logger.warning(
                f"Risk percent {risk_percent*100}% exceeds maximum "
                f"{self.max_risk_per_trade*100}%, capping to maximum"
            )
            risk_percent = self.max_risk_per_trade
        
        # Calculate based on method
        if method == PositionSizingMethod.PERCENT_RISK:
            return self._calculate_percent_risk(
                pair, account_balance, stop_loss_pips, risk_percent, current_price
            )

        elif method == PositionSizingMethod.KELLY:
            return self._calculate_kelly(
                pair, account_balance, stop_loss_pips,
                kelly_win_rate, kelly_avg_win, kelly_avg_loss, current_price
            )
        
        else:
            self.logger.error(f"Unknown position sizing method: {method}")
            return None
    
    def _calculate_percent_risk(
        self,
        pair: str,
        account_balance: float,
        stop_loss_pips: int,
        risk_percent: float,
        current_price: Optional[float] = None
    ) -> PositionSizeResult:
        """
        Calculate position size based on risk percentage.

        Args:
            pair: Trading pair
            account_balance: Account balance
            stop_loss_pips: Stop loss in pips
            risk_percent: Risk percentage (e.g., 0.02 for 2%)
            current_price: Live market price for accurate pip value conversion

        Returns:
            PositionSizeResult
        """
        # Calculate risk amount in dollars
        risk_amount = account_balance * risk_percent

        # Calculate pip value per unit
        pip_value_per_unit = self._get_pip_value(pair, 1, current_price)

        # Calculate required position size
        # Risk = Stop Loss (pips) × Pip Value × Position Size
        # Position Size = Risk / (Stop Loss × Pip Value per unit)
        position_size = risk_amount / (stop_loss_pips * pip_value_per_unit)

        # Round to integer units
        units = int(position_size)

        # Ensure minimum trade size
        pair_info = PAIR_INFO[pair]
        if units < pair_info['min_trade_units']:
            units = pair_info['min_trade_units']
            self.logger.warning(
                f"Position size below minimum, using {units} units"
            )

        # Recalculate actual risk with rounded units
        pip_value = self._get_pip_value(pair, units, current_price)
        actual_risk_amount = stop_loss_pips * pip_value
        actual_risk_percent = actual_risk_amount / account_balance

        # Calculate leverage
        notional_value = units * (current_price or 1.0)
        leverage_used = notional_value / account_balance

        # Check constraints
        notes = ""
        if leverage_used > self.max_leverage:
            # Recalculate with max leverage constraint
            max_units = int(account_balance * self.max_leverage / (current_price or 1.0))
            if max_units < units:
                units = max_units
                pip_value = self._get_pip_value(pair, units, current_price)
                actual_risk_amount = stop_loss_pips * pip_value
                actual_risk_percent = actual_risk_amount / account_balance
                leverage_used = self.max_leverage
                notes = f"⚠️ Position capped by max leverage {self.max_leverage}:1"
                self.logger.warning(notes)

        return PositionSizeResult(
            units=units,
            risk_amount=actual_risk_amount,
            risk_percent=actual_risk_percent,
            method=PositionSizingMethod.PERCENT_RISK,
            leverage_used=leverage_used,
            pip_value=pip_value,
            notes=notes
        )
    
    def _calculate_kelly(
        self,
        pair: str,
        account_balance: float,
        stop_loss_pips: int,
        win_rate: Optional[float],
        avg_win: Optional[float],
        avg_loss: Optional[float],
        current_price: Optional[float] = None
    ) -> Optional[PositionSizeResult]:
        """
        Calculate position size using Kelly criterion.
        
        Kelly % = (Win Rate × Avg Win - Loss Rate × Avg Loss) / Avg Win
        
        Args:
            pair: Trading pair
            account_balance: Account balance
            stop_loss_pips: Stop loss in pips
            win_rate: Historical win rate (0-1)
            avg_win: Average win in dollars
            avg_loss: Average loss in dollars (positive value)
        
        Returns:
            PositionSizeResult or None if parameters invalid
        """
        # Validate Kelly parameters
        if win_rate is None or avg_win is None or avg_loss is None:
            self.logger.error("Kelly criterion requires win_rate, avg_win, and avg_loss")
            return None
        
        if not (0 < win_rate < 1):
            self.logger.error(f"Invalid win rate: {win_rate} (must be 0-1)")
            return None
        
        if avg_win <= 0 or avg_loss <= 0:
            self.logger.error("Average win and loss must be positive")
            return None
        
        # Calculate Kelly percentage
        loss_rate = 1 - win_rate
        kelly_percent = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
        
        # Apply fractional Kelly (typically 0.25 to 0.5 of full Kelly for safety)
        fractional_kelly = 0.25
        adjusted_kelly = kelly_percent * fractional_kelly
        
        # Ensure Kelly doesn't exceed maximum risk
        if adjusted_kelly > self.max_risk_per_trade:
            self.logger.warning(
                f"Kelly {adjusted_kelly*100:.1f}% exceeds max risk "
                f"{self.max_risk_per_trade*100}%, capping to maximum"
            )
            adjusted_kelly = self.max_risk_per_trade
        
        # Ensure positive Kelly
        if adjusted_kelly <= 0:
            self.logger.warning(
                f"Negative Kelly criterion {kelly_percent*100:.1f}% "
                f"(system has negative expectancy), using minimum risk"
            )
            adjusted_kelly = 0.01  # 1% minimum
        
        notes = f"Full Kelly: {kelly_percent*100:.1f}%, Fractional: {fractional_kelly*100:.0f}%"
        
        # Use percent risk calculation with Kelly percentage
        result = self._calculate_percent_risk(
            pair, account_balance, stop_loss_pips, adjusted_kelly, current_price
        )
        
        if result:
            result.method = PositionSizingMethod.KELLY
            result.notes = notes + (f" | {result.notes}" if result.notes else "")
        
        return result
    
    def _get_pip_value(self, pair: str, position_size: int, current_price: Optional[float] = None) -> float:
        """
        Calculate pip value in account currency (USD).

        Args:
            pair: Trading pair
            position_size: Position size in units
            current_price: Live market price — required for accurate conversion on USD_JPY, USD_CHF

        Returns:
            Pip value in USD
        """
        pair_info = PAIR_INFO[pair]

        # For pairs quoted in USD (EUR_USD, GBP_USD, AUD_USD): pip value is in USD already
        if pair_info['quote_currency'] == 'USD':
            return pair_info['pip_value'] * position_size

        # For USD_XXX pairs, pip value is in the quote currency — divide by live rate to get USD
        # pip_value_usd = pip_value_quote / current_price (quote units per 1 USD)
        if current_price and current_price > 0:
            return (pair_info['pip_value'] * position_size) / current_price

        # current_price not available — log a warning and return raw pip value as best effort
        self.logger.warning(
            f"_get_pip_value: no current_price for {pair}, pip value may be inaccurate"
        )
        return pair_info['pip_value'] * position_size
    
    def get_max_position_size(
        self,
        pair: str,
        account_balance: float,
        current_price: float
    ) -> int:
        """
        Calculate maximum position size based on leverage limit.
        
        Args:
            pair: Trading pair
            account_balance: Account balance
            current_price: Current market price
        
        Returns:
            Maximum position size in units
        """
        # Maximum notional value = account balance × max leverage
        max_notional = account_balance * self.max_leverage
        
        # Position size = max notional / current price
        max_units = int(max_notional / current_price)
        
        return max_units
    
    def validate_position_size(
        self,
        pair: str,
        units: int,
        account_balance: float,
        current_price: float
    ) -> tuple[bool, str]:
        """
        Validate if a position size is within acceptable limits.
        
        Args:
            pair: Trading pair
            units: Position size in units
            account_balance: Account balance
            current_price: Current market price
        
        Returns:
            Tuple of (is_valid, message)
        """
        pair_info = PAIR_INFO.get(pair)
        if not pair_info:
            return False, f"Unknown trading pair: {pair}"
        
        # Check minimum trade size
        if units < pair_info['min_trade_units']:
            return False, f"Position size {units} below minimum {pair_info['min_trade_units']}"
        
        # Check leverage limit
        notional_value = units * current_price
        leverage = notional_value / account_balance
        
        if leverage > self.max_leverage:
            return False, (
                f"Leverage {leverage:.1f}:1 exceeds maximum {self.max_leverage}:1 "
                f"(max units: {int(account_balance * self.max_leverage / current_price)})"
            )
        
        return True, "Position size valid"


def main():
    """Test position sizer functionality."""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Initialize position sizer
    sizer = PositionSizer()
    
    # Test parameters
    pair = 'EUR_USD'
    account_balance = 10000.0
    stop_loss_pips = 50
    
    print("\n" + "="*70)
    print("POSITION SIZING CALCULATOR TEST")
    print("="*70)
    print(f"Pair: {pair}")
    print(f"Account Balance: ${account_balance:,.2f}")
    print(f"Stop Loss: {stop_loss_pips} pips")
    print(f"Max Risk per Trade: {settings.MAX_RISK_PER_TRADE*100}%")
    print(f"Max Leverage: {settings.MAX_LEVERAGE}:1")
    
    # Test 1: Percent Risk Method (default)
    print("\n" + "-"*70)
    print("TEST 1: Percent Risk Method (2% risk)")
    print("-"*70)
    result = sizer.calculate(
        pair=pair,
        account_balance=account_balance,
        stop_loss_pips=stop_loss_pips,
        risk_percent=0.02,
        method=PositionSizingMethod.PERCENT_RISK
    )
    
    if result:
        print(f"✅ Position Size: {result.units:,} units")
        print(f"   Risk Amount: ${result.risk_amount:.2f}")
        print(f"   Risk Percent: {result.risk_percent*100:.2f}%")
        print(f"   Leverage: {result.leverage_used:.2f}:1")
        print(f"   Pip Value: ${result.pip_value:.4f}")
        if result.notes:
            print(f"   Notes: {result.notes}")
    
    # Test 2: Kelly Criterion
    print("\n" + "-"*70)
    print("TEST 2: Kelly Criterion (50% win rate, 2:1 R:R)")
    print("-"*70)
    result = sizer.calculate(
        pair=pair,
        account_balance=account_balance,
        stop_loss_pips=stop_loss_pips,
        method=PositionSizingMethod.KELLY,
        kelly_win_rate=0.50,
        kelly_avg_win=100.0,
        kelly_avg_loss=50.0
    )
    
    if result:
        print(f"✅ Position Size: {result.units:,} units")
        print(f"   Risk Amount: ${result.risk_amount:.2f}")
        print(f"   Risk Percent: {result.risk_percent*100:.2f}%")
        print(f"   Leverage: {result.leverage_used:.2f}:1")
        if result.notes:
            print(f"   Notes: {result.notes}")
    
    # Test 3: Validation
    print("\n" + "-"*70)
    print("TEST 3: Position Size Validation")
    print("-"*70)
    
    test_units = 50000
    current_price = 1.0850
    is_valid, message = sizer.validate_position_size(
        pair=pair,
        units=test_units,
        account_balance=account_balance,
        current_price=current_price
    )
    
    print(f"Testing {test_units:,} units at ${current_price}")
    if is_valid:
        print(f"✅ {message}")
    else:
        print(f"❌ {message}")
    
    print("\n" + "="*70)


if __name__ == "__main__":
    main()
