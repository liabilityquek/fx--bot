"""Supabase trade logger — persists closed trades for future RAG retrieval.

Instantiated by TradeManager if SUPABASE_URL and SUPABASE_KEY env vars are present.
All inserts are non-fatal: failures are logged as warnings and never propagate.

Runtime usage: Python supabase library (pip install supabase)
Dev usage:     Supabase MCP server (separate setup, not required for runtime)
"""

import logging
import os

_logger = logging.getLogger('supabase_logger')


class SupabaseTradeLogger:
    """Insert closed trade records into the Supabase `trades` table."""

    def __init__(self) -> None:
        url = os.environ.get('SUPABASE_URL', '').strip()
        key = os.environ.get('SUPABASE_KEY', '').strip()
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in environment")

        try:
            from supabase import create_client, Client  # type: ignore
            self._client: Client = create_client(url, key)
            _logger.info("Supabase client initialised")
        except ImportError as exc:
            raise ImportError(
                "supabase package not installed — run: pip install supabase"
            ) from exc

    def log_closed_trade(self, data: dict) -> None:
        """Insert a closed trade row.  Non-fatal — logs warning on failure."""
        try:
            self._client.table('trades').insert(data).execute()
        except Exception as exc:
            _logger.warning(f"Supabase insert failed: {exc}")


def create_supabase_logger() -> 'SupabaseTradeLogger | None':
    """Factory — returns a SupabaseTradeLogger if env vars are configured, else None."""
    if os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_KEY'):
        try:
            return SupabaseTradeLogger()
        except Exception as exc:
            _logger.warning(f"Supabase logger disabled: {exc}")
    return None
