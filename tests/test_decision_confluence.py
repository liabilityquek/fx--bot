"""Confluence-direction picker checks for the deterministic DecisionEngine.

Bullish-skewed indicators -> BUY, bearish -> SELL, balanced/insufficient -> HOLD.

Run with:
    python -m pytest tests/test_decision_confluence.py -v
"""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.agents.base import Signal
from src.voting.engine import DecisionEngine, _count_indicator_confluences


PRICE = 1.1000

# bb_mid set so price 1.1000 sits >15% of the band above/below the midline.
_BULLISH = {
    'rsi': 60, 'macd_hist': 0.5, 'trend': 'bullish', 'fisher': 1.2,
    'bb_upper': 1.1050, 'bb_lower': 1.0950, 'bb_mid': 1.0980,
    'market_structure': 'bullish_structure',
}
_BEARISH = {
    'rsi': 40, 'macd_hist': -0.5, 'trend': 'bearish', 'fisher': -1.2,
    'bb_upper': 1.1050, 'bb_lower': 1.0950, 'bb_mid': 1.1020,
    'market_structure': 'bearish_structure',
}
# One indicator each way, nothing else → tie at low count.
_BALANCED = {'rsi': 60, 'macd_hist': -0.5}


def _engine_with(indicators):
    eng = DecisionEngine(logger=MagicMock())
    eng._tech = MagicMock();     eng._tech.get_indicators.return_value = indicators
    eng._trend = MagicMock();    eng._trend.get_indicators.return_value = {}
    eng._momentum = MagicMock(); eng._momentum.get_indicators.return_value = {}
    return eng


def test_bullish_indicators_produce_buy():
    res = _engine_with(_BULLISH).run_decision("EUR_USD", [], PRICE)
    assert res.final_signal == Signal.BUY
    assert res.confluence_count >= settings.MIN_CONFLUENCES


def test_bearish_indicators_produce_sell():
    res = _engine_with(_BEARISH).run_decision("EUR_USD", [], PRICE)
    assert res.final_signal == Signal.SELL
    assert res.confluence_count >= settings.MIN_CONFLUENCES


def test_balanced_or_insufficient_holds():
    res = _engine_with(_BALANCED).run_decision("EUR_USD", [], PRICE)
    assert res.final_signal == Signal.HOLD


def test_counter_mirror_is_opposite():
    long_c, _ = _count_indicator_confluences(_BULLISH, True, PRICE)
    short_c, _ = _count_indicator_confluences(_BULLISH, False, PRICE)
    assert long_c == 6 and short_c == 0


def test_di_confluence_only_counts_when_enabled():
    from unittest.mock import patch
    bull_di = {**_BULLISH, 'plus_di': 30.0, 'minus_di': 10.0}
    # Off by default → DI ignored, still 6.
    with patch.object(settings, 'DI_CONFLUENCE_ENABLED', False):
        assert _count_indicator_confluences(bull_di, True, PRICE)[0] == 6
    # Enabled → +DI>-DI adds a 7th on the long side, 0 on the short side.
    with patch.object(settings, 'DI_CONFLUENCE_ENABLED', True):
        long_c, long_t = _count_indicator_confluences(bull_di, True, PRICE)
        short_c, _ = _count_indicator_confluences(bull_di, False, PRICE)
        assert long_c == 7 and 'DI' in long_t and short_c == 0


if __name__ == "__main__":
    test_bullish_indicators_produce_buy()
    test_bearish_indicators_produce_sell()
    test_balanced_or_insufficient_holds()
    test_counter_mirror_is_opposite()
    print("ok")
