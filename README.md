# FX Trading Bot

AI-powered FX trading bot for 5 major pairs on the H1 timeframe. Uses a multi-agent voting system for trade signals, executes through OANDA's paper trading API, and sends alerts via Telegram.

---

## Architecture

```
TradingEngine (H1 loop)
├── Kill switch check (file / env / Telegram /stop)
├── Weekend guard
├── News suspension check (JB News API)
├── Candle fetch (OANDA)
├── VotingEngine
│   ├── Technical agents (indicators, RSI, MA, etc.)
│   └── LLMAgent (Groq — llama-3.3-70b-versatile, Anthropic as backup)
├── OrderExecutor (OANDA API, retry + slippage tracking)
└── AlertsManager (Telegram alerts out, /stop /resume in)
```

**Stack:** Python 3.11, Docker, OANDA API, Groq API (Llama 3.3 70B), Anthropic API (Claude Haiku, backup), Telegram Bot API, JB News API

---

## Project Structure

```
fx-trading-bot/
├── config/
│   ├── settings.py        # Environment variable management
│   └── pairs.py           # Pair definitions and pip values
├── src/
│   ├── main.py            # Entry point
│   ├── agents/            # Technical indicator agents
│   ├── broker/            # OANDA API integration
│   ├── dataflows/         # Market data sources
│   ├── execution/         # Trade execution engine
│   ├── monitoring/        # Logging and alerts
│   ├── news/              # News filtering and suspension
│   ├── risk/              # Risk management engine
│   ├── strategist/        # LLM strategy layer
│   ├── utils/             # Helpers
│   └── voting/            # Multi-agent vote tallying
├── data/
│   └── cache/             # Market data cache (gitignored)
├── logs/                  # Trade logs (gitignored)
├── Dockerfile
├── requirements.txt
└── .env.template
```

---

## Voting & Signal Logic

Trades only execute when the weighted consensus score meets or exceeds `CONSENSUS_THRESHOLD` (default 0.60):

- `buy_score >= 0.60` and `buy_score > sell_score` → **BUY**
- `sell_score >= 0.60` and `sell_score > buy_score` → **SELL**
- Otherwise → **HOLD** (no trade placed)

HOLD votes count toward the denominator but not the numerator, diluting the score. If the LLM agent is unavailable, the engine falls back to technical-only weighting so an outage never permanently blocks trades.

---

## Trade Closing Logic

Trades are closed under the following conditions:

| Trigger | Detail |
|---------|--------|
| **Stop Loss** | Broker-side order. Calculated via fixed pips, ATR (2× multiplier), or percentage of entry |
| **Take Profit** | Broker-side order. Set at `SL distance × risk/reward ratio` (default 1.5:1) |
| **Trailing Stop** | Activates after 20 pips profit; trails 15 pips behind peak price |
| **Exposure breach** | Total exposure >150% of `MAX_TOTAL_EXPOSURE` → emergency close all |
| **Max drawdown** | Account down ≥20% from starting balance → emergency shutdown |
| **Daily loss limit** | Day's loss ≥5% of balance → halt trading |
| **Margin call** | Account balance ≤ 0 |
| **Large unrealized loss** | Floating loss >15% of account → critical alert, positions flagged |
| **Manual** | Telegram `/stop`, kill switch file, or `emergency_close_all()` |

---

## Environment Variables

Copy `.env.template` to `.env` and fill in all values before deployment.

| Variable | Description |
|----------|-------------|
| `OANDA_API_KEY` | OANDA personal access token |
| `OANDA_ACCOUNT_ID` | OANDA practice account ID |
| `OANDA_ENVIRONMENT` | `practice` (paper) or `live` |
| `GROQ_API_KEY` | Groq API key (primary LLM) |
| `ANTHROPIC_API_KEY` | Anthropic API key (backup LLM, Claude Haiku) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `JB_NEWS_API_KEY` | JB News API key for high-impact event filtering |
| `CONSENSUS_THRESHOLD` | Minimum vote score to execute a trade (default `0.60`) |
| `MAX_DAILY_LOSS_PERCENT` | Max daily drawdown before halt (e.g. `2.0`) |

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

## Trading Pairs

EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD — H1 timeframe only.

---

## Safety

- Bot defaults to `OANDA_ENVIRONMENT=practice` (paper trading)
- Never switch to `live` without thorough paper validation
- `.env` is gitignored — never commit it
- Docker container runs as non-root (`botuser`, UID 1000)
- Three-layer kill switch available at all times

---

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full step-by-step VPS deployment guide.

---

## Disclaimer

This software is for educational purposes. Forex trading carries significant financial risk. Always validate thoroughly in paper trading mode before any live deployment.
