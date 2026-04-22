"""DecisionEngine — sequential two-agent decision pipeline.

Pipeline per cycle:
  1. TechAgent/TrendAgent/MomentumAgent.get_indicators() → merged indicators dict
  2. MacroContext.build()                                  → macro dict
  3. LLMAgent.vote(indicators, macro_context)             → BUY/SELL/HOLD + confidence
  4. If HOLD or confidence < threshold                    → DecisionResult(HOLD)
  5. ReviewerAgent.review(indicators, llm_vote)           → APPROVED/ADJUSTED/REJECTED
  6. Apply reviewer verdict                               → final DecisionResult

Provider failure behaviour:
  - LLM both exhausted      → HOLD + alert_llm_credits_exhausted()
  - Reviewer both exhausted → HOLD + alert_reviewer_unavailable()
  - Reviewer transient      → pass-through (APPROVED, reviewer_available=True)
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.agents.base import AgentVote, Signal
from src.agents.llm_agent import LLMAgent
from src.agents.macro_context import MacroContext
from src.agents.momentum_agent import MomentumAgent
from src.agents.reviewer_agent import ReviewResult, ReviewVerdict, ReviewerAgent
from src.agents.tech_agent import TechAgent
from src.agents.trend_agent import TrendAgent
from config.settings import settings


@dataclass
class DecisionResult:
    pair: str
    final_signal: Signal
    confidence: float           # LLM confidence, possibly adjusted by reviewer
    llm_reasoning: str
    llm_available: bool
    reviewer_verdict: str       # APPROVED / ADJUSTED / REJECTED / SKIPPED / UNAVAILABLE
    reviewer_reason: str
    reviewer_available: bool


class DecisionEngine:
    """Orchestrates the sequential two-agent decision pipeline."""

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        alert_manager=None,
        event_monitor=None,
    ):
        self.logger = logger or logging.getLogger("DecisionEngine")
        self._alert_manager = alert_manager
        self._credits_alert_sent = False

        self._tech     = TechAgent(logger)
        self._trend    = TrendAgent(logger)
        self._momentum = MomentumAgent(logger)
        self._llm      = LLMAgent(logger)
        self._reviewer = ReviewerAgent(logger)
        self._macro    = MacroContext(event_monitor=event_monitor, logger=logger)

        self._last_results: dict = {}   # pair -> DecisionResult

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_decision(self, pair: str, candles: List[Dict], price: float) -> DecisionResult:
        """Run the full decision pipeline and return a DecisionResult."""
        threshold = settings.CONSENSUS_THRESHOLD

        # 1. Collect indicators from all three tech agents
        indicators: dict = {}
        indicators.update(self._tech.get_indicators(pair, candles, price))
        indicators.update(self._trend.get_indicators(pair, candles, price))
        indicators.update(self._momentum.get_indicators(pair, candles, price))

        # 2. Build macro context (fails silently)
        macro: dict = {}
        try:
            macro = self._macro.build(pair)
        except Exception as exc:
            self.logger.debug(f"MacroContext.build failed for {pair}: {exc}")

        # 3. LLM analyst vote
        llm_vote: AgentVote = self._llm.vote(
            pair, candles, price, indicators, macro_context=macro
        )
        llm_available = self._llm.is_available and llm_vote.reasoning not in (
            "LLM call failed", "All LLM providers exhausted"
        )

        # 4. One-shot alert when both LLM providers are permanently exhausted
        if self._llm.both_exhausted and not self._credits_alert_sent:
            self._credits_alert_sent = True
            self.logger.critical("Both LLM providers (Groq + Anthropic) credits exhausted")
            if self._alert_manager is not None:
                try:
                    self._alert_manager.alert_llm_credits_exhausted()
                except Exception as alert_exc:
                    self.logger.warning(f"Failed to send credits-exhausted alert: {alert_exc}")

        # 5. If HOLD or below confidence threshold — skip reviewer
        if llm_vote.signal == Signal.HOLD or llm_vote.confidence < threshold:
            result = DecisionResult(
                pair=pair,
                final_signal=Signal.HOLD,
                confidence=llm_vote.confidence,
                llm_reasoning=llm_vote.reasoning,
                llm_available=llm_available,
                reviewer_verdict='SKIPPED',
                reviewer_reason='LLM HOLD or confidence below threshold',
                reviewer_available=True,
            )
            self._last_results[pair] = result
            return result

        # 6. Reviewer
        review: ReviewResult = self._reviewer.review(
            pair, candles, price, indicators, llm_vote
        )

        # 7. Apply reviewer verdict
        final_signal = llm_vote.signal
        final_conf   = llm_vote.confidence
        rev_verdict  = review.verdict.value
        rev_reason   = review.reason

        if not review.reviewer_available:
            # Permanently unavailable → HOLD + Telegram alert
            final_signal = Signal.HOLD
            rev_verdict  = 'UNAVAILABLE'
            if self._alert_manager is not None:
                try:
                    self._alert_manager.alert_reviewer_unavailable(pair, review.reason)
                except Exception:
                    pass
        elif review.verdict == ReviewVerdict.REJECTED:
            final_signal = Signal.HOLD
            final_conf   = 0.0
        elif review.verdict == ReviewVerdict.ADJUSTED:
            final_conf = review.adjusted_confidence
            if final_conf < threshold:
                final_signal = Signal.HOLD

        result = DecisionResult(
            pair=pair,
            final_signal=final_signal,
            confidence=round(final_conf, 4),
            llm_reasoning=llm_vote.reasoning,
            llm_available=llm_available,
            reviewer_verdict=rev_verdict,
            reviewer_reason=rev_reason,
            reviewer_available=review.reviewer_available,
        )
        self._last_results[pair] = result
        return result

    def get_llm_provider_status(self) -> str:
        """Return human-readable status for both analyst and reviewer providers."""
        groq_ok    = not self._llm._groq_exhausted and self._llm._groq_client is not None
        ant_ok     = not self._llm._anthropic_exhausted and self._llm._anthropic_client is not None
        rev_groq_ok = not self._reviewer._groq_exhausted and self._reviewer._groq_client is not None
        rev_ant_ok  = not self._reviewer._anthropic_exhausted and self._reviewer._anthropic_client is not None

        return (
            "=== Analyst ===\n"
            f"Groq: {'active' if groq_ok else 'exhausted / unavailable'}\n"
            f"Anthropic: {'active (fallback)' if ant_ok else 'exhausted / unavailable'}\n"
            f"Active provider: {self._llm.active_provider}\n\n"
            "=== Reviewer ===\n"
            f"Groq: {'active' if rev_groq_ok else 'exhausted / unavailable'}\n"
            f"Anthropic: {'active (fallback)' if rev_ant_ok else 'exhausted / unavailable'}\n"
            f"Active provider: {self._reviewer.active_provider}"
        )

    def get_analyst_summary(self) -> str:
        """Return last analyst decision per pair — used by /analyst Telegram command."""
        if not self._last_results:
            return 'Analyst History\n\nNo decisions yet this session.'

        lines = ['Analyst History\n']
        for pair, result in self._last_results.items():
            lines.append(
                f'{pair}\n'
                f'  Signal:     {result.final_signal.value}\n'
                f'  Confidence: {result.confidence:.2f}\n'
                f'  Reasoning:  {result.llm_reasoning}\n'
                f'  LLM:        {"available" if result.llm_available else "unavailable"}'
            )
        return '\n\n'.join(lines)

    def get_reviewer_summary(self) -> str:
        """Return last reviewer verdict per pair — used by /reviewer Telegram command."""
        if not self._last_results:
            return 'Reviewer History\n\nNo decisions yet this session.'

        verdict_labels = {
            'APPROVED':    'APPROVED',
            'ADJUSTED':    'ADJUSTED',
            'REJECTED':    'REJECTED',
            'SKIPPED':     'SKIPPED (HOLD/low-conf)',
            'UNAVAILABLE': 'REVIEWER DOWN',
        }

        lines = ['Reviewer History\n']
        for pair, result in self._last_results.items():
            badge = verdict_labels.get(result.reviewer_verdict, result.reviewer_verdict)
            lines.append(
                f'{pair}\n'
                f'  Verdict:    {badge}\n'
                f'  Confidence: {result.confidence:.2f}\n'
                f'  Reason:     {result.reviewer_reason}'
            )
        return '\n\n'.join(lines)
