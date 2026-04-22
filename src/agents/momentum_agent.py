"""MomentumAgent — Fisher Transform + ATR."""

from typing import Dict, List, Optional

from .base import AgentVote, BaseAgent, Signal
from .indicators import atr, fisher_transform, to_dataframe


class MomentumAgent(BaseAgent):
    """Votes based on Fisher Transform crossovers and ATR volatility regime."""

    @property
    def name(self) -> str:
        return "MomentumAgent"

    def get_indicators(self, pair: str, candles: List[Dict], price: float) -> dict:
        """Return raw indicator values for use by the DecisionEngine pipeline."""
        df = to_dataframe(candles)
        result = {}

        fisher_result = fisher_transform(df, period=10)
        atr_val = atr(df, 14)

        if fisher_result is not None:
            fisher_now = fisher_result[0]
            result['fisher'] = round(fisher_now, 4)

        if atr_val is not None:
            result['atr'] = atr_val

        return result

    def _vote(self, pair: str, candles: List[Dict], price: float) -> AgentVote:
        df = to_dataframe(candles)

        fisher_result = fisher_transform(df, period=10)
        atr_val = atr(df, 14)

        if fisher_result is None:
            return AgentVote(
                agent_name=self.name,
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning="Insufficient data for Fisher Transform",
            )

        fisher_now, signal_now, fisher_prev, signal_prev = fisher_result

        # Crossover detection (primary signal)
        bull_cross = (fisher_prev < signal_prev) and (fisher_now >= signal_now)
        bear_cross = (fisher_prev > signal_prev) and (fisher_now <= signal_now)

        if bull_cross:
            signal = Signal.BUY
            confidence = 0.75
            reasoning = f"Fisher bullish cross ({fisher_now:.3f} vs sig {signal_now:.3f})"
        elif bear_cross:
            signal = Signal.SELL
            confidence = 0.75
            reasoning = f"Fisher bearish cross ({fisher_now:.3f} vs sig {signal_now:.3f})"
        elif fisher_now > 1.5:
            # Static threshold — secondary signal
            signal = Signal.SELL  # overbought → potential reversal
            confidence = 0.60
            reasoning = f"Fisher overbought ({fisher_now:.3f} > 1.5)"
        elif fisher_now < -1.5:
            signal = Signal.BUY   # oversold → potential reversal
            confidence = 0.60
            reasoning = f"Fisher oversold ({fisher_now:.3f} < -1.5)"
        else:
            signal = Signal.HOLD
            confidence = 0.50
            reasoning = f"No Fisher signal ({fisher_now:.3f})"

        # ATR regime modifier: high volatility → reduce confidence
        if atr_val is not None and signal != Signal.HOLD:
            avg_atr = _rolling_atr_avg(df, period=14, lookback=20)
            if avg_atr is not None and atr_val > avg_atr:
                confidence -= 0.10
                reasoning += f" | volatile ATR={atr_val:.5f} > avg={avg_atr:.5f}"

        confidence = max(0.0, min(1.0, confidence))

        return AgentVote(
            agent_name=self.name,
            pair=pair,
            signal=signal,
            confidence=round(confidence, 4),
            reasoning=reasoning,
        )


def _rolling_atr_avg(df, period: int = 14, lookback: int = 20) -> Optional[float]:
    """Return the average ATR over the last `lookback` bars."""
    if len(df) < period + lookback + 5:
        return None

    import pandas as pd
    high = df['high']
    low = df['low']
    close = df['close']

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_series = tr.rolling(window=period).mean()
    recent = atr_series.dropna().iloc[-lookback:]
    if len(recent) == 0:
        return None
    return float(recent.mean())
