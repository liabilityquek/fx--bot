"""Direction + confirmation-gate checks for the deterministic DecisionEngine.

DMI/ADX owns direction (ADX>=min gives permission, +DI vs -DI gives the side).
The two ported confirmation gates (area-of-value, RSI-turning) are pure functions.

Run with:
    python -m pytest tests/test_decision_confluence.py -v
"""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.agents.base import Signal
from src.voting.engine import DecisionEngine, _dmi_direction
from src.agents.indicators import ema20_pullback_ok, rsi_turning_ok, to_dataframe


PRICE = 1.1000
_MIN = settings.ADX_MIN_TREND

_BUY = {'adx': _MIN + 7, 'plus_di': 30.0, 'minus_di': 12.0}
_SELL = {'adx': _MIN + 7, 'plus_di': 12.0, 'minus_di': 30.0}
_RANGING = {'adx': _MIN - 5, 'plus_di': 30.0, 'minus_di': 12.0}
_TIE = {'adx': _MIN + 7, 'plus_di': 20.0, 'minus_di': 20.0}


def _engine_with(indicators):
    eng = DecisionEngine(logger=MagicMock())
    eng._tech = MagicMock();     eng._tech.get_indicators.return_value = indicators
    eng._trend = MagicMock();    eng._trend.get_indicators.return_value = {}
    eng._momentum = MagicMock(); eng._momentum.get_indicators.return_value = {}
    return eng


# ---- direction ------------------------------------------------------------

def test_adx_ok_plus_di_leads_buys():
    assert _dmi_direction(_BUY)[0] == Signal.BUY
    assert _engine_with(_BUY).run_decision("EUR_USD", [], PRICE).final_signal == Signal.BUY


def test_adx_ok_minus_di_leads_sells():
    assert _dmi_direction(_SELL)[0] == Signal.SELL
    assert _engine_with(_SELL).run_decision("EUR_USD", [], PRICE).final_signal == Signal.SELL


def test_adx_below_min_holds():
    assert _dmi_direction(_RANGING)[0] == Signal.HOLD
    res = _engine_with(_RANGING).run_decision("EUR_USD", [], PRICE)
    assert res.final_signal == Signal.HOLD and res.confidence == 0.0


def test_di_tie_holds():
    assert _dmi_direction(_TIE)[0] == Signal.HOLD


def test_missing_dmi_holds():
    assert _dmi_direction({})[0] == Signal.HOLD
    assert _dmi_direction({'adx': _MIN + 7})[0] == Signal.HOLD


# ---- confirmation gate: area of value -------------------------------------

def test_area_of_value_long():
    # at EMA20 -> pass; beyond ema20 + tol*atr -> fail; mirror for short.
    assert ema20_pullback_ok(1.1000, 1.1000, 0.0010, 1.5, True) is True
    assert ema20_pullback_ok(1.1030, 1.1000, 0.0010, 1.5, True) is False
    assert ema20_pullback_ok(1.1000, 1.1000, 0.0010, 1.5, False) is True
    assert ema20_pullback_ok(1.0970, 1.1000, 0.0010, 1.5, False) is False


def test_area_of_value_missing_is_none():
    assert ema20_pullback_ok(1.1000, None, 0.0010, 1.5, True) is None
    assert ema20_pullback_ok(1.1000, 1.1000, None, 1.5, True) is None
    assert ema20_pullback_ok(1.1000, 1.1000, 0.0, 1.5, True) is None


# ---- confirmation gate: RSI turning ---------------------------------------

def _candles(closes):
    return [{'open': c, 'high': c + 0.05, 'low': c - 0.05, 'close': c, 'volume': 100} for c in closes]

_RISING_TAIL = [100, 100, 100, 100, 100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 92, 94, 96, 98, 100, 103]
_FALLING_TAIL = [90, 90, 90, 90, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 98, 96, 94, 92, 90, 87]


def test_rsi_turning_up_confirms_long():
    df = to_dataframe(_candles(_RISING_TAIL))
    assert rsi_turning_ok(df, True) is True
    assert rsi_turning_ok(df, False) is False


def test_rsi_turning_down_confirms_short():
    df = to_dataframe(_candles(_FALLING_TAIL))
    assert rsi_turning_ok(df, False) is True
    assert rsi_turning_ok(df, True) is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
