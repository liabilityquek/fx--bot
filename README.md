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
│   ├── LLMAgent / Analyst (Groq llama-3.3-70b-versatile, NVIDIA nvidia_nim/z-ai/glm4.7 as fallback, Anthropic Claude Haiku as final fallback)
│   │   └── Receives H1 indicators + D1/H4 bias + macro context + USD sentiment
│   └── ReviewerAgent (Groq llama-3.1-8b-instant, NVIDIA nvidia_nim/z-ai/glm4.7 as fallback, Anthropic Claude Haiku as final fallback)
├── TradeManager (per-cycle)
│   ├── Break-even stop (moves SL to entry after N pips profit)
│   ├── Partial take-profit (closes 50% at 1:1 RR)
│   └── Trailing stop (follows price after activation threshold)
├── OrderExecutor (OANDA API, retry + slippage tracking)
└── AlertsManager (Telegram alerts out, commands in)
```

**Stack:** Python 3.11, Docker, OANDA API, Groq API (Llama 3.3 70B), NVIDIA API (NIM), Anthropic API (Claude Haiku, backup), Telegram Bot API, JB News API, FRED API (St. Louis Fed), exchange_calendars

---

## Decision Logic

Every cycle per pair:

1. Technical agents compute indicators — RSI, MACD, EMA20/50, ADX, ATR, Bollinger, Fisher Transform, **market structure** (HH/HL/LH/LL, swing S/R levels)
2. D1 and H4 candles fetched for higher-timeframe bias (EMA20/50 + ADX per timeframe)
3. USD sentiment score computed from close-price changes across all 5 pairs
4. MacroContext assembles: rate differentials (FRED auto-fetch), USD sentiment, news headlines, upcoming events
5. USD correlation guard — if `MAX_USD_CORRELATED_TRADES` (default 2) already open in same USD direction → skip pair
6. **LLMAgent (Analyst)** receives all data and returns BUY / SELL / HOLD + confidence + `setup_type` (BREAKOUT / PULLBACK / REVERSAL / LIQUIDITY_SWEEP / RANGE)
7. If HOLD or confidence < `CONSENSUS_THRESHOLD` (default 0.60) → skip, no trade
8. **ReviewerAgent** checks the analyst's decision for consistency
   - APPROVED → trade executes
   - ADJUSTED → modified confidence, may drop below threshold → no trade
   - REJECTED → trade blocked
9. **Phase 1 trade quality filters** (applied after reviewer APPROVED/ADJUSTED):
   - **Confluence gate** — counts indicator signals aligned with direction (RSI, MACD, EMA trend, ADX, Fisher, Bollinger, Market Structure). Rejects if `confluence_count < MIN_CONFLUENCES` (default 3). Deterministic — reads from indicators dict, not LLM text
   - **Setup type quality filter** — RANGE and NONE are rejected outright. Lower-quality setups (LIQUIDITY_SWEEP, REVERSAL) require higher minimum confidence
   - **Minimum RR validation** — rejects if `tp_pips / sl_pips < MIN_RR_RATIO` (default 2.0)
   - **M15 momentum gate** — rejects if the last 5 × 15-minute candles show momentum clearly contradicting the signal direction
10. If either AI provider is unavailable → HOLD, Telegram alert fired. Provider hierarchy: Groq (primary) → NVIDIA (fallback) → Anthropic (final fallback) → HOLD

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
| **Break-even stop** | At `BREAK_EVEN_ACTIVATION_PIPS` (default 5) profit → SL moves to entry + `BREAK_EVEN_BUFFER_PIPS` (default 1). Triggered once per trade |
| **Partial take-profit** | At 1:1 RR (`PARTIAL_TP_RR_TARGET=1.0`) → closes `PARTIAL_TP_RATIO` (default 50%) of position. Remainder rides to full TP. Disable with `PARTIAL_TP_ENABLED=false` |
| **Trailing stop** | Activates after `TRAILING_STOP_ACTIVATION_PIPS` (default 7) pips profit; trails ATR × 1.5 in price behind the peak (ATR stored at trade entry). Falls back to `TRAILING_STOP_DISTANCE_PIPS` × pip size if ATR unavailable. State persisted to `data/managed_trades.json` — survives restarts |
| **Trade age alert** | Fires after 72 market hours open (weekends Fri 22:00–Sun 22:00 UTC excluded) |
| **Exposure breach** | Total exposure >150% of `MAX_TOTAL_EXPOSURE` → emergency close all |
| **Max drawdown** | Account down ≥20% from starting balance → emergency shutdown |
| **Daily loss limit** | Day's loss ≥6% of balance → halt new trades for the day |
| **Margin call** | Account balance ≤ 0 |
| **Large unrealized loss** | Floating loss >15% of account → critical alert, positions flagged |
| **Manual** | Telegram `/stop`, kill switch file, or `emergency_close_all()` |

**Sequence per open trade each cycle:** break-even check → partial TP check → trailing stop check.

Note: the weekend guard, holiday guard, and kill switch block **new** trades only. Existing open positions remain active and protected by broker-side SL/TP.

---

## Supabase Integration

The bot uses Supabase for persistent trade logging and future RAG retrieval.

### Table Schema

**trades table:**
- trade_id (text, primary key)
- pair (text)
- side (text) - BUY or SELL
- units (numeric)
- entry_price (numeric)
- stop_loss (numeric)
- take_profit (numeric)
- close_price (numeric, optional)
- sl_pips (numeric, optional)
- tp_pips (numeric, optional)
- pips_gained (numeric, optional)
- realized_pnl (numeric, optional)
- close_reason (text, optional)
- entry_reason (text, optional)
- confidence (numeric, optional)
- setup_type (text, optional)
- reviewer_verdict (text, optional)
- reviewer_reason (text, optional)
- strategy_name (text, optional)
- atr_value (numeric, optional)
- r_multiple (numeric, optional)
- open_time (timestamp, optional)
- close_time (timestamp, optional)

### CRUD Operations

**Insert trade:**
```python
from src.monitoring.supabase_logger import create_supabase_logger
logger = create_supabase_logger()
if logger:
    trade_data = {
        'trade_id': 'trade_123',
        'pair': 'EUR/USD',
        'direction': 'BUY',
        'units': 1000,
        'entry_price': 1.0850,
        'stop_loss': 1.0800,
        'take_profit': 1.0950,
        'entry_time': datetime.now().isoformat(),
        'confidence': 0.75,
        'setup_type': 'BREAKOUT'
    }
    logger.insert_trade(trade_data)
