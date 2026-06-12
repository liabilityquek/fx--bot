# FX Trading Bot

AI-powered FX trading bot for 5 major pairs on the H1 timeframe. Uses a sequential two-agent decision pipeline (Analyst → Reviewer) for trade signals, executes through OANDA's paper trading API, and sends alerts via Telegram.

---

## Architecture

```
TradingEngine (H1 loop)
├── Kill switch check (file / env / Telegram /stop)
├── Weekend guard (blocks Sat/Sun + pre-close Friday window)
├── Holiday guard (blocks FX market holidays via NYSE calendar)
├── Candle fetch — H1 (primary) + D1 + H4 (multi-timeframe bias)
├── USD correlation guard (blocks overexposure to same USD direction)
├── DecisionEngine
│   ├── Technical agents (TechAgent, TrendAgent, MomentumAgent — indicators only)
│   │   └── Market structure detection (HH/HL/LH/LL, swing S/R levels)
│   ├── MacroContext
│   │   ├── Rate differentials (FRED API auto-fetch, 24h cache — falls back to .env)
│   │   ├── USD sentiment score (computed from all 5 pair price changes)
│   │   ├── JB News headlines
│   │   └── Upcoming high-impact events (EventMonitor)
│   ├── LLMAgent / Analyst (Groq llama-3.3-70b-versatile, Anthropic Claude Haiku as fallback)
│   │   └── Receives H1 indicators + D1/H4 bias + macro context + USD sentiment
│   └── ReviewerAgent (Groq llama-3.1-8b-instant, Anthropic Claude Haiku as fallback)
├── TradeManager (per-cycle)
│   ├── Break-even stop (moves SL to entry after N pips profit)
│   ├── Partial take-profit (closes 50% at 1:1 RR)
│   └── Trailing stop (follows price after activation threshold)
├── OrderExecutor (OANDA API, retry + slippage tracking)
└── AlertsManager (Telegram alerts out, commands in)
```

**Stack:** Python 3.11, Docker, OANDA API, Groq API (Llama 3.3 70B), Anthropic API (Claude Haiku, backup), Telegram Bot API, JB News API, FRED API (St. Louis Fed), exchange_calendars

---

## Decision Modes

The bot runs in one of two modes (`STRATEGY_MODE` env var):

- **`llm`** (default) — the LLM analyst + reviewer pipeline described below.
- **`strategy`** — deterministic **SuperTrend(10, 3.0) + EMA200** trend-following.
  No LLM calls at all (zero API cost). BUY when SuperTrend flips bullish within
  the last `STRATEGY_SIGNAL_VALIDITY_BARS` closed H1 bars AND close > EMA200;
  SELL on the mirror condition. An opposite flip closes the open position
  (`STRATEGY_EXIT_ON_FLIP`). All deterministic quality gates (ADX, H4 alignment,
  confluences, session/spread/cooldown) and the full trade-management suite stay
  active. Validate with the backtest harness before going live:

  ```bash
  python scripts/backtest.py --pairs EUR_USD,GBP_USD,USD_JPY,USD_CHF,AUD_USD --years 5
  ```

  The backtest downloads real OANDA H1 candles (paginated, cached under
  `data/backtest/`), replays the strategy with the same SL/TP/management
  formulas as live, and reports win rate, profit factor, avg R, and max
  drawdown per pair plus a last-12-months breakdown. Known live/backtest
  deltas: M15 momentum gate and news suspensions are not simulated; candles
  are mid-price with spread modeled as half `typical_spread` per side (stress
  with `--spread-mult`); H4 bars are UTC-anchored resamples.

## Decision Logic

Every cycle per pair:

1. Technical agents compute indicators — RSI, MACD, EMA20/50, ADX, ATR, Bollinger, Fisher Transform, **market structure** (HH/HL/LH/LL, swing S/R levels)
2. D1 and H4 candles fetched for higher-timeframe bias (EMA20/50 + ADX per timeframe)
3. USD sentiment score computed from close-price changes across all 5 pairs
4. MacroContext assembles: rate differentials (FRED auto-fetch), USD sentiment, news headlines, upcoming events
5. USD correlation guard — if `MAX_USD_CORRELATED_TRADES` (default 2) already open in same USD direction → skip pair
6. **LLMAgent (Analyst)** receives all data and returns BUY / SELL / HOLD + confidence + `setup_type` (BREAKOUT / PULLBACK / REVERSAL / RANGE)
7. If HOLD or confidence < `CONSENSUS_THRESHOLD` (default 0.60) → skip, no trade
8. **ReviewerAgent** checks the analyst's decision for consistency
   - APPROVED → trade executes
   - ADJUSTED → modified confidence, may drop below threshold → no trade
   - REJECTED → trade blocked
