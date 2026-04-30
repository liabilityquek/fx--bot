"""
Unit tests for TradingEngine — Layers 2, 3, and 6 wiring.

Tests verify correct behaviour on both normal trading days and public holidays.

Run with:
    python -m pytest tests/test_engine_layers.py -v
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.broker.base import AccountInfo, OrderSide, Trade
from src.execution.engine import TradingEngine
from src.risk.emergency_controller import EmergencyStatus, EmergencyLevel, ShutdownReason
from src.news.suspension_manager import SuspensionStatus, SuspensionReason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_account(balance=10000.0, nav=10000.0, unrealized_pnl=0.0):
    return AccountInfo(
        account_id="test-123",
        balance=balance,
        nav=nav,
        margin_used=0.0,
        margin_available=10000.0,
        unrealized_pnl=unrealized_pnl,
        open_trade_count=0,
        currency="USD",
    )


def _normal_emergency_status():
    return EmergencyStatus(
        level=EmergencyLevel.NONE,
        active_alerts=[],
        positions_at_risk=0,
        recommended_action="Continue normal operations",
        requires_shutdown=False,
        shutdown_reason=None,
    )


def _panic_emergency_status(reason=ShutdownReason.DRAWDOWN_LIMIT):
    return EmergencyStatus(
        level=EmergencyLevel.PANIC,
        active_alerts=["Risk limit breached"],
        positions_at_risk=2,
        recommended_action="EMERGENCY SHUTDOWN",
        requires_shutdown=True,
        shutdown_reason=reason,
    )


def _make_engine(is_holiday=False, is_weekend=False, kill_switch_active=False):
    """Build a TradingEngine with all external dependencies mocked."""
    broker = MagicMock()
    broker.get_account_info.return_value = _make_account()
    broker.get_positions.return_value = []
    broker.get_open_trades.return_value = []

    decision_engine = MagicMock()
    alert_manager = MagicMock()

    kill_switch = MagicMock()
    kill_switch.is_active.return_value = kill_switch_active

    weekend_guard = MagicMock()
    weekend_guard.is_safe_to_trade.return_value = not is_weekend

    holiday_guard = MagicMock()
    holiday_guard.is_safe_to_trade.return_value = not is_holiday

    engine = TradingEngine(
        broker=broker,
        decision_engine=decision_engine,
        alert_manager=alert_manager,
        kill_switch=kill_switch,
        weekend_guard=weekend_guard,
        holiday_guard=holiday_guard,
        dry_run=True,
    )

    # Replace internally-created risk modules with mocks
    engine.emergency_controller = MagicMock()
    engine.trade_manager = MagicMock()
    engine.exposure_tracker = MagicMock()
    engine.exposure_tracker.get_current_exposure.return_value = MagicMock(
        total_exposure_percent=5.0
    )

    # Default: all clear
    engine.emergency_controller.check_emergency_conditions.return_value = (
        _normal_emergency_status()
    )

    # Patch _process_pair so tests don't need real candle/price data
    engine._process_pair = MagicMock()

    return engine


# ---------------------------------------------------------------------------
# Normal trading day
# ---------------------------------------------------------------------------

class TestNormalTradingDay(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine(is_holiday=False)

    # Layer 3
    def test_emergency_check_runs(self):
        """Emergency check runs in monitoring cycle, not main cycle."""
        self.engine._run_monitoring_cycle()
        self.engine.emergency_controller.check_emergency_conditions.assert_called_once()

    # Layer 2 + 6
    def test_trade_manager_update_runs(self):
        """Trade manager update runs in monitoring cycle, not main cycle."""
        self.engine._run_monitoring_cycle()
        self.engine.trade_manager.update_all_trades.assert_called_once()

    # Pair processing should run on normal days
    def test_pair_processing_runs(self):
        self.engine._run_cycle()
        self.engine._process_pair.assert_called()

    # Initial balance
    def test_initial_balance_set_on_first_cycle(self):
        self.assertIsNone(self.engine._initial_balance)
        self.engine._run_cycle()
        self.assertEqual(self.engine._initial_balance, 10000.0)

    def test_initial_balance_not_overwritten_on_second_cycle(self):
        self.engine._run_cycle()
        self.engine.broker.get_account_info.return_value = _make_account(
            balance=9500.0, nav=9500.0
        )
        self.engine._run_cycle()
        self.assertEqual(self.engine._initial_balance, 10000.0)

    # Layer 3 — emergency shutdown triggered
    def test_emergency_shutdown_calls_close_all(self):
        """Emergency shutdown triggered in monitoring cycle."""
        self.engine.emergency_controller.check_emergency_conditions.return_value = (
            _panic_emergency_status(ShutdownReason.DRAWDOWN_LIMIT)
        )
        self.engine._run_monitoring_cycle()
        self.engine.trade_manager.emergency_close_all.assert_called_once_with(
            reason=ShutdownReason.DRAWDOWN_LIMIT.value
        )

    def test_no_emergency_close_when_conditions_normal(self):
        """No emergency close when conditions are normal."""
        self.engine._run_monitoring_cycle()
        self.engine.trade_manager.emergency_close_all.assert_not_called()

    # Layer 4 — close detection: trade that disappears triggers alert
    def test_closed_trade_detection_fires_alert(self):
        """Close detection runs in monitoring cycle."""
        fake_trade = MagicMock()
        fake_trade.trade_id = "t1"
        fake_trade.pair = "EUR_USD"
        fake_trade.entry_price = 1.1000
        fake_trade.current_price = 1.1050
        fake_trade.stop_loss = 1.0950
        fake_trade.take_profit = 1.1100
        fake_trade.is_long = True
        fake_trade.units = 100000
        fake_trade.unrealized_pnl = 50.0
        self.engine._known_open_trades = {"t1": fake_trade}
        self.engine.broker.get_open_trades.return_value = []
        self.engine.broker.get_closed_trade_info.return_value = {
            'close_price': 1.1050,
            'realized_pnl': 50.0,
            'reason': 'take_profit'
        }
        self.engine._run_monitoring_cycle()
        self.engine.alert_manager.alert_trade_closed.assert_called_once()

    # Exposure tracker is updated each cycle
    def test_exposure_tracker_updated(self):
        self.engine._run_cycle()
        self.engine.exposure_tracker.update_positions.assert_called_once()


# ---------------------------------------------------------------------------
# Public holiday
# ---------------------------------------------------------------------------

class TestPublicHoliday(unittest.TestCase):

    def setUp(self):
        self.engine = _make_engine(is_holiday=True)

    # Pair processing must be blocked
    def test_pair_processing_blocked(self):
        self.engine._run_cycle()
        self.engine._process_pair.assert_not_called()

    # Layer 3 must still run
    def test_emergency_check_still_runs(self):
        """Emergency check still runs in monitoring cycle on holiday."""
        self.engine._run_monitoring_cycle()
        self.engine.emergency_controller.check_emergency_conditions.assert_called_once()

    # Layer 2 + 6 must still run
    def test_trade_manager_still_runs(self):
        """Trade manager still runs in monitoring cycle on holiday."""
        self.engine._run_monitoring_cycle()
        self.engine.trade_manager.update_all_trades.assert_called_once()

    # Layer 4 must still run
    def test_close_detection_still_runs(self):
        """Close detection still runs in monitoring cycle on holiday."""
        fake_trade = MagicMock()
        fake_trade.trade_id = "t1"
        fake_trade.pair = "EUR_USD"
        fake_trade.entry_price = 1.1000
        fake_trade.current_price = 1.1050
        fake_trade.stop_loss = 1.0950
        fake_trade.take_profit = 1.1100
        fake_trade.is_long = True
        fake_trade.units = 100000
        fake_trade.unrealized_pnl = 50.0
        self.engine._known_open_trades = {"t1": fake_trade}
        self.engine.broker.get_open_trades.return_value = []
        self.engine.broker.get_closed_trade_info.return_value = {
            'close_price': 1.1050,
            'realized_pnl': 50.0,
            'reason': 'take_profit'
        }
        self.engine._run_monitoring_cycle()
        self.engine.alert_manager.alert_trade_closed.assert_called_once()

    # Layer 3 emergency shutdown still works on holiday
    def test_emergency_shutdown_on_holiday(self):
        """Emergency shutdown still works in monitoring cycle on holiday."""
        self.engine.emergency_controller.check_emergency_conditions.return_value = (
            _panic_emergency_status(ShutdownReason.EXPOSURE_BREACH)
        )
        self.engine._run_monitoring_cycle()
        self.engine.trade_manager.emergency_close_all.assert_called_once_with(
            reason=ShutdownReason.EXPOSURE_BREACH.value
        )

    # Holiday alert sent to Telegram
    def test_holiday_telegram_alert_sent(self):
        self.engine._run_cycle()
        self.engine.alert_manager.alert_error.assert_called()
        msg = self.engine.alert_manager.alert_error.call_args[0][0]
        self.assertIn("Market holiday", msg)

    # Exposure tracker still updated on holiday
    def test_exposure_tracker_updated_on_holiday(self):
        self.engine._run_cycle()
        self.engine.exposure_tracker.update_positions.assert_called_once()


# ---------------------------------------------------------------------------
# Kill switch — skips everything
# ---------------------------------------------------------------------------

class TestKillSwitch(unittest.TestCase):

    def test_kill_switch_skips_all_layers(self):
        engine = _make_engine(kill_switch_active=True)
        engine._run_cycle()
        engine.emergency_controller.check_emergency_conditions.assert_not_called()
        engine.trade_manager.update_all_trades.assert_not_called()
        engine._process_pair.assert_not_called()
        engine.alert_manager.alert_trade_closed.assert_not_called()


# ---------------------------------------------------------------------------
# Weekend guard — skips everything
# ---------------------------------------------------------------------------

class TestWeekendGuard(unittest.TestCase):

    def test_weekend_skips_all_layers(self):
        engine = _make_engine(is_weekend=True)
        engine._run_cycle()
        engine.emergency_controller.check_emergency_conditions.assert_not_called()
        engine.trade_manager.update_all_trades.assert_not_called()
        engine._process_pair.assert_not_called()


# ---------------------------------------------------------------------------
# News suspension — Rule 1
# ---------------------------------------------------------------------------

def _make_engine_with_suspension(is_suspended: bool):
    """Build TradingEngine with real _process_pair but mocked suspension_manager."""
    broker = MagicMock()
    broker.get_account_info.return_value = _make_account()
    broker.get_positions.return_value = []
    broker.get_open_trades.return_value = []
    broker.get_historical_candles.return_value = []
    broker.get_current_price.return_value = None

    decision_engine = MagicMock()
    alert_manager = MagicMock()

    engine = TradingEngine(
        broker=broker,
        decision_engine=decision_engine,
        alert_manager=alert_manager,
        dry_run=True,
    )

    engine.emergency_controller = MagicMock()
    engine.trade_manager = MagicMock()
    engine.exposure_tracker = MagicMock()
    engine.exposure_tracker.get_current_exposure.return_value = MagicMock(
        total_exposure_percent=5.0
    )

    status = SuspensionStatus(
        is_suspended=is_suspended,
        reason=SuspensionReason.HIGH_IMPACT_NEWS if is_suspended else None,
        suspended_pairs={"EUR_USD"} if is_suspended else set(),
        triggering_event=None,
        resume_time=None,
        message="Suspended: NFP" if is_suspended else "Trading allowed",
    )
    engine.suspension_manager = MagicMock()
    engine.suspension_manager.check_suspension_status.return_value = status

    return engine


class TestNewsEventSuspension(unittest.TestCase):

    def test_suspended_pair_skips_candle_fetch(self):
        engine = _make_engine_with_suspension(is_suspended=True)
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.broker.get_historical_candles.assert_not_called()

    def test_suspended_pair_skips_decision_engine(self):
        engine = _make_engine_with_suspension(is_suspended=True)
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.decision_engine.run_decision.assert_not_called()

    def test_unsuspended_pair_proceeds_to_candle_fetch(self):
        engine = _make_engine_with_suspension(is_suspended=False)
        engine._process_pair("EUR_USD", _make_account(), [])
        engine.broker.get_historical_candles.assert_called_once()


# ---------------------------------------------------------------------------
# Monitoring cycle tests
# ---------------------------------------------------------------------------

class TestMonitoringCycle(unittest.TestCase):

    def test_monitoring_cycle_calls_trade_manager_update(self):
        """Verify monitoring cycle calls trade_manager.update_all_trades."""
        engine = _make_engine()
        engine._run_monitoring_cycle()
        engine.trade_manager.update_all_trades.assert_called_once()

    def test_monitoring_cycle_calls_exposure_tracker_update(self):
        """Verify monitoring cycle calls exposure_tracker.update_positions."""
        engine = _make_engine()
        engine._run_monitoring_cycle()
        engine.exposure_tracker.update_positions.assert_called_once()

    def test_monitoring_cycle_checks_emergency_conditions(self):
        """Verify monitoring cycle checks emergency conditions."""
        engine = _make_engine()
        engine._run_monitoring_cycle()
        engine.emergency_controller.check_emergency_conditions.assert_called_once()

    def test_monitoring_cycle_detects_closed_trades(self):
        """Verify monitoring cycle detects closed trades."""
        engine = _make_engine()
        fake_trade = MagicMock()
        fake_trade.trade_id = "t1"
        fake_trade.pair = "EUR_USD"
        fake_trade.entry_price = 1.1000
        fake_trade.current_price = 1.1050
        fake_trade.stop_loss = 1.0950
        fake_trade.take_profit = 1.1100
        fake_trade.is_long = True
        fake_trade.units = 100000
        fake_trade.unrealized_pnl = 50.0
        engine._known_open_trades = {"t1": fake_trade}
        engine.broker.get_open_trades.return_value = []
        engine.broker.get_closed_trade_info.return_value = {
            'close_price': 1.1050,
            'realized_pnl': 50.0,
            'reason': 'take_profit'
        }
        engine._run_monitoring_cycle()
        engine.alert_manager.alert_trade_closed.assert_called_once()

    def test_monitoring_cycle_updates_trailing_stops(self):
        """Verify monitoring cycle updates trailing stops via trade_manager."""
        engine = _make_engine()
        engine._run_monitoring_cycle()
        engine.trade_manager.update_all_trades.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
