"""ReviewerAgent — Senior FX Execution Trader.

Role: 20-year institutional execution trader with final authority on whether
a trade recommendation is executed right now.

Provider priority (same as LLMAgent):
  1. Groq llama-3.1-8b-instant (primary — fast, sufficient for review task)
  2. NVIDIA nvidia_nim/z-ai/glm4.7 (fallback on Groq credit exhaustion)
  3. Anthropic Claude Haiku (fallback on NVIDIA credit exhaustion)
  4. REJECTED + reviewer_available=False when all are permanently exhausted.
     Caller (DecisionEngine) treats this as HOLD + fires Telegram alert.

The reviewer uses a smaller Groq model than the analyst deliberately —
the review task (logical consistency check) does not require the same
reasoning depth as the analysis task.
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from openai import OpenAI

from .base import AgentVote, Signal
from ._llm_utils import _is_credit_exhausted

_MIN_CALL_SPACING_SECONDS = 5
_last_reviewer_call: float = 0.0


class ReviewVerdict(Enum):
    APPROVED = 'APPROVED'
    ADJUSTED = 'ADJUSTED'
    REJECTED = 'REJECTED'


@dataclass
class ReviewResult:
    verdict: ReviewVerdict
    adjusted_confidence: float
    reason: str
    reviewer_available: bool


# ---------------------------------------------------------------------------
# Role and system prompt
# ---------------------------------------------------------------------------

_REVIEWER_SYSTEM_PROMPT = """You are a senior FX execution trader with 20 years of institutional
trading experience across G10 currency pairs.

You will receive:
1. A complete market briefing (technical indicators, macro context, recent news, upcoming events)
2. A trade recommendation from a quantitative analyst, including their reasoning

Your job is to make the final execution decision: should this trade be placed RIGHT NOW?

You are NOT re-analysing the market from scratch.
You are reviewing whether the analyst's recommendation is sound enough to execute given:
  - Is the analyst's reasoning logically consistent with the data shown?
  - Are there upcoming events that make this a bad time to enter?
  - Is the confidence level appropriate given the signal strength?
  - Would a professional trader execute this right now?

Rules you never break:
  1. Never approve a new position within 30 minutes of a VERY HIGH impact event
     (NFP, FOMC, rate decisions, GDP) for either currency in the pair.
  2. If the analyst's reasoning directly contradicts the data shown, REJECT.
  3. If confidence is clearly inflated vs the actual signal quality, ADJUST it down.
  4. You cannot change the direction (BUY to SELL or SELL to BUY).
     If you disagree with direction entirely, REJECT and let the next cycle decide.
  5. If technical signals are mixed but analyst still voted directionally,
     ADJUST confidence to reflect that uncertainty.

Respond with valid JSON only — no markdown, no preamble:
{"verdict": "APPROVED|ADJUSTED|REJECTED", "adjusted_confidence": 0.0-1.0, "reason": "max 150 chars"}

