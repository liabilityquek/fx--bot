"""
Unit tests for the SuperTrend indicator (src/agents/indicators.py).

Run with:
    python -m pytest tests/test_supertrend_indicator.py -v
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from src.agents.indicators import supertrend


def _df(closes, spread=0.0010):
    """Build an OHLC DataFrame from a close series (high/low = close ± spread)."""
    closes = list(closes)
    return pd.DataFrame({
        'open':   closes,
        'high':   [c + spread for c in closes],
        'low':    [c - spread for c in closes],
        'close':  closes,
        'volume': [100] * len(closes),
    })


def _trend_series(n_down=30, n_up=30, start=1.2000, step=0.0030):
    """V-shaped series: steady decline then steady rally — guarantees one flip."""
    down = [start - i * step for i in range(n_down)]
    up = [down[-1] + (i + 1) * step for i in range(n_up)]
    return down + up


class TestSuperTrend(unittest.TestCase):

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(supertrend(_df([1.1] * 5)))
        self.assertIsNone(supertrend(_df([1.1] * 14), period=10))  # < period + 5

    def test_uptrend_is_bullish_with_line_below_price(self):
        df = _df(_trend_series())
        result = supertrend(df, period=10, multiplier=3.0)
        self.assertIsNotNone(result)
        direction, _prev, line, _age = result
        self.assertEqual(direction, 1)
        self.assertLess(line, float(df['close'].iloc[-1]))

    def test_downtrend_is_bearish_with_line_above_price(self):
        # Rally then steady decline — must end bearish
        closes = [1.1000 + i * 0.0030 for i in range(30)]
        closes += [closes[-1] - (i + 1) * 0.0030 for i in range(30)]
        df = _df(closes)
        direction, _prev, line, _age = supertrend(df, period=10, multiplier=3.0)
        self.assertEqual(direction, -1)
        self.assertGreater(line, float(df['close'].iloc[-1]))

    def test_flip_age_zero_on_flip_bar(self):
        """Find the exact flip bar in a V-shaped series: flip_age must be 0 there."""
        closes = _trend_series()
        df = _df(closes)
        flip_index = None
        prev_dir = None
        for i in range(16, len(closes) + 1):
            res = supertrend(df.iloc[:i].reset_index(drop=True), period=10, multiplier=3.0)
            if res is None:
                continue
            direction = res[0]
            if prev_dir is not None and direction != prev_dir:
                flip_index = i
                d_now, d_prev, _line, age = res
                self.assertEqual(age, 0)
                self.assertNotEqual(d_now, d_prev)
            prev_dir = direction
        self.assertIsNotNone(flip_index, "expected at least one flip in a V-shaped series")

    def test_flip_age_increments_after_flip(self):
        closes = _trend_series()
        df = _df(closes)
        ages = []
        for i in range(16, len(closes) + 1):
            res = supertrend(df.iloc[:i].reset_index(drop=True), period=10, multiplier=3.0)
            if res is not None:
                ages.append(res[3])
        # After the flip the age sequence must contain 0 then 1 then 2 consecutively
        flip_pos = ages.index(0)
        self.assertEqual(ages[flip_pos:flip_pos + 3], [0, 1, 2])

    def test_lower_band_ratchets_in_uptrend(self):
        """During a sustained uptrend, the supertrend line must never decrease."""
        closes = _trend_series(n_down=15, n_up=45)
        df = _df(closes)
        lines = []
        for i in range(40, len(closes) + 1):
            res = supertrend(df.iloc[:i].reset_index(drop=True), period=10, multiplier=3.0)
            if res is not None and res[0] == 1:
                lines.append(res[2])
        self.assertGreater(len(lines), 5)
        for earlier, later in zip(lines, lines[1:]):
            self.assertGreaterEqual(later, earlier - 1e-12)

    def test_flat_market_does_not_flip_spuriously(self):
        """Tiny noise inside wide ATR bands must not cross the 3x ATR band."""
        rng = np.random.default_rng(42)
        closes = 1.1000 + rng.normal(0, 0.0001, 100).cumsum() * 0.01
        df = _df(list(closes))
        res = supertrend(df, period=10, multiplier=3.0)
        self.assertIsNotNone(res)
        direction, _prev, _line, age = res
        # One direction held for a long time — age far beyond the validity window
        self.assertGreater(age, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
