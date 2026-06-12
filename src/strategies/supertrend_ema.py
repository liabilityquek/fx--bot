"""SuperTrendEmaStrategy — deterministic SuperTrend + EMA200 trend-following.

One of TradingView's most popular mechanical systems:
  BUY  — SuperTrend flips bullish (within the validity window) AND close > EMA200
  SELL — SuperTrend flips bearish (within the validity window) AND close < EMA200
  HOLD — otherwise

The vote() signature mirrors LLMAgent.vote so DecisionEngine can use either
interchangeably (STRATEGY_MODE setting). Contract: vote() never raises —
returns HOLD(0.5) on any failure, same as BaseAgent.
"""

import logging
from typing import Dict, List, Optional

from config.settings import settings
from src.agents.base import AgentVote, Signal
from src.agents.indicators import atr as _atr, ema as _ema, supertrend as _supertrend, to_dataframe


class SuperTrendEmaStrategy:
    """Deterministic analyst: SuperTrend flip entries filtered by EMA200 trend."""

    name = "SuperTrendEMA"

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("SuperTrendEmaStrategy")

    def vote(
        self,
        pair: str,
        candles: List[Dict],
        price: float,
        indicators: Optional[dict] = None,
        macro_context: Optional[dict] = None,
        htf_candles: Optional[dict] = None,
    ) -> AgentVote:
        """Generate a deterministic vote. Always returns an AgentVote — never raises."""
        try:
            return self._vote(
                pair, candles, price,
                indicators if indicators is not None else {},
            )
        except Exception as exc:
            self.logger.warning(f"{self.name} vote failed for {pair}: {exc}")
            return AgentVote(
                agent_name=self.name,
                pair=pair,
                signal=Signal.HOLD,
                confidence=0.5,
                reasoning=f"Strategy error: {exc}",
            )

    def _vote(self, pair: str, candles: List[Dict], price: float, indicators: dict) -> AgentVote:
        df = to_dataframe(candles)

        st = _supertrend(
            df,
            period=settings.STRATEGY_SUPERTREND_PERIOD,
            multiplier=settings.STRATEGY_SUPERTREND_MULTIPLIER,
        )
        ema200 = _ema(df, settings.STRATEGY_EMA_PERIOD)

        if st is None or ema200 is None:
            return AgentVote(
                agent_name=self.name, pair=pair, signal=Signal.HOLD, confidence=0.5,
                reasoning=(
                    f"Insufficient data for SuperTrend/EMA{settings.STRATEGY_EMA_PERIOD} "
                    f"({len(candles)} candles)"
                ),
            )

        direction, _direction_prev, line, flip_age = st
        close = float(df['close'].iloc[-1])

        # Expose strategy values so the engine can log/alert on them
        indicators['supertrend_dir'] = direction
        indicators['supertrend_line'] = round(line, 5)
        indicators['ema200'] = round(ema200, 5)

        fresh = flip_age < settings.STRATEGY_SIGNAL_VALIDITY_BARS

        if direction == 1 and fresh and close > ema200:
            signal = Signal.BUY
        elif direction == -1 and fresh and close < ema200:
            signal = Signal.SELL
        else:
            if not fresh:
                why = f"no fresh flip (age={flip_age} bars, dir={'bull' if direction == 1 else 'bear'})"
            elif direction == 1:
                why = f"bull flip but close {close:.5f} <= EMA200 {ema200:.5f}"
            else:
                why = f"bear flip but close {close:.5f} >= EMA200 {ema200:.5f}"
            return AgentVote(
                agent_name=self.name, pair=pair, signal=Signal.HOLD,
                confidence=0.5, reasoning=f"HOLD — {why}",
            )

        confidence = self._confidence(df, close, ema200, flip_age, indicators)
        side = 'bull' if signal == Signal.BUY else 'bear'
        reasoning = (
            f"ST flip {side} age={flip_age}, close {close:.5f} "
            f"{'>' if signal == Signal.BUY else '<'} EMA200 {ema200:.5f}, "
            f"ADX {indicators.get('adx', 'n/a')}"
        )[:120]

        return AgentVote(
            agent_name=self.name,
            pair=pair,
            signal=signal,
            confidence=round(confidence, 4),
            reasoning=reasoning,
            setup_type="BREAKOUT",
        )

    def _confidence(
        self, df, close: float, ema200: float, flip_age: int, indicators: dict
    ) -> float:
        """Deterministic confidence: base + trend-quality bonuses - late-entry decay."""
        conf = settings.STRATEGY_BASE_CONFIDENCE

        adx_val = indicators.get('adx')
        if isinstance(adx_val, (int, float)):
            if adx_val >= 25:
                conf += 0.05
            if adx_val >= 30:
                conf += 0.05

        atr_val = _atr(df, 14)
        if atr_val and abs(close - ema200) >= 0.5 * atr_val:
            conf += 0.05

        # EMA200 slope agreement: compare against the EMA200 ten bars back
        ema200_back = _ema(df.iloc[:-10], settings.STRATEGY_EMA_PERIOD) if len(df) > 10 else None
        if ema200_back is not None:
            slope_up = ema200 > ema200_back
            if (close > ema200 and slope_up) or (close < ema200 and not slope_up):
                conf += 0.05

        conf -= 0.03 * flip_age

        return max(0.60, min(0.95, conf))
