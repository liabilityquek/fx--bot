"""
Unit tests for PositionSizer USD-notional / leverage-cap handling.

Regression for a live bug: USD_JPY notional was computed as units x price
(~150x overstated), so the 20:1 leverage cap squeezed every USD_JPY position
to ~1/150th of its intended size.

Run with:
    python -m pytest tests/test_position_sizer.py -v
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.position_sizer import PositionSizer


class TestUsdNotional(unittest.TestCase):

    def setUp(self):
        self.sizer = PositionSizer()

    def test_usd_base_pair_notional_is_units(self):
        # 10,000 USD_JPY units = $10,000 notional regardless of the JPY rate
        self.assertEqual(self.sizer._usd_notional('USD_JPY', 10_000, 150.0), 10_000.0)
        self.assertEqual(self.sizer._usd_notional('USD_CHF', 10_000, 0.88), 10_000.0)

    def test_usd_quote_pair_notional_uses_price(self):
        # 10,000 EUR at 1.10 = $11,000 notional
        self.assertAlmostEqual(self.sizer._usd_notional('EUR_USD', 10_000, 1.10), 11_000.0)


class TestJpySizingRegression(unittest.TestCase):

    def setUp(self):
        self.sizer = PositionSizer()

    def test_usd_jpy_not_strangled_by_leverage_cap(self):
        """2% risk, 20-pip SL at 150.00 needs ~150k units — 15:1 leverage, no cap."""
        result = self.sizer.calculate(
            pair='USD_JPY', account_balance=10_000,
            stop_loss_pips=20, current_price=150.0,
        )
        self.assertIsNotNone(result)
        self.assertGreater(result.units, 100_000)   # was ~1,300 with the bug
        self.assertLessEqual(result.leverage_used, self.sizer.max_leverage)
        self.assertAlmostEqual(result.risk_percent, 0.02, places=3)

    def test_usd_jpy_risk_amount_accurate(self):
        result = self.sizer.calculate(
            pair='USD_JPY', account_balance=10_000,
            stop_loss_pips=20, current_price=150.0,
        )
        # 20 pips x 0.01 / 150 x units ≈ $200
        self.assertAlmostEqual(result.risk_amount, 200.0, delta=2.0)

    def test_leverage_cap_still_enforced_on_usd_jpy(self):
        """A tiny SL would imply a huge position — cap must bind at units = 20 x balance."""
        result = self.sizer.calculate(
            pair='USD_JPY', account_balance=10_000,
            stop_loss_pips=2, current_price=150.0,
        )
        self.assertLessEqual(result.units, 10_000 * self.sizer.max_leverage)
        self.assertAlmostEqual(result.leverage_used, self.sizer.max_leverage, places=1)

    def test_eur_usd_sizing_unchanged(self):
        result = self.sizer.calculate(
            pair='EUR_USD', account_balance=10_000,
            stop_loss_pips=20, current_price=1.10,
        )
        # $200 risk / (20 pips x $0.0001) = 100,000 units; notional $110k = 11x
        self.assertEqual(result.units, 100_000)
        self.assertAlmostEqual(result.leverage_used, 11.0, places=1)

    def test_validate_position_size_usd_jpy(self):
        ok, msg = self.sizer.validate_position_size(
            'USD_JPY', units=150_000, account_balance=10_000, current_price=150.0,
        )
        self.assertTrue(ok, msg)  # 15:1 — was rejected as 2,250:1 with the bug

    def test_get_max_position_size_usd_jpy(self):
        max_units = self.sizer.get_max_position_size('USD_JPY', 10_000, 150.0)
        self.assertEqual(max_units, 200_000)  # 20 x balance, not /150


if __name__ == "__main__":
    unittest.main(verbosity=2)
