"""Supabase trade logger — persists closed trades for future RAG retrieval.

Instantiated by TradeManager if SUPABASE_URL and SUPABASE_KEY env vars are present.
All inserts are non-fatal: failures are logged as warnings and never propagate.

Runtime usage: Python supabase library (pip install supabase)
Dev usage:     Supabase MCP server (separate setup, not required for runtime)
"""

import logging
import os
from typing import List, Optional

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
        self.insert_trade(data)

    def _normalize_fields(self, data: dict) -> dict:
        """Normalize field names to match Supabase table schema."""
        field_mapping = {
            'direction': 'side',
            'entry_time': 'open_time',
            'pnl': 'realized_pnl',
            'pnl_pips': 'pips_gained'
        }

        normalized = {}
        for key, value in data.items():
            # Use mapped name if exists, otherwise keep original
            normalized_key = field_mapping.get(key, key)
            normalized[normalized_key] = value

        return normalized

    def insert_trade(self, data: dict) -> None:
        """Insert a trade row. Non-fatal — logs warning on failure."""
        try:
            normalized_data = self._normalize_fields(data)
            self._client.table('trades').insert(normalized_data).execute()
        except Exception as exc:
            _logger.warning(f"Supabase insert failed: {exc}")

    def update_trade(self, trade_id: str, data: dict) -> None:
        """Update a trade row by trade_id. Non-fatal — logs warning on failure."""
        try:
            normalized_data = self._normalize_fields(data)
            self._client.table('trades').update(normalized_data).eq('trade_id', trade_id).execute()
        except Exception as exc:
            _logger.warning(f"Supabase update failed for trade {trade_id}: {exc}")

    def get_trade(self, trade_id: str) -> Optional[dict]:
        """Get a trade by trade_id. Returns None if not found or on error."""
        try:
            response = self._client.table('trades').select('*').eq('trade_id', trade_id).execute()
            return response.data[0] if response.data else None
        except Exception as exc:
            _logger.warning(f"Supabase query failed for trade {trade_id}: {exc}")
            return None

    def get_recent_trades(self, limit: int = 10) -> List[dict]:
        """Get recent trades ordered by close_time DESC."""
        try:
            response = self._client.table('trades').select('*').order('close_time', desc=True).limit(limit).execute()
            return response.data
        except Exception as exc:
            _logger.warning(f"Supabase query failed for recent trades: {exc}")
            return []


def create_supabase_logger() -> 'SupabaseTradeLogger | None':
    """Factory — returns a SupabaseTradeLogger if env vars are configured, else None."""
    if os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_KEY'):
        try:
            return SupabaseTradeLogger()
        except Exception as exc:
            _logger.warning(f"Supabase logger disabled: {exc}")
    return None
