#!/usr/bin/env python
"""Backtest the SuperTrend+EMA200 strategy on historical OANDA data.

Usage:
    python scripts/backtest.py --pairs EUR_USD,USD_JPY --years 5
    python scripts/backtest.py --pairs EUR_USD --from 2021-06-01 --to 2026-06-01 \
        --balance 10000 --spread-mult 1.5 --csv-out reports/

Requires OANDA_API_KEY in the environment / .env (practice account is fine).
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Strategy mode must be set BEFORE config.settings is imported so the
# H1_CANDLE_COUNT default and validation behave correctly.
os.environ.setdefault('STRATEGY_MODE', 'strategy')

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings  # noqa: E402
from src.backtest import (             # noqa: E402
    BacktestConfig,
    BacktestSimulator,
    HistoricalDataDownloader,
    format_report,
)
from src.monitoring.logger import get_logger  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--pairs', default=','.join(settings.TRADING_PAIRS),
        help='Comma-separated pairs (default: configured TRADING_PAIRS)',
    )
    parser.add_argument('--years', type=float, default=5.0,
                        help='Years of history to test (default 5)')
    parser.add_argument('--from', dest='date_from', default=None,
                        help='Start date YYYY-MM-DD (overrides --years)')
    parser.add_argument('--to', dest='date_to', default=None,
                        help='End date YYYY-MM-DD (default: now)')
    parser.add_argument('--granularity', default='H1')
    parser.add_argument('--balance', type=float, default=10_000.0)
    parser.add_argument('--spread-mult', type=float, default=1.0,
                        help='Spread stress multiplier (default 1.0)')
    parser.add_argument('--no-cache', action='store_true',
                        help='Bypass the CSV candle cache')
    parser.add_argument('--csv-out', default=None,
                        help='Directory to write trades.csv and equity.csv')
    return parser.parse_args()


def main():
    args = parse_args()
    logger = get_logger('backtest')

    end = (
        datetime.strptime(args.date_to, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if args.date_to else datetime.now(timezone.utc)
    )
    start = (
        datetime.strptime(args.date_from, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if args.date_from else end - timedelta(days=int(args.years * 365.25))
    )
    pairs = [p.strip() for p in args.pairs.split(',') if p.strip()]

    logger.info(
        f"Backtest: {pairs} | {start:%Y-%m-%d} -> {end:%Y-%m-%d} | "
        f"{args.granularity} | balance ${args.balance:,.0f} | "
        f"spread x{args.spread_mult}"
    )
    logger.info(
        f"Strategy: SuperTrend({settings.STRATEGY_SUPERTREND_PERIOD}, "
        f"{settings.STRATEGY_SUPERTREND_MULTIPLIER}) + EMA{settings.STRATEGY_EMA_PERIOD} | "
        f"window {settings.H1_CANDLE_COUNT} bars"
    )

    downloader = HistoricalDataDownloader(logger=logger)
    candles_by_pair = {}
    for pair in pairs:
        candles = downloader.load(
            pair, args.granularity, start, end, use_cache=not args.no_cache
        )
        logger.info(f"{pair}: {len(candles)} candles loaded")
        if candles:
            candles_by_pair[pair] = candles

    if not candles_by_pair:
        logger.error("No candle data — check OANDA_API_KEY / network")
        sys.exit(1)

    config = BacktestConfig(
        pairs=pairs,
        start=start,
        end=end,
        balance=args.balance,
        spread_mult=args.spread_mult,
        granularity=args.granularity,
    )
    result = BacktestSimulator(config, logger).run(candles_by_pair)

    report = format_report(
        result.trades,
        result.equity,
        args.balance,
        result.final_balance,
        gate_rejections=result.gate_rejections,
        signals_seen=result.signals_seen,
        title=(
            f"SUPERTREND+EMA200 BACKTEST — {', '.join(pairs)} "
            f"({start:%Y-%m-%d} -> {end:%Y-%m-%d})"
        ),
    )
    print(report)

    if args.csv_out:
        import pandas as pd
        out_dir = Path(args.csv_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(
            out_dir / 'trades.csv', index=False
        )
        pd.DataFrame(result.equity, columns=['time', 'equity']).to_csv(
            out_dir / 'equity.csv', index=False
        )
        logger.info(f"Wrote trades.csv and equity.csv to {out_dir}/")


if __name__ == '__main__':
    main()
