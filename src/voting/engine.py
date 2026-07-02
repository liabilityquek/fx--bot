"""DecisionEngine — deterministic technical-confluence decision pipeline.

Pipeline per cycle:
  1. TechAgent/TrendAgent/MomentumAgent.get_indicators() → merged indicators dict
  2. Count directional confluences for long and short sides
  3. Pick the side with more aligned indicators; must hit MIN_CONFLUENCES, else HOLD
  4. confidence = confluence_count / 6.0  (informational only, NOT a gate)

No LLM, no reviewer, no consensus threshold. SL/TP/sizing stay pure math downstream.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.agents.base import Signal
from src.agents.momentum_agent import MomentumAgent
from src.agents.tech_agent import TechAgent
from src.agents.trend_agent import TrendAgent
from config.settings import settings


def _count_indicator_confluences(
    indicators: dict, is_long: bool, price: float
) -> tuple:
    """
    Count indicator signals aligned with the trade direction.
    Returns (count, [list of confluence names]).
    Max 6 directional confluences: RSI, MACD, EMA trend, Fisher, Bollinger, Market Structure.
    ADX is a pre-gate (trend strength only, not direction) — checked in _is_adx_trending().
    """
    aligned = []

    rsi_val = indicators.get('rsi')
    if rsi_val is not None:
        if is_long and rsi_val > 50:
            aligned.append('RSI')
        elif not is_long and rsi_val < 50:
            aligned.append('RSI')

    macd_hist = indicators.get('macd_hist')
    if macd_hist is not None:
        if is_long and macd_hist > 0:
            aligned.append('MACD')
        elif not is_long and macd_hist < 0:
            aligned.append('MACD')

    trend = indicators.get('trend')
    if trend is not None:
        if is_long and trend == 'bullish':
            aligned.append('EMA trend')
        elif not is_long and trend == 'bearish':
            aligned.append('EMA trend')

    fisher_val = indicators.get('fisher')
    if fisher_val is not None:
        if is_long and fisher_val > 0:
            aligned.append('Fisher')
        elif not is_long and fisher_val < 0:
            aligned.append('Fisher')

    bb_upper = indicators.get('bb_upper')
    bb_lower = indicators.get('bb_lower')
    bb_mid   = indicators.get('bb_mid')
    if bb_upper and bb_lower and bb_mid and price > 0:
        band_range = bb_upper - bb_lower
        if band_range > 0:
            # Only count Bollinger as confluence when price is meaningfully above/below midline
            # (at least 15% into the upper or lower half of the band)
            threshold = band_range * 0.15
            if is_long and price > bb_mid + threshold:
                aligned.append('Bollinger')
            elif not is_long and price < bb_mid - threshold:
                aligned.append('Bollinger')

    ms = indicators.get('market_structure')
    if ms is not None:
        if is_long and ms == 'bullish_structure':
            aligned.append('Market Structure')
        elif not is_long and ms == 'bearish_structure':
            aligned.append('Market Structure')

    return len(aligned), aligned


@dataclass
class DecisionResult:
    pair: str
    final_signal: Signal
    confidence: float           # confluence_count / 6.0 — informational, not gated
    reasoning: str
    indicators: dict = field(default_factory=dict)
    confluence_count: int = 0
    confluence_types: list = field(default_factory=list)


class DecisionEngine:
    """Orchestrates the deterministic technical-confluence decision pipeline."""

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        alert_manager=None,
        event_monitor=None,
    ):
        self.logger = logger or logging.getLogger("DecisionEngine")
        self._alert_manager = alert_manager

        self._tech     = TechAgent(logger)
        self._trend    = TrendAgent(logger)
        self._momentum = MomentumAgent(logger)

        self._last_results: dict = {}   # pair -> DecisionResult

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_decision(self, pair: str, candles: List[Dict], price: float, pair_prices: Optional[dict] = None, htf_candles: Optional[dict] = None) -> DecisionResult:
        """Run the deterministic pipeline and return a DecisionResult.

        Direction is chosen by whichever side (long/short) has more aligned
        indicator confluences. Ties or an insufficient count → HOLD.
        """
        # 1. Collect indicators from all three tech agents
        indicators: dict = {}
        indicators.update(self._tech.get_indicators(pair, candles, price))
        indicators.update(self._trend.get_indicators(pair, candles, price))
        indicators.update(self._momentum.get_indicators(pair, candles, price))

        # 2. Count directional confluences for both sides
        long_conf, long_types   = _count_indicator_confluences(indicators, True, price)
        short_conf, short_types = _count_indicator_confluences(indicators, False, price)

        # 3. Pick direction
        best = max(long_conf, short_conf)
        if best < settings.MIN_CONFLUENCES or long_conf == short_conf:
            signal = Signal.HOLD
            count, types = best, (long_types if long_conf >= short_conf else short_types)
        elif long_conf > short_conf:
            signal = Signal.BUY
            count, types = long_conf, long_types
        else:
            signal = Signal.SELL
            count, types = short_conf, short_types

        # 4. Confidence is informational only (max 6 confluences)
        confidence = round(count / 6.0, 4)
        reasoning = f"{signal.value} {long_conf}/{short_conf} [{', '.join(types)}]"

        result = DecisionResult(
            pair=pair,
            final_signal=signal,
            confidence=confidence,
            reasoning=reasoning,
            indicators=indicators,
            confluence_count=count,
            confluence_types=types,
        )
        self._last_results[pair] = result
        return result

    def get_analyst_summary(self) -> str:
        """Return last technical decision per pair — used by /analyst Telegram command."""
        if not self._last_results:
            return 'Decision Summary\n\nNo decisions yet this session.'

        lines = ['Decision Summary\n']
        for pair, result in self._last_results.items():
            conf_str = (
                f"{result.confluence_count}/{settings.MIN_CONFLUENCES} "
                f"[{', '.join(result.confluence_types)}]"
                if result.confluence_types else "not yet computed"
            )
            lines.append(
                f'{pair}\n'
                f'  Signal:      {result.final_signal.value}\n'
                f'  Confidence:  {result.confidence:.2f}\n'
                f'  Confluences: {conf_str}\n'
                f'  Reasoning:   {result.reasoning}'
            )
        return '\n\n'.join(lines)