9. **Phase 1 trade quality filters** (applied after reviewer APPROVED/ADJUSTED):
   - **Confluence gate** — counts indicator signals aligned with direction (RSI, MACD, EMA trend, ADX, Fisher, Bollinger, Market Structure). Rejects if `confluence_count < MIN_CONFLUENCES` (default 3). Deterministic — reads from indicators dict, not LLM text
   - **Setup type quality filter** — RANGE and NONE are rejected outright. Lower-quality setups (REVERSAL) require higher minimum confidence
   - **Minimum RR validation** — rejects if `tp_pips / sl_pips < MIN_RR_RATIO` (default 2.0)
   - **M15 momentum gate** — rejects if the last 5 × 15-minute candles show momentum clearly contradicting the signal direction
   - **H4 trend alignment gate** — rejects if the trade direction contradicts the H4 EMA20/50 trend (`HTF_ALIGNMENT_ENABLED`, default on)
10. **Phase 2 entry quality gates** (applied before the LLM is even called — saves API cost):
   - **Session filter** — new entries only between `SESSION_START_UTC_HOUR` and `SESSION_END_UTC_HOUR` (default 06:00–20:00 UTC); avoids the rollover spread spike and thin Asian-session liquidity
   - **Spread gate** — skips the pair when the live spread exceeds `MAX_SPREAD_PIPS` (default 3.0)
   - **Post-loss cooldown** — after a losing close, the pair is blocked for `LOSS_COOLDOWN_HOURS` (default 4) to prevent immediate re-entry into a failed setup
   - **Consecutive-loss throttle** — after `CONSECUTIVE_LOSS_RISK_REDUCTION_AFTER` (default 3) straight losses, risk per trade is halved; after `MAX_CONSECUTIVE_LOSSES` (default 5), new trades halt for the rest of the day (streak resets on a win or at the daily rollover)
11. If either AI provider is unavailable → HOLD, Telegram alert fired. Provider hierarchy: Groq (primary) → Anthropic (fallback) → HOLD

---

## Project Structure

```
fx-trading-bot/
├── config/
│   ├── settings.py        # Environment variable management
│   └── pairs.py           # Pair definitions and pip values
├── src/
│   ├── main.py            # Entry point
│   ├── agents/            # Technical indicator agents + LLM analyst + reviewer
│   ├── broker/            # OANDA API integration
│   ├── execution/         # Trade execution engine
│   ├── monitoring/        # Logging and Telegram alerts
│   ├── news/              # Economic calendar (JB News API)
│   ├── risk/              # Kill switch, weekend guard, holiday guard, position sizing
│   ├── utils/             # Helpers
│   └── voting/            # DecisionEngine (analyst → reviewer pipeline)
├── tests/                 # Unit tests
├── data/
│   ├── cache/             # Market data cache (gitignored)
│   └── managed_trades.json  # Trailing stop state — persisted across restarts
├── logs/                  # Trade logs (gitignored)
├── Dockerfile
├── requirements.txt
└── .env.template
```

---

## Trade Closing Logic

Trades are closed under the following conditions:

| Trigger | Detail |
|---------|--------|
| **Stop Loss** | Broker-side order. SL distance = adaptive ATR multiplier × ATR. Multiplier is 1.5× (quiet market), 2.0× (normal), or 3.0× (high volatility) — chosen by comparing current ATR against the 50-bar ATR average. Fallback to `DEFAULT_STOP_LOSS_PIPS` if ATR unavailable |
| **Take Profit** | Broker-side order. TP = `SL distance × DEFAULT_TAKE_PROFIT_RATIO` (default 2.0 = 1:2 RR) |
| **Break-even stop** | SL moves to entry + `BREAK_EVEN_BUFFER_PIPS` (default 1) once profit reaches `BREAK_EVEN_TRIGGER_R` (default 0.5) × the initial SL distance — i.e. at +0.5R, scaled to the trade's actual stop, not a fixed pip count. Falls back to `BREAK_EVEN_ACTIVATION_PIPS` (default 5) when the initial SL is unknown. Triggered once per trade |
| **Partial take-profit** | Closes `PARTIAL_TP_RATIO` (default 50%) of the position at `PARTIAL_TP_RR_TARGET` (default 1.0 = 1R) profit, then moves SL to break-even. Disable with `PARTIAL_TP_ENABLED=false` |
| **Trailing stop** | Activates after profit reaches `TRAILING_STOP_ACTIVATION_R` (default 1.0) × the initial SL distance (falls back to `TRAILING_STOP_ACTIVATION_PIPS` when initial SL unknown); trails ATR × `TRAILING_ATR_MULTIPLIER` (default 1.5) in price behind the peak (ATR stored at trade entry). Falls back to `TRAILING_STOP_DISTANCE_PIPS` × pip size if ATR unavailable. State persisted to `data/managed_trades.json` — survives restarts |
| **Time stop** | A trade still losing after `TIME_STOP_HOURS` (default 48) market hours is closed — the entry thesis has expired. Winners are left to run. Disable with `TIME_STOP_ENABLED=false` |
| **Trade age alert** | Fires after 72 market hours open (weekends Fri 22:00–Sun 22:00 UTC excluded) |
| **Exposure breach** | Total exposure >150% of `MAX_TOTAL_EXPOSURE` → emergency close all |
| **Max drawdown** | Account down ≥20% from starting balance → emergency shutdown |
| **Daily loss limit** | Day's loss ≥6% of balance → halt new trades for the day |
| **Margin call** | Account balance ≤ 0 |
| **Large unrealized loss** | Floating loss >15% of account → critical alert, positions flagged |
| **Manual** | Telegram `/stop`, kill switch file, or `emergency_close_all()` |

