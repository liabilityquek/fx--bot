"""
Unit tests for the backtest simulator (src/backtest/simulator.py).

Run with:
    python -m pytest tests/test_backtest_simulator.py -v
"""

import sys
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.backtest.simulator import (
    BacktestConfig,
    BacktestSimulator,
    SimPosition,
    _half_spread,
    _to_usd,
)


def _config(pairs=None):
    return BacktestConfig(
        pairs=pairs or ['EUR_USD'],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2025, 1, 1, tzinfo=timezone.utc),
        balance=10_000.0,
    )


def _candles(closes, start=None, spread=0.0010):
    start = start or datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    out = []
    t = start
    for c in closes:
        out.append({
            'time': t.strftime('%Y-%m-%dT%H:%M:%S.000000000Z'),
            'open': c, 'high': c + spread, 'low': c - spread,
            'close': c, 'volume': 100,
        })
        t += timedelta(hours=1)
    return out


def _position(entry=1.1000, sl=1.0950, tp=1.1100, side='long', units=100_000):
    return SimPosition(
        pair='EUR_USD', side=side, units=units, initial_units=units,
        entry_price=entry,
        entry_time=datetime(2024, 1, 2, 10, tzinfo=timezone.utc),
        sl=sl, tp=tp,
        initial_sl_distance=abs(entry - sl),
        risk_usd=abs(entry - sl) * units,
        peak=entry, trough=entry,
    )


def _bar(o, h, l, c, ts='2024-01-02T12:00:00.000000000Z'):
    return {'time': ts, 'open': o, 'high': h, 'low': l, 'close': c, 'volume': 100}


