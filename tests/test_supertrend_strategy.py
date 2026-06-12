"""
Unit tests for SuperTrendEmaStrategy (src/strategies/supertrend_ema.py).

Uses a reduced EMA period (20) so test fixtures stay small; the rule logic is
period-independent.

Run with:
    python -m pytest tests/test_supertrend_strategy.py -v
"""

import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.agents.base import Signal
from src.strategies import SuperTrendEmaStrategy


def _candles(closes, spread=0.0010):
    return [
        {
            'open': c, 'high': c + spread, 'low': c - spread,
            'close': c, 'volume': 100,
        }
        for c in closes
    ]


def _v_shape(n_down=40, n_up=20, start=1.2000, step=0.0030):
    down = [start - i * step for i in range(n_down)]
    up = [down[-1] + (i + 1) * step for i in range(n_up)]
    return down + up


def _find_fresh_flip_series(closes, direction=1, max_age=0):
    """Trim the series so the last closed bar has a flip of `direction` with flip_age == max_age."""
    from src.agents.indicators import supertrend, to_dataframe
    candles = _candles(closes)
    for i in range(20, len(closes) + 1):
        df = to_dataframe(candles[:i])
        res = supertrend(df, settings.STRATEGY_SUPERTREND_PERIOD,
                         settings.STRATEGY_SUPERTREND_MULTIPLIER)
        if res is not None and res[3] == max_age and res[0] == direction:
            return candles[:i]
    raise AssertionError(f"no fresh flip (dir={direction}) found in fixture series")


class TestSuperTrendStrategy(unittest.TestCase):

    def setUp(self):
        self.strategy = SuperTrendEmaStrategy()
        patcher = patch.object(settings, 'STRATEGY_EMA_PERIOD', 20)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bullish_flip_above_ema_is_buy(self):
        # Long flat base keeps EMA20 low; rally flips ST bullish above the EMA
        closes = [1.1000 + i * 0.00001 for i in range(60)] + \
                 [1.1006 + (i + 1) * 0.0040 for i in range(15)]
        candles = _find_fresh_flip_series(closes)
        price = candles[-1]['close']
        vote = self.strategy.vote('EUR_USD', candles, price, {})
        self.assertEqual(vote.signal, Signal.BUY)
        self.assertEqual(vote.setup_type, 'BREAKOUT')
        self.assertGreaterEqual(vote.confidence, 0.60)

    def test_bearish_flip_below_ema_is_sell(self):
        # Gentle rally first (ST turns bullish, EMA hugs price), then a sharp
        # drop produces a genuine bearish FLIP with price already below the EMA
        closes = [1.1000 + i * 0.0008 for i in range(60)]
        closes += [closes[-1] - (i + 1) * 0.0050 for i in range(20)]
        candles = _find_fresh_flip_series(closes, direction=-1)
        vote = self.strategy.vote('EUR_USD', candles, candles[-1]['close'], {})
        self.assertEqual(vote.signal, Signal.SELL)

    def test_bullish_flip_below_ema_is_hold(self):
        """ST flips bullish but price is still below EMA200 → trend filter blocks."""
        # Strong decline (EMA high above price), then a sharp bounce that flips
        # ST bullish while price remains far below the EMA
        closes = [1.2000 - i * 0.0030 for i in range(50)] + \
                 [1.0530 + (i + 1) * 0.0060 for i in range(20)]
        candles = _find_fresh_flip_series(closes, direction=1)
        close = candles[-1]['close']
        from src.agents.indicators import ema, to_dataframe
        ema_val = ema(to_dataframe(candles), 20)
        self.assertLess(close, ema_val, "fixture invalid: bounce overshot the EMA")
        vote = self.strategy.vote('EUR_USD', candles, close, {})
        self.assertEqual(vote.signal, Signal.HOLD)
        self.assertIn('EMA', vote.reasoning)

    def test_stale_flip_is_hold(self):
        """Direction is bullish but the flip is older than the validity window."""
        closes = _v_shape(n_down=30, n_up=40)  # flip long past
        candles = _candles(closes)
        with patch.object(settings, 'STRATEGY_SIGNAL_VALIDITY_BARS', 3):
            vote = self.strategy.vote('EUR_USD', candles, closes[-1], {})
        self.assertEqual(vote.signal, Signal.HOLD)
        self.assertIn('no fresh flip', vote.reasoning)

    def test_insufficient_data_is_hold(self):
        vote = self.strategy.vote('EUR_USD', _candles([1.1] * 10), 1.1, {})
        self.assertEqual(vote.signal, Signal.HOLD)
        self.assertIn('Insufficient data', vote.reasoning)

    def test_garbage_candles_hold_without_raising(self):
        vote = self.strategy.vote('EUR_USD', [{'bogus': True}] * 300, 1.1, {})
        self.assertEqual(vote.signal, Signal.HOLD)

    def test_confidence_adx_bonus_and_clamps(self):
        closes = [1.1000 + i * 0.00001 for i in range(60)] + \
                 [1.1006 + (i + 1) * 0.0040 for i in range(15)]
        candles = _find_fresh_flip_series(closes)
        price = candles[-1]['close']

        low_adx = self.strategy.vote('EUR_USD', candles, price, {'adx': 15.0})
        high_adx = self.strategy.vote('EUR_USD', candles, price, {'adx': 35.0})
        self.assertEqual(low_adx.signal, Signal.BUY)
        self.assertEqual(high_adx.signal, Signal.BUY)
        self.assertGreater(high_adx.confidence, low_adx.confidence)
        self.assertLessEqual(high_adx.confidence, 0.95)
        self.assertGreaterEqual(low_adx.confidence, 0.60)

    def test_strategy_values_merged_into_indicators(self):
        closes = [1.1000 + i * 0.00001 for i in range(60)] + \
                 [1.1006 + (i + 1) * 0.0040 for i in range(15)]
        candles = _find_fresh_flip_series(closes)
        indicators = {}
        self.strategy.vote('EUR_USD', candles, candles[-1]['close'], indicators)
        self.assertIn('supertrend_dir', indicators)
        self.assertIn('supertrend_line', indicators)
        self.assertIn('ema200', indicators)


if __name__ == "__main__":
    unittest.main(verbosity=2)
