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
    # GROQ / LLM AGENT
    # ==========================================
    GROQ_API_KEY: str = os.getenv('GROQ_API_KEY', '')
    LLM_MODEL: str = os.getenv('LLM_MODEL', 'llama-3.3-70b-versatile')
    LLM_AGENT_WEIGHT: float = float(os.getenv('LLM_AGENT_WEIGHT', '1.5'))

    # Anthropic fallback (used when Groq credits are exhausted)
    ANTHROPIC_API_KEY: str = os.getenv('ANTHROPIC_API_KEY', '')
    ANTHROPIC_LLM_MODEL: str = os.getenv('ANTHROPIC_LLM_MODEL', 'claude-haiku-4-5-20251001')

    # Reviewer model — Groq small model (fast, sufficient for review task)
    # Anthropic fallback uses ANTHROPIC_LLM_MODEL above
    REVIEWER_LLM_MODEL: str = os.getenv('REVIEWER_LLM_MODEL', 'llama-3.1-8b-instant')

    # ==========================================
    # VOTING ENGINE
    # ==========================================
    CONSENSUS_THRESHOLD: float = float(os.getenv('CONSENSUS_THRESHOLD', '0.60'))
    CANDLE_COUNT: int = int(os.getenv('CANDLE_COUNT', '100'))

    # ==========================================
    # RISK GUARDRAILS
    # ==========================================
    MAX_DAILY_DRAWDOWN: float = float(os.getenv('MAX_DAILY_DRAWDOWN', '0.05'))
    MAX_CONSECUTIVE_LOSSES: int = int(os.getenv('MAX_CONSECUTIVE_LOSSES', '5'))
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
    TRADING_PAIRS: List[str] = os.getenv(
        'TRADING_PAIRS',
        'EUR_USD,GBP_USD,USD_JPY,USD_CHF,AUD_USD'
    ).split(',')

    TIMEFRAME: str = os.getenv('TIMEFRAME', 'H1')
    INITIAL_CAPITAL: float = float(os.getenv('INITIAL_CAPITAL', '10000'))
    MAX_LEVERAGE: int = int(os.getenv('MAX_LEVERAGE', '30'))

    # Execution interval derived from timeframe (seconds)
    EXECUTION_INTERVAL_SECONDS: int = int(os.getenv('EXECUTION_INTERVAL_SECONDS', '3600'))

    # Risk Management
    MAX_RISK_PER_TRADE: float = float(os.getenv('MAX_RISK_PER_TRADE', '0.02'))
    MAX_TOTAL_EXPOSURE: float = float(os.getenv('MAX_TOTAL_EXPOSURE', '0.10'))
    DEFAULT_STOP_LOSS_PIPS: int = int(os.getenv('DEFAULT_STOP_LOSS_PIPS', '50'))
    DEFAULT_TAKE_PROFIT_RATIO: float = float(os.getenv('DEFAULT_TAKE_PROFIT_RATIO', '2.0'))

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
    NEWS_SUSPEND_BEFORE_MINUTES: int = int(os.getenv('NEWS_SUSPEND_BEFORE_MINUTES', '30'))
    NEWS_RESUME_AFTER_MINUTES: int = int(os.getenv('NEWS_RESUME_AFTER_MINUTES', '15'))
    EVENT_CACHE_TTL_HOURS: int = int(os.getenv('EVENT_CACHE_TTL_HOURS', '1'))
    HIGH_IMPACT_EVENTS: List[str] = os.getenv(
        'HIGH_IMPACT_EVENTS',
        'NFP,FOMC,GDP,CPI,Interest Rate,Central Bank'
    ).split(',')

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

        if not cls.GROQ_API_KEY:
            errors.append("GROQ_API_KEY is required (LLM agent will fall back to HOLD without it)")

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
            # ANTHROPIC_API_KEY missing is non-fatal — LLM agent has a fallback
            fatal = [e for e in errors if 'GROQ_API_KEY' not in e]
            return len(fatal) == 0

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
        print(f"  Consensus Threshold: {cls.CONSENSUS_THRESHOLD}")
        print(f"  LLM Model: {cls.LLM_MODEL}")
        print(f"  Max Risk per Trade: {cls.MAX_RISK_PER_TRADE*100}%")
        print(f"  Max Total Exposure: {cls.MAX_TOTAL_EXPOSURE*100}%")
        print(f"  Alerts Enabled: {cls.ALERT_ENABLED}")
        print(f"  Log Level: {cls.LOG_LEVEL}\n")


# Create singleton instance
settings = Settings()
