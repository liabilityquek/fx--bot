"""Settings management using environment variables."""

import os
import re
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path=env_path)


class Settings:
    """Centralized configuration from environment variables."""

    # ==========================================
    # BROKER: OANDA
    # ==========================================
    OANDA_API_KEY: str = os.getenv('OANDA_API_KEY')
    OANDA_ACCOUNT_ID: str = os.getenv('OANDA_ACCOUNT_ID')
    OANDA_ENVIRONMENT: str = os.getenv('OANDA_ENVIRONMENT', 'practice')

    @property
    def oanda_api_url(self) -> str:
        """Get OANDA API URL based on environment."""
        if self.OANDA_ENVIRONMENT == 'live':
            return 'https://api-fxtrade.oanda.com'
        return 'https://api-fxpractice.oanda.com'

    @property
    def oanda_stream_url(self) -> str:
        """Get OANDA streaming URL based on environment."""
        if self.OANDA_ENVIRONMENT == 'live':
            return 'https://stream-fxtrade.oanda.com'
        return 'https://stream-fxpractice.oanda.com'

    # ==========================================
    # DECISION ENGINE (deterministic technical confluence)
    # ==========================================
    H1_CANDLE_COUNT: int = int(os.getenv('H1_CANDLE_COUNT', '100'))
    M15_CANDLE_COUNT: int = int(os.getenv('M15_CANDLE_COUNT', '100'))
    H4_CANDLE_COUNT: int = int(os.getenv('H4_CANDLE_COUNT', '60'))
    D1_CANDLE_COUNT: int = int(os.getenv('D1_CANDLE_COUNT', '60'))

    # ==========================================
    # RISK GUARDRAILS
    # ==========================================
    MAX_DAILY_DRAWDOWN: float = float(os.getenv('MAX_DAILY_DRAWDOWN', '0.05'))
    MAX_CONSECUTIVE_LOSSES: int = int(os.getenv('MAX_CONSECUTIVE_LOSSES', '5'))
    MAX_USD_CORRELATED_TRADES: int = int(os.getenv('MAX_USD_CORRELATED_TRADES', '2'))
    CIRCUIT_BREAKER_COOLDOWN_MINUTES: int = int(os.getenv('CIRCUIT_BREAKER_COOLDOWN_MINUTES', '60'))
    MAX_ORDERS_PER_MINUTE: int = int(os.getenv('MAX_ORDERS_PER_MINUTE', '10'))

    # Weekend gap protection
    WEEKEND_BLOCK_FRIDAY_UTC_HOUR: int = int(os.getenv('WEEKEND_BLOCK_FRIDAY_UTC_HOUR', '19'))
    WEEKEND_RESUME_SUNDAY_UTC_HOUR: int = int(os.getenv('WEEKEND_RESUME_SUNDAY_UTC_HOUR', '22'))
    WEEKEND_MIN_SL_BUFFER_PIPS: float = float(os.getenv('WEEKEND_MIN_SL_BUFFER_PIPS', '20.0'))
    MAX_DAILY_LOSS_PERCENT: float = float(os.getenv('MAX_DAILY_LOSS_PERCENT', '0.06'))

    # ==========================================
    # TRADING PARAMETERS
    # ==========================================
    TRADING_PAIRS: List[str] = [
        p.strip() for p in os.getenv(
            'TRADING_PAIRS',
            'EUR_USD,GBP_USD,USD_JPY,USD_CAD,AUD_USD'
        ).split(',')
    ]

    TIMEFRAME: str = os.getenv('TIMEFRAME', 'H1')
    INITIAL_CAPITAL: float = float(os.getenv('INITIAL_CAPITAL', '10000'))
    MAX_LEVERAGE: int = int(os.getenv('MAX_LEVERAGE', '20'))

    # Execution interval derived from timeframe (seconds)
    EXECUTION_INTERVAL_SECONDS: int = int(os.getenv('EXECUTION_INTERVAL_SECONDS', '3600'))

    # Monitoring interval for high-frequency checks (trailing stops, risk, close detection)
    MONITORING_INTERVAL_SECONDS: int = int(os.getenv('MONITORING_INTERVAL_SECONDS', '60'))

    # Risk Management
    MAX_RISK_PER_TRADE: float = float(os.getenv('MAX_RISK_PER_TRADE', '0.02'))
    MAX_TOTAL_EXPOSURE: float = float(os.getenv('MAX_TOTAL_EXPOSURE', '0.80'))
    DEFAULT_STOP_LOSS_PIPS: int = int(os.getenv('DEFAULT_STOP_LOSS_PIPS', '50'))
    DEFAULT_TAKE_PROFIT_RATIO: float = float(os.getenv('DEFAULT_TAKE_PROFIT_RATIO', '2.0'))

    # Trade quality filters (Phase 1)
    MIN_CONFLUENCES: int = int(os.getenv('MIN_CONFLUENCES', '3'))
    # Optional 7th confluence: +DI/-DI directional cross (long when +DI>-DI). Off by default.
    DI_CONFLUENCE_ENABLED: bool = os.getenv('DI_CONFLUENCE_ENABLED', 'false').lower() == 'true'
    # TP is constructed as SL distance x DEFAULT_TAKE_PROFIT_RATIO, so the RR gate
    # default must not exceed that ratio or every signal is rejected.
    MIN_RR_RATIO: float = float(os.getenv('MIN_RR_RATIO', '2.0'))

    # Entry quality gates (Phase 2)
    # Skip entries when the live spread exceeds this many pips — bad fills kill expectancy.
    MAX_SPREAD_PIPS: float = float(os.getenv('MAX_SPREAD_PIPS', '3.0'))
    # Only open new trades during high-liquidity hours (London/NY overlap window, UTC).
    # Avoids the rollover spread spike (~21:00 UTC) and thin Asian-session chop.
    SESSION_FILTER_ENABLED: bool = os.getenv('SESSION_FILTER_ENABLED', 'true').lower() == 'true'
    SESSION_START_UTC_HOUR: int = int(os.getenv('SESSION_START_UTC_HOUR', '6'))
    SESSION_END_UTC_HOUR: int = int(os.getenv('SESSION_END_UTC_HOUR', '20'))
    # Hard gate: H4 EMA20/50 trend must agree with the trade direction.
    HTF_ALIGNMENT_ENABLED: bool = os.getenv('HTF_ALIGNMENT_ENABLED', 'true').lower() == 'true'
    # Block re-entry on a pair for this many hours after a losing close.
    LOSS_COOLDOWN_HOURS: float = float(os.getenv('LOSS_COOLDOWN_HOURS', '4.0'))
    # After this many consecutive losses, risk per trade is halved.
    # MAX_CONSECUTIVE_LOSSES (above) halts new trades for the rest of the day.
    CONSECUTIVE_LOSS_RISK_REDUCTION_AFTER: int = int(
        os.getenv('CONSECUTIVE_LOSS_RISK_REDUCTION_AFTER', '3')
    )

    # Trailing stop
    TRAILING_STOP_ACTIVATION_PIPS: float = float(os.getenv('TRAILING_STOP_ACTIVATION_PIPS', '15.0'))
    TRAILING_STOP_DISTANCE_PIPS: float = float(os.getenv('TRAILING_STOP_DISTANCE_PIPS', '8.0'))
    # Trail only after price has moved this fraction of the initial SL distance (R-based).
    # Falls back to TRAILING_STOP_ACTIVATION_PIPS when the initial SL is unknown.
    TRAILING_STOP_ACTIVATION_R: float = float(os.getenv('TRAILING_STOP_ACTIVATION_R', '1.0'))
    TRAILING_ATR_MULTIPLIER: float = float(os.getenv('TRAILING_ATR_MULTIPLIER', '1.5'))
    # Minimum pip movement required before re-issuing a trailing SL to the broker.
    # Prevents float-drift from generating redundant cancel+replace cycles on OANDA.
    TRAILING_STOP_MIN_UPDATE_PIPS: float = float(os.getenv('TRAILING_STOP_MIN_UPDATE_PIPS', '1.0'))

    # Break-even stop
    BREAK_EVEN_ACTIVATION_PIPS: float = float(os.getenv('BREAK_EVEN_ACTIVATION_PIPS', '5.0'))
    BREAK_EVEN_BUFFER_PIPS: float = float(os.getenv('BREAK_EVEN_BUFFER_PIPS', '1.0'))
    # Move SL to break-even after price has moved this fraction of the initial SL distance.
    # Falls back to BREAK_EVEN_ACTIVATION_PIPS when the initial SL is unknown.
    BREAK_EVEN_TRIGGER_R: float = float(os.getenv('BREAK_EVEN_TRIGGER_R', '0.5'))

    # Time stop — close trades still under water after this many market hours
    TIME_STOP_ENABLED: bool = os.getenv('TIME_STOP_ENABLED', 'true').lower() == 'true'
    TIME_STOP_HOURS: float = float(os.getenv('TIME_STOP_HOURS', '48.0'))

    # Partial take-profits
    PARTIAL_TP_ENABLED: bool = os.getenv('PARTIAL_TP_ENABLED', 'true').lower() == 'true'
    PARTIAL_TP_RATIO: float = float(os.getenv('PARTIAL_TP_RATIO', '0.5'))
    PARTIAL_TP_RR_TARGET: float = float(os.getenv('PARTIAL_TP_RR_TARGET', '1.0'))

    # ==========================================
    # MONITORING & ALERTS (Telegram)
    # ==========================================
    ALERT_ENABLED: bool = os.getenv('ALERT_ENABLED', 'false').lower() == 'true'
    TELEGRAM_BOT_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '') or os.getenv('TELEGRAM_BOT', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')

    # ==========================================
    # LOGGING
    # ==========================================
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_TO_FILE: bool = os.getenv('LOG_TO_FILE', 'true').lower() == 'true'
    LOG_FILE_PATH: str = os.getenv('LOG_FILE_PATH', 'logs/trading_bot.log')

    # ==========================================
    # ECONOMIC CALENDAR
    # ==========================================
    JB_NEWS_API_KEY: str = os.getenv('JB_NEWS_API_KEY', '')
    FRED_API_KEY: str = os.getenv('FRED_API_KEY', '')   # free from fred.stlouisfed.org
    # Rule 1 & 2 — suspension window
    NEWS_SUSPEND_BEFORE_MINUTES: int = int(os.getenv('NEWS_SUSPEND_BEFORE_MINUTES', '30'))
    NEWS_RESUME_AFTER_MINUTES: int = int(os.getenv('NEWS_RESUME_AFTER_MINUTES', '30'))
    EVENT_CACHE_TTL_HOURS: int = int(os.getenv('EVENT_CACHE_TTL_HOURS', '1'))
    # Rule 3 — deterministic pre-event close: profit below this R-multiple → close
    NEWS_RISK_MIN_R: float = float(os.getenv('NEWS_RISK_MIN_R', '1.0'))
    NEWS_RISK_MINUTES_BEFORE: int = int(os.getenv('NEWS_RISK_MINUTES_BEFORE', '20'))
    NEWS_RISK_POLL_INTERVAL_SECONDS: int = int(os.getenv('NEWS_RISK_POLL_INTERVAL_SECONDS', '120'))
    HIGH_IMPACT_EVENTS: List[str] = os.getenv(
        'HIGH_IMPACT_EVENTS',
        'NFP,FOMC,GDP,CPI,Interest Rate,Central Bank'
    ).split(',')

    # ==========================================
    # CENTRAL BANK RATES
    # ==========================================
    CB_RATE_USD: float = float(os.getenv('CB_RATE_USD', '4.50'))
    CB_RATE_EUR: float = float(os.getenv('CB_RATE_EUR', '2.40'))
    CB_RATE_GBP: float = float(os.getenv('CB_RATE_GBP', '4.50'))
    CB_RATE_JPY: float = float(os.getenv('CB_RATE_JPY', '0.50'))
    CB_RATE_CAD: float = float(os.getenv('CB_RATE_CAD', '3.75'))
    CB_RATE_AUD: float = float(os.getenv('CB_RATE_AUD', '4.10'))

    # ==========================================
    # SYSTEM
    # ==========================================
    PAPER_TRADING_MODE: bool = os.getenv('PAPER_TRADING_MODE', 'true').lower() == 'true'
    DATA_CACHE_HOURS: int = int(os.getenv('DATA_CACHE_HOURS', '24'))

    @classmethod
    def validate(cls) -> bool:
        """Validate that required settings are present."""
        errors = []
        pair_pattern = re.compile(r'^[A-Z]{3}_[A-Z]{3}$')

        if not cls.OANDA_API_KEY:
            errors.append("OANDA_API_KEY is required")

        if not cls.OANDA_ACCOUNT_ID:
            errors.append("OANDA_ACCOUNT_ID is required")

        # Validate each trading pair name
        for pair in cls.TRADING_PAIRS:
            if not pair_pattern.match(pair.strip()):
                errors.append(f"Invalid trading pair format: '{pair}' (expected format: AAA_BBB)")

        if cls.ALERT_ENABLED and (not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID):
            errors.append("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required when ALERT_ENABLED is true")

        if errors:
            print("Configuration Errors:")
            for error in errors:
                print(f"  - {error}")
            return False

        return True

    @classmethod
    def display(cls):
        """Display current configuration (without sensitive data). Only runs in DEBUG mode."""
        if os.getenv('DEBUG', '').lower() not in ('1', 'true', 'yes'):
            return
        print("\nCurrent Configuration:")
        print(f"  Environment: {cls.OANDA_ENVIRONMENT}")
        print(f"  Paper Trading: {cls.PAPER_TRADING_MODE}")
        print(f"  Timeframe: {cls.TIMEFRAME}")
        print(f"  Min Confluences: {cls.MIN_CONFLUENCES}")
        print(f"  Max Risk per Trade: {cls.MAX_RISK_PER_TRADE*100}%")
        print(f"  Max Total Exposure: {cls.MAX_TOTAL_EXPOSURE*100}%")
        print(f"  Alerts Enabled: {cls.ALERT_ENABLED}")
        print(f"  Log Level: {cls.LOG_LEVEL}\n")


# Create singleton instance
settings = Settings()
