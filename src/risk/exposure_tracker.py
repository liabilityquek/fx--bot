"""Exposure tracker for monitoring aggregate risk across all positions.

This module tracks total exposure across all open positions to ensure
compliance with risk limits (max 10% total exposure).
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from config.settings import settings
from src.broker.base import Position


@dataclass
class CurrencyExposure:
    """Exposure data for a single currency."""
    currency: str
    long_units: float
    short_units: float
    net_units: float
    net_percent: float  # Percentage of account
    long_value_usd: float
    short_value_usd: float
    net_value_usd: float


@dataclass
class ExposureReport:
    """Comprehensive exposure report."""
    total_exposure_percent: float
    currency_exposures: Dict[str, CurrencyExposure]
    total_long_value: float
    total_short_value: float
    total_net_value: float
    open_positions_count: int
    under_limit: bool
    limit_percent: float


class ExposureTracker:
    """Track and monitor aggregate exposure across all positions."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize exposure tracker.
        
        Args:
            logger: Logger instance (optional)
        """
        self.logger = logger or logging.getLogger('exposure_tracker')
        self.max_total_exposure = settings.MAX_TOTAL_EXPOSURE
        
        # Track current state
        self._current_positions: List[Dict] = []
        self._current_balance: float = 0.0
        self._last_report: Optional[ExposureReport] = None
    
    def update_positions(
        self,
        positions: List[Position],
        account_balance: float
    ) -> ExposureReport:
        """
        Update tracked positions and recalculate exposure.
        
        Args:
            positions: Current open positions from broker
            account_balance: Current account balance
        
        Returns:
            Updated ExposureReport
        """
        self._current_positions = positions or []
        self._current_balance = account_balance
        
        # Calculate and cache the exposure report
        self._last_report = self.calculate_exposure(
            open_positions=self._current_positions,
            account_balance=self._current_balance
        )
        
        # Log summary
        if self._last_report.open_positions_count > 0:
            self.logger.debug(
                f"Exposure: {self._last_report.total_exposure_percent:.1%} "
                f"({self._last_report.open_positions_count} positions)"
            )
        
        return self._last_report
    
    def get_current_exposure(self) -> Optional[ExposureReport]:
        """Get the last calculated exposure report."""
        return self._last_report
    
    def calculate_exposure(
        self,
        open_positions: List[Position],
        account_balance: float,
        current_prices: Optional[Dict[str, float]] = None
    ) -> ExposureReport:
        """
        Calculate total exposure across all open positions.
        
        Args:
            open_positions: List of open positions from broker
            account_balance: Current account balance
            current_prices: Dict of pair -> current price (optional, for accurate valuation)
        
        Returns:
            ExposureReport with detailed exposure breakdown
        """
        # Initialize currency tracking
        currency_data = {}
        
        total_long_value = 0.0
        total_short_value = 0.0
        
        # Process each position
        for position in open_positions:
            pair = position.pair.replace('-', '_')
            units = float(position.net_units)

            # Determine direction
            is_long = units > 0
            abs_units = abs(units)

            # Get currencies from pair
            if '_' in pair:
                base_currency, quote_currency = pair.split('_')
            else:
                self.logger.warning(f"Invalid pair format: {pair}")
                continue

            # Get current price for valuation
            current_price = current_prices.get(pair) if current_prices else None
            
            # Calculate USD value of position
            if quote_currency == 'USD':
                # EUR_USD, GBP_USD, AUD_USD: units are in base currency, convert to USD
                value_usd = abs_units * (current_price if current_price else 1.0)
            elif base_currency == 'USD':
                # USD_JPY, USD_CHF: units are in USD — 1 unit = 1 USD, no conversion needed
                value_usd = abs_units
            else:
                # Cross pairs not currently traded — rough fallback
                value_usd = abs_units * 0.0001
            
            # Track base currency exposure
            if base_currency not in currency_data:
                currency_data[base_currency] = {
                    'long_units': 0.0,
                    'short_units': 0.0,
                    'long_value_usd': 0.0,
                    'short_value_usd': 0.0
                }
            
            if is_long:
                currency_data[base_currency]['long_units'] += abs_units
                currency_data[base_currency]['long_value_usd'] += value_usd
                total_long_value += value_usd
            else:
                currency_data[base_currency]['short_units'] += abs_units
                currency_data[base_currency]['short_value_usd'] += value_usd
                total_short_value += value_usd
            
            # Track quote currency exposure (opposite direction)
            if quote_currency not in currency_data:
                currency_data[quote_currency] = {
                    'long_units': 0.0,
                    'short_units': 0.0,
                    'long_value_usd': 0.0,
                    'short_value_usd': 0.0
                }
            
            # Quote currency has opposite exposure
            if is_long:
                currency_data[quote_currency]['short_units'] += abs_units
                currency_data[quote_currency]['short_value_usd'] += value_usd
            else:
                currency_data[quote_currency]['long_units'] += abs_units
                currency_data[quote_currency]['long_value_usd'] += value_usd
        
        # Build currency exposure objects
        currency_exposures = {}
        
        for currency, data in currency_data.items():
            net_units = data['long_units'] - data['short_units']
            net_value_usd = data['long_value_usd'] - data['short_value_usd']
            net_percent = (abs(net_value_usd) / account_balance) * 100 if account_balance > 0 else 0
            
            currency_exposures[currency] = CurrencyExposure(
                currency=currency,
                long_units=data['long_units'],
                short_units=data['short_units'],
                net_units=net_units,
                net_percent=net_percent,
                long_value_usd=data['long_value_usd'],
                short_value_usd=data['short_value_usd'],
                net_value_usd=net_value_usd
            )
        
        # Calculate total exposure
        total_net_value = total_long_value - total_short_value
        total_exposure_percent = (abs(total_net_value) / account_balance) * 100 if account_balance > 0 else 0
        
        under_limit = total_exposure_percent <= (self.max_total_exposure * 100)
        
        report = ExposureReport(
            total_exposure_percent=total_exposure_percent,
            currency_exposures=currency_exposures,
            total_long_value=total_long_value,
            total_short_value=total_short_value,
            total_net_value=total_net_value,
            open_positions_count=len(open_positions),
            under_limit=under_limit,
            limit_percent=self.max_total_exposure * 100
        )
        
        self.logger.debug(
            f"Exposure: {total_exposure_percent:.2f}% "
            f"(limit: {self.max_total_exposure * 100}%)"
        )
        
        return report
    
    def check_new_position_exposure(
        self,
        pair: str,
        units: int,
        current_exposure_report: ExposureReport,
        account_balance: float,
        current_price: Optional[float] = None
    ) -> Tuple[bool, str]:
        """
        Check if adding a new position would exceed exposure limits.
        
        Args:
            pair: Trading pair (e.g., 'EUR_USD')
            units: Position size in units (positive = long, negative = short)
            current_exposure_report: Current exposure report
            account_balance: Current account balance
            current_price: Current price for the pair
        
        Returns:
            Tuple of (is_allowed, reason_if_not_allowed)
        """
        # Calculate value of new position
        abs_units = abs(units)
        
        # Extract currencies
        if '_' in pair:
            base_currency, quote_currency = pair.split('_')
        else:
            return False, f"Invalid pair format: {pair}"
        
        # Estimate value in USD
        if quote_currency == 'USD':
            new_position_value = abs_units * (current_price if current_price else 1.0)
        else:
            new_position_value = abs_units * 0.0001  # Rough estimate
        
        # Calculate new total exposure
        new_total_value = abs(current_exposure_report.total_net_value + 
                             (new_position_value if units > 0 else -new_position_value))
        
        new_exposure_percent = (new_total_value / account_balance) * 100 if account_balance > 0 else 0
        
        limit_percent = self.max_total_exposure * 100
        
        if new_exposure_percent > limit_percent:
            return False, (
                f"New position would exceed exposure limit: "
                f"{new_exposure_percent:.2f}% > {limit_percent:.2f}%"
            )
        
        self.logger.debug(
            f"New position check passed: {new_exposure_percent:.2f}% <= {limit_percent:.2f}%"
        )
        
        return True, ""
    
    def get_currency_exposure_summary(
        self,
        exposure_report: ExposureReport
    ) -> str:
        """
        Get human-readable exposure summary.
        
        Args:
            exposure_report: Exposure report
        
        Returns:
            Formatted summary string
        """
        lines = [
            f"\n📊 Exposure Summary:",
            f"  Total Exposure: {exposure_report.total_exposure_percent:.2f}% "
            f"(Limit: {exposure_report.limit_percent:.2f}%)",
            f"  Status: {'✅ Under Limit' if exposure_report.under_limit else '❌ OVER LIMIT'}",
            f"  Open Positions: {exposure_report.open_positions_count}",
            f"\n  Currency Breakdown:"
        ]
        
        for currency, exp in sorted(
            exposure_report.currency_exposures.items(),
            key=lambda x: abs(x[1].net_percent),
            reverse=True
        ):
            direction = "LONG" if exp.net_units > 0 else "SHORT"
            lines.append(
                f"    {currency}: {direction} {abs(exp.net_percent):.2f}% "
                f"(${abs(exp.net_value_usd):,.2f})"
            )
        
        return "\n".join(lines)
    
    def get_available_exposure(
        self,
        current_exposure_report: ExposureReport,
        account_balance: float
    ) -> float:
        """
        Calculate remaining available exposure percentage.
        
        Args:
            current_exposure_report: Current exposure report
            account_balance: Current account balance
        
        Returns:
            Available exposure as percentage
        """
        limit_percent = self.max_total_exposure * 100
        used_percent = current_exposure_report.total_exposure_percent
        
        available = max(0, limit_percent - used_percent)
        
        return available
