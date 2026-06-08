"""MechanicalRunner — Donchian-200 / 6×ATR forward-test path.

Per-pair-per-cycle runner that bypasses the LLM/voting/reviewer pipeline for
the four mechanical USD majors (EUR/USD, GBP/USD, AUD/USD, USD/JPY). Mirrors
`backtest/rules/donchian.py` + `backtest/engine.py` row-for-row.

Flow per call:
    1. Fetch D1 candles (broker already drops in-progress bar).
    2. Compute MechanicalSignal from the last fully closed bar.
    3. Idempotency: skip if already evaluated this UTC D1 close AND there is
       no open mechanical trade for this pair.
    4. Exit phase: for any open mechanical trade, call
       `trade_manager.evaluate_mechanical_exit(...)` against today's D1 bar.
       Close fires only on D1-close breach of `extreme ∓ 6×ATR_at_entry`.
    5. Entry phase: if no mechanical trade remains and an entry signal fired,
       size at MECHANICAL_RISK_PCT of full account and place a market order.
       Broker-side SL = entry ± 6×ATR (disaster fallback only). No TP.
    6. Reversal: if opposite signal fires while in a mechanical trade,
       steps 4 and 5 will naturally close-and-flip in the same cycle.

This runner is invoked by `TradingEngine._process_pair()` before the LLM
pipeline. USD/CHF and any other pair NOT in `settings.MECHANICAL_PAIRS`
falls through to the existing LLM/voting path unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import settings
from src.broker.base import BaseBroker, OrderSide
from src.execution.order_executor import OrderExecutor, OrderRequest, OrderType
from src.execution.trade_manager import TradeManager
from src.monitoring.alerts import AlertManager
from src.monitoring.logger import get_logger
from src.risk import PositionSizer, PositionSizingMethod
from src.strategy.donchian_mechanical import MechanicalSignal, compute_signal
from backtest.rules.donchian import DonchianConfig


STRATEGY_NAME = "mechanical_donchian_200_6atr"


class MechanicalRunner:
    """Donchian-200 / 6×ATR runner — one instance, all mechanical pairs."""

    def __init__(
        self,
        broker: BaseBroker,
        trade_manager: TradeManager,
        position_sizer: PositionSizer,
        alert_manager: Optional[AlertManager] = None,
        order_executor: Optional[OrderExecutor] = None,
        logger: Optional[logging.Logger] = None,
        dry_run: bool = False,
    ):
        self.broker = broker
        self.trade_manager = trade_manager
        self.position_sizer = position_sizer
        self.alert_manager = alert_manager
        self.order_executor = order_executor
        self.logger = logger or get_logger("MechanicalRunner")
        self.dry_run = dry_run

        self.cfg = DonchianConfig(
            lookback=settings.MECHANICAL_D1_LOOKBACK,
            sma_fast=settings.MECHANICAL_SMA_FAST,
            sma_slow=settings.MECHANICAL_SMA_SLOW,
            atr_period=settings.MECHANICAL_ATR_PERIOD,
            atr_mult=settings.MECHANICAL_ATR_MULT,
        )

        # Per-pair idempotency stamps (YYYY-MM-DD UTC of last evaluated D1 close)
        self._state_file = Path(settings.MECHANICAL_STATE_FILE)
        self._last_eval_day: Dict[str, str] = {}
        self._load_state()

    # ----------------------------------------------------------------------
    # State persistence
    # ----------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            if self._state_file.exists():
                self._last_eval_day = json.loads(self._state_file.read_text()) or {}
                self.logger.info(
                    f"[MECH] Loaded eval state for {len(self._last_eval_day)} pair(s)"
                )
        except Exception as exc:
            self.logger.warning(f"[MECH] Could not load eval state: {exc}")
            self._last_eval_day = {}

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._last_eval_day, indent=2))
        except Exception as exc:
            self.logger.warning(f"[MECH] Could not save eval state: {exc}")

    # ----------------------------------------------------------------------
    # Public entrypoint
    # ----------------------------------------------------------------------

    def evaluate(self, pair: str, account, positions) -> None:
        """Run one mechanical evaluation cycle for `pair`.

        Called once per `TradingEngine` cycle. Safe to call repeatedly within
        the same UTC D1: idempotency guard short-circuits when there is no
        new closed D1 bar AND no open mechanical trade requires tending.
        """
        if pair not in settings.MECHANICAL_PAIRS:
            return

        # Fetch D1 candles — broker.get_historical_candles already filters incomplete bars.
        # Derive fetch count from strategy requirements so the request auto-scales
        # if lookback / SMA / ATR settings change. MECHANICAL_D1_CANDLE_COUNT acts
        # as a manual floor.
        min_bars = (
            self.cfg.lookback + self.cfg.sma_slow + self.cfg.atr_period + 5
        )
        fetch_count = max(settings.MECHANICAL_D1_CANDLE_COUNT, min_bars + 30)
        try:
            candles = self.broker.get_historical_candles(
                pair, granularity="D", count=fetch_count
            ) or []
        except Exception as exc:
            self.logger.warning(f"[MECH] {pair}: D1 candle fetch failed: {exc}")
            return

        if len(candles) < min_bars:
            self.logger.warning(
                f"[MECH] {pair}: insufficient D1 candles ({len(candles)} < {min_bars})"
            )
            return

        signal = compute_signal(candles, self.cfg, pair=pair)
        if signal is None:
            self.logger.warning(f"[MECH] {pair}: compute_signal returned None")
            return

        eval_day = signal.signal_time.strftime("%Y-%m-%d")
        open_trades = self.trade_manager.get_mechanical_trades_for_pair(pair)
        already_evaluated = self._last_eval_day.get(pair) == eval_day

        if already_evaluated and not open_trades:
            # No new D1 bar AND nothing to manage — true no-op.
            return

        # ------------------------------------------------------------------
        # 1. EXIT PHASE — daily-close 6×ATR trail
        # ------------------------------------------------------------------
        for managed in open_trades:
            if managed.last_d1_eval_day == eval_day:
                continue  # already trailed against this D1 close
            if self.dry_run:
                self.logger.info(
                    f"[MECH-DRY] {pair}: would evaluate trail exit "
                    f"close={signal.close:.5f} extreme={managed.trailing_extreme} "
                    f"atr_at_entry={managed.atr_at_entry}"
                )
                continue
            self.trade_manager.evaluate_mechanical_exit(
                managed=managed,
                bar_high=signal.bar_high,
                bar_low=signal.bar_low,
                bar_close=signal.close,
                eval_day=eval_day,
                atr_mult=self.cfg.atr_mult,
            )

        # Refresh open trades after potential exit so reversal logic sees the
        # post-exit state.
        open_trades = self.trade_manager.get_mechanical_trades_for_pair(pair)

        # ------------------------------------------------------------------
        # 2. ENTRY PHASE — only on a fresh D1 close
        # ------------------------------------------------------------------
        if not already_evaluated:
            self._maybe_enter(pair, account, signal, open_trades)
            self._last_eval_day[pair] = eval_day
            self._save_state()

    # ----------------------------------------------------------------------
    # Entry logic
    # ----------------------------------------------------------------------

    def _maybe_enter(
        self,
        pair: str,
        account,
        signal: MechanicalSignal,
        open_trades: list,
    ) -> None:
        if not (signal.entry_long or signal.entry_short):
            return

        # Block entry if a same-direction mechanical trade is already open.
        # Opposite-direction is impossible here because the exit phase would
        # have closed it on signal flip (Donchian breakout in the opposite
        # channel implies trail exit on the same D1 close).
        if open_trades:
            existing = open_trades[0]
            same_dir = (
                (signal.entry_long and existing.trade.is_long)
                or (signal.entry_short and not existing.trade.is_long)
            )
            if same_dir:
                self.logger.info(
                    f"[MECH] {pair}: signal fired but same-direction trade already open — skip"
                )
                return
            # Opposite-direction — close existing first (reversal flow).
            self.logger.info(
                f"[MECH] {pair}: reversal — closing existing {existing.trade.side.value.upper()} "
                f"before flipping"
            )
            self.trade_manager.close_trade(
                existing.trade.trade_id, reason="mechanical_trail_close"
            )

        is_long = signal.entry_long
        side = OrderSide.BUY if is_long else OrderSide.SELL

        if signal.atr <= 0:
            self.logger.warning(f"[MECH] {pair}: invalid ATR {signal.atr} — skip entry")
            return

        pip_size = 0.01 if "JPY" in pair else 0.0001
        stop_distance = self.cfg.atr_mult * signal.atr
        stop_pips = int(round(stop_distance / pip_size))
        if stop_pips <= 0:
            self.logger.warning(f"[MECH] {pair}: degenerate stop_pips {stop_pips} — skip")
            return

        # Use the next D1 open as the entry-price proxy — broker fills at market.
        # The bar that produced the signal is `signal.signal_time`; we'll execute
        # at the current live ask/bid via place_market_order, but we need a
        # reference price for SL/sizing math.
        price_info = self.broker.get_current_price(pair)
        if not price_info:
            self.logger.warning(f"[MECH] {pair}: no live price — skip entry")
            return
        ref_price = price_info["ask"] if is_long else price_info["bid"]

        if is_long:
            stop_loss = ref_price - stop_distance
        else:
            stop_loss = ref_price + stop_distance

        # Position sizing at MECHANICAL_RISK_PCT of full account balance.
        size_result = self.position_sizer.calculate(
            pair=pair,
            account_balance=account.balance,
            stop_loss_pips=stop_pips,
            risk_percent=settings.MECHANICAL_RISK_PCT,
            method=PositionSizingMethod.PERCENT_RISK,
            current_price=ref_price,
        )
        if not size_result or size_result.units <= 0:
            self.logger.warning(f"[MECH] {pair}: position sizing failed")
            return
        units = size_result.units

        log_msg = (
            f"[MECH] {pair}: ENTRY signal {('LONG' if is_long else 'SHORT')} "
            f"close={signal.close:.5f} atr={signal.atr:.5f} "
            f"n_high={signal.n_high:.5f} n_low={signal.n_low:.5f} "
            f"sma_fast={signal.sma_fast:.5f} sma_slow={signal.sma_slow:.5f} "
            f"ref_price={ref_price:.5f} sl={stop_loss:.5f} "
            f"stop_pips={stop_pips} units={units:,} "
            f"risk={settings.MECHANICAL_RISK_PCT*100:.2f}%"
        )

        if self.dry_run:
            self.logger.info(f"[MECH-DRY] would place: {log_msg}")
            return

        self.logger.info(log_msg)

        # Place the order. No TP — mechanical trail handles exit.
        try:
            trade_id = self.broker.place_market_order(
                pair=pair,
                side=side,
                units=units,
                stop_loss=stop_loss,
                take_profit=None,
            )
        except Exception as exc:
            self.logger.error(f"[MECH] {pair}: order failed: {exc}")
            return

        if not trade_id:
            self.logger.error(f"[MECH] {pair}: broker returned no trade_id")
            return

        # Fetch the filled Trade so we have the real fill price.
        placed_trade = None
        for t in self.broker.get_open_trades():
            if t.trade_id == trade_id:
                placed_trade = t
                break

        if not placed_trade:
            self.logger.error(
                f"[MECH] {pair}: trade {trade_id} not found at broker post-fill"
            )
            return

        # Register with TradeManager flagged as mechanical so the per-second
        # loop skips break-even / partial-TP / live trailing.
        eval_day = signal.signal_time.strftime("%Y-%m-%d")
        self.trade_manager.register_trade(
            trade=placed_trade,
            strategy_name=STRATEGY_NAME,
            trailing_stop=False,
            confidence=1.0,
            entry_reason=(
                f"Donchian-200 {('LONG' if is_long else 'SHORT')} "
                f"close={signal.close:.5f} n_high={signal.n_high:.5f} "
                f"n_low={signal.n_low:.5f} atr={signal.atr:.5f}"
            ),
            setup_type="MECHANICAL_DONCHIAN",
            is_mechanical=True,
            atr_at_entry=signal.atr,
            trailing_extreme=placed_trade.entry_price,
            last_d1_eval_day=eval_day,
        )

        self.logger.info(
            f"[MECH] {pair}: filled trade_id={trade_id} entry={placed_trade.entry_price:.5f}"
        )

        if self.alert_manager:
            try:
                self.alert_manager.send_alert(
                    f"[MECH] {pair} {side.value.upper()} {units:,} units @ "
                    f"{placed_trade.entry_price:.5f} | SL {stop_loss:.5f} "
                    f"({stop_pips} pips, 6×ATR) | Strategy: Donchian-200/6×ATR (mechanical)",
                    priority="INFO",
                )
            except Exception:
                pass
