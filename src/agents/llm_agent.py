"""LLMAgent — Groq (primary) + NVIDIA (fallback) + Anthropic (fallback) analyst synthesizer.

Provider priority:
  1. Groq  (llama-3.3-70b-versatile by default)
  2. NVIDIA (nvidia_nim/z-ai/glm4.7 by default) — activates when Groq credits are exhausted
  3. Anthropic  (claude-haiku-4-5-20251001 by default) — activates when NVIDIA credits are exhausted
  4. HOLD fallback — when all providers are credit-exhausted; DecisionEngine
     fires a Telegram alert at this point.

Receives a merged indicators dict (from TechAgent/TrendAgent/MomentumAgent)
and optional macro context dict. Produces a BUY/SELL/HOLD AgentVote.

Credit exhaustion is detected by inspecting error messages for quota/billing
keywords. Transient rate-limit errors (too-many-requests per minute) are NOT
treated as exhaustion — they fall back to HOLD(0.5) for that single call and
retry on the next cycle.
"""

import json
import logging
import time
from typing import Dict, List, Optional

from openai import OpenAI

from .base import AgentVote, Signal
from .indicators import to_dataframe
from ._llm_utils import _is_credit_exhausted

# Minimum seconds between successive LLM calls (rate-limit guard)
_MIN_CALL_SPACING_SECONDS = 10
_last_call_time: float = 0.0

_SYSTEM_PROMPT = (
    "You are an FX trading signal agent. Respond with valid JSON only:\n"
    '{"vote": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "reasoning": "max 120 chars", '
    '"setup_type": "BREAKOUT|PULLBACK|REVERSAL|LIQUIDITY_SWEEP|RANGE|NONE"}\n'
    "Only vote BUY or SELL if confidence > 0.55, otherwise HOLD.\n"
    "setup_type must be NONE if vote is HOLD."
)


