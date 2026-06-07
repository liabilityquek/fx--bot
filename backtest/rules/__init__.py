"""Book-style rule sets. Each rule exposes signals(df) -> DataFrame with columns:
   entry_long, entry_short, exit_long, exit_short, stop_distance (ATR-multiplied).
Rules are pure functions of OHLCV — no broker, no equity, no sizing."""
