"""Base classes for the multi-agent voting system."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class AgentVote:
    agent_name: str
    pair: str
    signal: Signal
    confidence: float   # 0.0–1.0
    reasoning: str


class BaseAgent(ABC):
    """Abstract base for technical agents.

    CONTRACT: vote() must never raise. Wrap all logic in try/except and
    return HOLD(0.5) on any failure.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(self.__class__.__name__)

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent identifier used in vote breakdown."""

    def vote(self, pair: str, candles: List[Dict], price: float) -> AgentVote:
        """Generate a vote for the given pair.

        Args:
            pair: OANDA instrument string, e.g. 'EUR_USD'
            candles: List of OANDA candle dicts (raw API response items)
            price: Current mid price

        Returns:
            AgentVote — never raises; returns HOLD(0.5) on error.
        """
        try:
            return self._vote(pair, candles, price)
        except Exception as exc:
            self.logger.warning(f"{self.name} vote failed for {pair}: {exc}")
            return AgentVote(
                agent_name=self.name,
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning=f"Error: {exc}",
            )

    @abstractmethod
    def _vote(self, pair: str, candles: List[Dict], price: float) -> AgentVote:
        """Internal vote implementation — may raise."""
