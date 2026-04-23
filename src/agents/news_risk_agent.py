"""NewsRiskAgent — Groq-primary / Anthropic-fallback LLM agent.

Evaluates whether an open trade should be closed ahead of an imminent
VERY_HIGH impact economic event.  Called by NewsWatcher.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from ._llm_utils import _is_credit_exhausted


@dataclass
class NewsRiskDecision:
    should_close: bool
    confidence: float
    reason: str


class NewsRiskAgent:
    """LLM agent for pre-event trade risk evaluation."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("NewsRiskAgent")

        self._groq_client = None
        self._groq_model: str = ""
        self._groq_exhausted: bool = False

        self._anthropic_client = None
        self._anthropic_model: str = ""
        self._anthropic_exhausted: bool = False

        self._init_groq()
        self._init_anthropic()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_groq(self) -> None:
        try:
            from openai import OpenAI
            from config.settings import settings
            if not settings.GROQ_API_KEY:
                self._groq_exhausted = True
                return
            self._groq_client = OpenAI(
                api_key=settings.GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
            self._groq_model = settings.LLM_MODEL
            self.logger.info(f"NewsRiskAgent: Groq initialised ({self._groq_model})")
        except Exception as exc:
            self.logger.warning(f"NewsRiskAgent: Groq init failed: {exc}")
            self._groq_exhausted = True

    def _init_anthropic(self) -> None:
        try:
            import anthropic
            from config.settings import settings
            if not settings.ANTHROPIC_API_KEY:
                self._anthropic_exhausted = True
                return
            self._anthropic_client = anthropic.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY
            )
            self._anthropic_model = settings.ANTHROPIC_LLM_MODEL
            self.logger.info(
                f"NewsRiskAgent: Anthropic fallback initialised ({self._anthropic_model})"
            )
        except Exception as exc:
            self.logger.warning(f"NewsRiskAgent: Anthropic init failed: {exc}")
            self._anthropic_exhausted = True

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def evaluate(self, trade, event) -> NewsRiskDecision:
        """Evaluate whether an open trade should be closed before an event.

        Args:
            trade: Trade object (pair, is_long, entry_price, unrealized_pnl, stop_loss)
            event: EconomicEvent object

        Returns:
            NewsRiskDecision
        """
        from config.settings import settings

        system_prompt = (
            "You are an FX risk agent. A major news event is imminent.\n"
            "Decide whether to close an open trade to protect capital.\n"
            "Respond with valid JSON only:\n"
            '{"decision": "CLOSE|HOLD", "confidence": 0.0-1.0, "reason": "max 120 chars"}\n'
            f"Only decide CLOSE if confidence > {settings.NEWS_RISK_CLOSE_THRESHOLD}, "
            "otherwise HOLD."
        )

        direction = "LONG" if trade.is_long else "SHORT"
        pnl_sign = "+" if trade.unrealized_pnl >= 0 else ""
        sl_info = f"{trade.stop_loss:.5f}" if trade.stop_loss else "none"

        user_msg = (
            f"Trade: {trade.pair} {direction} | "
            f"Entry: {trade.entry_price:.5f} | "
            f"Unrealised P/L: {pnl_sign}{trade.unrealized_pnl:.2f} | "
            f"SL: {sl_info}\n"
            f"Event: {event.event_name} ({event.currency}) | "
            f"Impact: {event.impact.value.upper()} | "
            f"In: {event.minutes_until:.0f} min | "
            f"Forecast: {event.forecast} | Previous: {event.previous}"
        )

        raw = self._call_llm(system_prompt, user_msg)
        return self._parse(raw)

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _call_llm(self, system_prompt: str, user_msg: str) -> str:
        if not self._groq_exhausted and self._groq_client:
            try:
                resp = self._groq_client.chat.completions.create(
                    model=self._groq_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.1,
                    max_tokens=120,
                )
                return resp.choices[0].message.content.strip()
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning("NewsRiskAgent: Groq credits exhausted — switching to Anthropic")
                    self._groq_exhausted = True
                else:
                    self.logger.warning(f"NewsRiskAgent: Groq call failed: {exc}")
                    return '{"decision": "HOLD", "confidence": 0.0, "reason": "groq_error"}'

        if not self._anthropic_exhausted and self._anthropic_client:
            try:
                resp = self._anthropic_client.messages.create(
                    model=self._anthropic_model,
                    max_tokens=120,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_msg}],
                )
                return resp.content[0].text.strip()
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning("NewsRiskAgent: Anthropic credits exhausted")
                    self._anthropic_exhausted = True
                else:
                    self.logger.warning(f"NewsRiskAgent: Anthropic call failed: {exc}")

        return '{"decision": "HOLD", "confidence": 0.0, "reason": "no_llm_available"}'

    def _parse(self, raw: str) -> NewsRiskDecision:
        from config.settings import settings
        try:
            data = json.loads(raw)
            decision = str(data.get("decision", "HOLD")).upper()
            confidence = float(data.get("confidence", 0.0))
            reason = str(data.get("reason", ""))[:120]
            should_close = (
                decision == "CLOSE"
                and confidence > settings.NEWS_RISK_CLOSE_THRESHOLD
            )
            return NewsRiskDecision(
                should_close=should_close,
                confidence=confidence,
                reason=reason,
            )
        except Exception as exc:
            self.logger.warning(f"NewsRiskAgent: parse error ({exc}) — defaulting HOLD")
            return NewsRiskDecision(should_close=False, confidence=0.0, reason="parse_error")
