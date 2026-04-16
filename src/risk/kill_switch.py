"""Emergency kill switch for the FX trading bot.

Supports three activation methods (all checked on every call to is_active()):

  1. FILE   — Create  data/KILL_SWITCH  to activate;  delete it to deactivate.
  2. ENV    — Set  KILL_SWITCH=true  in .env (requires restart to re-read).
  3. IN-MEMORY — Call  kill_switch.activate()  at runtime (e.g. from Telegram).

When active:
  - The execution engine skips the trading cycle and closes all positions.
  - An alert is sent via Telegram (if configured).

Usage::

    ks = KillSwitch()
    if ks.is_active():
        ...  # halt

    ks.activate("emergency stop")
    ks.deactivate()
"""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


class KillSwitch:
    """Emergency stop mechanism.

    Thread-safe. All public methods are safe to call from any thread.
    """

    # Sentinel file path — create this file to activate the kill switch
    KILL_FILE: Path = Path("data/KILL_SWITCH")

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("kill_switch")
        self._lock = threading.Lock()
        self._active: bool = False
        self._reason: str = ""
        self._activated_at: Optional[datetime] = None

        # Ensure the data directory exists
        self.KILL_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Warn on startup if the kill file is already present
        if self.KILL_FILE.exists():
            self.logger.critical(
                "⛔ KILL SWITCH FILE DETECTED on startup — trading will be blocked "
                f"until the file is removed: {self.KILL_FILE.resolve()}"
            )

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """
        Return True if the kill switch is active.

        Checks (in order):
        1. In-memory flag (set by activate() or Telegram /stop command)
        2. Sentinel file  data/KILL_SWITCH
        3. Environment variable  KILL_SWITCH=true
        """
        with self._lock:
            if self._active:
                return True

        # File check (outside lock — file I/O shouldn't hold the lock)
        if self.KILL_FILE.exists():
            return True

        # Environment variable check
        env_val = os.getenv("KILL_SWITCH", "").lower()
        if env_val in ("1", "true", "yes"):
            return True

        return False

    def activate(self, reason: str = "manual") -> None:
        """
        Activate the kill switch.

        Creates the sentinel file, sets the in-memory flag, and logs CRITICAL.

        Args:
            reason: Human-readable reason (written to kill file and logs)
        """
        with self._lock:
            self._active = True
            self._reason = reason
            self._activated_at = datetime.utcnow()

        # Write kill file with reason and timestamp
        try:
            self.KILL_FILE.write_text(
                f"activated: {datetime.utcnow().isoformat()}Z\nreason: {reason}\n",
                encoding="utf-8"
            )
        except OSError as exc:
            self.logger.warning(f"Could not write kill file: {exc}")

        self.logger.critical(
            f"⛔ KILL SWITCH ACTIVATED — reason: {reason} — "
            "all trading halted and open positions will be closed"
        )

    def deactivate(self) -> None:
        """
        Deactivate the kill switch.

        Deletes the sentinel file and clears the in-memory flag.
        """
        with self._lock:
            self._active = False
            self._reason = ""
            self._activated_at = None

        # Remove kill file if present
        try:
            if self.KILL_FILE.exists():
                self.KILL_FILE.unlink()
        except OSError as exc:
            self.logger.warning(f"Could not remove kill file: {exc}")

        self.logger.info("✅ Kill switch deactivated — trading may resume")

    def get_reason(self) -> str:
        """Return the reason the kill switch was activated, or empty string."""
        with self._lock:
            if self._reason:
                return self._reason

        # Try reading from file
        if self.KILL_FILE.exists():
            try:
                content = self.KILL_FILE.read_text(encoding="utf-8")
                for line in content.splitlines():
                    if line.startswith("reason:"):
                        return line.split(":", 1)[1].strip()
            except OSError:
                pass

        env_val = os.getenv("KILL_SWITCH", "").lower()
        if env_val in ("1", "true", "yes"):
            return "KILL_SWITCH env var set"

        return ""

    def get_status(self) -> dict:
        """Return a status dictionary for logging / health endpoints."""
        active = self.is_active()
        return {
            "active": active,
            "reason": self.get_reason() if active else "",
            "activated_at": (
                self._activated_at.isoformat() if self._activated_at else None
            ),
            "kill_file_exists": self.KILL_FILE.exists(),
        }
