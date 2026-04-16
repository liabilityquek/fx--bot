"""TechAgent — RSI + MACD + Bollinger Bands."""

from typing import Dict, List

from .base import AgentVote, BaseAgent, Signal
from .indicators import bollinger_bands, macd, rsi, to_dataframe


class TechAgent(BaseAgent):
    """Votes based on RSI, MACD histogram, and Bollinger Band position."""

    @property
    def name(self) -> str:
        return "TechAgent"

    def _vote(self, pair: str, candles: List[Dict], price: float) -> AgentVote:
        df = to_dataframe(candles)

        rsi_val   = rsi(df)
        macd_vals = macd(df)
        bb_vals   = bollinger_bands(df)

        score = 0.0
        max_score = 0.0

        # RSI component (max contribution ±0.3)
        if rsi_val is not None:
            max_score += 0.3
            if rsi_val < 30:
                score += 0.3
            elif rsi_val < 40:
                score += 0.1
            elif rsi_val > 70:
                score -= 0.3
            elif rsi_val > 60:
                score -= 0.1

        # MACD component (max contribution ±0.2)
        if macd_vals is not None:
            max_score += 0.2
            _, _, histogram = macd_vals
            # Need previous histogram to check "growing"
            macd_prev = _macd_prev_histogram(df)
            if histogram > 0 and (macd_prev is None or histogram > macd_prev):
                score += 0.2
            elif histogram < 0 and (macd_prev is None or histogram < macd_prev):
                score -= 0.2

        # Bollinger Bands component (max contribution ±0.2)
        if bb_vals is not None:
            max_score += 0.2
            upper, _, lower = bb_vals
            if price < lower:
                score += 0.2
            elif price > upper:
                score -= 0.2

        # Map to signal
        if score > 0.2:
            signal = Signal.BUY
        elif score < -0.2:
            signal = Signal.SELL
        else:
            signal = Signal.HOLD

        # Confidence: 0.5 + (|score| / max_possible) * 0.5
        max_possible = 0.7  # sum of all component maxima
        confidence = 0.5 + (abs(score) / max_possible) * 0.5
        confidence = max(0.0, min(1.0, confidence))

        # Build reasoning
        parts = []
        if rsi_val is not None:
            parts.append(f"RSI={rsi_val:.1f}")
        if macd_vals is not None:
            parts.append(f"MACD_hist={macd_vals[2]:.5f}")
        if bb_vals is not None:
            parts.append(f"BB_upper={bb_vals[0]:.5f} lower={bb_vals[2]:.5f}")
        reasoning = "; ".join(parts) if parts else "insufficient data"

        return AgentVote(
            agent_name=self.name,
            pair=pair,
            signal=signal,
            confidence=round(confidence, 4),
            reasoning=reasoning,
        )


def _macd_prev_histogram(df):
    """Return the MACD histogram from the second-to-last bar, or None."""
    if len(df) < 35:
        return None
    from .indicators import macd as _macd
    result = _macd(df.iloc[:-1])
    return result[2] if result is not None else None
