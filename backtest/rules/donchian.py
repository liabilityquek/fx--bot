"""Book edges #1 and #2: N-day Donchian breakout, SMA50/100 trend filter,
M-multiple ATR trailing stop, daily-close confirmation exit (book edge #4)."""

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DonchianConfig:
    lookback: int = 200       # book #1=200d, #2=100d
    sma_fast: int = 50
    sma_slow: int = 100
    atr_period: int = 14
    atr_mult: float = 6.0     # book #1=6.0, #2=4.0
    use_trend_filter: bool = True

    @property
    def name(self) -> str:
        return f"Donchian{self.lookback}_ATR{self.atr_mult:.0f}"


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def build_signals(df: pd.DataFrame, cfg: DonchianConfig) -> pd.DataFrame:
    """Return a DataFrame aligned to df with signal columns.

    Signal semantics (book-style, daily-close confirmation):
      - entry_long  : close[t] = N-day high AND (no trend filter OR SMA50 > SMA100)
      - entry_short : close[t] = N-day low  AND (no trend filter OR SMA50 < SMA100)
      - atr         : ATR value at bar close — used by engine for stop distance
      - sma_fast / sma_slow: for diagnostics

    The engine consumes these and decides actual entry on next bar's open.
    Exit is engine-managed: 6×ATR trailing stop confirmed only on daily close.
    """
    out = pd.DataFrame(index=df.index)
    close = df["close"]

    # Rolling N-day high/low (exclude current bar to require a strict break)
    n_high = close.shift(1).rolling(window=cfg.lookback).max()
    n_low  = close.shift(1).rolling(window=cfg.lookback).min()

    out["sma_fast"] = close.rolling(window=cfg.sma_fast).mean()
    out["sma_slow"] = close.rolling(window=cfg.sma_slow).mean()
    out["atr"] = _atr(df, cfg.atr_period)
    out["n_high"] = n_high
    out["n_low"]  = n_low

    bull_break = close >= n_high
    bear_break = close <= n_low

    if cfg.use_trend_filter:
        bull_filter = out["sma_fast"] > out["sma_slow"]
        bear_filter = out["sma_fast"] < out["sma_slow"]
        out["entry_long"]  = bull_break & bull_filter
        out["entry_short"] = bear_break & bear_filter
    else:
        out["entry_long"]  = bull_break
        out["entry_short"] = bear_break

    # Drop NaN warmup
    out["entry_long"]  = out["entry_long"].fillna(False)
    out["entry_short"] = out["entry_short"].fillna(False)
    return out


# Book parameterisations
DONCHIAN_200_ATR6 = DonchianConfig(lookback=200, atr_mult=6.0)   # book edge #1
DONCHIAN_100_ATR4 = DonchianConfig(lookback=100, atr_mult=4.0)   # book edge #2
