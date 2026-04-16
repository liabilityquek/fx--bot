"""Stop-loss and take-profit calculator for FX trading.

This module calculates dynamic stop-loss and take-profit levels based on
various methods: fixed pips, ATR (Average True Range), or risk/reward ratios.
"""

import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from config.settings import settings
from config.pairs import PAIR_INFO


class StopLossMethod(Enum):
    """Stop-loss calculation methods."""
    FIXED_PIPS = "fixed_pips"  # Fixed pip distance
    ATR = "atr"  # Average True Range based
    PERCENT = "percent"  # Percentage of entry price


@dataclass
class StopLossTakeProfitLevels:
    """Calculated SL/TP levels."""
    entry_price: float
    stop_loss: float
    take_profit: float
    stop_loss_pips: float
    take_profit_pips: float
    risk_reward_ratio: float
    method: str


class StopLossTakeProfitCalculator:
    """Calculate stop-loss and take-profit levels."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize SL/TP calculator.
        
        Args:
            logger: Logger instance (optional)
        """
        self.logger = logger or logging.getLogger('sl_tp_calculator')
        self.default_sl_pips = settings.DEFAULT_STOP_LOSS_PIPS
        self.default_rr_ratio = settings.DEFAULT_TAKE_PROFIT_RATIO
    
    def calculate_fixed_pips(
        self,
        pair: str,
        entry_price: float,
        is_long: bool,
        stop_loss_pips: Optional[int] = None,
        risk_reward_ratio: Optional[float] = None
    ) -> StopLossTakeProfitLevels:
        """
        Calculate SL/TP using fixed pip distance.
        
        Args:
            pair: Trading pair (e.g., 'EUR_USD')
            entry_price: Entry price
            is_long: True for long, False for short
            stop_loss_pips: Stop loss in pips (uses default if None)
            risk_reward_ratio: Risk/reward ratio (uses default if None)
        
        Returns:
            StopLossTakeProfitLevels
        """
        sl_pips = stop_loss_pips or self.default_sl_pips
        rr_ratio = risk_reward_ratio or self.default_rr_ratio
        
        # Get pip value for the pair
        pair_info = PAIR_INFO.get(pair)
        if not pair_info:
            self.logger.error(f"Unknown pair: {pair}")
            raise ValueError(f"Unknown pair: {pair}")
        
        pip_value = pair_info['pip_value']
        
        # Calculate stop loss
        if is_long:
            stop_loss = entry_price - (sl_pips * pip_value)
            take_profit = entry_price + (sl_pips * rr_ratio * pip_value)
        else:
            stop_loss = entry_price + (sl_pips * pip_value)
            take_profit = entry_price - (sl_pips * rr_ratio * pip_value)
        
        tp_pips = sl_pips * rr_ratio
        
        result = StopLossTakeProfitLevels(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            stop_loss_pips=sl_pips,
            take_profit_pips=tp_pips,
            risk_reward_ratio=rr_ratio,
            method="fixed_pips"
        )
        
        self.logger.debug(
            f"{pair} ({'LONG' if is_long else 'SHORT'}): "
            f"Entry={entry_price:.5f}, SL={stop_loss:.5f} ({sl_pips} pips), "
            f"TP={take_profit:.5f} ({tp_pips:.1f} pips, RR={rr_ratio}:1)"
        )
        
        return result
    
    def calculate_atr_based(
        self,
        pair: str,
        entry_price: float,
        is_long: bool,
        historical_data: pd.DataFrame,
        atr_multiplier: float = 2.0,
        risk_reward_ratio: Optional[float] = None
    ) -> StopLossTakeProfitLevels:
        """
        Calculate SL/TP using ATR (Average True Range).
        
        Args:
            pair: Trading pair
            entry_price: Entry price
            is_long: True for long, False for short
            historical_data: DataFrame with 'high', 'low', 'close' columns
            atr_multiplier: Multiplier for ATR (default 2.0)
            risk_reward_ratio: Risk/reward ratio (uses default if None)
        
        Returns:
            StopLossTakeProfitLevels
        """
        rr_ratio = risk_reward_ratio or self.default_rr_ratio
        
        # Calculate ATR
        atr = self._calculate_atr(historical_data)
        
        if atr is None or atr == 0:
            self.logger.warning(
                f"Could not calculate ATR for {pair}, falling back to fixed pips"
            )
            return self.calculate_fixed_pips(pair, entry_price, is_long)
        
        # Get pip value for conversion
        pair_info = PAIR_INFO.get(pair)
        if not pair_info:
            raise ValueError(f"Unknown pair: {pair}")
        
        pip_value = pair_info['pip_value']
        
        # Stop loss distance in price
        sl_distance = atr * atr_multiplier
        
        # Calculate levels
        if is_long:
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + (sl_distance * rr_ratio)
        else:
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - (sl_distance * rr_ratio)
        
        # Convert to pips for reporting
        sl_pips = sl_distance / pip_value
        tp_pips = sl_pips * rr_ratio
        
        result = StopLossTakeProfitLevels(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            stop_loss_pips=sl_pips,
            take_profit_pips=tp_pips,
            risk_reward_ratio=rr_ratio,
            method=f"atr_{atr_multiplier}x"
        )
        
        self.logger.debug(
            f"{pair} ({'LONG' if is_long else 'SHORT'}) ATR: "
            f"Entry={entry_price:.5f}, ATR={atr:.5f}, "
            f"SL={stop_loss:.5f} ({sl_pips:.1f} pips), "
            f"TP={take_profit:.5f} ({tp_pips:.1f} pips)"
        )
        
        return result
    
    def _calculate_atr(
        self,
        data: pd.DataFrame,
        period: int = 14
    ) -> Optional[float]:
        """
        Calculate Average True Range.
        
        Args:
            data: DataFrame with 'high', 'low', 'close' columns
            period: ATR period (default 14)
        
        Returns:
            ATR value or None if insufficient data
        """
        if len(data) < period + 1:
            self.logger.warning(f"Insufficient data for ATR calculation (need {period + 1}, got {len(data)})")
            return None
        
        # Calculate True Range
        high_low = data['high'] - data['low']
        high_close = abs(data['high'] - data['close'].shift())
        low_close = abs(data['low'] - data['close'].shift())
        
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        
        # Calculate ATR (simple moving average of TR)
        atr = true_range.rolling(window=period).mean().iloc[-1]
        
        return atr if pd.notna(atr) else None
    
    def calculate_percent_based(
        self,
        pair: str,
        entry_price: float,
        is_long: bool,
        stop_loss_percent: float = 0.02,
        risk_reward_ratio: Optional[float] = None
    ) -> StopLossTakeProfitLevels:
        """
        Calculate SL/TP as percentage of entry price.
        
        Args:
            pair: Trading pair
            entry_price: Entry price
            is_long: True for long, False for short
            stop_loss_percent: Stop loss as decimal (0.02 = 2%)
            risk_reward_ratio: Risk/reward ratio
        
        Returns:
            StopLossTakeProfitLevels
        """
        rr_ratio = risk_reward_ratio or self.default_rr_ratio
        
        sl_distance = entry_price * stop_loss_percent
        
        if is_long:
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + (sl_distance * rr_ratio)
        else:
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - (sl_distance * rr_ratio)
        
        # Convert to pips
        pair_info = PAIR_INFO.get(pair)
        if not pair_info:
            raise ValueError(f"Unknown pair: {pair}")
        
        pip_value = pair_info['pip_value']
        sl_pips = sl_distance / pip_value
        tp_pips = sl_pips * rr_ratio
        
        result = StopLossTakeProfitLevels(
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            stop_loss_pips=sl_pips,
            take_profit_pips=tp_pips,
            risk_reward_ratio=rr_ratio,
            method=f"percent_{stop_loss_percent * 100}%"
        )
        
        return result
    
    def adjust_to_support_resistance(
        self,
        levels: StopLossTakeProfitLevels,
        support_level: Optional[float] = None,
        resistance_level: Optional[float] = None,
        is_long: bool = True
    ) -> StopLossTakeProfitLevels:
        """
        Adjust SL/TP to nearby support/resistance levels.
        
        Args:
            levels: Initial calculated levels
            support_level: Support price level
            resistance_level: Resistance price level
            is_long: Position direction
        
        Returns:
            Adjusted StopLossTakeProfitLevels
        """
        adjusted_sl = levels.stop_loss
        adjusted_tp = levels.take_profit
        
        if is_long:
            # For long: SL below support, TP below resistance
            if support_level and support_level < levels.stop_loss:
                # Place SL slightly below support
                adjusted_sl = support_level * 0.9995  # 0.05% buffer
                self.logger.debug(f"Adjusted SL to support: {adjusted_sl:.5f}")
            
            if resistance_level and resistance_level > levels.entry_price:
                # Place TP slightly below resistance
                adjusted_tp = resistance_level * 0.9995
                self.logger.debug(f"Adjusted TP to resistance: {adjusted_tp:.5f}")
        else:
            # For short: SL above resistance, TP above support
            if resistance_level and resistance_level > levels.stop_loss:
                adjusted_sl = resistance_level * 1.0005  # 0.05% buffer
                self.logger.debug(f"Adjusted SL to resistance: {adjusted_sl:.5f}")
            
            if support_level and support_level < levels.entry_price:
                adjusted_tp = support_level * 1.0005
                self.logger.debug(f"Adjusted TP to support: {adjusted_tp:.5f}")
        
        # Recalculate pips
        pair_info = PAIR_INFO.get(levels.method.split('_')[0], PAIR_INFO.get('EUR_USD'))
        pip_value = pair_info.get('pip_value', 0.0001)
        
        sl_pips = abs(levels.entry_price - adjusted_sl) / pip_value
        tp_pips = abs(levels.entry_price - adjusted_tp) / pip_value
        
        return StopLossTakeProfitLevels(
            entry_price=levels.entry_price,
            stop_loss=adjusted_sl,
            take_profit=adjusted_tp,
            stop_loss_pips=sl_pips,
            take_profit_pips=tp_pips,
            risk_reward_ratio=tp_pips / sl_pips if sl_pips > 0 else levels.risk_reward_ratio,
            method=f"{levels.method}_adjusted"
        )
    
    def validate_levels(
        self,
        pair: str,
        levels: StopLossTakeProfitLevels,
        is_long: bool
    ) -> Tuple[bool, str]:
        """
        Validate that SL/TP levels are reasonable.
        
        Args:
            pair: Trading pair
            levels: Calculated levels
            is_long: Position direction
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check SL is on correct side of entry
        if is_long:
            if levels.stop_loss >= levels.entry_price:
                return False, "Stop loss must be below entry for long positions"
            if levels.take_profit <= levels.entry_price:
                return False, "Take profit must be above entry for long positions"
        else:
            if levels.stop_loss <= levels.entry_price:
                return False, "Stop loss must be above entry for short positions"
            if levels.take_profit >= levels.entry_price:
                return False, "Take profit must be below entry for short positions"
        
        # Check reasonable pip distances
        if levels.stop_loss_pips < 5:
            return False, f"Stop loss too tight: {levels.stop_loss_pips:.1f} pips (min 5)"
        
        if levels.stop_loss_pips > 500:
            return False, f"Stop loss too wide: {levels.stop_loss_pips:.1f} pips (max 500)"
        
        # Check risk/reward ratio
        if levels.risk_reward_ratio < 0.5:
            return False, f"Risk/reward too low: {levels.risk_reward_ratio:.2f} (min 0.5)"
        
        return True, ""
