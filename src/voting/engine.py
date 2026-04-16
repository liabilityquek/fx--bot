"""VotingEngine — tallies agent votes into a consensus signal.

Consensus formula:
    buy_score  = sum(confidence * weight for BUY votes) / total_weight
    sell_score = sum(confidence * weight for SELL votes) / total_weight
    HOLD votes count toward total_weight but not numerator.

If LLM agent is unavailable or fails, evaluation uses tech-only weight (3.0)
so that an LLM outage never permanently blocks trades.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.agents.base import AgentVote, Signal
from src.agents.llm_agent import LLMAgent
from src.agents.momentum_agent import MomentumAgent
from src.agents.tech_agent import TechAgent
from src.agents.trend_agent import TrendAgent
from config.settings import settings


@dataclass
class VoteResult:
    pair: str
    final_signal: Signal
    consensus_score: float      # buy_score or sell_score, whichever triggered
    buy_score: float
    sell_score: float
    agent_votes: List[AgentVote]
    llm_reasoning: str
    llm_available: bool


class VotingEngine:
    """Orchestrates all agents and produces a consensus VoteResult."""

    # Agent weights
    TECH_WEIGHT = 1.0
    TREND_WEIGHT = 1.0
    MOMENTUM_WEIGHT = 1.0
    # LLM weight comes from settings

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("VotingEngine")
        self._llm_weight = settings.LLM_AGENT_WEIGHT
        self._threshold = settings.CONSENSUS_THRESHOLD

        self._tech = TechAgent(logger)
        self._trend = TrendAgent(logger)
        self._momentum = MomentumAgent(logger)
        self._llm = LLMAgent(logger)

        self._agent_weights: Dict[str, float] = {
            "TechAgent":     self.TECH_WEIGHT,
            "TrendAgent":    self.TREND_WEIGHT,
            "MomentumAgent": self.MOMENTUM_WEIGHT,
            "LLMAgent":      self._llm_weight,
        }

    def run_vote(self, pair: str, candles: List[Dict], price: float) -> VoteResult:
        """Run all agents and return a VoteResult."""
        # 1. Technical agents (each wrapped — cannot raise)
        tech_votes: List[AgentVote] = [
            self._tech.vote(pair, candles, price),
            self._trend.vote(pair, candles, price),
            self._momentum.vote(pair, candles, price),
        ]

        # 2. LLM synthesizer (sees tech votes)
        llm_vote = self._llm.vote(pair, candles, price, tech_votes)
        llm_available = self._llm.is_available and llm_vote.reasoning != "LLM call failed"

        all_votes = tech_votes + [llm_vote]

        # 3. Tally
        buy_score, sell_score = self._tally(tech_votes, llm_vote, llm_available)

        if buy_score >= self._threshold and buy_score > sell_score:
            final_signal = Signal.BUY
            consensus_score = buy_score
        elif sell_score >= self._threshold and sell_score > buy_score:
            final_signal = Signal.SELL
            consensus_score = sell_score
        else:
            final_signal = Signal.HOLD
            consensus_score = max(buy_score, sell_score)

        return VoteResult(
            pair=pair,
            final_signal=final_signal,
            consensus_score=round(consensus_score, 4),
            buy_score=round(buy_score, 4),
            sell_score=round(sell_score, 4),
            agent_votes=all_votes,
            llm_reasoning=llm_vote.reasoning,
            llm_available=llm_available,
        )

    def _tally(
        self,
        tech_votes: List[AgentVote],
        llm_vote: AgentVote,
        llm_available: bool,
    ):
        """Compute buy_score and sell_score."""
        buy_num = 0.0
        sell_num = 0.0
        total_weight = 0.0

        votes_with_weights = [
            (v, self._agent_weights.get(v.agent_name, 1.0))
            for v in tech_votes
        ]

        # Include LLM only when available
        if llm_available:
            votes_with_weights.append((llm_vote, self._llm_weight))

        for vote, weight in votes_with_weights:
            total_weight += weight
            if vote.signal == Signal.BUY:
                buy_num += vote.confidence * weight
            elif vote.signal == Signal.SELL:
                sell_num += vote.confidence * weight
            # HOLD: adds to denominator only

        if total_weight == 0:
            return 0.0, 0.0

        return buy_num / total_weight, sell_num / total_weight
