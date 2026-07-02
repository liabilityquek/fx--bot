"""
Unit tests for the Phase 2 trade-quality gates and R-based trade management:
spread gate, session filter, H4 trend alignment, post-loss cooldown,
consecutive-loss throttle, R-based break-even/trailing, and the time stop.

Run with:
    python -m pytest tests/test_trade_quality_gates.py -v
"""

import sys
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.agents.base import Signal
from src.broker.base import AccountInfo
from src.execution.engine import TradingEngine, _htf_trend_aligned
from src.execution.trade_manager import TradeManager, ManagedTrade
from src.voting.engine import DecisionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_account(balance=10000.0, nav=10000.0):
    return AccountInfo(
        account_id="test-123",
        balance=balance,
        nav=nav,
        margin_used=0.0,
        margin_available=10000.0,
        unrealized_pnl=0.0,
        open_trade_count=0,
        currency="USD",
    )


def _make_candles(n=60, close=1.1000, rising=False):
    candles = []
    px = close
    for i in range(n):
        if rising:
            px += 0.0005
        candles.append({
            'open': px - 0.0002, 'high': px + 0.0003,
            'low': px - 0.0004, 'close': px, 'volume': 100,
        })
    return candles


def _hold_result(pair="EUR_USD"):
    return DecisionResult(
        pair=pair,
        final_signal=Signal.HOLD,
        confidence=0.5,
        reasoning="test",
    )


def _make_engine(bid=1.1000, ask=1.1001):
    broker = MagicMock()
    broker.get_account_info.return_value = _make_account()
    broker.get_positions.return_value = []
    broker.get_open_trades.return_value = []
    broker.get_historical_candles.return_value = _make_candles()
    broker.get_current_price.return_value = {'bid': bid, 'ask': ask}

    decision_engine = MagicMock()
    decision_engine.run_decision.return_value = _hold_result()

    engine = TradingEngine(
        broker=broker,
        decision_engine=decision_engine,
        alert_manager=MagicMock(),
        dry_run=True,
    )
    engine.emergency_controller = MagicMock()
    engine.trade_manager = MagicMock()
    engine.exposure_tracker = MagicMock()
    engine._in_trading_session = MagicMock(return_value=True)
    return engine


# ---------------------------------------------------------------------------
# Spread gate
# ---------------------------------------------------------------------------

class TestSpreadGate(unittest.TestCase):

    def test_wide_spread_blocks_entry(self):
        engine = _make_engine(bid=1.1000, ask=1.1010)  # 10 pips
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.decision_engine.run_decision.assert_not_called()

    def test_normal_spread_proceeds(self):
        engine = _make_engine(bid=1.1000, ask=1.1001)  # 1 pip
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.decision_engine.run_decision.assert_called_once()


# ---------------------------------------------------------------------------
# Session filter
# ---------------------------------------------------------------------------

class TestSessionFilter(unittest.TestCase):

    def test_outside_session_blocks_entry(self):
        engine = _make_engine()
        engine._in_trading_session = MagicMock(return_value=False)
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.broker.get_historical_candles.assert_not_called()

    def test_session_window_logic(self):
        engine = _make_engine()
        # Restore the real method for this test
        engine._in_trading_session = TradingEngine._in_trading_session.__get__(engine)
        with patch.object(settings, 'SESSION_FILTER_ENABLED', True), \
             patch.object(settings, 'SESSION_START_UTC_HOUR', 6), \
             patch.object(settings, 'SESSION_END_UTC_HOUR', 20):
            with patch('src.execution.engine.datetime') as mock_dt:
                mock_dt.utcnow.return_value = datetime(2026, 6, 10, 12, 0)
                self.assertTrue(engine._in_trading_session())
                mock_dt.utcnow.return_value = datetime(2026, 6, 10, 22, 0)
                self.assertFalse(engine._in_trading_session())
                mock_dt.utcnow.return_value = datetime(2026, 6, 10, 3, 0)
                self.assertFalse(engine._in_trading_session())

    def test_filter_disabled_always_allows(self):
        engine = _make_engine()
        engine._in_trading_session = TradingEngine._in_trading_session.__get__(engine)
        with patch.object(settings, 'SESSION_FILTER_ENABLED', False):
            self.assertTrue(engine._in_trading_session())


# ---------------------------------------------------------------------------
# Post-loss cooldown + consecutive-loss throttle
# ---------------------------------------------------------------------------

class TestLossManagement(unittest.TestCase):

    def test_pair_in_cooldown_skipped(self):
        engine = _make_engine()
        engine._pair_loss_cooldown["EUR_USD"] = datetime.utcnow() + timedelta(hours=2)
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.broker.get_historical_candles.assert_not_called()

    def test_expired_cooldown_allows_entry(self):
        engine = _make_engine()
        engine._pair_loss_cooldown["EUR_USD"] = datetime.utcnow() - timedelta(hours=1)
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.broker.get_historical_candles.assert_called()

    def test_loss_sets_cooldown_and_increments_streak(self):
        engine = _make_engine()
        engine._record_trade_outcome("EUR_USD", -50.0)
        self.assertIn("EUR_USD", engine._pair_loss_cooldown)
        self.assertEqual(engine._consecutive_losses, 1)

    def test_win_resets_streak(self):
        engine = _make_engine()
        engine._consecutive_losses = 3
        engine._record_trade_outcome("EUR_USD", 80.0)
        self.assertEqual(engine._consecutive_losses, 0)

    def test_streak_limit_halts_trading(self):
        engine = _make_engine()
        for _ in range(settings.MAX_CONSECUTIVE_LOSSES):
            engine._record_trade_outcome("EUR_USD", -50.0)
        self.assertTrue(engine._loss_streak_halted)
        engine.alert_manager.alert_error.assert_called_once()


