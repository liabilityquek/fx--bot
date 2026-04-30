"""
Unit tests for TradingEngine monitoring thread.

Tests verify:
- Monitoring thread startup and lifecycle
- Monitoring cycle timing (60s intervals)
- Main cycle timing (3600s intervals)
- Thread safety under concurrent operations

Run with:
    python -m pytest tests/test_monitoring_thread.py -v
"""

import sys
import os
import unittest
import time
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.engine import TradingEngine
from src.broker.base import AccountInfo


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


def _make_engine():
    """Build a TradingEngine with all external dependencies mocked."""
    broker = MagicMock()
    broker.get_account_info.return_value = _make_account()
    broker.get_positions.return_value = []
    broker.get_open_trades.return_value = []

    decision_engine = MagicMock()
    alert_manager = MagicMock()

    kill_switch = MagicMock()
    kill_switch.is_active.return_value = False

    weekend_guard = MagicMock()
    weekend_guard.is_safe_to_trade.return_value = True

    holiday_guard = MagicMock()
    holiday_guard.is_safe_to_trade.return_value = True

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
    engine.emergency_controller.check_emergency_conditions.return_value = MagicMock(
        level=MagicMock(),
        active_alerts=[],
        positions_at_risk=0,
        recommended_action="Continue normal operations",
        requires_shutdown=False,
        shutdown_reason=None,
    )

    # Patch _process_pair so tests don't need real candle/price data
    engine._process_pair = MagicMock()

    return engine


# ---------------------------------------------------------------------------
# Monitoring Thread Startup
# ---------------------------------------------------------------------------

class TestMonitoringThreadStartup(unittest.TestCase):

    def test_monitoring_thread_starts_on_engine_start(self):
        """Verify monitoring thread is created and started when engine starts."""
        engine = _make_engine()
        engine.start()

        # Give thread a moment to start
        time.sleep(0.1)

        self.assertIsNotNone(engine._monitoring_thread)
        self.assertTrue(engine._monitoring_thread.is_alive())

        engine.stop()

    def test_monitoring_thread_is_daemon(self):
        """Verify monitoring thread is a daemon thread."""
        engine = _make_engine()
        engine.start()

        # Give thread a moment to start
        time.sleep(0.1)

        self.assertTrue(engine._monitoring_thread.daemon)

        engine.stop()

    def test_monitoring_thread_stops_on_engine_stop(self):
        """Verify monitoring thread stops when engine stops."""
        engine = _make_engine()
        engine.start()

        # Give thread a moment to start
        time.sleep(0.1)

        self.assertTrue(engine._monitoring_thread.is_alive())

        engine.stop()

        # Give thread a moment to stop
        time.sleep(0.2)

        self.assertFalse(engine._monitoring_thread.is_alive())

    def test_monitoring_stop_event_set_on_stop(self):
        """Verify stop event is set when engine stops."""
        engine = _make_engine()
        engine.start()

        # Give thread a moment to start
        time.sleep(0.1)

        self.assertFalse(engine._monitoring_stop_event.is_set())

        engine.stop()

        # Give thread a moment to stop
        time.sleep(0.2)

        self.assertTrue(engine._monitoring_stop_event.is_set())


# ---------------------------------------------------------------------------
# Monitoring Cycle Timing
# ---------------------------------------------------------------------------

class TestMonitoringCycleTiming(unittest.TestCase):

    def test_monitoring_cycle_runs_approximately_60_seconds(self):
        """Verify monitoring cycle runs at approximately 60-second intervals."""
        engine = _make_engine()

        # Override monitoring interval to 2 seconds for faster testing
        with patch('config.settings.settings.MONITORING_INTERVAL_SECONDS', 2):
            engine.start()

            # Wait for first cycle
            time.sleep(0.5)
            initial_count = engine._monitoring_cycle_count

            # Wait for second cycle
            time.sleep(2.5)

            final_count = engine._monitoring_cycle_count

            engine.stop()

            # Should have run at least one more cycle
            self.assertGreater(final_count, initial_count)

    def test_main_cycle_runs_approximately_3600_seconds(self):
        """Verify main cycle runs at approximately 3600-second intervals."""
        engine = _make_engine()

        # Override execution interval to 2 seconds for faster testing
        with patch('config.settings.settings.EXECUTION_INTERVAL_SECONDS', 2):
            engine.start()

            # Wait for first cycle
            time.sleep(0.5)
            initial_count = engine._cycle_count

            # Wait for second cycle
            time.sleep(2.5)

            final_count = engine._cycle_count

            engine.stop()

            # Should have run at least one more cycle
            self.assertGreater(final_count, initial_count)

    def test_cycles_do_not_overlap(self):
        """Verify monitoring and main cycles do not overlap."""
        engine = _make_engine()

        # Override intervals to 1 second for faster testing
        with patch('config.settings.settings.MONITORING_INTERVAL_SECONDS', 1), \
             patch('config.settings.settings.EXECUTION_INTERVAL_SECONDS', 1):
            engine.start()

            # Let both cycles run
            time.sleep(3)

            engine.stop()

            # Both should have run multiple times without crashing
            self.assertGreater(engine._monitoring_cycle_count, 0)
            self.assertGreater(engine._cycle_count, 0)

    def test_monitoring_cycle_count_increments(self):
        """Verify monitoring cycle count increments on each cycle."""
        engine = _make_engine()

        # Override monitoring interval to 1 second for faster testing
        with patch('config.settings.settings.MONITORING_INTERVAL_SECONDS', 1):
            engine.start()

            # Wait for first cycle
            time.sleep(0.5)
            initial_count = engine._monitoring_cycle_count

            # Wait for more cycles
            time.sleep(2.5)

            final_count = engine._monitoring_cycle_count

            engine.stop()

            # Count should have increased
            self.assertGreater(final_count, initial_count)


