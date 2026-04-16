"""Logging configuration and utilities."""

import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional
import colorlog

from config.settings import settings


def setup_logger(
    name: str = 'fx_trading_bot',
    log_level: Optional[str] = None,
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    Set up a logger with console and optional file handlers.
    
    Args:
        name: Logger name
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file (if None, uses settings)
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    # Clear existing handlers
    logger.handlers = []
    
    # Set log level
    level = log_level or settings.LOG_LEVEL
    logger.setLevel(getattr(logging, level.upper()))
    
    # Console handler with color
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    
    console_format = colorlog.ColoredFormatter(
        '%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler (if enabled)
    if settings.LOG_TO_FILE:
        log_path = Path(log_file or settings.LOG_FILE_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        file_format = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get or create a logger instance.
    
    Args:
        name: Logger name
    
    Returns:
        Logger instance
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger


class TradeLogger:
    """Specialized logger for trade execution audit trail."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize trade logger.
        
        Args:
            logger: Base logger instance (creates new if None)
        """
        self.logger = logger or get_logger('trade_audit')
        
        # Create separate audit log file
        audit_path = Path('logs') / 'trade_audit.log'
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        
        audit_handler = logging.FileHandler(audit_path, encoding='utf-8')
        audit_handler.setLevel(logging.INFO)
        
        audit_format = logging.Formatter(
            '%(asctime)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        audit_handler.setFormatter(audit_format)
        self.logger.addHandler(audit_handler)
    
    def log_decision(
        self,
        pair: str,
        action: str,
        reason: str,
        data: Optional[dict] = None
    ):
        """
        Log a trading decision with full context.
        
        Args:
            pair: Trading pair
            action: Action taken (OPEN, CLOSE, SKIP, SUSPEND)
            reason: Explanation for the decision
            data: Additional context data
        """
        timestamp = datetime.now().isoformat()
        message = f"{pair} | {action} | {reason}"
        
        if data:
            message += f" | {data}"
        
        self.logger.info(message)
    
    def log_trade_execution(
        self,
        pair: str,
        side: str,
        units: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        trade_id: Optional[str] = None
    ):
        """
        Log a trade execution with all parameters.
        
        Args:
            pair: Trading pair
            side: BUY or SELL
            units: Position size in units
            entry_price: Entry price
            stop_loss: Stop loss price
            take_profit: Take profit price
            trade_id: Broker's trade ID
        """
        message = (
            f"{pair} | EXECUTE | {side} {units} units @ {entry_price} | "
            f"SL: {stop_loss} | TP: {take_profit}"
        )
        
        if trade_id:
            message += f" | ID: {trade_id}"
        
        self.logger.info(message)
    
    def log_trade_close(
        self,
        pair: str,
        trade_id: str,
        close_price: float,
        pnl: float,
        reason: str
    ):
        """
        Log trade closure.
        
        Args:
            pair: Trading pair
            trade_id: Trade ID
            close_price: Closing price
            pnl: Profit/Loss
            reason: Reason for closure
        """
        pnl_sign = "+" if pnl >= 0 else ""
        message = (
            f"{pair} | CLOSE | ID: {trade_id} @ {close_price} | "
            f"P/L: {pnl_sign}${pnl:.2f} | {reason}"
        )
        
        self.logger.info(message)