**Sequence per open trade each cycle:** time-stop check (closes stale losers) → break-even check (moves SL only) → partial TP check (closes 50% at 1R + moves SL to break-even) → trailing stop check.

Note: the weekend guard, holiday guard, and kill switch block **new** trades only. Existing open positions remain active and protected by broker-side SL/TP.

---

## Environment Variables

Copy `.env.template` to `.env` and fill in all values before deployment.

**Required:**

| Variable | Description |
|----------|-------------|
| `OANDA_API_KEY` | OANDA personal access token |
| `OANDA_ACCOUNT_ID` | OANDA practice account ID |
| `OANDA_ENVIRONMENT` | `practice` (paper) or `live` |
| `GROQ_API_KEY` | Groq API key (primary LLM — analyst + reviewer) |
| `ANTHROPIC_API_KEY` | Anthropic API key (fallback LLM — Claude Haiku) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `ALERT_ENABLED` | `true` to enable Telegram alerts |
| `JB_NEWS_API_KEY` | JB News API key for economic calendar and news headlines |

**Recommended:**

| Variable | Description |
|----------|-------------|
| `FRED_API_KEY` | Free key from fred.stlouisfed.org — enables auto-fetch of central bank rates (24h cache). Without this, rates are read from `CB_RATE_*` env vars below |

**Central bank rates (only needed without FRED key, or to override FRED):**

| Variable | Default | Description |
|----------|---------|-------------|
| `CB_RATE_USD` | `4.50` | Fed Funds Rate |
| `CB_RATE_EUR` | `2.40` | ECB Deposit Facility Rate |
| `CB_RATE_GBP` | `4.50` | BOE Bank Rate |
| `CB_RATE_JPY` | `0.50` | BOJ Policy Rate |
| `CB_RATE_CHF` | `0.25` | SNB Policy Rate |
| `CB_RATE_AUD` | `4.10` | RBA Cash Rate |

**Optional overrides (defaults shown):**