class TestManageBar(unittest.TestCase):

    def setUp(self):
        self.sim = BacktestSimulator(_config())
        self.ts = datetime(2024, 1, 2, 12, tzinfo=timezone.utc)

    def test_sl_only_hit_closes_at_sl(self):
        pos = _position()
        pnl, exit_px, reason = self.sim._manage_bar(
            pos, _bar(1.0990, 1.0995, 1.0940, 1.0960), self.ts, hs=0.0, atr_now=0.001
        )
        self.assertEqual(reason, 'sl')
        self.assertAlmostEqual(exit_px, 1.0950)
        self.assertLess(pnl, 0)

    def test_both_sl_and_tp_touched_sl_fills_first(self):
        pos = _position()
        _pnl, _exit_px, reason = self.sim._manage_bar(
            pos, _bar(1.1000, 1.1150, 1.0940, 1.1000), self.ts, hs=0.0, atr_now=0.001
        )
        self.assertEqual(reason, 'sl')

    def test_gap_open_beyond_sl_fills_at_open(self):
        pos = _position()
        _pnl, exit_px, reason = self.sim._manage_bar(
            pos, _bar(1.0900, 1.0920, 1.0890, 1.0910), self.ts, hs=0.0, atr_now=0.001
        )
        self.assertEqual(reason, 'sl')
        self.assertAlmostEqual(exit_px, 1.0900)  # open, not the SL level

    def test_tp_hit_takes_partial_first(self):
        pos = _position()  # SL dist 50 pips, TP at +100 pips, 1R partial at +50
        with patch.object(settings, 'PARTIAL_TP_ENABLED', True), \
             patch.object(settings, 'PARTIAL_TP_RATIO', 0.5), \
             patch.object(settings, 'PARTIAL_TP_RR_TARGET', 1.0):
            pnl, _exit_px, reason = self.sim._manage_bar(
                pos, _bar(1.1010, 1.1110, 1.1005, 1.1100), self.ts, hs=0.0, atr_now=0.001
            )
        self.assertEqual(reason, 'tp')
        self.assertTrue(pos.partial_done)
        # 50k units at +50 pips (partial) + 50k at +100 pips (TP) = 250 + 500
        self.assertAlmostEqual(pnl, 0.0050 * 50_000 + 0.0100 * 50_000, places=2)

    def test_partial_at_one_r_halves_units_and_moves_sl_to_be(self):
        pos = _position()
        with patch.object(settings, 'PARTIAL_TP_ENABLED', True), \
             patch.object(settings, 'PARTIAL_TP_RATIO', 0.5), \
             patch.object(settings, 'PARTIAL_TP_RR_TARGET', 1.0):
            pnl, exit_px, _reason = self.sim._manage_bar(
                pos, _bar(1.1010, 1.1055, 1.1005, 1.1040), self.ts, hs=0.0, atr_now=0.001
            )
        self.assertIsNone(exit_px)          # still open
        self.assertEqual(pos.units, 50_000)  # half closed
        self.assertGreater(pnl, 0)
        self.assertGreater(pos.sl, pos.entry_price)  # SL at break-even + buffer
        self.assertTrue(pos.be_triggered)

    def test_break_even_at_half_r(self):
        pos = _position()  # 0.5R = +25 pips
        with patch.object(settings, 'PARTIAL_TP_ENABLED', False), \
             patch.object(settings, 'BREAK_EVEN_TRIGGER_R', 0.5):
            _pnl, exit_px, _r = self.sim._manage_bar(
                pos, _bar(1.1010, 1.1030, 1.1005, 1.1020), self.ts, hs=0.0, atr_now=0.001
            )
        self.assertIsNone(exit_px)
        self.assertGreater(pos.sl, pos.entry_price)

    def test_no_be_below_half_r(self):
        pos = _position()
        with patch.object(settings, 'PARTIAL_TP_ENABLED', False), \
             patch.object(settings, 'BREAK_EVEN_TRIGGER_R', 0.5):
            self.sim._manage_bar(
                pos, _bar(1.1005, 1.1015, 1.1000, 1.1010), self.ts, hs=0.0, atr_now=0.001
            )
        self.assertAlmostEqual(pos.sl, 1.0950)

    def test_time_stop_closes_losing_trade(self):
        pos = _position()
        with patch.object(settings, 'TIME_STOP_ENABLED', True), \
             patch.object(settings, 'TIME_STOP_HOURS', 48.0), \
             patch('src.backtest.simulator._market_hours_elapsed', return_value=50.0):
            _pnl, exit_px, reason = self.sim._manage_bar(
                pos, _bar(1.0990, 1.0995, 1.0970, 1.0980), self.ts, hs=0.0, atr_now=0.001
            )
        self.assertEqual(reason, 'time_stop')
        self.assertAlmostEqual(exit_px, 1.0980)

    def test_time_stop_keeps_winning_trade(self):
        pos = _position()
        with patch.object(settings, 'TIME_STOP_ENABLED', True), \
             patch.object(settings, 'PARTIAL_TP_ENABLED', False), \
             patch.object(settings, 'BREAK_EVEN_TRIGGER_R', 5.0), \
             patch('src.backtest.simulator._market_hours_elapsed', return_value=50.0):
            _pnl, exit_px, _r = self.sim._manage_bar(
                pos, _bar(1.1005, 1.1015, 1.1002, 1.1010), self.ts, hs=0.0, atr_now=0.001
            )
        self.assertIsNone(exit_px)

    def test_trailing_ratchets_after_one_r(self):
        pos = _position()
        with patch.object(settings, 'PARTIAL_TP_ENABLED', False), \
             patch.object(settings, 'BREAK_EVEN_TRIGGER_R', 5.0), \
             patch.object(settings, 'TRAILING_STOP_ACTIVATION_R', 1.0), \
             patch.object(settings, 'TRAILING_ATR_MULTIPLIER', 1.5):
            # +60 pip high = peak profit 1.2R -> trailing active
            self.sim._manage_bar(
                pos, _bar(1.1010, 1.1060, 1.1005, 1.1050), self.ts, hs=0.0, atr_now=0.0010
            )
        # SL trailed to peak - 1.5*ATR = 1.1060 - 0.0015 = 1.1045
        self.assertAlmostEqual(pos.sl, 1.1045, places=4)


