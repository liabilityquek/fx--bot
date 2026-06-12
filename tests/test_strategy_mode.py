"""
Unit tests for STRATEGY_MODE wiring: DecisionEngine deterministic pipeline and
TradingEngine flip exit.

Run with:
    python -m pytest tests/test_strategy_mode.py -v
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.agents.base import AgentVote, Signal
from src.execution.engine import TradingEngine
from src.voting.engine import DecisionEngine


def _candles(n=250, base=1.1000):
    return [
        {'open': base, 'high': base + 0.001, 'low': base - 0.001,
         'close': base, 'volume': 100}
        for _ in range(n)
    ]


class TestDecisionEngineStrategyMode(unittest.TestCase):

    def _engine(self):
        with patch.object(settings, 'STRATEGY_MODE', 'strategy'):
            return DecisionEngine()

    def test_no_llm_or_reviewer_constructed(self):
        engine = self._engine()
        self.assertIsNone(engine._llm)
        self.assertIsNone(engine._reviewer)
        self.assertIsNone(engine._macro)
        self.assertIsNotNone(engine._strategy)

    def test_run_decision_uses_strategy_vote(self):
        engine = self._engine()
        engine._strategy = MagicMock()
        engine._strategy.vote.return_value = AgentVote(
            agent_name='SuperTrendEMA', pair='EUR_USD', signal=Signal.BUY,
            confidence=0.82, reasoning='ST flip bull', setup_type='BREAKOUT',
        )
        with patch.object(settings, 'STRATEGY_MODE', 'strategy'):
            result = engine.run_decision('EUR_USD', _candles(), 1.1)
        self.assertEqual(result.final_signal, Signal.BUY)
        self.assertEqual(result.confidence, 0.82)
        self.assertEqual(result.reviewer_verdict, 'SKIPPED')
        self.assertEqual(result.setup_type, 'BREAKOUT')
        engine._strategy.vote.assert_called_once()

    def test_low_confidence_becomes_hold(self):
        engine = self._engine()
        engine._strategy = MagicMock()
        engine._strategy.vote.return_value = AgentVote(
            agent_name='SuperTrendEMA', pair='EUR_USD', signal=Signal.SELL,
            confidence=0.40, reasoning='weak', setup_type='BREAKOUT',
        )
        result = engine.run_decision('EUR_USD', _candles(), 1.1)
        self.assertEqual(result.final_signal, Signal.HOLD)

    def test_provider_status_does_not_crash(self):
        engine = self._engine()
        status = engine.get_llm_provider_status()
        self.assertIn('Strategy mode', status)

    def test_llm_mode_unaffected(self):
        with patch.object(settings, 'STRATEGY_MODE', 'llm'):
            engine = DecisionEngine()
        self.assertIsNone(engine._strategy)
        self.assertIsNotNone(engine._llm)
        self.assertIsNotNone(engine._reviewer)


class TestFlipExit(unittest.TestCase):

    def _engine(self, dry_run=False):
        broker = MagicMock()
        broker.get_positions.return_value = []
        engine = TradingEngine(
            broker=broker,
            decision_engine=MagicMock(),
            alert_manager=MagicMock(),
            dry_run=dry_run,
        )
        engine.trade_manager = MagicMock()
        return engine

    def _opposite_position(self, pair='EUR_USD'):
        pos = MagicMock()
        pos.pair = pair
        pos.is_flat = False
        pos.is_long = False
        pos.is_short = True
        return pos

    def test_flip_closes_opposite_position(self):
        engine = self._engine()
        pos = self._opposite_position()
        close_result = MagicMock(success=True, trade_id='t1', pnl=-25.0)
        engine.trade_manager.close_all_trades.return_value = [close_result]

        with patch.object(settings, 'STRATEGY_MODE', 'strategy'), \
             patch.object(settings, 'STRATEGY_EXIT_ON_FLIP', True):
            engine._handle_strategy_flip_exit('EUR_USD', True, [pos])

        engine.trade_manager.close_all_trades.assert_called_once_with(
            reason='strategy_flip', pairs={'EUR_USD'}
        )
        # A losing flip exit must feed the streak/cooldown accounting
        self.assertEqual(engine._consecutive_losses, 1)
        self.assertIn('EUR_USD', engine._pair_loss_cooldown)

    def test_same_direction_position_not_closed(self):
        engine = self._engine()
        pos = self._opposite_position()
        pos.is_long, pos.is_short = True, False  # same direction as signal

        with patch.object(settings, 'STRATEGY_MODE', 'strategy'), \
             patch.object(settings, 'STRATEGY_EXIT_ON_FLIP', True):
            engine._handle_strategy_flip_exit('EUR_USD', True, [pos])

        engine.trade_manager.close_all_trades.assert_not_called()

    def test_disabled_flag_skips_close(self):
        engine = self._engine()
        pos = self._opposite_position()
        with patch.object(settings, 'STRATEGY_MODE', 'strategy'), \
             patch.object(settings, 'STRATEGY_EXIT_ON_FLIP', False):
            result = engine._handle_strategy_flip_exit('EUR_USD', True, [pos])
        engine.trade_manager.close_all_trades.assert_not_called()
        self.assertEqual(result, [pos])

    def test_llm_mode_skips_close(self):
        engine = self._engine()
        pos = self._opposite_position()
        with patch.object(settings, 'STRATEGY_MODE', 'llm'):
            engine._handle_strategy_flip_exit('EUR_USD', True, [pos])
        engine.trade_manager.close_all_trades.assert_not_called()

    def test_dry_run_skips_close(self):
        engine = self._engine(dry_run=True)
        pos = self._opposite_position()
        with patch.object(settings, 'STRATEGY_MODE', 'strategy'), \
             patch.object(settings, 'STRATEGY_EXIT_ON_FLIP', True):
            engine._handle_strategy_flip_exit('EUR_USD', True, [pos])
        engine.trade_manager.close_all_trades.assert_not_called()

    def test_unmanaged_position_falls_back_to_close_position(self):
        engine = self._engine()
        pos = self._opposite_position()
        engine.trade_manager.close_all_trades.return_value = []  # nothing managed
        with patch.object(settings, 'STRATEGY_MODE', 'strategy'), \
             patch.object(settings, 'STRATEGY_EXIT_ON_FLIP', True):
            engine._handle_strategy_flip_exit('EUR_USD', True, [pos])
        engine.broker.close_position.assert_called_once_with('EUR_USD')


if __name__ == "__main__":
    unittest.main(verbosity=2)