| Variable | Default | Description |
|----------|---------|-------------|
| `CONSENSUS_THRESHOLD` | `0.60` | Minimum analyst confidence to proceed to reviewer |
| `MIN_CONFLUENCES` | `3` | Minimum indicator confluences required to place a trade |
| `MIN_RR_RATIO` | `2.0` | Minimum risk:reward ratio (tp_pips / sl_pips) to proceed — must not exceed `DEFAULT_TAKE_PROFIT_RATIO` or every trade is rejected |
| `MAX_RISK_PER_TRADE` | `0.02` | Max risk per trade as fraction of balance (2%) |
| `DEFAULT_TAKE_PROFIT_RATIO` | `2.0` | TP = SL distance × this ratio (1:2 RR) |
| `MAX_DAILY_LOSS_PERCENT` | `0.06` | Daily loss limit before trading halts (6%) |
| `MAX_SPREAD_PIPS` | `3.0` | Max live spread (pips) to allow a new entry |
| `SESSION_FILTER_ENABLED` | `true` | Restrict new entries to the UTC session window |
| `SESSION_START_UTC_HOUR` | `6` | Session window start (UTC hour, inclusive) |
| `SESSION_END_UTC_HOUR` | `20` | Session window end (UTC hour, exclusive) |
| `HTF_ALIGNMENT_ENABLED` | `true` | Require trade direction to match H4 EMA20/50 trend |
| `LOSS_COOLDOWN_HOURS` | `4.0` | Hours a pair is blocked after a losing close |
| `CONSECUTIVE_LOSS_RISK_REDUCTION_AFTER` | `3` | Consecutive losses before risk per trade is halved |
| `MAX_CONSECUTIVE_LOSSES` | `5` | Consecutive losses before new trades halt for the day |
| `TRAILING_STOP_ACTIVATION_R` | `1.0` | Profit (× initial SL distance) before trailing activates |
| `TRAILING_ATR_MULTIPLIER` | `1.5` | Trailing distance = ATR × this multiplier |
| `TRAILING_STOP_ACTIVATION_PIPS` | `15.0` | Fallback activation (pips) when initial SL unknown |
| `TRAILING_STOP_DISTANCE_PIPS` | `8.0` | Fallback trail distance (pips) when ATR unavailable |
| `BREAK_EVEN_TRIGGER_R` | `0.5` | Profit (× initial SL distance) before SL moves to break-even |
| `BREAK_EVEN_ACTIVATION_PIPS` | `5.0` | Fallback break-even trigger (pips) when initial SL unknown |
| `BREAK_EVEN_BUFFER_PIPS` | `1.0` | Buffer above entry when setting break-even SL |
| `PARTIAL_TP_ENABLED` | `true` | Enable partial close at the RR target |
| `PARTIAL_TP_RATIO` | `0.5` | Fraction of position to close at the partial TP (50%) |
| `PARTIAL_TP_RR_TARGET` | `1.0` | Partial TP fires at this R multiple of the initial SL distance |
| `TIME_STOP_ENABLED` | `true` | Close trades still losing after `TIME_STOP_HOURS` |
| `TIME_STOP_HOURS` | `48.0` | Market hours before a losing trade is time-stopped |
| `STRATEGY_MODE` | `llm` | `llm` (AI pipeline) or `strategy` (deterministic SuperTrend+EMA200, no LLM calls) |
| `STRATEGY_EXIT_ON_FLIP` | `true` | Strategy mode: opposite SuperTrend flip closes the open position |
| `STRATEGY_SUPERTREND_PERIOD` | `10` | SuperTrend ATR period |
| `STRATEGY_SUPERTREND_MULTIPLIER` | `3.0` | SuperTrend ATR band multiplier |
| `STRATEGY_EMA_PERIOD` | `200` | Trend filter EMA period (H1_CANDLE_COUNT must be ≥ this + 10; default bumps to 250 in strategy mode) |
| `STRATEGY_SIGNAL_VALIDITY_BARS` | `3` | A flip stays tradeable for this many closed bars |
| `STRATEGY_BASE_CONFIDENCE` | `0.70` | Base confidence for strategy signals (bonuses for ADX/EMA separation/slope, decay per bar of flip age) |
| `MAX_USD_CORRELATED_TRADES` | `2` | Max open trades in the same USD-directional bucket |
| `PAPER_TRADING_MODE` | `true` | Set to `false` when going live |
| `EXECUTION_INTERVAL_SECONDS` | `3600` | Cycle interval in seconds (1 hour) |
| `CANDLE_COUNT` | `100` | H1 candles fetched per cycle per pair |
| `ANTHROPIC_LLM_MODEL` | `claude-haiku-4-5-20251001` | Anthropic fallback model |
| `REVIEWER_LLM_MODEL` | `llama-3.1-8b-instant` | Groq model for the reviewer agent |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) |

---

## Kill Switch

Three ways to halt trading immediately:

| Method | Action |
|--------|--------|
| File | `touch data/KILL_SWITCH` on the VPS |
| Env var | Set `KILL_SWITCH=true` in `.env` and restart the container |
| Telegram | Send `/stop` to the bot |

To resume: remove the file or send `/resume` via Telegram.

---

## Telegram Commands

| Command | What it does |
|---------|-------------|
| `/stop` | Activate kill switch — halt trading + close positions |
| `/resume` | Deactivate kill switch — resume trading |
| `/status` | Current bot status: balance, NAV, unrealized P/L, open trades |
| `/calendar` | Upcoming economic events today (next 24h) |
| `/calhistory` | Past economic events today (last 24h) |
| `/logs` | Today's bot log entries |
| `/credits` | LLM analyst + reviewer provider status |
| `/analyst` | Last analyst decision per pair (signal, confidence, confluence count + named types, reasoning) |
| `/reviewer` | Last reviewer verdict per pair (APPROVED/ADJUSTED/REJECTED, confluence count + named types, reason) |
| `/help` | Show this command list |

---

## Trading Pairs

EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD — H1 timeframe only.

---

## Safety

- Bot defaults to `OANDA_ENVIRONMENT=practice` (paper trading)
- Never switch to `live` without thorough paper validation
- `.env` is gitignored — never commit it
- Docker container runs as non-root (`botuser`, UID 1000)
- Three-layer kill switch available at all times
- Weekend guard blocks trading from Friday 21:00 UTC to Sunday 22:00 UTC
- Holiday guard blocks new trades on weekday FX market holidays (NYSE calendar proxy — Good Friday, Christmas, etc.). Weekends are handled separately by the weekend guard, not the holiday guard

---

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full step-by-step VPS deployment guide.

---

## Disclaimer

This software is for educational purposes. Forex trading carries significant financial risk. Always validate thoroughly in paper trading mode before any live deployment.
