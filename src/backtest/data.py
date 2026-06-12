"""Paginated OANDA historical candle downloader with CSV cache.

OANDA's candles endpoint returns at most 5000 candles per request; the live
broker wrapper (src/broker/oanda.py) intentionally has no pagination. This
module loops `from` + `count` pages to assemble multi-year series and caches
them per calendar year under data/backtest/.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from oandapyV20 import API
from oandapyV20.exceptions import V20Error
import oandapyV20.endpoints.instruments as instruments

from config.settings import settings

# Granularity → seconds per candle (used to advance the pagination cursor)
_GRANULARITY_SECONDS = {
    'M15': 15 * 60,
    'M30': 30 * 60,
    'H1': 3600,
    'H4': 4 * 3600,
    'D': 24 * 3600,
}

_MAX_CANDLES_PER_REQUEST = 5000

# Never cache candles newer than this — the tail of the series may still change
_CACHE_TAIL_GUARD = timedelta(days=2)


class HistoricalDataDownloader:
    """Download and cache complete historical candles from OANDA."""

    def __init__(
        self,
        api: Optional[API] = None,
        cache_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or logging.getLogger('backtest_data')
        self.api = api or API(
            access_token=settings.OANDA_API_KEY,
            environment=settings.OANDA_ENVIRONMENT,
            request_params={"timeout": 30},
        )
        self.cache_dir = cache_dir or (
            Path(__file__).parent.parent.parent / "data" / "backtest"
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(
        self,
        pair: str,
        granularity: str,
        start: datetime,
        end: datetime,
        use_cache: bool = True,
    ) -> List[Dict]:
        """Return candles for [start, end), using per-year CSV caches."""
        start = _as_utc(start)
        end = _as_utc(end)
        candles: List[Dict] = []

        for year in range(start.year, end.year + 1):
            year_start = max(start, datetime(year, 1, 1, tzinfo=timezone.utc))
            year_end = min(end, datetime(year + 1, 1, 1, tzinfo=timezone.utc))
            if year_start >= year_end:
                continue
            candles.extend(
                self._load_year(pair, granularity, year, year_start, year_end, use_cache)
            )

        # Dedupe on timestamp, keep chronological order, trim to range
        seen = set()
        out = []
        for c in sorted(candles, key=lambda c: c['time']):
            t = c['time']
            if t in seen:
                continue
            seen.add(t)
            ts = _parse_time(t)
            if start <= ts < end:
                out.append(c)
        return out

    def fetch(
        self,
        pair: str,
        granularity: str,
        start: datetime,
        end: datetime,
    ) -> List[Dict]:
        """Fetch candles for [start, end) directly from OANDA, paginated."""
        start = _as_utc(start)
        end = _as_utc(end)
        gran_seconds = _GRANULARITY_SECONDS.get(granularity, 3600)
        candles: List[Dict] = []
        cursor = start

        while cursor < end:
            params = {
                'granularity': granularity,
                'price': 'M',
                'from': cursor.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'count': _MAX_CANDLES_PER_REQUEST,
            }
            endpoint = instruments.InstrumentsCandles(instrument=pair, params=params)
            response = self._request_with_retry(endpoint)
            page = response.get('candles', [])

            page_candles = []
            last_time: Optional[datetime] = None
            for candle in page:
                ts = _parse_time(candle['time'])
                last_time = ts
                if not candle.get('complete', False):
                    continue
                if ts >= end:
                    continue
                mid = candle['mid']
                page_candles.append({
                    'time': candle['time'],
                    'open': float(mid['o']),
                    'high': float(mid['h']),
                    'low': float(mid['l']),
                    'close': float(mid['c']),
                    'volume': int(candle.get('volume', 0)),
                })

            candles.extend(page_candles)
            self.logger.info(
                f"{pair} {granularity}: fetched {len(page_candles)} candles "
                f"from {cursor:%Y-%m-%d} (total {len(candles)})"
            )

            if last_time is None:
                # Empty page (e.g. weekend-only range) — advance the cursor
                # past the empty window to avoid spinning
                cursor = cursor + timedelta(seconds=gran_seconds * _MAX_CANDLES_PER_REQUEST)
                continue
            if last_time >= end or len(page) < _MAX_CANDLES_PER_REQUEST:
                break
            cursor = last_time + timedelta(seconds=gran_seconds)

        return candles

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_year(
        self,
        pair: str,
        granularity: str,
        year: int,
        start: datetime,
        end: datetime,
        use_cache: bool,
    ) -> List[Dict]:
        cache_file = self.cache_dir / f"{pair}_{granularity}_{year}.csv"

        if use_cache and cache_file.exists():
            cached = self._read_cache(cache_file)
            if cached:
                last_ts = _parse_time(cached[-1]['time'])
                # Cache is sufficient when it already covers the requested end
                # (allow one tail-guard window of slack for the current year)
                if last_ts >= end - _CACHE_TAIL_GUARD - timedelta(days=1):
                    return cached
                self.logger.info(
                    f"{pair} {granularity} {year}: cache stale "
                    f"(ends {last_ts:%Y-%m-%d}) — refetching"
                )

        candles = self.fetch(pair, granularity, start, end)

        # Persist only the immutable part of the series
        cache_cutoff = datetime.now(timezone.utc) - _CACHE_TAIL_GUARD
        cacheable = [c for c in candles if _parse_time(c['time']) < cache_cutoff]
        if cacheable:
            self._write_cache(cache_file, cacheable)
        return candles

    def _read_cache(self, cache_file: Path) -> List[Dict]:
        try:
            df = pd.read_csv(cache_file)
            return df.to_dict('records')
        except Exception as exc:
            self.logger.warning(f"Cache read failed for {cache_file.name}: {exc}")
            return []

    def _write_cache(self, cache_file: Path, candles: List[Dict]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(candles).to_csv(cache_file, index=False)
            self.logger.info(f"Cached {len(candles)} candles → {cache_file.name}")
        except Exception as exc:
            self.logger.warning(f"Cache write failed for {cache_file.name}: {exc}")

    def _request_with_retry(self, endpoint, retries: int = 4, base_delay: float = 2.0):
        """Exponential-backoff retry — same pattern as OandaBroker._with_retry."""
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return self.api.request(endpoint)
            except V20Error as exc:
                if getattr(exc, 'code', None) == 429:
                    last_exc = exc
                    if attempt < retries - 1:
                        delay = base_delay * (2 ** attempt)
                        self.logger.warning(
                            f"OANDA rate limit (429) — retrying in {delay:.0f}s"
                        )
                        time.sleep(delay)
                    continue
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    delay = base_delay * (2 ** attempt)
                    self.logger.warning(
                        f"Candle fetch failed (attempt {attempt + 1}/{retries}): {exc} "
                        f"— retrying in {delay:.0f}s"
                    )
                    time.sleep(delay)
        raise last_exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    """Parse an OANDA RFC3339 timestamp (nanosecond precision) to aware UTC."""
    s = str(value).rstrip('Z')
    if '.' in s:
        head, frac = s.split('.', 1)
        s = f"{head}.{frac[:6]}"  # datetime supports microseconds only
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
