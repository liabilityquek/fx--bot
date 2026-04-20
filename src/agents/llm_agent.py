"""LLMAgent — Groq LLM synthesizer.

Not a BaseAgent subclass. Called by VotingEngine after tech votes are collected
so it can see the preliminary vote breakdown as additional context.
"""

import json
import logging
import time
from typing import Dict, List, Optional

# import anthropic  # replaced by Groq via OpenAI-compatible SDK
from openai import OpenAI

from .base import AgentVote, Signal
from .indicators import to_dataframe

# Minimum seconds between successive LLM calls (rate-limit guard)
_MIN_CALL_SPACING_SECONDS = 10
_last_call_time: float = 0.0

_SYSTEM_PROMPT = (
    "You are an FX trading signal agent. Respond with valid JSON only:\n"
    '{"vote": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reasoning": "max 120 chars"}\n'
    "Only vote BUY or SELL if confidence > 0.55, otherwise HOLD."
)

_FALLBACK_VOTE = AgentVote(
    agent_name="LLMAgent",
    pair="",
    signal=Signal.HOLD,
    confidence=0.5,
    reasoning="LLM call failed",
)


class LLMAgent:
    """Synthesizer agent powered by Groq (llama-3.3-70b-versatile)."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("LLMAgent")
        self._client = None
        self._model: str = ""
        self._available = False
        self._init_client()

    def _init_client(self) -> None:
        try:
            from config.settings import settings

            if not settings.GROQ_API_KEY:
                self.logger.warning("GROQ_API_KEY not set — LLM agent disabled")
                return

            self._client = OpenAI(
                api_key=settings.GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
            self._model = settings.LLM_MODEL
            self._available = True
            self.logger.info(f"LLMAgent initialised with model {self._model}")
        except ImportError:
            self.logger.warning("openai package not installed — LLM agent disabled")
        except Exception as exc:
            self.logger.warning(f"LLMAgent init failed: {exc}")

    @property
    def is_available(self) -> bool:
        return self._available

    def vote(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        tech_votes: List[AgentVote],
    ) -> AgentVote:
        """Generate a synthesizer vote.

        Always returns an AgentVote — falls back to HOLD(0.5) on any error.
        """
        try:
            return self._vote(pair, candles, price, tech_votes)
        except Exception as exc:
            self.logger.warning(f"LLMAgent vote failed for {pair}: {exc}")
            return AgentVote(
                agent_name="LLMAgent",
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="LLM call failed",
            )

    def _vote(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        tech_votes: List[AgentVote],
    ) -> AgentVote:
        global _last_call_time

        if not self._available or self._client is None:
            return AgentVote(
                agent_name="LLMAgent",
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="LLM unavailable",
            )

        # Rate-limit guard
        elapsed = time.time() - _last_call_time
        if elapsed < _MIN_CALL_SPACING_SECONDS:
            time.sleep(_MIN_CALL_SPACING_SECONDS - elapsed)

        user_msg = _build_user_message(pair, candles, price, tech_votes)

        _last_call_time = time.time()
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=256,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )

        raw_text = response.choices[0].message.content.strip()
        return _parse_response(raw_text, pair)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_user_message(
    pair: str,
    candles: List[Dict],
    price: float,
    tech_votes: List[AgentVote],
) -> str:
    """Construct the LLM user message with market context and tech votes."""
    # Last 10 candles as a mini table (supports both flat and mid formats)
    recent = candles[-10:] if len(candles) >= 10 else candles
    candle_lines = []
    for c in recent:
        if 'mid' in c:
            mid = c['mid']
            o, h, l, cl = mid.get('o','?'), mid.get('h','?'), mid.get('l','?'), mid.get('c','?')
        else:
            o = c.get('open', '?')
            h = c.get('high', '?')
            l = c.get('low', '?')
            cl = c.get('close', '?')
        candle_lines.append(f"  O={o} H={h} L={l} C={cl}")
    candle_table = "\n".join(candle_lines)

    # Tech vote summary
    vote_lines = []
    for v in tech_votes:
        vote_lines.append(
            f"  {v.agent_name}: {v.signal.value} conf={v.confidence:.2f} | {v.reasoning}"
        )
    vote_summary = "\n".join(vote_lines) if vote_lines else "  (none)"

    return (
        f"Pair: {pair}\n"
        f"Current price: {price}\n\n"
        f"Last 10 candles (H1):\n{candle_table}\n\n"
        f"Technical agent votes:\n{vote_summary}\n\n"
        "Based on the above, provide your synthesized trading signal as JSON."
    )


def _parse_response(raw: str, pair: str) -> AgentVote:
    """Parse LLM JSON response defensively."""
    # Strip markdown code blocks if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object from surrounding text
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "JSON parse error")
        else:
            return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "JSON parse error")

    # Accept both "vote" and "signal" keys
    vote_str = data.get("vote") or data.get("signal") or "HOLD"
    try:
        signal = Signal[vote_str.upper()]
    except KeyError:
        signal = Signal.HOLD

    try:
        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    reasoning = str(data.get("reasoning", ""))[:120]

    return AgentVote(
        agent_name="LLMAgent",
        pair=pair,
        signal=signal,
        confidence=round(confidence, 4),
        reasoning=reasoning,
    )