# ---------------------------------------------------------------------------
# H4 trend alignment gate
# ---------------------------------------------------------------------------

class TestHtfTrendAlignment(unittest.TestCase):

    def test_buy_aligned_with_rising_h4(self):
        candles = _make_candles(n=80, rising=True)
        self.assertTrue(_htf_trend_aligned(candles, is_long=True))

    def test_sell_blocked_by_rising_h4(self):
        candles = _make_candles(n=80, rising=True)
        self.assertFalse(_htf_trend_aligned(candles, is_long=False))

    def test_insufficient_data_is_allowed(self):
        self.assertTrue(_htf_trend_aligned(None, is_long=True))
        self.assertTrue(_htf_trend_aligned(_make_candles(n=10), is_long=False))


# ---------------------------------------------------------------------------
# R-based break-even and trailing activation
# ---------------------------------------------------------------------------

def _make_trade(entry=1.1000, current=1.1000, sl=1.0950, tp=1.1150, is_long=True):
    trade = MagicMock()
    trade.trade_id = "t1"
    trade.pair = "EUR_USD"
    trade.entry_price = entry
    trade.current_price = current
    trade.stop_loss = sl
    trade.take_profit = tp
    trade.is_long = is_long
    trade.is_short = not is_long
    trade.units = 100000
    return trade


def _make_trade_manager():
    broker = MagicMock()
    broker.get_open_trades.return_value = []
    broker.modify_trade.return_value = True
    tm = TradeManager(broker=broker, alert_manager=None)
    tm._state_file = Path(tempfile.mkdtemp()) / "managed_trades.json"
    tm._persisted_state = {}
    return tm


class TestRBasedManagement(unittest.TestCase):

    def test_break_even_not_triggered_below_half_r(self):
        tm = _make_trade_manager()
        # SL is 50 pips → 0.5R trigger = 25 pips; +10 pips must not move SL
        trade = _make_trade(current=1.1010)
        managed = ManagedTrade(trade=trade, initial_sl=1.0950)
        with patch.object(settings, 'BREAK_EVEN_TRIGGER_R', 0.5):
            tm._check_break_even(managed)
        tm.broker.modify_trade.assert_not_called()
        self.assertFalse(managed.break_even_triggered)

    def test_break_even_triggered_at_half_r(self):
        tm = _make_trade_manager()
        trade = _make_trade(current=1.1030)  # +30 pips > 25-pip trigger
        managed = ManagedTrade(trade=trade, initial_sl=1.0950)
        with patch.object(settings, 'BREAK_EVEN_TRIGGER_R', 0.5):
            tm._check_break_even(managed)
        tm.broker.modify_trade.assert_called_once()
        self.assertTrue(managed.break_even_triggered)

    def test_trailing_not_active_below_one_r(self):
        tm = _make_trade_manager()
        trade = _make_trade(current=1.1030)  # +30 pips < 1R (50 pips)
        managed = ManagedTrade(
            trade=trade, initial_sl=1.0950,
            trailing_stop_active=True, trailing_stop_distance=8.0,
            highest_price=1.1030, lowest_price=1.1000,
        )
        with patch.object(settings, 'TRAILING_STOP_ACTIVATION_R', 1.0):
            result = tm._check_trailing_stop(managed)
        self.assertIsNone(result)
        tm.broker.modify_trade.assert_not_called()

    def test_trailing_active_after_one_r(self):
        tm = _make_trade_manager()
        trade = _make_trade(current=1.1060)  # +60 pips > 1R (50 pips)
        managed = ManagedTrade(
            trade=trade, initial_sl=1.0950,
            trailing_stop_active=True, trailing_stop_distance=8.0,
            highest_price=1.1060, lowest_price=1.1000,
        )
        with patch.object(settings, 'TRAILING_STOP_ACTIVATION_R', 1.0):
            result = tm._check_trailing_stop(managed)
        self.assertIsNotNone(result)
        tm.broker.modify_trade.assert_called_once()


# ---------------------------------------------------------------------------
# Time stop
# ---------------------------------------------------------------------------

class TestTimeStop(unittest.TestCase):

    def _run_update(self, tm, trade, age_hours):
        tm.broker.get_open_trades.return_value = [trade]
        tm.broker.close_trade.return_value = MagicMock(
            success=True, realized_pnl=-30.0, close_price=trade.current_price
        )
        with patch(
            'src.execution.trade_manager._market_hours_elapsed',
            return_value=age_hours,
        ), patch.object(settings, 'TIME_STOP_ENABLED', True), \
             patch.object(settings, 'TIME_STOP_HOURS', 48.0):
            return tm.update_all_trades()

    def test_stale_losing_trade_closed(self):
        tm = _make_trade_manager()
        trade = _make_trade(current=1.0980)  # losing long
        self._run_update(tm, trade, age_hours=50.0)
        tm.broker.close_trade.assert_called_once_with("t1")

    def test_stale_winning_trade_kept(self):
        tm = _make_trade_manager()
        trade = _make_trade(current=1.1040)  # winning long
        self._run_update(tm, trade, age_hours=50.0)
        tm.broker.close_trade.assert_not_called()

    def test_young_losing_trade_kept(self):
        tm = _make_trade_manager()
        trade = _make_trade(current=1.0980)
        self._run_update(tm, trade, age_hours=10.0)
        tm.broker.close_trade.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