```

**Update trade:**
```python
logger.update_trade('trade_123', {
    'close_price': 1.0900,
    'pnl': 50.0,
    'pnl_pips': 50.0,
    'close_reason': 'TP'
})
```

**Query trades:**
```python
# Get single trade
trade = logger.get_trade('trade_123')

# Get recent trades
recent = logger.get_recent_trades(limit=10)
```

### Troubleshooting

Run diagnostic script:
```bash
python diagnose_supabase.py
```

Check connection and table access:
```bash
python check_url.py
python src/db/check_tables.py
```

### Environment Variables

Required:
- `SUPABASE_URL` - Project URL (e.g., https://<project-id>.supabase.co)
- `SUPABASE_KEY` - Service role key for server-side operations

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
| `NVIDIA_API_KEY` | NVIDIA API key (fallback LLM — analyst + reviewer) |
| `ANTHROPIC_API_KEY` | Anthropic API key (final fallback LLM — Claude Haiku) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `ALERT_ENABLED` | `true` to enable Telegram alerts |
| `JB_NEWS_API_KEY` | JB News API key for economic calendar and news headlines |
| `SUPABASE_URL` | Supabase project URL (e.g., https://<project-id>.supabase.co) |
| `SUPABASE_KEY` | Supabase service role key for server-side operations |

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
| `MIN_RR_RATIO` | `2.0` | Minimum risk:reward ratio (tp_pips / sl_pips) to proceed |
| `MAX_RISK_PER_TRADE` | `0.02` | Max risk per trade as fraction of balance (2%) |
| `DEFAULT_TAKE_PROFIT_RATIO` | `2.0` | TP = SL distance × this ratio (1:2 RR) |
| `MAX_DAILY_LOSS_PERCENT` | `0.06` | Daily loss limit before trading halts (6%) |
| `TRAILING_STOP_ACTIVATION_PIPS` | `7.0` | Pips in profit before trailing stop activates |
| `TRAILING_STOP_DISTANCE_PIPS` | `3.0` | Pips the stop trails behind the peak price |
| `BREAK_EVEN_ACTIVATION_PIPS` | `5.0` | Pips in profit before SL moves to break-even |
| `BREAK_EVEN_BUFFER_PIPS` | `1.0` | Buffer above entry when setting break-even SL |
| `PARTIAL_TP_ENABLED` | `true` | Enable partial close at 1:1 RR |
| `PARTIAL_TP_RATIO` | `0.5` | Fraction of position to close at partial TP (50%) |
| `PARTIAL_TP_RR_TARGET` | `1.0` | RR multiple at which partial TP fires (1:1) |
| `MAX_USD_CORRELATED_TRADES` | `2` | Max open trades in the same USD-directional bucket |
| `PAPER_TRADING_MODE` | `true` | Set to `false` when going live |
| `EXECUTION_INTERVAL_SECONDS` | `3600` | Cycle interval in seconds (1 hour) |
| `CANDLE_COUNT` | `100` | H1 candles fetched per cycle per pair |
| `ANTHROPIC_LLM_MODEL` | `claude-haiku-4-5-20251001` | Anthropic fallback model |
| `NVIDIA_LLM_MODEL` | `nvidia_nim/z-ai/glm4.7` | NVIDIA fallback model |
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
- Holiday guard blocks new trades on major FX market holidays (NYSE calendar proxy)

---

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full step-by-step VPS deployment guide.

---

## Disclaimer

This software is for educational purposes. Forex trading carries significant financial risk. Always validate thoroughly in paper trading mode before any live deployment.