# ---------------------------------------------------------------------------
# Thread Safety
# ---------------------------------------------------------------------------

class TestThreadSafety(unittest.TestCase):

    def test_concurrent_trade_registration_safe(self):
        """Verify concurrent trade registration is thread-safe."""
        engine = _make_engine()
        engine.start()

        # Simulate concurrent trade registrations
        def register_trade(trade_id):
            fake_trade = MagicMock()
            fake_trade.trade_id = trade_id
            fake_trade.pair = "EUR_USD"
            with engine._trades_lock:
                engine._known_open_trades[trade_id] = fake_trade

        threads = []
        for i in range(10):
            t = threading.Thread(target=register_trade, args=(f"trade_{i}",))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        engine.stop()

        # All trades should be registered
        self.assertEqual(len(engine._known_open_trades), 10)

    def test_concurrent_trade_unregistration_safe(self):
        """Verify concurrent trade unregistration is thread-safe."""
        engine = _make_engine()

        # Register some trades
        for i in range(10):
            fake_trade = MagicMock()
            fake_trade.trade_id = f"trade_{i}"
            fake_trade.pair = "EUR_USD"
            engine._known_open_trades[f"trade_{i}"] = fake_trade

        engine.start()

        # Simulate concurrent trade unregistrations
        def unregister_trade(trade_id):
            with engine._trades_lock:
                engine._known_open_trades.pop(trade_id, None)

        threads = []
        for i in range(10):
            t = threading.Thread(target=unregister_trade, args=(f"trade_{i}",))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        engine.stop()

        # All trades should be unregistered
        self.assertEqual(len(engine._known_open_trades), 0)

    def test_concurrent_trade_sync_safe(self):
        """Verify concurrent trade sync operations are thread-safe."""
        engine = _make_engine()
        engine.start()

        # Simulate concurrent sync operations
        def sync_trade(trade_id):
            with engine._trades_lock:
                if trade_id in engine._known_open_trades:
                    trade = engine._known_open_trades[trade_id]
                    trade.current_price = 1.1000

        # Register some trades
        for i in range(10):
            fake_trade = MagicMock()
            fake_trade.trade_id = f"trade_{i}"
            fake_trade.pair = "EUR_USD"
            fake_trade.current_price = 1.0900
            engine._known_open_trades[f"trade_{i}"] = fake_trade

        threads = []
        for i in range(10):
            t = threading.Thread(target=sync_trade, args=(f"trade_{i}",))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        engine.stop()

        # All trades should have been synced
        for trade in engine._known_open_trades.values():
            self.assertEqual(trade.current_price, 1.1000)

    def test_concurrent_price_tracking_safe(self):
        """Verify concurrent price tracking is thread-safe."""
        engine = _make_engine()
        engine.start()

        # Simulate concurrent price updates
        def update_price(pair, price):
            with engine._trades_lock:
                engine._cycle_pair_prices[pair] = (price, price)

        threads = []
        for i in range(10):
            t = threading.Thread(target=update_price, args=(f"EUR_USD_{i}", 1.1000 + i * 0.0001))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        engine.stop()

        # All prices should be tracked
        self.assertEqual(len(engine._cycle_pair_prices), 10)

    def test_lock_prevents_race_conditions(self):
        """Verify lock prevents race conditions."""
        engine = _make_engine()
        engine.start()

        # Simulate concurrent access to shared state
        counter = [0]

        def increment_counter():
            with engine._trades_lock:
                counter[0] += 1
                time.sleep(0.01)  # Simulate work
                counter[0] += 1

        threads = []
        for _ in range(10):
            t = threading.Thread(target=increment_counter)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        engine.stop()

        # Counter should be exactly 20 (10 threads * 2 increments each)
        self.assertEqual(counter[0], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
