"""Alert management for Telegram notifications.

Includes an optional Telegram command poller that listens for:
  /stop   — activates the kill switch (halts all trading + closes positions)
  /resume — deactivates the kill switch
  /status — replies with current bot status
"""

import logging
import os
import threading
import time
import requests
from typing import Optional, Callable
from datetime import datetime, date

from config.settings import settings


class AlertManager:
    """Manage Telegram alerts for trading notifications."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize alert manager.
        
        Args:
            logger: Logger instance
        """
        self.logger = logger or logging.getLogger('alerts')
        self.enabled = settings.ALERT_ENABLED
        self.bot_token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '') or ''
        self.chat_id = getattr(settings, 'TELEGRAM_CHAT_ID', '') or ''
        
        if self.enabled:
            if not self.bot_token or not self.chat_id:
                self.logger.warning("Telegram credentials missing. Alerts disabled.")
                self.logger.warning("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
                self.enabled = False
            else:
                self.logger.info("✅ Telegram alerts initialized")
    
    def _send_telegram(self, text: str, parse_mode: str = 'Markdown') -> bool:
        """
        Send message via Telegram Bot API.
        
        Args:
            text: Message text
            parse_mode: 'Markdown' or 'HTML'
        
        Returns:
            True if sent successfully
        """
        if not self.enabled:
            return False
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': parse_mode
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            safe_error = str(e)
            if self.bot_token:
                safe_error = safe_error.replace(self.bot_token, '[REDACTED]')
            self.logger.error(f"Failed to send Telegram alert: {safe_error}")
            return False
    
    def send_alert(self, message: str, priority: str = 'INFO') -> bool:
        """
        Send Telegram alert.
        
        Args:
            message: Alert message
            priority: Priority level (INFO, WARNING, ERROR, CRITICAL)
        
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled:
            self.logger.debug(f"Alert (disabled): {message}")
            return False
        
        # Priority emoji
        priority_emoji = {
            'INFO': 'ℹ️',
            'WARNING': '⚠️',
            'ERROR': '❌',
            'CRITICAL': '🚨'
        }.get(priority, 'ℹ️')
        
        # Format message with timestamp and priority
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted_message = (
            f"🤖 *FX Bot Alert*\n\n"
            f"{priority_emoji} *{priority}*\n"
            f"{message}\n\n"
            f"_{timestamp}_"
        )
        
        success = self._send_telegram(formatted_message)
        
        if success:
            self.logger.info(f"✅ Telegram alert sent: {message[:50]}...")
        
        return success
    
    def alert_trade_opened(
        self,
        pair: str,
        side: str,
        units: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float
    ):
        """Send alert for new trade."""
        direction_emoji = "🟢" if side.upper() == 'BUY' else "🔴"
        
        message = (
            f"{direction_emoji} *Trade Opened*\n\n"
            f"*Pair:* `{pair}`\n"
            f"*Direction:* {side.upper()}\n"
            f"*Size:* {units:,} units\n"
            f"*Entry:* `{entry_price:.5f}`\n"
            f"*Stop Loss:* `{stop_loss:.5f}`\n"
            f"*Take Profit:* `{take_profit:.5f}`"
        )
        self.send_alert(message, 'INFO')
    
    def alert_trade_closed(
        self,
        pair: str,
        pnl: float,
        reason: str
    ):
        """Send alert for closed trade."""
        emoji = "✅" if pnl >= 0 else "❌"
        outcome = "Profit" if pnl >= 0 else "Loss"

        message = (
            f"{emoji} *Trade Closed*\n\n"
            f"*Pair:* `{pair}`\n"
            f"*Outcome:* {outcome}\n"
            f"*Reason:* {reason}"
        )
        priority = 'INFO' if pnl >= 0 else 'WARNING'
        self.send_alert(message, priority)
    
    def alert_error(self, error_message: str):
        """Send error alert."""
        message = f"⚠️ *Error*\n\n`{error_message}`"
        self.send_alert(message, 'ERROR')

    def alert_llm_credits_exhausted(self):
        """Send critical alert when both Groq and Anthropic credits are exhausted."""
        message = (
            "🔴 *LLM Credits Exhausted*\n\n"
            "Both Groq and Anthropic API credits are depleted.\n"
            "The LLM analyst is now offline — trading halted this cycle.\n\n"
            "Top up credits to restore full functionality."
        )
        self.send_alert(message, 'CRITICAL')

    def alert_reviewer_unavailable(self, pair: str, reason: str):
        """Send alert when ReviewerAgent is unavailable — trade blocked this cycle."""
        message = (
            f"⚠️ *Reviewer Unavailable*\n\n"
            f"*Pair:* `{pair}`\n"
            f"*Reason:* {reason}\n"
            f"Trade blocked — reviewer could not run this cycle."
        )
        self.send_alert(message, 'WARNING')
    
    def alert_system_start(self):
        """Send system startup alert."""
        mode = "📝 PAPER" if settings.PAPER_TRADING_MODE else "💰 LIVE"

        message = (
            f"🚀 *System Started*\n\n"
            f"*Mode:* {mode}"
        )
        self.send_alert(message, 'INFO')
    
    def alert_system_stop(self):
        """Send system shutdown alert."""
        message = "🛑 *System Stopped*\n\nTrading bot has been shut down."
        self.send_alert(message, 'INFO')
    
    def alert_news_suspend(self, event_name: str, pair: str):
        """Send alert for news-based trading suspension."""
        message = (
            f"📰 *Trading Suspended*\n\n"
            f"*Event:* {event_name}\n"
            f"*Affected:* `{pair}`\n"
            f"*Window:* {settings.NEWS_SUSPEND_BEFORE_MINUTES}min before → "
            f"{settings.NEWS_RESUME_AFTER_MINUTES}min after"
        )
        self.send_alert(message, 'WARNING')
    
    def alert_strategy_update(self, summary: str):
        """Send alert when LLM strategist updates config."""
        message = (
            f"🧠 *Strategy Updated*\n\n"
            f"{summary}"
        )
        self.send_alert(message, 'INFO')
    
    def alert_daily_summary(
        self,
        trades_count: int,
        total_pnl: float,
        win_rate: float
    ):
        """Send daily trading summary."""
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        pnl_direction = "Positive" if total_pnl >= 0 else "Negative"

        message = (
            f"📊 *Daily Summary*\n\n"
            f"*Trades:* {trades_count}\n"
            f"*P/L:* {pnl_direction} {pnl_emoji}\n"
            f"*Win Rate:* {win_rate:.1f}%"
        )
        self.send_alert(message, 'INFO')
    
    def test_connection(self) -> bool:
        """Test Telegram connection."""
        if not self.bot_token or not self.chat_id:
            self.logger.error("Telegram credentials not configured")
            return False

        return self._send_telegram("🔔 *Test Alert*\n\nFX Bot Telegram alerts working!")

    # ------------------------------------------------------------------
    # Telegram command poller
    # ------------------------------------------------------------------

    def start_command_poller(
        self,
        kill_switch=None,
        get_status_fn: Optional[Callable[[], str]] = None,
        get_calendar_fn: Optional[Callable[[], str]] = None,
        get_calhistory_fn: Optional[Callable[[], str]] = None,
        get_credits_fn: Optional[Callable[[], str]] = None,
        get_analyst_fn: Optional[Callable[[], str]] = None,
        get_reviewer_fn: Optional[Callable[[], str]] = None,
        poll_interval_seconds: int = 10
    ) -> None:
        """
        Start a background daemon thread that polls Telegram for commands.

        Args:
            kill_switch: KillSwitch instance to activate/deactivate
            get_status_fn: Optional callable returning a status string for /status
            get_calendar_fn: Optional callable returning a formatted calendar string for /calendar
            get_calhistory_fn: Optional callable returning past events string for /calhistory
            get_credits_fn: Optional callable returning LLM provider credit status for /credits
            poll_interval_seconds: How often to poll (default: 10s)
        """
        if not self.enabled:
            self.logger.debug("Telegram alerts disabled — command poller not started")
            return

        self._kill_switch_ref = kill_switch
        self._get_status_fn = get_status_fn
        self._get_calendar_fn = get_calendar_fn
        self._get_calhistory_fn = get_calhistory_fn
        self._get_credits_fn = get_credits_fn
        self._get_analyst_fn = get_analyst_fn
        self._get_reviewer_fn = get_reviewer_fn
        self._poll_interval = poll_interval_seconds
        self._last_update_id: int = self._fetch_latest_update_id()

        thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="TelegramCommandPoller"
        )
        thread.start()
        self.logger.info(
            f"📱 Telegram command poller started (interval: {poll_interval_seconds}s) "
            "— commands: /stop, /resume, /status, /calendar, /calhistory, /logs, /credits, /analyst, /reviewer"
        )

    def _fetch_latest_update_id(self) -> int:
        """Get the current highest update_id so we don't replay old commands on restart."""
        if not self.bot_token:
            return 0
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getUpdates",
                params={"offset": -1, "timeout": 0},
                timeout=8,
            )
            resp.raise_for_status()
            results = resp.json().get("result", [])
            if results:
                return results[-1]["update_id"]
        except Exception:
            pass
        return 0

    def _poll_loop(self) -> None:
        """Background loop: poll Telegram getUpdates and dispatch commands."""
        while True:
            try:
                self._check_commands()
            except Exception as exc:
                self.logger.warning(f"Telegram poll error: {exc}")
            time.sleep(self._poll_interval)

    def _check_commands(self) -> None:
        """Fetch new Telegram updates and handle recognised commands."""
        if not self.bot_token:
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {
            "offset": self._last_update_id + 1,
            "timeout": 0,
            "allowed_updates": ["message"],
        }

        try:
            resp = requests.get(url, params=params, timeout=8)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            safe = str(exc)
            if self.bot_token:
                safe = safe.replace(self.bot_token, "[REDACTED]")
            self.logger.warning(f"getUpdates failed: {safe}")
            return

        for update in data.get("result", []):
            self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip().lower()
            from_chat = str(msg.get("chat", {}).get("id", ""))

            # Only respond to the configured chat
            if from_chat != str(self.chat_id):
                self.logger.debug(
                    f"Ignoring message from chat {from_chat} (expected {self.chat_id})"
                )
                continue

            self.logger.debug(f"Telegram command received: {text!r}")
            try:
                if text in ("/stop", "/kill"):
                    self._handle_stop()
                elif text in ("/resume", "/start"):
                    self._handle_resume()
                elif text == "/status":
                    self._handle_status()
                elif text == "/calendar":
                    self._handle_calendar()
                elif text == "/calhistory":
                    self._handle_calhistory()
                elif text == "/logs":
                    self._handle_logs()
                elif text == "/credits":
                    self._handle_credits()
                elif text == "/analyst":
                    self._handle_analyst()
                elif text == "/reviewer":
                    self._handle_reviewer()
                elif text == "/help":
                    self._send_telegram(
                        "🤖 *FX Bot Commands*\n\n"
                        "/stop — activate kill switch (halt trading + close positions)\n"
                        "/resume — deactivate kill switch (resume trading)\n"
                        "/status — show current bot status\n"
                        "/calendar — upcoming economic events today\n"
                        "/calhistory — past economic events today\n"
                        "/logs — today's bot log entries\n"
                        "/credits — LLM provider credit status\n"
                        "/analyst — last analyst decision per pair\n"
                        "/reviewer — last reviewer verdict per pair\n"
                        "/help — show this message"
                    )
            except Exception as exc:
                self.logger.error(f"Error handling Telegram command {text!r}: {exc}")

    def _handle_stop(self) -> None:
        """Activate kill switch via Telegram /stop command."""
        ks = getattr(self, '_kill_switch_ref', None)
        if ks:
            ks.activate("Telegram /stop command")
        self._send_telegram(
            "⛔ *Kill Switch Activated*\n\n"
            "All trading halted and open positions will be closed.\n"
            "Send /resume to reactivate trading."
        )
        self.logger.warning("Kill switch activated via Telegram /stop command")

    def _handle_resume(self) -> None:
        """Deactivate kill switch via Telegram /resume command."""
        ks = getattr(self, '_kill_switch_ref', None)
        if ks:
            ks.deactivate()
        self._send_telegram(
            "✅ *Kill Switch Deactivated*\n\n"
            "Trading will resume on the next cycle."
        )
        self.logger.info("Kill switch deactivated via Telegram /resume command")

    def _handle_status(self) -> None:
        """Reply with current bot status."""
        ks = getattr(self, '_kill_switch_ref', None)
        ks_status = "⛔ HALTED" if (ks and ks.is_active()) else "✅ ACTIVE"

        fn = getattr(self, '_get_status_fn', None)
        extra = fn() if fn else ""

        msg = (
            f"📊 Bot Status\n\n"
            f"Trading: {ks_status}\n"
            f"Mode: {'PAPER' if settings.PAPER_TRADING_MODE else 'LIVE'}\n"
        )
        if extra:
            msg += f"\n{extra}"

        self._send_telegram(msg, parse_mode="")

    def _handle_calendar(self) -> None:
        """Reply with today's upcoming economic calendar events."""
        fn = getattr(self, '_get_calendar_fn', None)
        if not fn:
            self._send_telegram("📅 Calendar\n\nCalendar not configured.", parse_mode="")
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode="")
        except Exception as exc:
            self._send_telegram(f"📅 Calendar\n\nFailed to fetch events: {exc}", parse_mode="")

    def _handle_calhistory(self) -> None:
        """Reply with today's past economic calendar events."""
        fn = getattr(self, '_get_calhistory_fn', None)
        if not fn:
            self._send_telegram("📅 Calendar History\n\nCalendar not configured.", parse_mode="")
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode="")
        except Exception as exc:
            self._send_telegram(f"📅 Calendar History\n\nFailed to fetch events: {exc}", parse_mode="")

    def _handle_logs(self) -> None:
        """Reply with today's log entries (last 30 lines)."""
        log_path = settings.LOG_FILE_PATH
        today_str = date.today().strftime("%Y-%m-%d")

        if not os.path.exists(log_path):
            self._send_telegram(f"📋 *Logs*\n\nLog file not found at `{log_path}`.")
            return

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            today_lines = [ln.rstrip() for ln in lines if today_str in ln]

            if not today_lines:
                self._send_telegram(f"📋 *Logs*\n\nNo entries for today ({today_str}).")
                return

            tail = today_lines[-30:]
            text = "\n".join(tail)
            # Telegram message limit is 4096 chars
            if len(text) > 3800:
                text = "...\n" + text[-3800:]

            self._send_telegram(
                f"📋 *Logs — {today_str}*\n\n```\n{text}\n```",
                parse_mode="Markdown"
            )
        except Exception as exc:
            self._send_telegram(f"📋 *Logs*\n\nFailed to read log file: {exc}")

    def _handle_credits(self) -> None:
        """Reply with LLM provider credit/availability status."""
        fn = getattr(self, '_get_credits_fn', None)
        if not fn:
            self._send_telegram("💳 *LLM Credits*\n\nCredit status not available.")
            return
        try:
            status_text = fn()
            self._send_telegram(f"💳 *LLM Provider Status*\n\n`{status_text}`")
        except Exception as exc:
            self._send_telegram(f"💳 *LLM Credits*\n\nFailed to fetch status: {exc}")

    def _handle_analyst(self) -> None:
        """Reply with the last analyst decision for each pair."""
        fn = getattr(self, '_get_analyst_fn', None)
        if not fn:
            self._send_telegram("Analyst History\n\nAnalyst history not available.", parse_mode="")
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode="")
        except Exception as exc:
            self._send_telegram(f"Analyst History\n\nFailed to fetch analyst history: {exc}", parse_mode="")

    def _handle_reviewer(self) -> None:
        """Reply with the last reviewer verdict for each pair."""
        fn = getattr(self, '_get_reviewer_fn', None)
        if not fn:
            self._send_telegram("Reviewer History\n\nReviewer history not available.", parse_mode="")
            return
        try:
            msg = fn()
            self._send_telegram(msg, parse_mode="")
        except Exception as exc:
            self._send_telegram(f"Reviewer History\n\nFailed to fetch reviewer history: {exc}", parse_mode="")
