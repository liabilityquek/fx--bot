"""Risk validator for pre-trade safety checks.

This module validates trades before execution to ensure compliance with
all risk management rules and prevents over-leveraging or excessive exposure.
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from config.settings import settings
from config.pairs import PAIR_INFO


class ValidationResult(Enum):
    """Trade validation results."""
    APPROVED = "approved"
    REJECTED = "rejected"
    WARNING = "warning"


@dataclass
class TradeValidationReport:
    """Comprehensive trade validation report."""
    result: ValidationResult
    approved: bool
    reasons: List[str]
    warnings: List[str]
    checks_passed: Dict[str, bool]
    risk_metrics: Dict[str, float]


class RiskValidator:
    """Validate trades against risk management rules."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize risk validator.
        
        Args:
            logger: Logger instance (optional)
        """
        self.logger = logger or logging.getLogger('risk_validator')
        self.max_risk_per_trade = settings.MAX_RISK_PER_TRADE
        self.max_total_exposure = settings.MAX_TOTAL_EXPOSURE
        self.max_leverage = settings.MAX_LEVERAGE
    
    def validate_trade(
        self,
        pair: str,
        units: int,
        stop_loss_pips: float,
        account_balance: float,
        current_exposure_percent: float,
        open_positions: List[Dict],
        entry_price: Optional[float] = None,
        margin_available: Optional[float] = None
    ) -> TradeValidationReport:
        """
        Validate a trade before execution.
        
        Args:
            pair: Trading pair (e.g., 'EUR_USD')
            units: Position size in units
            stop_loss_pips: Stop loss distance in pips
            account_balance: Current account balance
            current_exposure_percent: Current total exposure percentage
            open_positions: List of current open positions
            entry_price: Entry price (optional, for detailed checks)
            margin_available: Available margin (optional)
        
        Returns:
            TradeValidationReport with validation result
        """
        reasons = []
        warnings = []
        checks_passed = {}
        risk_metrics = {}
        
        # Check 1: Position size reasonable
        check_name = "position_size"
        max_units = (account_balance * self.max_leverage) / (entry_price or 1.0)

        if abs(units) > max_units:
            reasons.append(
                f"Position size too large: {abs(units):,} units > "
                f"max {max_units:,.0f} units (at {self.max_leverage}x leverage)"
            )
            checks_passed[check_name] = False
        else:
            checks_passed[check_name] = True
        
        risk_metrics['position_size_units'] = abs(units)
        risk_metrics['max_units_allowed'] = max_units
        
        # Check 2: Risk per trade
        check_name = "risk_per_trade"
        pair_info = PAIR_INFO.get(pair)
        
        if pair_info:
            pip_value = self._pip_value_usd(pair, pair_info)
            risk_amount = abs(units) * stop_loss_pips * pip_value
            risk_percent = (risk_amount / account_balance) * 100 if account_balance > 0 else 0
            
            max_risk_percent = self.max_risk_per_trade * 100
            
            risk_metrics['risk_amount_usd'] = risk_amount
            risk_metrics['risk_percent'] = risk_percent
            
            if risk_percent > max_risk_percent:
                reasons.append(
                    f"Risk per trade too high: {risk_percent:.2f}% > "
                    f"max {max_risk_percent:.2f}%"
                )
                checks_passed[check_name] = False
            elif risk_percent > max_risk_percent * 0.8:
                warnings.append(
                    f"Risk approaching limit: {risk_percent:.2f}% "
                    f"(max {max_risk_percent:.2f}%)"
                )
                checks_passed[check_name] = True
            else:
                checks_passed[check_name] = True
        else:
            warnings.append(f"Unknown pair {pair}, cannot validate risk accurately")
            checks_passed[check_name] = True  # Allow but warn
        
        # Check 3: Total exposure
        check_name = "total_exposure"
        
        # Estimate margin required for new position — same formula as Check 4
        new_margin_required = (abs(units) * (entry_price or 1.0)) / self.max_leverage
        new_exposure_percent = current_exposure_percent + (
            (new_margin_required / account_balance) * 100 if account_balance > 0 else 0
        )
        
        max_exposure_percent = self.max_total_exposure * 100
        
        risk_metrics['current_exposure_percent'] = current_exposure_percent
        risk_metrics['new_exposure_percent'] = new_exposure_percent
        
        if new_exposure_percent > max_exposure_percent:
            reasons.append(
                f"Total exposure would exceed limit: {new_exposure_percent:.2f}% > "
                f"max {max_exposure_percent:.2f}%"
            )
            checks_passed[check_name] = False
        elif new_exposure_percent > max_exposure_percent * 0.9:
            warnings.append(
                f"Exposure nearing limit: {new_exposure_percent:.2f}% "
                f"(max {max_exposure_percent:.2f}%)"
            )
            checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True
        
        # Check 4: Margin available
        check_name = "margin_available"
        
        if margin_available is not None:
            # Estimate margin required (simplified)
            margin_required = (abs(units) * (entry_price or 1.0)) / self.max_leverage
            
            risk_metrics['margin_required'] = margin_required
            risk_metrics['margin_available'] = margin_available
            
            if margin_required > margin_available:
                reasons.append(
                    f"Insufficient margin: need ${margin_required:,.2f}, "
                    f"available ${margin_available:,.2f}"
                )
                checks_passed[check_name] = False
            elif margin_required > margin_available * 0.9:
                warnings.append(
                    f"Margin usage high: {(margin_required / margin_available * 100):.1f}%"
                )
                checks_passed[check_name] = True
            else:
                checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True  # Skip if not provided
        
        # Check 5: Stop loss reasonable
        check_name = "stop_loss"
        
        if stop_loss_pips < 5:
            reasons.append(f"Stop loss too tight: {stop_loss_pips:.1f} pips (min 5)")
            checks_passed[check_name] = False
        elif stop_loss_pips > 500:
            warnings.append(f"Stop loss very wide: {stop_loss_pips:.1f} pips")
            checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True
        
        risk_metrics['stop_loss_pips'] = stop_loss_pips
        
        # Check 6: Account balance positive
        check_name = "account_balance"
        
        if account_balance <= 0:
            reasons.append(f"Account balance insufficient: ${account_balance:,.2f}")
            checks_passed[check_name] = False
        elif account_balance < 100:
            warnings.append(f"Account balance low: ${account_balance:,.2f}")
            checks_passed[check_name] = True
        else:
            checks_passed[check_name] = True
        
        risk_metrics['account_balance'] = account_balance
        
        # Determine final result
        all_checks_passed = all(checks_passed.values())
        
        if all_checks_passed:
            if warnings:
                result = ValidationResult.WARNING
            else:
                result = ValidationResult.APPROVED
            approved = True
        else:
            result = ValidationResult.REJECTED
            approved = False
        
        # Build report
        report = TradeValidationReport(
            result=result,
            approved=approved,
            reasons=reasons,
            warnings=warnings,
            checks_passed=checks_passed,
            risk_metrics=risk_metrics
        )
        
        # Log result
        if approved:
            self.logger.info(
                f"✅ Trade validation PASSED for {pair}: "
                f"{abs(units):,} units, SL={stop_loss_pips:.1f} pips"
            )
            if warnings:
                for warning in warnings:
                    self.logger.warning(f"⚠️  {warning}")
        else:
            self.logger.error(
                f"❌ Trade validation FAILED for {pair}: {', '.join(reasons)}"
            )
        
        return report
    
    def get_validation_summary(self, report: TradeValidationReport) -> str:
        """
        Get human-readable validation summary.
        
        Args:
            report: Trade validation report
        
        Returns:
            Formatted summary string
        """
        lines = [
            f"\n🔍 Trade Validation Report",
            f"  Result: {report.result.value.upper()}",
            f"  Approved: {'✅ YES' if report.approved else '❌ NO'}",
        ]
        
        if report.reasons:
            lines.append(f"\n  ❌ Rejection Reasons:")
            for reason in report.reasons:
                lines.append(f"    • {reason}")
        
        if report.warnings:
            lines.append(f"\n  ⚠️  Warnings:")
            for warning in report.warnings:
                lines.append(f"    • {warning}")
        
        lines.append(f"\n  Checks:")
        for check, passed in report.checks_passed.items():
            status = "✅" if passed else "❌"
            lines.append(f"    {status} {check.replace('_', ' ').title()}")
        
        if report.risk_metrics:
            lines.append(f"\n  Risk Metrics:")
            for metric, value in report.risk_metrics.items():
                if isinstance(value, float):
                    lines.append(f"    {metric.replace('_', ' ').title()}: {value:,.2f}")
                else:
                    lines.append(f"    {metric.replace('_', ' ').title()}: {value:,}")
        
        return "\n".join(lines)
    
    def _pip_value_usd(self, pair: str, pair_info: dict) -> float:
        """Return pip value converted to USD for accurate risk calculation."""
        pv = pair_info['pip_value']
        quote = pair_info.get('quote_currency', '')
        if quote == 'USD':
            return pv  # EUR_USD, GBP_USD, AUD_USD — already in USD
        if pair == 'USD_JPY':
            return pv / 150.0  # convert JPY → USD (approx)
        if pair == 'USD_CHF':
            return pv / 0.9    # convert CHF → USD (approx)
        return pv  # fallback for unknown quote currencies

    def validate_multiple_trades(
        self,
        trades: List[Dict],
        account_balance: float,
        current_exposure_percent: float,
        open_positions: List[Dict]
    ) -> Dict[str, TradeValidationReport]:
        """
        Validate multiple trades at once.
        
        Args:
            trades: List of trade dicts with 'pair', 'units', 'stop_loss_pips'
            account_balance: Current account balance
            current_exposure_percent: Current exposure
            open_positions: Current open positions
        
        Returns:
            Dict mapping trade id/index to validation report
        """
        results = {}
        
        cumulative_exposure = current_exposure_percent
        
        for i, trade in enumerate(trades):
            trade_id = trade.get('id', f"trade_{i}")
            
            report = self.validate_trade(
                pair=trade['pair'],
                units=trade['units'],
                stop_loss_pips=trade['stop_loss_pips'],
                account_balance=account_balance,
                current_exposure_percent=cumulative_exposure,
                open_positions=open_positions,
                entry_price=trade.get('entry_price'),
                margin_available=trade.get('margin_available')
            )
            
            results[trade_id] = report
            
            # Update cumulative exposure for next trade
            if report.approved:
                cumulative_exposure = report.risk_metrics.get(
                    'new_exposure_percent',
                    cumulative_exposure
                )
        
        return results
