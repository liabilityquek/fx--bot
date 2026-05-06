"""TradingEngine — main H1 loop for the multi-agent FX bot.

Every cycle:
  1. Kill switch check
  2. Weekend guard check
  3. Holiday guard check
  4. Account info + daily loss circuit breaker
  5. For each pair: fetch candles + price → decision_engine.run_decision()
     → risk checks → place order → register with TradeManager → Telegram alert
  6. Trade close detection (compare known open IDs vs current broker state)
  7. TradeManager.update_all_trades() — trailing stop updates + age alerts
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
from src.news.suspension_manager import SuspensionManager
from src.risk.conflict_checker import TradeConflictChecker


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
        event_monitor=None,
        news_watcher=None,
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
        self.conflict_checker = TradeConflictChecker(allow_hedging=False)

        # News suspension (Rule 1 & 2) — only active when event_monitor provided
        self.suspension_manager = (
            SuspensionManager(event_monitor=event_monitor, logger=self.logger)
            if event_monitor else None
        )

        # News risk watcher (Rule 3) — set after construction via main.py
        self.news_watcher = news_watcher

        # State
        self._stop_event = threading.Event()
        self._cycle_count = 0
        self._trades_lock = threading.Lock()
        self._known_open_trades: Dict[str, Trade] = {}   # trade_id → Trade
        self._initial_balance: Optional[float] = None

        # Daily loss circuit breaker
        self._daily_loss_start_balance: Optional[float] = None
        self._daily_loss_date: Optional[str] = None
        self._daily_loss_halted: bool = False

        # Holiday alert dedup
        self._holiday_alert_sent_date: Optional[str] = None

        # Monitoring thread
        self._monitoring_stop_event = threading.Event()
        self._monitoring_thread = None
        self._monitoring_cycle_count = 0

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
            with self._trades_lock:
                for t in self.broker.get_open_trades():
                    self._known_open_trades[t.trade_id] = t
        except Exception as exc:
            self.logger.warning(f"Failed to seed open trades on startup: {exc}")

        if self.news_watcher:
            self.news_watcher.start()

        # Start monitoring thread
        self._monitoring_stop_event = threading.Event()
        self._monitoring_thread = threading.Thread(
            target=self._run_monitoring_loop,
            name="MonitoringThread",
            daemon=True
        )
        self._monitoring_thread.start()
        self.logger.info("Monitoring thread started")

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
        if self.news_watcher:
            self.news_watcher.stop()

        # Stop monitoring thread
        self._monitoring_stop_event.set()
        if self._monitoring_thread:
            self._monitoring_thread.join(timeout=5)

    def _run_monitoring_cycle(self) -> None:
        """High-frequency monitoring cycle for trailing stops, risk checks, and close detection."""
        cycle_num = self._monitoring_cycle_count + 1
        self.logger.info(
            f"Monitoring cycle #{cycle_num} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )

        # 1. Kill switch check
        if self.kill_switch and self.kill_switch.is_active():
            reason = self.kill_switch.get_reason()
            self.logger.critical(f"KILL SWITCH ACTIVE ({reason}) — skipping monitoring cycle")
            return

        # 2. Account info
        account = self.broker.get_account_info()
        if not account:
            self.logger.error("Failed to get account info — skipping monitoring cycle")
            return

        # 3. Update exposure tracker
        positions = self.broker.get_positions()
        self.exposure_tracker.update_positions(positions, account.balance)

        # 4. Emergency risk check
        try:
            self._run_emergency_check(account, positions)
        except Exception as exc:
            self.logger.error(f"Emergency check error: {exc}")

        # 5. Trade close detection
        try:
            self._check_closed_trades()
        except Exception as exc:
            self.logger.error(f"Trade close detection error: {exc}")

        # 6. Trailing stop updates and age alerts
        try:
            self.trade_manager.update_all_trades()
        except Exception as exc:
            self.logger.error(f"Trade manager update error: {exc}")

        self._monitoring_cycle_count += 1

    def _run_monitoring_loop(self) -> None:
        """Background monitoring loop — runs at MONITORING_INTERVAL_SECONDS."""
        interval = settings.MONITORING_INTERVAL_SECONDS
        self.logger.info(f"Monitoring loop started | interval={interval}s")

        while not self._monitoring_stop_event.is_set():
            self._run_monitoring_cycle()
            self._monitoring_stop_event.wait(interval)

        self.logger.info("Monitoring loop stopped")

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
                    f"  {t.pair} {direction} {t.units / 100_000:.2f} lots"
                    f" | Entry: {t.entry_price:.5f}"
                    f" | P/L: {pnl_sign}${t.unrealized_pnl:.2f}"
                    f" | SL: {sl_str} | TP: {tp_str}"
                )

        lines.append(f"\nMonitoring cycles: #{self._monitoring_cycle_count}")

        return "\n".join(lines)

    def get_known_trades_snapshot(self) -> Dict[str, Trade]:
        """Return a thread-safe copy of currently tracked open trades."""
        with self._trades_lock:
            return dict(self._known_open_trades)

    def remove_known_trade(self, trade_id: str) -> None:
        """Remove a trade from the known-open set (called by NewsWatcher on close)."""
        with self._trades_lock:
            self._known_open_trades.pop(trade_id, None)

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
            today_str = datetime.utcnow().strftime('%Y-%m-%d')
            if self._holiday_alert_sent_date != today_str:
                self._holiday_alert_sent_date = today_str
                self.alert_manager._send_telegram(
                    "Market holiday detected. New trades blocked for today. "
                    "Existing positions remain open and are protected by broker SL/TP.",
                    parse_mode=''
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

        # 5. Process each pair (normal days only)
        if not is_holiday:
            self._cycle_pair_prices: dict = {}
            for pair in settings.TRADING_PAIRS:
                if self._daily_loss_halted:
                    break
                try:
                    self._process_pair(pair, account, positions)
                except Exception as exc:
                    self.logger.error(f"Error processing {pair}: {exc}")

        elapsed = time.time() - cycle_start
        self.logger.info(f"Cycle #{cycle_num} complete in {elapsed:.1f}s")

    def _process_pair(self, pair: str, account, positions) -> None:
        self.logger.info(f"--- {pair} ---")

        # Rule 1 & 2 — news suspension check
        if self.suspension_manager:
            status = self.suspension_manager.check_suspension_status(pair=pair)
            if status.is_suspended:
                resume_str = (
                    status.resume_time.strftime('%H:%M')
                    if status.resume_time else 'TBD'
                )
                self.logger.info(
                    f"{pair}: suspended — {status.message} (resumes ~{resume_str})"
                )
                return

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

        # Accumulate pair prices for USD sentiment (prev close, current close)
        if len(candles) >= 2:
            prev_close = float(candles[-2].get('close', 0) or (candles[-2].get('mid', {}).get('c', 0)))
            curr_close = float(candles[-1].get('close', 0) or (candles[-1].get('mid', {}).get('c', 0)))
            self._cycle_pair_prices[pair] = (prev_close, curr_close)

        # Fetch higher-timeframe candles for MTF bias (non-fatal)
        htf_candles: dict = {}
        try:
            d1 = self.broker.get_historical_candles(pair, granularity='D', count=60) or []
            if d1:
                htf_candles['D1'] = d1
        except Exception:
            pass
        try:
            h4 = self.broker.get_historical_candles(pair, granularity='H4', count=60) or []
            if h4:
                htf_candles['H4'] = h4
        except Exception:
            pass
        m15_candles: list = []
        try:
            m15 = self.broker.get_historical_candles(pair, granularity='M15', count=10) or []
            if m15:
                htf_candles['M15'] = m15
                m15_candles = m15
        except Exception:
            pass

        # Run decision pipeline
        result: DecisionResult = self.decision_engine.run_decision(pair, candles, price, pair_prices=self._cycle_pair_prices, htf_candles=htf_candles)
        self._log_vote_result(result)

        if result.final_signal == Signal.HOLD:
            self.logger.info(
                f"{pair}: HOLD (confidence={result.confidence:.2f} | "
                f"reviewer={result.reviewer_verdict})"
            )
            return

        is_long = result.final_signal == Signal.BUY

        # Phase 1.1: Confluence validation
        confluence_count, confluence_types = _count_indicator_confluences(
            result.indicators, is_long, price
        )
        result.confluence_count = confluence_count
        result.confluence_types = confluence_types
        min_confluences = settings.MIN_CONFLUENCES
        self.logger.info(
            f"{pair}: confluence check — {confluence_count}/{min_confluences} "
            f"[{', '.join(confluence_types) if confluence_types else 'none'}] "
            f"({'PASS' if confluence_count >= min_confluences else 'FAIL'})"
        )
        if confluence_count < min_confluences:
            self.logger.info(
                f"{pair}: REJECTED — insufficient confluences "
                f"({confluence_count} < {min_confluences} required)"
            )
            return

        # Phase 1.3: Setup type quality filter
        setup_quality = _get_setup_quality_score(result.setup_type)
        if setup_quality == 0:
            self.logger.info(
                f"{pair}: REJECTED — low-quality setup type ({result.setup_type})"
            )
            return

        # Require higher confidence for lower-quality setups
        min_confidence_for_setup = _get_min_confidence_for_setup(result.setup_type)
        if result.confidence < min_confidence_for_setup:
            self.logger.info(
                f"{pair}: REJECTED — confidence {result.confidence:.2f} below minimum "
                f"{min_confidence_for_setup:.2f} for setup type {result.setup_type}"
            )
            return

        # Conflict check — skip if existing position in opposite direction
        for pos in positions:
            if pos.pair == pair and not pos.is_flat:
                if (is_long and pos.is_short) or (not is_long and pos.is_long):
                    self.logger.info(
                        f"{pair}: skipping — existing position in opposite direction "
                        f"(net_units={pos.net_units})"
                    )
                    return

        # USD correlation guard — limit correlated USD-directional exposure
        usd_short_pairs = {'EUR_USD', 'GBP_USD', 'AUD_USD'}
        usd_long_pairs = {'USD_JPY', 'USD_CHF'}
        new_is_usd_short = (pair in usd_short_pairs and is_long) or (pair in usd_long_pairs and not is_long)
        new_is_usd_long  = (pair in usd_long_pairs and is_long) or (pair in usd_short_pairs and not is_long)

        # Debug logging to understand position counting
        self.logger.info(
            f"{pair}: checking USD correlation | total_positions={len(positions)} | "
            f"non_flat_positions={sum(1 for pos in positions if not pos.is_flat)}"
        )

        open_usd_short = sum(
            1 for pos in positions if not pos.is_flat and (
                (pos.pair in usd_short_pairs and pos.is_long) or
                (pos.pair in usd_long_pairs and pos.is_short)
            )
        )
        open_usd_long = sum(
            1 for pos in positions if not pos.is_flat and (
                (pos.pair in usd_long_pairs and pos.is_long) or
                (pos.pair in usd_short_pairs and pos.is_short)
            )
        )

        # Log details of positions being counted
        if open_usd_long > 0 or open_usd_short > 0:
            counted_positions = [
                f"{pos.pair} {'LONG' if pos.is_long else 'SHORT'} net_units={pos.net_units}"
                for pos in positions if not pos.is_flat
            ]
            self.logger.info(
                f"{pair}: USD correlation | open_usd_long={open_usd_long} | open_usd_short={open_usd_short} | "
                f"counted_positions={counted_positions}"
            )

        max_corr = settings.MAX_USD_CORRELATED_TRADES
        if new_is_usd_short and open_usd_short >= max_corr:
            self.logger.info(
                f"{pair}: USD overexposure blocked — {open_usd_short} USD-short positions already open (max {max_corr})"
            )
            return
        if new_is_usd_long and open_usd_long >= max_corr:
            self.logger.info(
                f"{pair}: USD overexposure blocked — {open_usd_long} USD-long positions already open (max {max_corr})"
            )
            return

        # M15 momentum gate — block if short-term momentum contradicts signal
        if m15_candles and not _m15_momentum_aligned(m15_candles, is_long):
            self.logger.info(
                f"{pair}: M15 momentum gate blocked — "
                f"{'BUY' if is_long else 'SELL'} signal conflicts with M15 short-term direction"
            )
            return

        # SL/TP via ATR
        entry_price = price_info['ask'] if is_long else price_info['bid']
        sl_pips, stop_loss, take_profit, atr_val = self._calc_sl_tp(
            pair, candles, entry_price, is_long
        )
        sl_pips_int = max(1, int(sl_pips))

        # Phase 1.2: Minimum RR validation
        from config.pairs import PAIR_INFO
        pip_value = PAIR_INFO.get(pair, {}).get('pip_value', 0.0001)
        tp_pips = abs(take_profit - entry_price) / pip_value
        rr_ratio = tp_pips / sl_pips if sl_pips > 0 else 0.0
        min_rr = settings.MIN_RR_RATIO
        if rr_ratio < min_rr:
            self.logger.info(
                f"{pair}: REJECTED — poor RR ratio ({rr_ratio:.2f} < {min_rr:.2f} required)"
            )
            return

        # Position size
        size_result = self.position_sizer.calculate(
            pair=pair,
            account_balance=account.balance,
            stop_loss_pips=sl_pips_int,
            method=PositionSizingMethod.PERCENT_RISK,
            current_price=entry_price,
        )
        if not size_result:
            self.logger.warning(f"{pair}: position sizing failed")
            return

        units = size_result.units

        # Risk validator
        exposure_report = self.exposure_tracker.get_current_exposure()
        margin_util_pct = (account.margin_used / account.nav * 100) if account.nav > 0 else 0.0
        validation = self.risk_validator.validate_trade(
            pair=pair,
            units=units,
            stop_loss_pips=sl_pips_int,
            account_balance=account.balance,
            current_exposure_percent=margin_util_pct,
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
                    with self._trades_lock:
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
                    confidence=result.confidence,
                    entry_reason=result.llm_reasoning,
                    setup_type=result.setup_type,
                    reviewer_verdict=result.reviewer_verdict,
                    reviewer_reason=result.reviewer_reason,
                )
                if atr_val and atr_val > 0:
                    self.trade_manager.update_trade_atr(placed_trade.trade_id, atr_val)

            self._send_vote_alert(
                pair, result, filled_price, stop_loss, take_profit, units
            )

    # ------------------------------------------------------------------
    # Trade close detection
    # ------------------------------------------------------------------

    def _check_closed_trades(self) -> None:
        with self._trades_lock:
            if not self._known_open_trades:
                return
            snapshot = dict(self._known_open_trades)

        current_trades = self.broker.get_open_trades()
        current_ids = {t.trade_id for t in current_trades}

        for trade_id, trade in snapshot.items():
            if trade_id not in current_ids:
                # Fetch real close details from OANDA (SL / TP / user)
                info = self.broker.get_closed_trade_info(trade_id)
                close_price = info.get('close_price', trade.current_price)
                realized_pnl = info.get('realized_pnl', 0.0)
                raw_reason = info.get('reason', 'user')

                reason_label = {
                    'stop_loss': 'Stop Loss Hit',
                    'take_profit': 'Take Profit Hit',
                    'user': 'Closed by User',
                }.get(raw_reason, 'Closed by User')

                pip_size = 0.01 if 'JPY' in trade.pair else 0.0001
                pips_gained = (close_price - trade.entry_price) / pip_size
                if not trade.is_long:
                    pips_gained = -pips_gained

                self.logger.info(
                    f"Trade closed: {trade_id} ({trade.pair}) | "
                    f"Entry: {trade.entry_price:.5f} | Close: {close_price:.5f} "
                    f"({pips_gained:+.1f} pips) | P/L: ${realized_pnl:+.2f} | {reason_label}"
                )

                self.alert_manager.alert_trade_closed(
                    pair=trade.pair,
                    pnl=realized_pnl,
                    close_price=close_price,
                    entry_price=trade.entry_price,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit,
                    pips=pips_gained,
                    reason=reason_label,
                )

                self.trade_manager.unregister_trade(trade_id)
                with self._trades_lock:
                    self._known_open_trades.pop(trade_id, None)

        # Add any new trades we don't know about yet
        with self._trades_lock:
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
        margin_util_pct = (account.margin_used / account.nav * 100) if account.nav > 0 else 0.0

        status = self.emergency_controller.check_emergency_conditions(
            account_balance=account.balance,
            initial_balance=self._initial_balance or account.balance,
            open_positions=positions,
            current_exposure_percent=margin_util_pct,
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
        """Return (sl_pips, stop_loss_price, take_profit_price, atr_val)."""
        from config.pairs import PAIR_INFO

        pip_value = PAIR_INFO.get(pair, {}).get('pip_value', 0.0001)
        atr_val = _calc_atr(candles)

        if atr_val and atr_val > 0:
            multiplier = _get_atr_multiplier(atr_val, candles)
            self.logger.info(f"{pair} ATR multiplier: {multiplier}x (ATR={atr_val:.5f})")
            sl_distance = atr_val * multiplier
        else:
            multiplier = 2.0
            sl_distance = settings.DEFAULT_STOP_LOSS_PIPS * pip_value

        tp_distance = sl_distance * settings.DEFAULT_TAKE_PROFIT_RATIO

        if is_long:
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + tp_distance
        else:
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - tp_distance

        sl_pips = sl_distance / pip_value
        return sl_pips, stop_loss, take_profit, atr_val

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
            f"Setup: {result.setup_type}",
            f"Confluences: {result.confluence_count}/{settings.MIN_CONFLUENCES} "
            f"[{', '.join(result.confluence_types)}]",
            f"Entry: {entry_price:.5f}",
            f"SL: {stop_loss:.5f} | TP: {take_profit:.5f}",
            f"Size: {units / 100_000:.2f} lots",
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


def _get_atr_multiplier(atr_val: float, candles: List[Dict], atr_period: int = 14, avg_period: int = 50) -> float:
    """Return adaptive ATR multiplier (1.5/2.0/3.0) based on current vs historical ATR."""
    if not candles or len(candles) < atr_period + avg_period + 1:
        return 2.0
    df = pd.DataFrame([
        {
            'high':  float(c.get('high', 0)),
            'low':   float(c.get('low', 0)),
            'close': float(c.get('close', 0)),
        }
        for c in candles
    ])
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(window=atr_period).mean()
    atr_avg = atr_series.iloc[-avg_period:].mean()
    if pd.isna(atr_avg) or atr_avg == 0:
        return 2.0
    ratio = atr_val / atr_avg
    if ratio > 1.5:
        return 3.0
    if ratio < 0.8:
        return 1.5
    return 2.0


def _m15_momentum_aligned(m15_candles: list, is_long: bool) -> bool:
    """
    Returns True if the last 5 M15 candles support the intended direction.
    Blocks only when momentum clearly contradicts — ambiguous/flat = allowed.
    """
    recent = m15_candles[-5:] if len(m15_candles) >= 5 else m15_candles
    if len(recent) < 3:
        return True  # insufficient data — ambiguous, allow per design

    bullish = sum(
        1 for c in recent
        if float(c.get('close', 0) or c.get('mid', {}).get('c', 0)) >
           float(c.get('open',  0) or c.get('mid', {}).get('o', 0))
    )
    bearish = len(recent) - bullish

    first_close = float(recent[0].get('close', 0) or recent[0].get('mid', {}).get('c', 0))
    last_close  = float(recent[-1].get('close', 0) or recent[-1].get('mid', {}).get('c', 0))
    net_move = last_close - first_close

    if is_long:
        # Block only if clearly bearish momentum
        return not (bearish > bullish and net_move < 0)
    else:
        # Block only if clearly bullish momentum
        return not (bullish > bearish and net_move > 0)


def _count_indicator_confluences(
    indicators: dict, is_long: bool, price: float
) -> tuple:
    """
    Count indicator signals aligned with the trade direction.
    Returns (count, [list of confluence names]).
    Max 7 confluences: RSI, MACD, EMA trend, ADX, Fisher, Bollinger, Market Structure
    """
    aligned = []

    rsi_val = indicators.get('rsi')
    if rsi_val is not None:
        if is_long and rsi_val > 50:
            aligned.append('RSI')
        elif not is_long and rsi_val < 50:
            aligned.append('RSI')

    macd_hist = indicators.get('macd_hist')
    if macd_hist is not None:
        if is_long and macd_hist > 0:
            aligned.append('MACD')
        elif not is_long and macd_hist < 0:
            aligned.append('MACD')

    trend = indicators.get('trend')
    if trend is not None:
        if is_long and trend == 'bullish':
            aligned.append('EMA trend')
        elif not is_long and trend == 'bearish':
            aligned.append('EMA trend')

    adx_val = indicators.get('adx')
    if adx_val is not None and adx_val >= 20:
        aligned.append('ADX')

    fisher_val = indicators.get('fisher')
    if fisher_val is not None:
        if is_long and fisher_val > 0:
            aligned.append('Fisher')
        elif not is_long and fisher_val < 0:
            aligned.append('Fisher')

    bb_mid = indicators.get('bb_mid')
    if bb_mid is not None and price > 0:
        if is_long and price > bb_mid:
            aligned.append('Bollinger')
        elif not is_long and price < bb_mid:
            aligned.append('Bollinger')

    ms = indicators.get('market_structure')
    if ms is not None:
        if is_long and ms == 'bullish_structure':
            aligned.append('Market Structure')
        elif not is_long and ms == 'bearish_structure':
            aligned.append('Market Structure')

    return len(aligned), aligned


def _get_setup_quality_score(setup_type: str) -> int:
    """
    Return quality score for setup type (0-5).

    Quality tiers:
    - 5: BREAKOUT (highest quality)
    - 4: PULLBACK
    - 3: REVERSAL
    - 2: LIQUIDITY_SWEEP
    - 0: RANGE, NONE (rejected)

    Returns 0 for setups that should be rejected.
    """
    quality_map = {
        'BREAKOUT': 5,
        'PULLBACK': 4,
        'REVERSAL': 3,
        'LIQUIDITY_SWEEP': 2,
        'RANGE': 0,
        'NONE': 0,
    }
    return quality_map.get(setup_type.upper(), 0)


def _get_min_confidence_for_setup(setup_type: str) -> float:
    """
    Return minimum confidence threshold for setup type.

    Higher-quality setups can proceed with lower confidence.
    Lower-quality setups require higher confidence.

    Returns:
    - BREAKOUT: 0.60 (standard threshold)
    - PULLBACK: 0.65
    - REVERSAL: 0.70
    - LIQUIDITY_SWEEP: 0.75
    - RANGE, NONE: 1.00 (effectively rejected)
    """
    confidence_map = {
        'BREAKOUT': 0.60,
        'PULLBACK': 0.65,
        'REVERSAL': 0.70,
        'LIQUIDITY_SWEEP': 0.75,
        'RANGE': 1.00,
        'NONE': 1.00,
    }
    return confidence_map.get(setup_type.upper(), 0.70)
