"""TradingEngine — main H1 loop for the multi-agent FX bot.

Every cycle:
  1. Kill switch check
  2. Weekend guard check
  3. Holiday guard check
  4. Account info + daily loss circuit breaker
  5. For each pair: fetch candles + price → decision_engine.run_decision()
     → risk checks → place order → register with TradeManager → Telegram alert
  6. TradeManager.update_all_trades() — trailing stop updates + age alerts
  7. Trade close detection (compare known open IDs vs current broker state)
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from config.settings import settings
from src.broker.base import BaseBroker, OrderSide, Trade
from src.monitoring.alerts import AlertManager
from src.monitoring.logger import get_logger
from src.risk import (
    ExposureTracker,
    PositionSizer,
    PositionSizingMethod,
    RiskValidator,
)
from src.risk.emergency_controller import EmergencyRiskController, ShutdownReason
from src.execution.trade_manager import TradeManager
from src.voting.engine import DecisionResult, DecisionEngine
from src.agents.base import Signal


class TradingEngine:
    """Main execution loop — sequential two-agent decision pipeline."""

    def __init__(
        self,
        broker: BaseBroker,
        decision_engine: DecisionEngine,
        alert_manager: AlertManager,
        kill_switch=None,
        weekend_guard=None,
        holiday_guard=None,
        logger: Optional[logging.Logger] = None,
        dry_run: bool = False,
    ):
        self.broker = broker
        self.decision_engine = decision_engine
        self.alert_manager = alert_manager
        self.kill_switch = kill_switch
        self.weekend_guard = weekend_guard
        self.holiday_guard = holiday_guard
        self.logger = logger or get_logger("TradingEngine")
        self.dry_run = dry_run

        # Risk modules
        self.risk_validator = RiskValidator(self.logger)
        self.position_sizer = PositionSizer(self.logger)
        self.exposure_tracker = ExposureTracker(self.logger)
        self.emergency_controller = EmergencyRiskController(logger=self.logger)
        self.trade_manager = TradeManager(
            broker=self.broker,
            logger=self.logger,
            alert_manager=self.alert_manager,
        )

        # State
        self._stop_event = threading.Event()
        self._cycle_count = 0
        self._known_open_trades: Dict[str, Trade] = {}   # trade_id → Trade
        self._initial_balance: Optional[float] = None

        # Daily loss circuit breaker
        self._daily_loss_start_balance: Optional[float] = None
        self._daily_loss_date: Optional[str] = None
        self._daily_loss_halted: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(
        self,
        interval_seconds: Optional[int] = None,
        max_cycles: Optional[int] = None,
    ) -> None:
        """Run the trading loop (blocking)."""
        interval = interval_seconds or settings.EXECUTION_INTERVAL_SECONDS
        self.alert_manager.alert_system_start()
        self.logger.info(
            f"TradingEngine started | interval={interval}s | "
            f"dry_run={self.dry_run} | pairs={settings.TRADING_PAIRS}"
        )

        # Seed known open trades from broker state
        try:
            for t in self.broker.get_open_trades():
                self._known_open_trades[t.trade_id] = t
        except Exception as exc:
            self.logger.warning(f"Failed to seed open trades on startup: {exc}")

        while not self._stop_event.is_set():
            self._run_cycle()
            self._cycle_count += 1

            if max_cycles and self._cycle_count >= max_cycles:
                self.logger.info(f"Reached max_cycles={max_cycles}, stopping.")
                break

            self._wait(interval)

        self.alert_manager.alert_system_stop()

    def stop(self) -> None:
        self._stop_event.set()

    def get_status(self) -> str:
        lines = [
            f"Cycle: #{self._cycle_count}",
            f"Dry run: {self.dry_run}",
        ]

        # Account info
        try:
            account = self.broker.get_account_info()
            if account:
                lines.append(
                    f"Balance: ${account.balance:.2f} | NAV: ${account.nav:.2f}"
                )
                pnl_sign = "+" if account.unrealized_pnl >= 0 else ""
                lines.append(f"Unrealized P/L: {pnl_sign}${account.unrealized_pnl:.2f}")
        except Exception:
            pass

        # Live open trades
        try:
            trades = self.broker.get_open_trades()
        except Exception:
            trades = list(self._known_open_trades.values())

        if not trades:
            lines.append("\nNo open positions.")
        else:
            lines.append(f"\nOpen positions ({len(trades)}):")
            for t in trades:
                direction = "LONG" if t.is_long else "SHORT"
                pnl_sign = "+" if t.unrealized_pnl >= 0 else ""
                sl_str = f"{t.stop_loss:.5f}" if t.stop_loss else "none"
                tp_str = f"{t.take_profit:.5f}" if t.take_profit else "none"
                lines.append(
                    f"  {t.pair} {direction} {t.units:,} units"
                    f" | Entry: {t.entry_price:.5f}"
                    f" | P/L: {pnl_sign}${t.unrealized_pnl:.2f}"
                    f" | SL: {sl_str} | TP: {tp_str}"
                )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cycle
    # ------------------------------------------------------------------

    def _run_cycle(self) -> None:
        cycle_num = self._cycle_count + 1
        cycle_start = time.time()
        self.logger.info(
            f"{'='*60}\n"
            f"Cycle #{cycle_num} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )

        # 1. Kill switch
        if self.kill_switch and self.kill_switch.is_active():
            reason = self.kill_switch.get_reason()
            self.logger.critical(f"KILL SWITCH ACTIVE ({reason}) — skipping cycle")
            return

        # 2. Weekend guard
        if self.weekend_guard and not self.weekend_guard.is_safe_to_trade():
            self.logger.info("Weekend guard: new trades blocked")
            return

        # 3. Holiday guard — flag only, do not return; risk layers still run
        is_holiday = False
        if self.holiday_guard and not self.holiday_guard.is_safe_to_trade():
            is_holiday = True
            self.logger.warning("Holiday guard: market holiday detected — new trades blocked")
            self.alert_manager.alert_error(
                "Market holiday detected. New trades blocked for today. "
                "Existing positions remain open and are protected by broker SL/TP."
            )

        # 4. Account info
        account = self.broker.get_account_info()
        if not account:
            self.logger.error("Failed to get account info — skipping cycle")
            return

        # Set initial balance once for drawdown tracking
        if self._initial_balance is None:
            self._initial_balance = account.balance
            self.logger.info(f"Initial balance recorded: {self._initial_balance:.2f}")

        self.logger.info(
            f"Account: balance={account.balance:.2f} "
            f"NAV={account.nav:.2f} "
            f"open_trades={account.open_trade_count}"
        )

        # Daily loss circuit breaker (normal days only — no new trades to halt on holidays)
        if not is_holiday:
            today = datetime.utcnow().strftime('%Y-%m-%d')
            if self._daily_loss_date != today:
                self._daily_loss_date = today
                self._daily_loss_start_balance = account.balance
                self._daily_loss_halted = False

            if self._daily_loss_start_balance and not self._daily_loss_halted:
                daily_loss_pct = (
                    (self._daily_loss_start_balance - account.nav) / self._daily_loss_start_balance
                )
                if daily_loss_pct >= settings.MAX_DAILY_LOSS_PERCENT:
                    self.logger.critical(
                        f"Daily loss limit reached ({daily_loss_pct:.1%}) — halting new trades today"
                    )
                    self._daily_loss_halted = True
                    self.alert_manager.alert_error(
                        f"Daily loss limit reached ({daily_loss_pct:.1%}) — trading halted"
                    )

        # Update exposure tracker
        positions = self.broker.get_positions()
        self.exposure_tracker.update_positions(positions, account.balance)

        # Layer 3 — Emergency risk check (normal days and holidays)
        try:
            self._run_emergency_check(account, positions)
        except Exception as exc:
            self.logger.error(f"Emergency check error: {exc}")

        # 5. Process each pair (normal days only)
        if not is_holiday:
            for pair in settings.TRADING_PAIRS:
                if self._daily_loss_halted:
                    break
                try:
                    self._process_pair(pair, account, positions)
                except Exception as exc:
                    self.logger.error(f"Error processing {pair}: {exc}")

        # Layer 2 + 6 — Trailing stop updates and age alerts (normal days and holidays)
        try:
            self.trade_manager.update_all_trades()
        except Exception as exc:
            self.logger.error(f"Trade manager update error: {exc}")

        # Layer 4 — Trade close detection (normal days and holidays)
        try:
            self._check_closed_trades()
        except Exception as exc:
            self.logger.error(f"Trade close detection error: {exc}")

        elapsed = time.time() - cycle_start
        self.logger.info(f"Cycle #{cycle_num} complete in {elapsed:.1f}s")

    def _process_pair(self, pair: str, account, positions) -> None:
        self.logger.info(f"--- {pair} ---")

        # Fetch candles — broker returns List[Dict] with flat keys
        candles: List[Dict] = []
        try:
            candles = self.broker.get_historical_candles(
                pair, granularity=settings.TIMEFRAME, count=settings.CANDLE_COUNT
            ) or []
        except Exception as exc:
            self.logger.warning(f"{pair}: candle fetch failed: {exc}")

        if not candles:
            self.logger.warning(f"{pair}: no candle data")
            return

        # Current price
        price_info = self.broker.get_current_price(pair)
        if not price_info:
            self.logger.warning(f"{pair}: no price data")
            return
        price = (price_info['bid'] + price_info['ask']) / 2

        # Run decision pipeline
        result: DecisionResult = self.decision_engine.run_decision(pair, candles, price)
        self._log_vote_result(result)

        if result.final_signal == Signal.HOLD:
            self.logger.info(
                f"{pair}: HOLD (confidence={result.confidence:.2f} | "
                f"reviewer={result.reviewer_verdict})"
            )
            return

        # Conflict check — skip if existing position in opposite direction
        is_long = result.final_signal == Signal.BUY

        for pos in positions:
            if pos.pair == pair and not pos.is_flat:
                if (is_long and pos.is_short) or (not is_long and pos.is_long):
                    self.logger.info(
                        f"{pair}: skipping — existing position in opposite direction "
                        f"(net_units={pos.net_units})"
                    )
                    return

        # SL/TP via ATR
        entry_price = price_info['ask'] if is_long else price_info['bid']
        sl_pips, stop_loss, take_profit = self._calc_sl_tp(
            pair, candles, entry_price, is_long
        )

        # Position size
        size_result = self.position_sizer.calculate(
            pair=pair,
            account_balance=account.balance,
            stop_loss_pips=max(1, int(sl_pips)),
            method=PositionSizingMethod.PERCENT_RISK,
        )
        if not size_result:
            self.logger.warning(f"{pair}: position sizing failed")
            return

        units = size_result.units

        # Risk validator
        exposure_report = self.exposure_tracker.get_current_exposure()
        validation = self.risk_validator.validate_trade(
            pair=pair,
            units=units,
            stop_loss_pips=sl_pips,
            account_balance=account.balance,
            current_exposure_percent=exposure_report.total_exposure_percent,
            open_positions=positions,
            entry_price=entry_price,
            margin_available=account.margin_available,
        )
        if not validation.approved:
            self.logger.info(f"{pair}: risk rejected — {', '.join(validation.reasons)}")
            return

        # Place order
        if self.dry_run:
            self.logger.info(
                f"{pair}: DRY RUN — would {result.final_signal.value} "
                f"{units:,} units @ {entry_price:.5f} "
                f"SL={stop_loss:.5f} TP={take_profit:.5f}"
            )
            self._send_vote_alert(
                pair, result, entry_price, stop_loss, take_profit, units, dry_run=True
            )
            return

        side = OrderSide.BUY if is_long else OrderSide.SELL
        try:
            trade_id = self.broker.place_market_order(
                pair=pair,
                side=side,
                units=units,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        except Exception as exc:
            self.logger.error(f"{pair}: order failed: {exc}")
            return

        if trade_id:
            self.logger.info(
                f"{pair}: order filled | trade_id={trade_id} "
                f"entry~{entry_price:.5f}"
            )
            # Track trade for close detection and TradeManager
            # Fetch from broker to get real fill price and full Trade object
            filled_price = entry_price
            placed_trade = None
            for t in self.broker.get_open_trades():
                if t.trade_id == trade_id:
                    filled_price = t.entry_price
                    self._known_open_trades[trade_id] = t
                    placed_trade = t
                    break

            # Register with TradeManager so trailing stops activate immediately
            if placed_trade:
                self.trade_manager.register_trade(
                    placed_trade,
                    strategy_name=f"{result.final_signal.value.lower()}_signal",
                    trailing_stop=True,
                    trailing_distance=self.trade_manager.trailing_stop_distance_pips,
                )

            self._send_vote_alert(
                pair, result, filled_price, stop_loss, take_profit, units
            )

    # ------------------------------------------------------------------
    # Trade close detection
    # ------------------------------------------------------------------

    def _check_closed_trades(self) -> None:
        if not self._known_open_trades:
            return

        current_trades = self.broker.get_open_trades()
        current_ids = {t.trade_id for t in current_trades}

        for trade_id, trade in list(self._known_open_trades.items()):
            if trade_id not in current_ids:
                self.logger.info(
                    f"Trade closed detected: {trade_id} ({trade.pair})"
                )
                self.alert_manager.alert_trade_closed(
                    pair=trade.pair,
                    pnl=0.0,
                    reason="Closed (SL/TP/manual)",
                )
                del self._known_open_trades[trade_id]

        # Add any new trades we don't know about yet
        for t in current_trades:
            if t.trade_id not in self._known_open_trades:
                self._known_open_trades[t.trade_id] = t

    # ------------------------------------------------------------------
    # Emergency risk check
    # ------------------------------------------------------------------

    def _run_emergency_check(self, account, positions) -> None:
        """Layer 3 — evaluate emergency risk conditions; close all positions if required.

        Runs on both normal trading days and public holidays so open positions
        are never left unprotected by the bot's risk layer.
        """
        exposure_report = self.exposure_tracker.get_current_exposure()

        status = self.emergency_controller.check_emergency_conditions(
            account_balance=account.balance,
            initial_balance=self._initial_balance or account.balance,
            open_positions=positions,
            current_exposure_percent=exposure_report.total_exposure_percent,
            unrealized_pnl=account.unrealized_pnl,
        )

        if status.requires_shutdown:
            reason = status.shutdown_reason.value if status.shutdown_reason else "risk_limit"
            self.logger.critical(
                f"Emergency shutdown required: {reason} — closing all positions"
            )
            self.alert_manager.alert_error(
                f"EMERGENCY SHUTDOWN: {reason} — closing all positions now"
            )
            self.trade_manager.emergency_close_all(reason=reason)

    # ------------------------------------------------------------------
    # SL/TP calculation
    # ------------------------------------------------------------------

    def _calc_sl_tp(
        self,
        pair: str,
        candles: List[Dict],
        entry_price: float,
        is_long: bool,
    ):
        """Return (sl_pips, stop_loss_price, take_profit_price)."""
        from config.pairs import PAIR_INFO

        pip_value = PAIR_INFO.get(pair, {}).get('pip_value', 0.0001)
        atr_val = _calc_atr(candles)

        if atr_val and atr_val > 0:
            sl_distance = atr_val * 2.0
        else:
            sl_distance = settings.DEFAULT_STOP_LOSS_PIPS * pip_value

        tp_distance = sl_distance * settings.DEFAULT_TAKE_PROFIT_RATIO

        if is_long:
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + tp_distance
        else:
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - tp_distance

        sl_pips = sl_distance / pip_value
        return sl_pips, stop_loss, take_profit

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def _send_vote_alert(
        self,
        pair: str,
        result: DecisionResult,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        units: int,
        dry_run: bool = False,
    ) -> None:
        """Send plain-text decision alert to Telegram."""
        prefix = "DRY RUN -- " if dry_run else ""
        lines = [
            f"{prefix}TRADE OPENED -- {pair}",
            f"Direction: {result.final_signal.value}",
            f"Entry: {entry_price:.5f}",
            f"SL: {stop_loss:.5f} | TP: {take_profit:.5f}",
            f"Size: {units:,} units",
            f"Confidence: {result.confidence:.2f}",
            "",
            f"LLM: {result.final_signal.value} ({result.confidence:.2f}) | "
            f"Reviewer: {result.reviewer_verdict} -- {result.reviewer_reason}",
        ]
        text = "\n".join(lines)
        self.alert_manager._send_telegram(text, parse_mode='')

    def _log_vote_result(self, result: DecisionResult) -> None:
        self.logger.info(
            f"{result.pair}: {result.final_signal.value} "
            f"conf={result.confidence:.2f} | "
            f"reviewer={result.reviewer_verdict} — {result.reviewer_reason}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wait(self, seconds: int) -> None:
        """Sleep in 1-second chunks so stop_event is checked promptly."""
        for _ in range(seconds):
            if self._stop_event.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _calc_atr(candles: List[Dict], period: int = 14) -> Optional[float]:
    """Simple ATR from broker candle list (flat-key format)."""
    if not candles or len(candles) < period + 1:
        return None
    df = pd.DataFrame([
        {
            'high':  float(c.get('high', 0)),
            'low':   float(c.get('low', 0)),
            'close': float(c.get('close', 0)),
        }
        for c in candles
    ])
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(window=period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None