class TestOpenPosition(unittest.TestCase):

    def test_geometry_tp_is_ratio_times_sl(self):
        sim = BacktestSimulator(_config())
        closes = [1.1000 + i * 0.0002 for i in range(120)]
        window = _candles(closes)
        pending = {'is_long': True, 'window': window}
        with patch.object(settings, 'DEFAULT_TAKE_PROFIT_RATIO', 2.0):
            pos = sim._open_position(
                'EUR_USD', pending, 1.1240, datetime(2024, 1, 8, tzinfo=timezone.utc),
                hs=0.0, balance=10_000.0, consecutive_losses=0,
            )
        self.assertIsNotNone(pos)
        sl_dist = pos.entry_price - pos.sl
        tp_dist = pos.tp - pos.entry_price
        self.assertGreater(sl_dist, 0)
        self.assertAlmostEqual(tp_dist, 2.0 * sl_dist, places=6)

    def test_spread_applied_to_entry(self):
        sim = BacktestSimulator(_config())
        window = _candles([1.1000 + i * 0.0002 for i in range(120)])
        hs = _half_spread('EUR_USD', 1.0)
        self.assertGreater(hs, 0)
        long_pos = sim._open_position(
            'EUR_USD', {'is_long': True, 'window': window}, 1.1240,
            datetime(2024, 1, 8, tzinfo=timezone.utc), hs=hs,
            balance=10_000.0, consecutive_losses=0,
        )
        short_pos = sim._open_position(
            'EUR_USD', {'is_long': False, 'window': window}, 1.1240,
            datetime(2024, 1, 8, tzinfo=timezone.utc), hs=hs,
            balance=10_000.0, consecutive_losses=0,
        )
        self.assertAlmostEqual(long_pos.entry_price, 1.1240 + hs)
        self.assertAlmostEqual(short_pos.entry_price, 1.1240 - hs)

    def test_risk_halved_on_loss_streak(self):
        sim = BacktestSimulator(_config())
        window = _candles([1.1000 + i * 0.0002 for i in range(120)])
        ts = datetime(2024, 1, 8, tzinfo=timezone.utc)
        normal = sim._open_position(
            'EUR_USD', {'is_long': True, 'window': window}, 1.1240, ts,
            hs=0.0, balance=10_000.0, consecutive_losses=0,
        )
        with patch.object(settings, 'CONSECUTIVE_LOSS_RISK_REDUCTION_AFTER', 3):
            reduced = sim._open_position(
                'EUR_USD', {'is_long': True, 'window': window}, 1.1240, ts,
                hs=0.0, balance=10_000.0, consecutive_losses=3,
            )
        self.assertAlmostEqual(reduced.units / normal.units, 0.5, places=1)


class TestPnlConversion(unittest.TestCase):

    def test_usd_quote_pair_is_direct(self):
        self.assertAlmostEqual(_to_usd('EUR_USD', 123.45, 1.1), 123.45)

    def test_jpy_quote_converted_at_rate(self):
        # 100,000 JPY of profit at USD/JPY 150 = $666.67
        self.assertAlmostEqual(_to_usd('USD_JPY', 100_000, 150.0), 666.67, places=2)

    def test_jpy_half_spread_uses_jpy_pip(self):
        hs = _half_spread('USD_JPY', 1.0)
        self.assertGreater(hs, 0.001)  # JPY pip is 0.01, so half spread >> 0.0001


class TestFullRun(unittest.TestCase):

    def test_trending_series_produces_trades(self):
        """End-to-end smoke: an uptrend with sharp shallow dips produces trades.

        The EMA200 filter rejects V-bottom flips by design (price is still below
        the EMA), so the fixture is a sustained rise with brief pullbacks: each
        dip flips SuperTrend bearish, each recovery flips it bullish again while
        price is comfortably above the EMA — a valid trend-continuation entry.
        """
        closes = []
        px = 1.0000
        for _cycle in range(10):
            for _ in range(60):
                px += 0.0015
                closes.append(px)
            for _ in range(10):
                px -= 0.0045
                closes.append(px)
        candles = _candles(closes)

        with patch.object(settings, 'H1_CANDLE_COUNT', 250), \
             patch.object(settings, 'STRATEGY_EMA_PERIOD', 200), \
             patch.object(settings, 'SESSION_FILTER_ENABLED', False), \
             patch.object(settings, 'HTF_ALIGNMENT_ENABLED', False), \
             patch.object(settings, 'MIN_CONFLUENCES', 0), \
             patch.object(settings, 'MIN_RR_RATIO', 2.0), \
             patch.object(settings, 'DEFAULT_TAKE_PROFIT_RATIO', 2.0), \
             patch.object(settings, 'MAX_DAILY_LOSS_PERCENT', 1.0), \
             patch('src.backtest.simulator._is_adx_trending', return_value=True):
            sim = BacktestSimulator(_config())
            result = sim.run({'EUR_USD': candles})

        self.assertGreater(result.signals_seen, 0)
        self.assertGreater(len(result.trades), 0)
        self.assertGreater(len(result.equity), 0)
        for t in result.trades:
            self.assertIn(t.exit_reason, ('sl', 'tp', 'time_stop', 'flip_exit', 'end_of_data'))
            self.assertGreater(t.units, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
