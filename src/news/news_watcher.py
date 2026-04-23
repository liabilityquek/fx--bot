"""NewsWatcher — background daemon that closes trades ahead of VERY_HIGH events.

Rule 3: evaluates open trades via NewsRiskAgent and closes if LLM confidence
exceeds NEWS_RISK_CLOSE_THRESHOLD.  Runs on its own thread, polling every
NEWS_RISK_POLL_INTERVAL_SECONDS seconds.
"""

import logging
import threading
from typing import Callable, Dict, Optional, Set

from .event_monitor import EventImpact


class NewsWatcher:
    """Background daemon: evaluate and close trades before VERY_HIGH events."""

    def __init__(
        self,
        event_monitor,
        broker,
        alert_manager,
        news_risk_agent,
        get_trades_snapshot_fn: Callable,
        on_trade_closed_fn: Callable,
        logger: Optional[logging.Logger] = None,
    ):
        self.event_monitor = event_monitor
        self.broker = broker
        self.alert_manager = alert_manager
        self.news_risk_agent = news_risk_agent
        self.get_trades_snapshot_fn = get_trades_snapshot_fn
        self.on_trade_closed_fn = on_trade_closed_fn
        self.logger = logger or logging.getLogger("NewsWatcher")

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._evaluated: Set[str] = set()  # trade_ids already evaluated this cycle

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="NewsWatcher", daemon=True
        )
        self._thread.start()
        self.logger.info("NewsWatcher: started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.logger.info("NewsWatcher: stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        from config.settings import settings
        while not self._stop_event.is_set():
            try:
                self._check()
            except Exception as exc:
                self.logger.error(f"NewsWatcher: check error: {exc}")
            self._stop_event.wait(settings.NEWS_RISK_POLL_INTERVAL_SECONDS)

    def _check(self) -> None:
        from config.settings import settings

        imminent = self.event_monitor.get_imminent_events(
            minutes=settings.NEWS_RISK_MINUTES_BEFORE,
            min_impact=EventImpact.VERY_HIGH,
        )
        if not imminent:
            return

        trades = self.get_trades_snapshot_fn()
        if not trades:
            return

        for event in imminent:
            for trade_id, trade in trades.items():
                if event.currency not in trade.pair:
                    continue
                if trade_id in self._evaluated:
                    continue
                self._evaluated.add(trade_id)

                try:
                    decision = self.news_risk_agent.evaluate(trade, event)
                except Exception as exc:
                    self.logger.warning(
                        f"NewsWatcher: risk evaluation failed for {trade_id}: {exc}"
                    )
                    continue

                self.logger.info(
                    f"NewsWatcher: {trade.pair} {trade_id} — "
                    f"decision={('CLOSE' if decision.should_close else 'HOLD')} "
                    f"conf={decision.confidence:.2f} reason={decision.reason}"
                )

                if decision.should_close:
                    self._close_and_alert(trade, event, decision)

    def _close_and_alert(self, trade, event, decision) -> None:
        try:
            success = self.broker.close_trade(trade.trade_id)
        except Exception as exc:
            self.logger.error(
                f"NewsWatcher: failed to close {trade.trade_id}: {exc}"
            )
            return

        if success:
            self.on_trade_closed_fn(trade.trade_id)
            msg = (
                f"NEWS RISK CLOSE -- {trade.pair}\n"
                f"Event: {event.event_name} in {event.minutes_until:.0f}min\n"
                f"Confidence: {decision.confidence:.2f}\n"
                f"Reason: {decision.reason}"
            )
            try:
                self.alert_manager._send_telegram(msg, parse_mode='')
            except Exception as exc:
                self.logger.warning(f"NewsWatcher: Telegram alert failed: {exc}")
        else:
            self.logger.warning(
                f"NewsWatcher: broker.close_trade returned falsy for {trade.trade_id}"
            )