class LLMAgent:
    """Synthesizer agent: Groq primary, NVIDIA fallback, Anthropic fallback on credit exhaustion."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("LLMAgent")

        # Groq state
        self._groq_client: Optional[OpenAI] = None
        self._groq_model: str = ""
        self._groq_exhausted: bool = False

        # NVIDIA state
        self._nvidia_client: Optional[OpenAI] = None
        self._nvidia_model: str = ""
        self._nvidia_exhausted: bool = False

        # Anthropic state
        self._anthropic_client = None
        self._anthropic_model: str = ""
        self._anthropic_exhausted: bool = False

        self._init_groq()
        self._init_nvidia()
        self._init_anthropic()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_groq(self) -> None:
        try:
            from config.settings import settings
            if not settings.GROQ_API_KEY:
                self.logger.warning("GROQ_API_KEY not set — Groq provider disabled")
                self._groq_exhausted = True
                return
            self._groq_client = OpenAI(
                api_key=settings.GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
            self._groq_model = settings.LLM_MODEL
            self.logger.info(f"LLMAgent: Groq initialised ({self._groq_model})")
        except ImportError:
            self.logger.warning("openai package not installed — Groq provider disabled")
            self._groq_exhausted = True
        except Exception as exc:
            self.logger.warning(f"LLMAgent: Groq init failed: {exc}")
            self._groq_exhausted = True

    def _init_nvidia(self) -> None:
        """NVIDIA fallback — activates when Groq credits are exhausted."""
        try:
            from config.settings import settings
            if not settings.NVIDIA_API_KEY:
                self.logger.info("NVIDIA_API_KEY not set — NVIDIA fallback disabled")
                self._nvidia_exhausted = True
                return
            self._nvidia_client = OpenAI(
                api_key=settings.NVIDIA_API_KEY,
                base_url="https://integrate.api.nvidia.com/v1",
            )
            self._nvidia_model = settings.NVIDIA_LLM_MODEL
            self.logger.info(f"LLMAgent: NVIDIA fallback ready ({self._nvidia_model})")
        except ImportError:
            self.logger.info("openai package not installed — NVIDIA fallback disabled")
            self._nvidia_exhausted = True
        except Exception as exc:
            self.logger.warning(f"LLMAgent: NVIDIA init failed: {exc}")
            self._nvidia_exhausted = True

    def _init_anthropic(self) -> None:
        try:
            from config.settings import settings
            if not settings.ANTHROPIC_API_KEY:
                self.logger.info("ANTHROPIC_API_KEY not set — Anthropic fallback disabled")
                self._anthropic_exhausted = True
                return
            import anthropic as _anthropic_sdk
            self._anthropic_client = _anthropic_sdk.Anthropic(
                api_key=settings.ANTHROPIC_API_KEY
            )
            self._anthropic_model = settings.ANTHROPIC_LLM_MODEL
            self.logger.info(
                f"LLMAgent: Anthropic fallback ready ({self._anthropic_model})"
            )
        except ImportError:
            self.logger.info(
                "anthropic package not installed — Anthropic fallback disabled"
            )
            self._anthropic_exhausted = True
        except Exception as exc:
            self.logger.warning(f"LLMAgent: Anthropic init failed: {exc}")
            self._anthropic_exhausted = True

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True if at least one provider can still service requests."""
        return not (self._groq_exhausted and self._nvidia_exhausted and self._anthropic_exhausted)

    @property
    def both_exhausted(self) -> bool:
        """True when all providers are exhausted."""
        return self._groq_exhausted and self._nvidia_exhausted and self._anthropic_exhausted

    @property
    def active_provider(self) -> str:
        """Human-readable name of the currently active provider."""
        if not self._groq_exhausted:
            return "groq"
        if not self._nvidia_exhausted:
            return "nvidia"
        if not self._anthropic_exhausted:
            return "anthropic"
        return "none"

    def vote(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: dict,
        macro_context: Optional[dict] = None,
        htf_candles: Optional[dict] = None,
    ) -> AgentVote:
        """Generate a synthesizer vote. Always returns an AgentVote — never raises."""
        if self.both_exhausted:
            return AgentVote(
                agent_name="LLMAgent",
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="All LLM providers exhausted",
            )
        try:
            return self._vote(pair, candles, price, indicators, macro_context, htf_candles)
        except Exception as exc:
            self.logger.warning(f"LLMAgent vote failed for {pair}: {exc}")
            return AgentVote(
                agent_name="LLMAgent",
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="LLM call failed",
            )

    # ------------------------------------------------------------------
    # Internal voting logic
    # ------------------------------------------------------------------

    def _vote(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: dict,
        macro_context: Optional[dict] = None,
        htf_candles: Optional[dict] = None,
    ) -> AgentVote:
        global _last_call_time

        # Rate-limit guard (shared across providers)
        elapsed = time.time() - _last_call_time
        if elapsed < _MIN_CALL_SPACING_SECONDS:
            time.sleep(_MIN_CALL_SPACING_SECONDS - elapsed)

        user_msg = _build_analyst_message(pair, candles, price, indicators, macro_context, htf_candles)
        _last_call_time = time.time()

        # Try Groq first
        if not self._groq_exhausted and self._groq_client is not None:
            try:
                return self._call_groq(user_msg, pair)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        f"LLMAgent: Groq credits exhausted — switching to Anthropic. ({exc})"
                    )
                    self._groq_exhausted = True
                else:
                    # Transient error — don't switch provider, just return HOLD for this cycle
                    self.logger.warning(f"LLMAgent: Groq transient error for {pair}: {exc}")
                    return AgentVote(
                        agent_name="LLMAgent",
                        pair=pair,
                        signal=Signal.HOLD,
                        confidence=0.5,
                        reasoning="Groq transient error",
                    )

        # Try NVIDIA fallback
        if not self._nvidia_exhausted and self._nvidia_client is not None:
            try:
                return self._call_nvidia(user_msg, pair)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        f"LLMAgent: NVIDIA credits exhausted — switching to Anthropic. ({exc})"
                    )
                    self._nvidia_exhausted = True
                else:
                    self.logger.warning(f"LLMAgent: NVIDIA transient error for {pair}: {exc}")
                    return AgentVote(
                        agent_name="LLMAgent",
                        pair=pair,
                        signal=Signal.HOLD,
                        confidence=0.5,
                        reasoning="NVIDIA transient error",
                    )

        # Try Anthropic fallback
        if not self._anthropic_exhausted and self._anthropic_client is not None:
            try:
                return self._call_anthropic(user_msg, pair)
            except Exception as exc:
                if _is_credit_exhausted(exc):
                    self.logger.warning(
                        f"LLMAgent: Anthropic credits exhausted — all providers down. ({exc})"
                    )
                    self._anthropic_exhausted = True
                else:
                    self.logger.warning(f"LLMAgent: Anthropic transient error for {pair}: {exc}")
                    return AgentVote(
                        agent_name="LLMAgent",
                        pair=pair,
                        signal=Signal.HOLD,
                        confidence=0.5,
                        reasoning="Anthropic transient error",
                    )

        # All exhausted
        return AgentVote(
            agent_name="LLMAgent",
            pair=pair,
            signal=Signal.HOLD,
            confidence=0.5,
            reasoning="All LLM providers exhausted",
        )

    def _call_groq(self, user_msg: str, pair: str) -> AgentVote:
        response = self._groq_client.chat.completions.create(
            model=self._groq_model,
            max_tokens=256,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        raw_text = response.choices[0].message.content.strip()
        return _parse_response(raw_text, pair)

    def _call_nvidia(self, user_msg: str, pair: str) -> AgentVote:
        response = self._nvidia_client.chat.completions.create(
            model=self._nvidia_model,
            max_tokens=256,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        raw_text = response.choices[0].message.content.strip()
        return _parse_response(raw_text, pair)

    def _call_anthropic(self, user_msg: str, pair: str) -> AgentVote:
        response = self._anthropic_client.messages.create(
            model=self._anthropic_model,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = response.content[0].text.strip()
        return _parse_response(raw_text, pair)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_analyst_message(
    pair: str,
    candles: List[Dict],
    price: float,
    indicators: dict,
    macro_context: Optional[dict] = None,
    htf_candles: Optional[dict] = None,
) -> str:
    """Construct the LLM analyst message with market context and indicator values."""
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

    ind_lines = []
    for key, val in indicators.items():
        if isinstance(val, float):
            ind_lines.append(f"  {key}: {val:.5f}")
        else:
            ind_lines.append(f"  {key}: {val}")
    ind_summary = "\n".join(ind_lines) if ind_lines else "  (none)"

    msg = (
        f"Pair: {pair}\n"
        f"Current price: {price}\n\n"
        f"Last 10 candles (H1):\n{candle_table}\n\n"
        f"Technical indicators:\n{ind_summary}\n\n"
    )

    if macro_context:
        macro_lines = []
        if macro_context.get('rate_differential'):
            macro_lines.append(f"  Rate differential: {macro_context['rate_differential']}")
        if macro_context.get('carry_bias'):
            macro_lines.append(f"  Carry bias: {macro_context['carry_bias']}")
        if macro_context.get('recent_news'):
            macro_lines.append(f"  Recent news:\n{macro_context['recent_news']}")
        if macro_context.get('upcoming_events'):
            macro_lines.append(f"  Upcoming events:\n{macro_context['upcoming_events']}")
        if macro_context.get('usd_sentiment'):
            usd = macro_context['usd_sentiment']
            score = usd.get('usd_score', 0.0)
            label = usd.get('usd_label', 'NEUTRAL')
            caution = ''
            if label == 'USD_STRONG':
                caution = ' — caution on EUR/GBP/AUD longs'
            elif label == 'USD_WEAK':
                caution = ' — caution on USD/JPY/CHF longs'
            macro_lines.append(
                f"  USD sentiment: {label} ({score:+.2f}){caution}"
            )
        if macro_lines:
            msg += "Macro context:\n" + "\n".join(macro_lines) + "\n\n"

    if htf_candles:
        htf_lines = ['Multi-timeframe context:']
        from .indicators import ema as _ema, adx as _adx, to_dataframe as _to_df
        for tf_label, tf_clist in htf_candles.items():
            if not tf_clist:
                continue
            try:
                tf_df = _to_df(tf_clist)
                tf_ema20 = _ema(tf_df, 20)
                tf_ema50 = _ema(tf_df, 50)
                tf_adx   = _adx(tf_df, 14)
                if tf_ema20 is not None and tf_ema50 is not None:
                    trend_str = 'bullish' if tf_ema20 > tf_ema50 else 'bearish'
                    adx_str   = f' | ADX={tf_adx:.1f}' if tf_adx is not None else ''
                    htf_lines.append(
                        f'  {tf_label} trend: {trend_str}'
                        f' | EMA20={tf_ema20:.5f} EMA50={tf_ema50:.5f}{adx_str}'
                    )
            except Exception:
                pass
        if len(htf_lines) > 1:
            msg += '\n'.join(htf_lines) + '\n\n'

    msg += "Based on the above, provide your synthesized trading signal as JSON."
    return msg


def _parse_response(raw: str, pair: str) -> AgentVote:
    """Parse LLM JSON response defensively."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "JSON parse error")
        else:
            return AgentVote("LLMAgent", pair, Signal.HOLD, 0.5, "JSON parse error")

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

    setup_type = str(data.get("setup_type", "NONE")).upper()
    valid_setup_types = {"BREAKOUT", "PULLBACK", "REVERSAL", "LIQUIDITY_SWEEP", "RANGE", "NONE"}
    if setup_type not in valid_setup_types:
        setup_type = "NONE"

    return AgentVote(
        agent_name="LLMAgent",
        pair=pair,
        signal=signal,
        confidence=round(confidence, 4),
        reasoning=reasoning,
        setup_type=setup_type,
    )
