"""Donchian-200 / 6×ATR mechanical signal layer (live runtime).

This is the live-runtime mirror of `backtest/rules/donchian.py`. To guarantee
zero drift between the backtest oracle and the forward-test path, we IMPORT
`build_signals` from the backtest module directly rather than re-implementing
it. Any change to the backtest rule automatically propagates to the live path.

The live runner consumes one MechanicalSignal per pair per cycle. It contains
only the values needed for entry / exit decisions; nothing about the broker,
account, or sizing lives here.

Run as a script to diff vs the backtest oracle:
    python -m src.strategy.donchian_mechanical \
        --candles data/historical/EUR_USD_D.csv \
        --rows 30
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# Import the backtest oracle directly so the live and backtest signal
# definitions cannot drift apart.
from backtest.rules.donchian import (
    DONCHIAN_200_ATR6,
    DonchianConfig,
    build_signals,
)
from src.agents.indicators import to_dataframe


@dataclass
class MechanicalSignal:
    """Last-completed-bar signal for one pair."""

    pair: str
    signal_time: datetime          # UTC time of the bar whose CLOSE produced this signal
    close: float
    entry_long: bool
    entry_short: bool
    atr: float                     # ATR14 at the signal bar's close
    n_high: float                  # rolling 200-bar high excluding signal bar
    n_low: float                   # rolling 200-bar low excluding signal bar
    sma_fast: float                # 50-bar SMA at signal bar
    sma_slow: float                # 100-bar SMA at signal bar
    bar_high: float                # signal bar's high (used to seed trailing_extreme on entry)
    bar_low: float                 # signal bar's low

    @property
    def direction(self) -> str:
        if self.entry_long:
            return "LONG"
        if self.entry_short:
            return "SHORT"
        return "NONE"


def _candles_to_df(candles: List[Dict]) -> pd.DataFrame:
    """Broker candle list → OHLCV DataFrame with UTC DatetimeIndex.

    Accepts both flat broker dicts and OANDA raw API dicts. Reuses
    `to_dataframe()` for OHLCV parsing, then attaches a UTC time index parsed
    from the candle 'time' field.
    """
    df = to_dataframe(candles)
    times: List[datetime] = []
    for c in candles:
        t = c.get('time')
        if isinstance(t, datetime):
            ts = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        elif isinstance(t, str):
            ts = pd.to_datetime(t, utc=True).to_pydatetime()
        else:
            ts = datetime.now(timezone.utc)
        times.append(ts)
    df.index = pd.DatetimeIndex(times, tz='UTC', name='time')
    return df


def compute_signal(
    candles: List[Dict],
    cfg: Optional[DonchianConfig] = None,
    pair: str = "UNKNOWN",
) -> Optional[MechanicalSignal]:
    """Compute the Donchian / SMA filter signal for the LAST completed bar.

    Returns None if there is not enough history to evaluate the rule
    (need at least lookback + sma_slow bars + an ATR warmup buffer).
    Caller must have already dropped the in-progress bar.
    """
    cfg = cfg or DONCHIAN_200_ATR6
    if not candles:
        return None

    df = _candles_to_df(candles)
    min_bars = cfg.lookback + cfg.sma_slow + cfg.atr_period + 5
    if len(df) < min_bars:
        return None

    signals = build_signals(df, cfg)
    last = signals.iloc[-1]

    atr_val   = float(last['atr'])      if pd.notna(last['atr'])      else float('nan')
    n_high    = float(last['n_high'])   if pd.notna(last['n_high'])   else float('nan')
    n_low     = float(last['n_low'])    if pd.notna(last['n_low'])    else float('nan')
    sma_fast  = float(last['sma_fast']) if pd.notna(last['sma_fast']) else float('nan')
    sma_slow  = float(last['sma_slow']) if pd.notna(last['sma_slow']) else float('nan')

    return MechanicalSignal(
        pair=pair,
        signal_time=df.index[-1].to_pydatetime(),
        close=float(df['close'].iloc[-1]),
        entry_long=bool(last['entry_long']),
        entry_short=bool(last['entry_short']),
        atr=atr_val,
        n_high=n_high,
        n_low=n_low,
        sma_fast=sma_fast,
        sma_slow=sma_slow,
        bar_high=float(df['high'].iloc[-1]),
        bar_low=float(df['low'].iloc[-1]),
    )


# ---------------------------------------------------------------------------
# CLI parity oracle — diff against backtest/rules/donchian.build_signals
# ---------------------------------------------------------------------------

def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Print last N rows of mechanical signals for parity vs backtest"
    )
    parser.add_argument("--candles", required=True, help="CSV path (data/historical/*_D.csv)")
    parser.add_argument("--rows", type=int, default=30, help="Tail rows to print")
    parser.add_argument("--pair", default=None, help="Pair label for display")
    args = parser.parse_args()

    csv_path = Path(args.candles)
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found", file=sys.stderr)
        return 1

    df = pd.read_csv(csv_path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()[["open", "high", "low", "close", "volume"]]

    signals = build_signals(df, DONCHIAN_200_ATR6)
    out = df[["close"]].join(signals)

    pair = args.pair or csv_path.stem.replace("_D", "")
    print(f"=== {pair} | last {args.rows} rows (live module imports backtest.rules.donchian) ===")
    print(out.tail(args.rows).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
