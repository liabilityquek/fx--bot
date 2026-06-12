# SuperTrend+EMA200 Backtest Experiments — 2026-06-12

All runs: 5 pairs (EUR_USD, GBP_USD, USD_JPY, USD_CHF, AUD_USD),
2021-06-14 → 2026-06-10, $10,000 starting balance, real OANDA mid candles
(cache pushed under `reports/`). Each directory contains `trades.csv` and
`equity.csv` from `scripts/backtest.py --csv-out`.

| Directory | Configuration | Win rate | PF | Avg R | Return | Max DD |
|---|---|---|---|---|---|---|
| `reports_run/` | H1, defaults, pre sizing-fix | 55.8% | 0.82 | −0.07 | −77% | 81% |
| `reports_fixed/` | H1, defaults, USD_JPY sizing fixed | 55.9% | 0.84 | −0.07 | −78% | 82% |
| `reports_nospread/` | H1, `--spread-mult 0` | 58.4% | 0.91 | −0.02 | −50% | 64% |
| `reports_pure/` | H1, no BE/partial/trailing/time-stop | 33.5% | 0.92 | −0.03 | −64% | 77% |
| `reports_slow/` | H1, SuperTrend(20, 4.0) | 59.8% | 0.89 | −0.03 | −41% | 51% |
| `reports_h4/` | **H4**, defaults | 64.2% | **1.31** | **+0.10** | **+21.7%** | **15.4%** |

## Conclusions

1. **H1 strategy mode has no edge.** Negative every calendar year, both
   directions. The zero-spread run shows the raw signal is ~breakeven
   (−0.02R); spread costs (~0.03–0.05R/trade) push it decisively negative.
   The no-management run (pure 1:2 SL/TP) gets 33.5% wins where 33.3% is
   breakeven — the entry is coin-flip; management is not the culprit.
2. **USD_JPY position-sizing bug** found via this backtest (P/L ~flat every
   year): USD notional was computed as units × price for USD-base pairs,
   overstating it ~150× and capping every USD_JPY trade at ~1/150th size.
   Fixed in `PositionSizer._usd_notional()` — affects live trading too.
3. **H4 is the only positive variant** (PF 1.31, +21.7%, DD 15.4%, last 12
   months PF 1.79) — but only 109 trades, profit concentrated in GBP_USD and
   USD_CHF, 3 of 6 years negative, t-stat 1.23 (~22% probability of this
   result from a zero-edge strategy). Suggestive, not proof. If pursued:
   paper-trade with `STRATEGY_MODE=strategy TIMEFRAME=H4` first.

## Reproduce

```bash
# candle cache: copy reports/*.csv into data/backtest/ (or let it download)
python scripts/backtest.py --pairs EUR_USD,GBP_USD,USD_JPY,USD_CHF,AUD_USD \
    --from 2021-06-14 --to 2026-06-10 --csv-out reports_fixed/
python scripts/backtest.py ... --spread-mult 0          # cost-free signal test
PARTIAL_TP_ENABLED=false BREAK_EVEN_TRIGGER_R=999 TRAILING_STOP_ACTIVATION_R=999 \
    TIME_STOP_ENABLED=false python scripts/backtest.py ...   # no management
STRATEGY_SUPERTREND_PERIOD=20 STRATEGY_SUPERTREND_MULTIPLIER=4.0 \
    python scripts/backtest.py ...                      # slow SuperTrend
python scripts/backtest.py ... --granularity H4         # H4 variant
```
