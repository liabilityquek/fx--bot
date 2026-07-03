"""DecisionEngine — deterministic DMI/ADX direction pipeline.

Direction is owned by the classic Wilder DMI/ADX system:
  - ADX(14) >= ADX_MIN_TREND gives permission to trade (trend strong enough).
  - +DI vs -DI gives the side: +DI > -DI -> BUY, -DI > +DI -> SELL.
  - Otherwise HOLD (ranging market or no directional lean).

There is no confluence vote. The remaining indicators act as confirmation hard
gates downstream in the execution engine (area-of-value EMA20 pullback + RSI-turning
trigger) — they decide whether the DMI-chosen direction is actually executed.
SL/TP/sizing stay pure math downstream. No LLM, no reviewer.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.agents.base import Signal
from src.agents.momentum_agent import MomentumAgent
from src.agents.tech_agent import TechAgent
from src.agents.trend_agent import TrendAgent
from config.settings import settings


def _dmi_direction(indicators: dict) -> Tuple[Signal, List[str]]:
    """Classic Wilder DMI/ADX direction.

    Returns (signal, reasons). HOLD when ADX/DI are unavailable, ADX is below
    ADX_MIN_TREND (ranging), or +DI == -DI (no directional lean).
    """
    adx_v = indicators.get('adx')
    plus_di = indicators.get('plus_di')
    minus_di = indicators.get('minus_di')

    if adx_v is None or plus_di is None or minus_di is None:
        return Signal.HOLD, ["DMI/ADX unavailable"]
    if adx_v < settings.ADX_MIN_TREND:
        return Signal.HOLD, [f"ADX {adx_v:.0f} < {settings.ADX_MIN_TREND:.0f} (ranging)"]
    if plus_di == minus_di:
        return Signal.HOLD, [f"+DI == -DI {plus_di:.0f} (no lean)"]

    up = plus_di > minus_di
    reasons = [
        f"+DI {plus_di:.0f} {'>' if up else '<'} -DI {minus_di:.0f}",
        f"ADX {adx_v:.0f} >= {settings.ADX_MIN_TREND:.0f}",
    ]
    return (Signal.BUY if up else Signal.SELL), reasons


@dataclass
class DecisionResult:
    pair: str
    final_signal: Signal
    confidence: float           # ADX-scaled — informational, not gated
    reasoning: str
    indicators: dict = field(default_factory=dict)


class DecisionEngine:
    """Orchestrates the deterministic DMI/ADX direction pipeline."""

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
        """Run the DMI/ADX pipeline and return a DecisionResult.

        Direction is the classic Wilder system; the confirmation gates
        (area-of-value + RSI-turning) run downstream in the execution engine.
        """
        # 1. Collect indicators — direction needs adx/plus_di/minus_di; the
        #    downstream area-of-value gate needs ema20/atr; rsi is recorded only.
        indicators: dict = {}
        indicators.update(self._tech.get_indicators(pair, candles, price))
        indicators.update(self._trend.get_indicators(pair, candles, price))
        indicators.update(self._momentum.get_indicators(pair, candles, price))

        # 2. Direction from DMI/ADX
        signal, reasons = _dmi_direction(indicators)

        # 3. Confidence — ADX-scaled, informational only (no gate reads it)
        adx_v = indicators.get('adx')
        if signal == Signal.HOLD or adx_v is None:
            confidence = 0.0
        else:
            confidence = round(
                min(0.55 + max(0.0, adx_v - settings.ADX_MIN_TREND) * 0.01, 0.90), 4
            )

        reasoning = f"{signal.value} | " + " | ".join(reasons)

        result = DecisionResult(
            pair=pair,
            final_signal=signal,
            confidence=confidence,
            reasoning=reasoning,
            indicators=indicators,
        )
        self._last_results[pair] = result
        return result

    def get_analyst_summary(self) -> str:
        """Return last technical decision per pair — used by /analyst Telegram command."""
        if not self._last_results:
            return 'Decision Summary\n\nNo decisions yet this session.'

        lines = ['Decision Summary\n']
        for pair, result in self._last_results.items():
            lines.append(
                f'{pair}\n'
                f'  Signal:      {result.final_signal.value}\n'
                f'  Confidence:  {result.confidence:.2f}\n'
                f'  Reasoning:   {result.reasoning}'
            )
        return '\n\n'.join(lines)
