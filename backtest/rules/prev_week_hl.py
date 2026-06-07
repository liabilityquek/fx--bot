"""Book edge #3: Previous-week High/Low breakout / fade.

Rule:
  - Long signal when today's close > previous completed week's HIGH
  - Short signal when today's close < previous completed week's LOW
  - Always-in-market: position reverses on opposite signal

Pair-personality:
  - Trender: take signal as-is
  - Fader  : take opposite of signal (flip)

Stop: no fixed ATR stop. Implied stop = opposite weekly extreme.
      Position sizing uses that implied distance (1% risk).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PrevWeekHLConfig:
    atr_period: int = 14   # only used for diagnostics

    @property
    def name(self) -> str:
        return "PrevWeekHL"


def _prev_week_high_low(df: pd.DataFrame) -> pd.DataFrame:
    """For each daily bar, attach the previous completed ISO-week's H/L.

    'Previous week' = the calendar week immediately before the bar's week.
    Computed via pandas resample on iso-week boundaries.
    """
    # Group by ISO year-week of the bar
    iso = df.index.isocalendar()
    week_key = (iso["year"].astype(int) * 100 + iso["week"].astype(int)).values
    out = pd.DataFrame(index=df.index)
    out["week_key"] = week_key

    weekly = pd.DataFrame({
        "week_key": week_key,
        "high": df["high"].values,
        "low":  df["low"].values,
    }).groupby("week_key").agg(week_high=("high", "max"), week_low=("low", "min"))

    # Shift by 1 week → previous week's values
    weekly["prev_week_high"] = weekly["week_high"].shift(1)
    weekly["prev_week_low"]  = weekly["week_low"].shift(1)

    out = out.merge(weekly[["prev_week_high", "prev_week_low"]],
                    left_on="week_key", right_index=True, how="left")
    out.index = df.index
    return out[["prev_week_high", "prev_week_low"]]


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def build_signals(df: pd.DataFrame, cfg: PrevWeekHLConfig) -> pd.DataFrame:
    """Return signal frame with entry_long/entry_short + prev_week_high/low + atr."""
    out = _prev_week_high_low(df)
    out["atr"] = _atr(df, cfg.atr_period)
    close = df["close"]

    out["entry_long"]  = (close > out["prev_week_high"]).fillna(False)
    out["entry_short"] = (close < out["prev_week_low"]).fillna(False)

    # Implied stop distance = entry-side gap to opposite weekly extreme
    # (long → entry - prev_week_low; short → prev_week_high - entry)
    # Engine uses this for sizing when atr_mult is None.
    out["implied_stop_long"]  = (close - out["prev_week_low"]).abs()
    out["implied_stop_short"] = (out["prev_week_high"] - close).abs()
    return out


PWHL = PrevWeekHLConfig()
