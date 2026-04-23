"""Multi-agent voting system for FX trading signals."""

from .base import BaseAgent, AgentVote, Signal
from .tech_agent import TechAgent
from .trend_agent import TrendAgent
from .momentum_agent import MomentumAgent
from .llm_agent import LLMAgent
from .news_risk_agent import NewsRiskAgent, NewsRiskDecision

__all__ = [
    'BaseAgent',
    'AgentVote',
    'Signal',
    'TechAgent',
    'TrendAgent',
    'MomentumAgent',
    'LLMAgent',
    'NewsRiskAgent',
    'NewsRiskDecision',
]
