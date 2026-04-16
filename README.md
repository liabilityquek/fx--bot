# FX Trading Bot

AI-powered FX trading bot for 5 major pairs on the H1 timeframe. Uses a multi-agent LLM voting system for trade signals, executes through OANDA's paper trading API, and sends alerts via Telegram.

---

## Architecture

```
TradingEngine (H1 loop)
├── Kill switch check (file / env / Telegram /stop)
├── Weekend guard
├── Candle fetch (OANDA)
├── VotingEngine
│   ├── Technical agents (indicators, RSI, MA, etc.)
│   └── LLMAgent (Claude Haiku — synthesises votes into signal)
├── OrderExecutor (OANDA API, retry + slippage tracking)
└── AlertsManager (Telegram alerts out, /stop /resume in)
```

**Stack:** Python 3.11, Docker, OANDA API, Anthropic API (Claude Haiku), Telegram Bot API, Alpha Vantage, Finnhub, Firecrawl

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

## Environment Variables

Copy `.env.template` to `.env` and fill in all values before deployment.

| Variable | Description |
|----------|-------------|
| `OANDA_API_KEY` | OANDA personal access token |
| `OANDA_ACCOUNT_ID` | OANDA practice account ID |
| `OANDA_ENVIRONMENT` | `practice` (paper) or `live` |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude Haiku) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
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
