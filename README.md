# FX Trading Bot

AI-powered FX trading bot for 5 major pairs on the H1 timeframe. Uses a sequential two-agent decision pipeline (Analyst → Reviewer) for trade signals, executes through OANDA's paper trading API, and sends alerts via Telegram.

---

## Architecture

```
TradingEngine (H1 loop)
├── Kill switch check (file / env / Telegram /stop)
├── Weekend guard (blocks Sat/Sun + pre-close Friday window)
├── Holiday guard (blocks FX market holidays via NYSE calendar)
├── Candle fetch (OANDA)
├── DecisionEngine
│   ├── Technical agents (TechAgent, TrendAgent, MomentumAgent — indicators only)
│   ├── MacroContext (rate differentials + JB News headlines + upcoming events)
│   ├── LLMAgent / Analyst (Groq llama-3.3-70b-versatile, Anthropic Claude Haiku as backup)
│   └── ReviewerAgent (Groq llama-3.1-8b-instant, Anthropic Claude Haiku as backup)
├── OrderExecutor (OANDA API, retry + slippage tracking)
└── AlertsManager (Telegram alerts out, commands in)
```

**Stack:** Python 3.11, Docker, OANDA API, Groq API (Llama 3.3 70B), Anthropic API (Claude Haiku, backup), Telegram Bot API, JB News API, exchange_calendars

---

## Decision Logic

Every cycle per pair:

1. Technical agents compute indicators (no votes — raw numbers only)
2. MacroContext assembles rate differentials, news headlines, and upcoming high-impact events
3. **LLMAgent (Analyst)** receives all data and returns BUY / SELL / HOLD + confidence score
4. If HOLD or confidence < `CONSENSUS_THRESHOLD` (default 0.60) → skip, no trade
5. **ReviewerAgent** checks the analyst's decision for consistency
   - APPROVED → trade executes
   - ADJUSTED → modified signal executes
   - REJECTED → trade blocked
6. If either AI provider is unavailable → HOLD, Telegram alert fired

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
| **Stop Loss** | Broker-side order. Calculated via fixed pips, ATR (2× multiplier), or percentage of entry |
| **Take Profit** | Broker-side order. Set at `SL distance × risk/reward ratio` (default 1.5:1) |
| **Trailing Stop** | Activates after `TRAILING_STOP_ACTIVATION_PIPS` (default 7) pips profit; trails `TRAILING_STOP_DISTANCE_PIPS` (default 3) pips behind peak. State persisted to `data/managed_trades.json` — survives restarts |
| **Trade age alert** | Fires after 72 market hours open (weekends Fri 22:00–Sun 22:00 UTC excluded) |
| **Exposure breach** | Total exposure >150% of `MAX_TOTAL_EXPOSURE` → emergency close all |
| **Max drawdown** | Account down ≥20% from starting balance → emergency shutdown |
| **Daily loss limit** | Day's loss ≥5% of balance → halt trading |
| **Margin call** | Account balance ≤ 0 |
| **Large unrealized loss** | Floating loss >15% of account → critical alert, positions flagged |
| **Manual** | Telegram `/stop`, kill switch file, or `emergency_close_all()` |

Note: the weekend guard, holiday guard, and kill switch block **new** trades only. Existing open positions remain active and protected by broker-side SL/TP.

---

## Environment Variables

Copy `.env.template` to `.env` and fill in all values before deployment.

| Variable | Description |
|----------|-------------|
| `OANDA_API_KEY` | OANDA personal access token |
| `OANDA_ACCOUNT_ID` | OANDA practice account ID |
| `OANDA_ENVIRONMENT` | `practice` (paper) or `live` |
| `GROQ_API_KEY` | Groq API key (primary LLM — analyst + reviewer) |
| `ANTHROPIC_API_KEY` | Anthropic API key (fallback LLM — Claude Haiku) |
| `ANTHROPIC_LLM_MODEL` | Anthropic model override (default: `claude-haiku-4-5-20251001`) |
| `REVIEWER_LLM_MODEL` | Groq model for reviewer (default: `llama-3.1-8b-instant`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `JB_NEWS_API_KEY` | JB News API key for economic calendar |
| `EVENT_CACHE_TTL_HOURS` | How long to cache calendar data (default: `1`) |
| `CONSENSUS_THRESHOLD` | Minimum analyst confidence to proceed to reviewer (default: `0.60`) |
| `MAX_DAILY_LOSS_PERCENT` | Max daily drawdown before trading halts (e.g. `2.0`) |
| `TRAILING_STOP_ACTIVATION_PIPS` | Pips in profit before trailing stop activates (default: `7.0`) |
| `TRAILING_STOP_DISTANCE_PIPS` | Pips the stop trails behind the peak price (default: `3.0`) |

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
| `/analyst` | Last analyst decision per pair (signal, confidence, reasoning) |
| `/reviewer` | Last reviewer verdict per pair (APPROVED/ADJUSTED/REJECTED + reason) |
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
