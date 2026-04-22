"""TrendAgent — EMA(20/50) crossover + ADX."""

from typing import Dict, List, Optional

from .base import AgentVote, BaseAgent, Signal
from .indicators import adx, atr, ema, to_dataframe


class TrendAgent(BaseAgent):
    """Votes based on EMA crossover direction, confirmed by ADX trend strength."""

    @property
    def name(self) -> str:
        return "TrendAgent"

    def get_indicators(self, pair: str, candles: List[Dict], price: float) -> dict:
        """Return raw indicator values for use by the DecisionEngine pipeline."""
        df = to_dataframe(candles)
        result = {}

        ema20 = ema(df, 20)
        ema50 = ema(df, 50)
        adx_val = adx(df, 14)

        if ema20 is not None:
            result['ema20'] = ema20
        if ema50 is not None:
            result['ema50'] = ema50
        if adx_val is not None:
            result['adx'] = round(adx_val, 4)

        if ema20 is not None and ema50 is not None:
            if ema20 > ema50:
                result['trend'] = 'bullish'
            elif ema20 < ema50:
                result['trend'] = 'bearish'
            else:
                result['trend'] = 'neutral'

        return result

    def _vote(self, pair: str, candles: List[Dict], price: float) -> AgentVote:
        df = to_dataframe(candles)

        ema20 = ema(df, 20)
        ema50 = ema(df, 50)
        adx_val = adx(df, 14)
        atr_val = atr(df, 14)

        if ema20 is None or ema50 is None:
            return AgentVote(
                agent_name=self.name,
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="Insufficient data for EMA calculation",
            )

        # Base direction from EMA crossover
        if ema20 > ema50:
            signal = Signal.BUY
            gap = ema20 - ema50
        else:
            signal = Signal.SELL
            gap = ema50 - ema20

        # Base confidence: recent cross vs established gap
        established_gap = (atr_val is not None) and (gap > 0.5 * atr_val)
        confidence = 0.70 if established_gap else 0.65

        # ADX modifier
        if adx_val is not None:
            if adx_val > 40:
                confidence += 0.10
            elif adx_val >= 25:
                confidence += 0.05
            elif adx_val < 20:
                confidence -= 0.15

        confidence = max(0.50, min(1.0, confidence))

        reasoning = f"EMA20={ema20:.5f} EMA50={ema50:.5f}"
        if adx_val is not None:
            reasoning += f" ADX={adx_val:.1f}"

        return AgentVote(
            agent_name=self.name,
            pair=pair,
            signal=signal,
            confidence=round(confidence, 4),
            reasoning=reasoning,
        )
