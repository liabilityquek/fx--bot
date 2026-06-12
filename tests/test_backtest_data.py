"""
Unit tests for the historical data downloader (src/backtest/data.py).

Run with:
    python -m pytest tests/test_backtest_data.py -v
"""

import sys
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.backtest.data import HistoricalDataDownloader, _parse_time


def _candle_payload(start: datetime, count: int, complete=True):
    out = []
    for i in range(count):
        t = start + timedelta(hours=i)
        out.append({
            'time': t.strftime('%Y-%m-%dT%H:%M:%S.000000000Z'),
            'complete': complete,
            'volume': 10,
            'mid': {'o': '1.1000', 'h': '1.1010', 'l': '1.0990', 'c': '1.1005'},
        })
    return out


class TestParseTime(unittest.TestCase):

    def test_nanosecond_timestamp(self):
        ts = _parse_time('2024-03-01T12:00:00.000000000Z')
        self.assertEqual(ts, datetime(2024, 3, 1, 12, tzinfo=timezone.utc))

    def test_plain_timestamp(self):
        ts = _parse_time('2024-03-01T12:00:00Z')
        self.assertEqual(ts.hour, 12)


class TestFetchPagination(unittest.TestCase):

    def _downloader(self, pages):
        api = MagicMock()
        api.request.side_effect = [{'candles': p} for p in pages]
        return HistoricalDataDownloader(
            api=api, cache_dir=Path(tempfile.mkdtemp()),
        ), api

    def test_two_pages_merged(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        page1 = _candle_payload(start, 5000)
        page2 = _candle_payload(start + timedelta(hours=5000), 100)
        dl, api = self._downloader([page1, page2])

        candles = dl.fetch('EUR_USD', 'H1', start, start + timedelta(hours=6000))
        self.assertEqual(len(candles), 5100)
        self.assertEqual(api.request.call_count, 2)

    def test_incomplete_candles_filtered(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        page = _candle_payload(start, 10) + _candle_payload(
            start + timedelta(hours=10), 1, complete=False
        )
        dl, _api = self._downloader([page])
        candles = dl.fetch('EUR_USD', 'H1', start, start + timedelta(hours=100))
        self.assertEqual(len(candles), 10)

    def test_candles_past_end_trimmed(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        page = _candle_payload(start, 100)
        dl, _api = self._downloader([page])
        candles = dl.fetch('EUR_USD', 'H1', start, start + timedelta(hours=50))
        self.assertEqual(len(candles), 50)

    def test_empty_page_advances_cursor(self):
        """An empty page must advance the cursor — no infinite loop."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # First page empty → cursor jumps 5000 bars; second page has data there
        page2 = _candle_payload(start + timedelta(hours=5000), 20)
        dl, api = self._downloader([[], page2])
        candles = dl.fetch('EUR_USD', 'H1', start, start + timedelta(hours=5500))
        self.assertEqual(api.request.call_count, 2)
        self.assertEqual(len(candles), 20)

    def test_all_pages_empty_terminates(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        dl, api = self._downloader([[], [], [], []])
        candles = dl.fetch('EUR_USD', 'H1', start, start + timedelta(hours=100))
        self.assertEqual(candles, [])
        self.assertEqual(api.request.call_count, 1)  # 100h < one 5000-bar jump


class TestLoadCache(unittest.TestCase):

    def test_load_dedupes_and_caches(self):
        start = datetime(2023, 3, 1, tzinfo=timezone.utc)
        end = datetime(2023, 3, 10, tzinfo=timezone.utc)
        page = _candle_payload(start, 9 * 24)
        api = MagicMock()
        api.request.return_value = {'candles': page}
        cache_dir = Path(tempfile.mkdtemp())
        dl = HistoricalDataDownloader(api=api, cache_dir=cache_dir)

        first = dl.load('EUR_USD', 'H1', start, end)
        self.assertEqual(len(first), 9 * 24)
        self.assertTrue((cache_dir / 'EUR_USD_H1_2023.csv').exists())

        # Second load must come from cache — no further API calls
        api.request.reset_mock()
        second = dl.load('EUR_USD', 'H1', start, end)
        api.request.assert_not_called()
        self.assertEqual(len(second), len(first))
        self.assertEqual(second[0]['close'], first[0]['close'])

    def test_load_trims_to_requested_range(self):
        start = datetime(2023, 3, 1, tzinfo=timezone.utc)
        page = _candle_payload(start, 100)
        api = MagicMock()
        api.request.return_value = {'candles': page}
        dl = HistoricalDataDownloader(api=api, cache_dir=Path(tempfile.mkdtemp()))

        out = dl.load('EUR_USD', 'H1', start + timedelta(hours=10),
                      start + timedelta(hours=20))
        self.assertEqual(len(out), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
