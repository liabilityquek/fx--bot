"""Risk management module for FX trading bot.

This module provides comprehensive risk management tools including:
- Position sizing based on account balance and risk parameters
- Exposure tracking across all positions
- Dynamic stop-loss and take-profit calculation
- Pre-trade validation against risk limits
- Trade conflict detection and prevention
- Emergency shutdown and account protection
"""

from .position_sizer import (
    PositionSizer,
    PositionSizingMethod,
    PositionSizeResult
)
from .exposure_tracker import (
    ExposureTracker,
    ExposureReport,
    CurrencyExposure
)
from .sl_tp_calculator import (
    StopLossTakeProfitCalculator,
    StopLossTakeProfitLevels,
    StopLossMethod
)
from .risk_validator import (
    RiskValidator,
    TradeValidationReport,
    ValidationResult
)
from .conflict_checker import (
    TradeConflictChecker,
    ConflictCheckResult,
    ConflictType
)
from .emergency_controller import (
    EmergencyRiskController,
    EmergencyStatus,
    EmergencyLevel,
    ShutdownReason,
    ShutdownReport
)
from .kill_switch import KillSwitch

__all__ = [
    # Position Sizer
    'PositionSizer',
    'PositionSizingMethod',
    'PositionSizeResult',
    
    # Exposure Tracker
    'ExposureTracker',
    'ExposureReport',
    'CurrencyExposure',
    
    # SL/TP Calculator
    'StopLossTakeProfitCalculator',
    'StopLossTakeProfitLevels',
    'StopLossMethod',
    
    # Risk Validator
    'RiskValidator',
    'TradeValidationReport',
    'ValidationResult',
    
    # Conflict Checker
    'TradeConflictChecker',
    'ConflictCheckResult',
    'ConflictType',
    
    # Emergency Controller
    'EmergencyRiskController',
    'EmergencyStatus',
    'EmergencyLevel',
    'ShutdownReason',
    'ShutdownReport',

    # Kill Switch
    'KillSwitch',
]