If APPROVED: set adjusted_confidence equal to the original analyst confidence.
If REJECTED: set adjusted_confidence to 0.0.
Speak like a trader in your reason — be direct and specific."""


class ReviewerAgent:
    """
    Senior FX execution trader.
    Provider priority: Groq (primary) → NVIDIA (fallback) → Anthropic (fallback).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger('ReviewerAgent')

        # Groq — primary
        self._groq_client: Optional[OpenAI] = None
        self._groq_model:     str  = ''
        self._groq_exhausted: bool = False

        # NVIDIA — fallback
        self._nvidia_client: Optional[OpenAI] = None
        self._nvidia_model:     str  = ''
        self._nvidia_exhausted: bool = False

        # Anthropic — fallback
        self._anthropic_client = None
        self._anthropic_model:     str  = ''
        self._anthropic_exhausted: bool = False

        self._init_groq()
        self._init_nvidia()
        self._init_anthropic()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_groq(self) -> None:
        """
        Groq primary — uses llama-3.1-8b-instant for the reviewer.
        Smaller and faster than the analyst's 70B model; sufficient
        for logical consistency checking.
        """
        try:
            from config.settings import settings
            if not settings.GROQ_API_KEY:
                self.logger.warning('ReviewerAgent: GROQ_API_KEY not set — Groq disabled')
                self._groq_exhausted = True
                return
            self._groq_client = OpenAI(
                api_key=settings.GROQ_API_KEY,
                base_url='https://api.groq.com/openai/v1',
            )
            self._groq_model = settings.REVIEWER_LLM_MODEL
            self.logger.info(f'ReviewerAgent: Groq ready ({self._groq_model})')
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent: Groq init failed: {exc}')
            self._groq_exhausted = True

    def _init_nvidia(self) -> None:
        """NVIDIA fallback — activates when Groq credits are exhausted."""
        try:
            from config.settings import settings
            if not settings.NVIDIA_API_KEY:
                self.logger.info('ReviewerAgent: NVIDIA_API_KEY not set — NVIDIA fallback disabled')
                self._nvidia_exhausted = True
                return
            self._nvidia_client = OpenAI(
                api_key=settings.NVIDIA_API_KEY,
                base_url='https://integrate.api.nvidia.com/v1',
            )
            self._nvidia_model = settings.NVIDIA_LLM_MODEL
            self.logger.info(f'ReviewerAgent: NVIDIA fallback ready ({self._nvidia_model})')
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent: NVIDIA init failed: {exc}')
            self._nvidia_exhausted = True

    def _init_anthropic(self) -> None:
        """Anthropic fallback — activates when Groq credits are exhausted."""
        try:
            from config.settings import settings
            if not settings.ANTHROPIC_API_KEY:
                self.logger.info(
                    'ReviewerAgent: ANTHROPIC_API_KEY not set — Anthropic fallback disabled'
                )
                self._anthropic_exhausted = True
                return
            import anthropic as _sdk
            self._anthropic_client = _sdk.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
            self._anthropic_model = settings.ANTHROPIC_LLM_MODEL
            self.logger.info(
                f'ReviewerAgent: Anthropic fallback ready ({self._anthropic_model})'
            )
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent: Anthropic init failed: {exc}')
            self._anthropic_exhausted = True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return not (self._groq_exhausted and self._nvidia_exhausted and self._anthropic_exhausted)

    @property
    def both_exhausted(self) -> bool:
        return self._groq_exhausted and self._nvidia_exhausted and self._anthropic_exhausted

    @property
    def active_provider(self) -> str:
        if not self._groq_exhausted:
            return 'groq'
        if not self._nvidia_exhausted:
            return 'nvidia'
        if not self._anthropic_exhausted:
            return 'anthropic'
        return 'none'

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def review(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: dict,
        analyst_vote: AgentVote,
    ) -> ReviewResult:
        """
        Review the analyst's recommendation.
        Always returns ReviewResult — never raises.

        When both providers are permanently exhausted, returns REJECTED with
        reviewer_available=False so the caller (DecisionEngine) treats it as
        HOLD and fires the reviewer-unavailable Telegram alert.
        """
        if self.both_exhausted:
            self.logger.warning('ReviewerAgent: both providers exhausted — blocking trade')
            return ReviewResult(
                verdict=ReviewVerdict.REJECTED,
                adjusted_confidence=0.0,
                reason='All reviewer providers exhausted',
                reviewer_available=False,
            )

        try:
            return self._review(pair, candles, price, indicators, analyst_vote)
        except Exception as exc:
            self.logger.warning(f'ReviewerAgent review failed for {pair}: {exc}')
            # Unexpected outer exception — pass through rather than block
            return ReviewResult(
                verdict=ReviewVerdict.APPROVED,
                adjusted_confidence=analyst_vote.confidence,
                reason='Reviewer unexpected error — passing through',
                reviewer_available=True,
            )

    # ------------------------------------------------------------------
    # Internal logic
    # ------------------------------------------------------------------

    def _review(
        self, pair, candles, price, indicators, analyst_vote
    ) -> ReviewResult:
        global _last_reviewer_call
        elapsed = time.time() - _last_reviewer_call
        if elapsed < _MIN_CALL_SPACING_SECONDS:
            time.sleep(_MIN_CALL_SPACING_SECONDS - elapsed)

        user_msg = _build_review_message(pair, candles, price, indicators, analyst_vote)
        _last_reviewer_call = time.time()

        # Try Groq first
        if not self._groq_exhausted and self._groq_client is not None:
            try:
                return self._call_groq(user_msg, analyst_vote)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        'ReviewerAgent: Groq credits exhausted — switching to Anthropic'
                    )
                    self._groq_exhausted = True
                else:
                    self.logger.warning(
                        f'ReviewerAgent: Groq transient error for {pair}: {exc}'
                    )
                    # Transient — pass through this cycle, don't fire unavailability alert
                    return ReviewResult(
                        verdict=ReviewVerdict.APPROVED,
                        adjusted_confidence=analyst_vote.confidence,
                        reason='Reviewer transient error — passing through',
                        reviewer_available=True,
                    )

        # Try NVIDIA fallback
        if not self._nvidia_exhausted and self._nvidia_client is not None:
            try:
                return self._call_nvidia(user_msg, analyst_vote)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        'ReviewerAgent: NVIDIA credits exhausted — switching to Anthropic'
                    )
                    self._nvidia_exhausted = True
                else:
                    self.logger.warning(
                        f'ReviewerAgent: NVIDIA transient error for {pair}: {exc}'
                    )
                    # Transient — pass through this cycle, don't fire unavailability alert
                    return ReviewResult(
                        verdict=ReviewVerdict.APPROVED,
                        adjusted_confidence=analyst_vote.confidence,
                        reason='Reviewer transient error — passing through',
                        reviewer_available=True,
                    )

        # Try Anthropic fallback
        if not self._anthropic_exhausted and self._anthropic_client is not None:
            try:
                return self._call_anthropic(user_msg, analyst_vote)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        'ReviewerAgent: Anthropic exhausted — all providers down'
                    )
                    self._anthropic_exhausted = True
                else:
                    self.logger.warning(
                        f'ReviewerAgent: Anthropic transient error for {pair}: {exc}'
                    )
                    return ReviewResult(
                        verdict=ReviewVerdict.APPROVED,
                        adjusted_confidence=analyst_vote.confidence,
                        reason='Reviewer transient error — passing through',
                        reviewer_available=True,
                    )

        # All permanently exhausted
        return ReviewResult(
            verdict=ReviewVerdict.REJECTED,
            adjusted_confidence=0.0,
            reason='All reviewer providers exhausted',
            reviewer_available=False,
        )

    def _call_groq(self, user_msg: str, analyst_vote: AgentVote) -> ReviewResult:
        response = self._groq_client.chat.completions.create(
            model=self._groq_model,
            max_tokens=200,
            messages=[
                {'role': 'system', 'content': _REVIEWER_SYSTEM_PROMPT},
                {'role': 'user',   'content': user_msg},
            ],
        )
        return _parse_review_response(
            response.choices[0].message.content.strip(), analyst_vote
        )

    def _call_nvidia(self, user_msg: str, analyst_vote: AgentVote) -> ReviewResult:
        response = self._nvidia_client.chat.completions.create(
            model=self._nvidia_model,
            max_tokens=200,
            messages=[
                {'role': 'system', 'content': _REVIEWER_SYSTEM_PROMPT},
                {'role': 'user',   'content': user_msg},
            ],
        )
        return _parse_review_response(
            response.choices[0].message.content.strip(), analyst_vote
        )

    def _call_anthropic(self, user_msg: str, analyst_vote: AgentVote) -> ReviewResult:
        response = self._anthropic_client.messages.create(
            model=self._anthropic_model,
            max_tokens=200,
            system=_REVIEWER_SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': user_msg}],
        )
        return _parse_review_response(
            response.content[0].text.strip(), analyst_vote
        )


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_review_message(
    pair: str,
    candles: List[Dict],
    price: float,
    indicators: dict,
    analyst_vote: AgentVote,
) -> str:
    from .llm_agent import _build_analyst_message
    briefing = _build_analyst_message(pair, candles, price, indicators)

    return (
        f'{briefing}\n\n'
        f'=== ANALYST RECOMMENDATION ===\n'
        f'Vote:       {analyst_vote.signal.value}\n'
        f'Confidence: {analyst_vote.confidence:.2f}\n'
        f'Reasoning:  {analyst_vote.reasoning}\n\n'
        f'Review this recommendation. Should we execute this trade right now?'
    )


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_review_response(raw: str, analyst_vote: AgentVote) -> ReviewResult:
    text = raw.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(lines[1:-1]) if len(lines) > 2 else text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return ReviewResult(
                    ReviewVerdict.APPROVED,
                    analyst_vote.confidence,
                    'JSON parse error — passing through',
                    True,
                )
        else:
            return ReviewResult(
                ReviewVerdict.APPROVED,
                analyst_vote.confidence,
                'JSON parse error — passing through',
                True,
            )

    verdict_str = data.get('verdict', 'APPROVED').upper()
    try:
        verdict = ReviewVerdict[verdict_str]
    except KeyError:
        verdict = ReviewVerdict.APPROVED

    try:
        adj_conf = float(data.get('adjusted_confidence', analyst_vote.confidence))
        adj_conf = max(0.0, min(1.0, adj_conf))
    except (TypeError, ValueError):
        adj_conf = analyst_vote.confidence

    reason = str(data.get('reason', ''))[:150]

    return ReviewResult(
        verdict=verdict,
        adjusted_confidence=round(adj_conf, 4),
        reason=reason,
        reviewer_available=True,
    )
