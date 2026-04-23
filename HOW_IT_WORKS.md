# How the Trading Bot Works

## What Does It Do?

The bot automatically buys and sells currencies — like swapping US Dollars for Euros — with the goal of selling them back later at a better price to make a profit.

It does this without a human clicking any buttons. It watches the markets every hour, makes a decision, and if the conditions look right, places a trade. It sends you a message on your phone through Telegram every time something important happens.

---

## What Is Currency Trading (Without the Jargon)?

Imagine you're at an airport. You swap your US Dollars for Euros before a trip. Later, if the Euro gets stronger against the Dollar, you could swap them back and end up with more Dollars than you started with. That price difference is the profit.

The bot does this automatically, all day, every day — but with five currency pairs:

- **Euro vs US Dollar**
- **British Pound vs US Dollar**
- **US Dollar vs Japanese Yen**
- **US Dollar vs Swiss Franc**
- **Australian Dollar vs US Dollar**

---

## The Big Picture

```
┌────────────────────────────────────────────────┐
│              EVERY HOUR, THE BOT:               │
│                                                  │
│  1. Checks if it's safe to trade                │
│  2. Collects price history                      │
│  3. Runs the numbers (math analysis)            │
│  4. Asks an AI: "Should I buy or sell?"         │
│  5. Asks a second AI: "Is this actually safe?"  │
│  6. Places the trade (if both AIs agree)        │
│  7. Manages open trades (protects profits)      │
│  8. Repeats in 1 hour                           │
└────────────────────────────────────────────────┘
```

---

## Step-by-Step: What Happens in One Hour

### Step 1 — Safety Checks

Before doing anything, the bot checks whether it's even allowed to trade right now.

```
┌──────────────────────────────────────────────────────┐
│                   SAFETY CHECKS                       │
│                                                        │
│  Kill Switch ON? ──────────────────────► STOP, no trade│
│  Is it the weekend? ───────────────────► STOP, no trade│
│  Is it a market holiday? ──────────────► STOP, no trade│
│  Lost too much money today? ───────────► STOP, no trade│
│                                                        │
│  All checks pass? ─────────────────────► CONTINUE     │
└──────────────────────────────────────────────────────┘
```

**Why these checks matter:**
- The **Kill Switch** is an emergency off button. You can trigger it by sending `/stop` on Telegram, and the bot freezes immediately.
- **Weekends**: Currency markets are mostly closed over the weekend. Opening a trade on Friday evening and coming back Monday to a big surprise is risky — so the bot does not open new trades from Friday 7 PM to Sunday 10 PM (London time).
- **Holidays**: Same idea — thin market days are skipped.
- **Daily loss limit**: If the account is already down 6% today, the bot stops opening new trades. Existing ones stay protected, but no new bets.

---

### Step 2 — Collecting Data

The bot fetches the last 100 hours of price history for each currency pair. Think of it as looking at the last 100 days on a chart.

It also collects:
- The current live prices (what you'd actually pay right now to buy or sell)
- Interest rates set by major central banks around the world
- Recent news headlines about each currency
- Any major announcements expected in the next 24 hours (like government jobs reports or central bank meetings)

---

### Step 3 — Running the Numbers

Three separate math tools analyse the price history and produce measurements. Think of them like three different instruments checking the same patient:

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  TOOL 1           │   │  TOOL 2           │   │  TOOL 3           │
│  Pattern Checker  │   │  Trend Checker    │   │  Speed Checker   │
│                   │   │                   │   │                  │
│  Is the price     │   │  Is the price     │   │  How fast is     │
│  unusually high   │   │  moving in a      │   │  the price       │
│  or low right now?│   │  clear direction? │   │  changing?       │
└──────────────────┘   └──────────────────┘   └──────────────────┘
         │                       │                       │
         └───────────────────────┴───────────────────────┘
                                 │
                         All results passed
                         to the AI for analysis
```

No decisions are made here — just raw measurements. The AI gets all these numbers in the next step.

---

### Step 4 — AI Analyst: "What Should We Do?"

All the data collected so far gets sent to an AI (a large language model — the same kind of technology behind ChatGPT). The AI is given the role of a market analyst.

The AI is asked a simple question:

> "Here is the price history, the current price trend, speed indicators, interest rates, recent news, and upcoming announcements for this currency pair. Should I **buy**, **sell**, or **do nothing**?"

The AI responds with:
- A decision: **BUY**, **SELL**, or **DO NOTHING**
- A confidence score: a number between 0 and 1 (e.g. 0.72 means 72% confident)
- A brief reason why

```
┌─────────────────────────────────────────────────┐
│             AI ANALYST RESPONSE                  │
│                                                   │
│  Decision:    SELL                               │
│  Confidence:  0.72  (72%)                        │
│  Reason:      "Price is at a high level while    │
│                upcoming news favours a drop.      │
│                Carry trade also points to sell."  │
└─────────────────────────────────────────────────┘
```

**The confidence gate:**

If the AI is less than 60% confident, or says "do nothing" — the trade idea is dropped completely. No trade is placed. The bot moves on to the next currency pair.

```
Confidence ≥ 60% AND signal is BUY or SELL  →  Proceed to Step 5
Confidence < 60% OR signal is DO NOTHING     →  Skip. No trade.
```

---

### Step 5 — AI Reviewer: "Is It Actually Safe to Trade Right Now?"

A second AI — playing the role of a cautious senior trader — reviews the first AI's decision before anything is committed.

This reviewer has one job: challenge the decision. It asks things like:
- Does this actually make sense given the data?
- Is there a major announcement coming up in the next 30 minutes that could cause wild price swings?
- Is the confidence level reasonable, or is it overconfident?

The reviewer gives one of three verdicts:

```
┌──────────────────────────────────────────────┐
│            REVIEWER VERDICTS                  │
│                                               │
│  APPROVED   →  Trade as planned               │
│  ADJUSTED   →  Trade, but with less size      │
│              (reviewer is less confident)     │
│  REJECTED   →  Do not trade                   │
└──────────────────────────────────────────────┘
```

**Important rule:** The reviewer will **never** approve a new trade if a major market announcement is less than 30 minutes away. Those events can cause prices to jump violently in seconds — no new bets during that window.

---

### Step 6 — Placing the Trade

If both AIs agree, the bot now figures out exactly how much money to put on the trade.

**The golden rule: never risk more than 2% of the account on a single trade.**

Here is how it works:

```
Example with a $10,000 account:

  2% of $10,000 = $200 maximum acceptable loss

  The bot sets a safety exit point 50 price units away from the entry.
  It calculates exactly how many units to buy so that:

    If the price moves 50 units against you → you lose exactly $200.
    Nothing more.

  That is the position size.
```

The bot also sets two automatic exits at the broker:

```
┌─────────────────────────────────────────────────────┐
│                  AUTOMATIC EXITS                     │
│                                                       │
│  Safety Exit (Stop Loss):                            │
│    If price moves against you by 50 units →          │
│    trade closes automatically. Loss capped at $200.  │
│                                                       │
│  Profit Exit (Take Profit):                          │
│    If price moves in your favour by 75 units →       │
│    trade closes automatically. Profit: ~$300.        │
│                                                       │
│  Risk:Reward = 1:1.5 (risk $200 to potentially       │
│  make $300)                                          │
└─────────────────────────────────────────────────────┘
```

These exits live at the broker — even if the bot crashes or loses internet, the exits still work.

The bot then sends you a Telegram message:

```
NEW TRADE OPENED
────────────────
Pair:   Euro vs US Dollar
Action: SELL
Price:  1.0948
Safety exit at: 1.1000  (50 units away)
Profit exit at: 1.0925  (75 units away)
Size:   0.4 lots
Max loss on this trade: $200
```

---

### Step 7 — Managing Open Trades

While a trade is open, the bot keeps watching it every hour.

**Trailing protection (locking in profits):**

Once a trade is 20 price units in profit, the bot automatically moves the safety exit to follow the price — always staying 15 units behind. This way, if the price suddenly reverses, you still walk away with a profit instead of nothing.

```
Price moves in your favour:

  Trade opened at ──────────────────► 1.0948  (entry)
  Safety exit starts at ────────────► 1.1000  (50 units away)

  Price drops to 1.0928 (20 units profit):
  Safety exit moves to ─────────────► 1.0943  (15 units behind peak)

  Price keeps dropping to 1.0910 (38 units profit):
  Safety exit moves to ─────────────► 1.0925  (15 units behind peak)

  Price reverses and hits 1.0925:
  Trade closes automatically. Profit locked in.
```

**Warning alerts the bot sends you:**
- Trade has been open for more than 24 hours
- Price is getting very close to the safety exit or profit exit
- Unrealised loss on a trade is getting unusually large
- Total risk across all open trades is getting too high

---

## News: The Danger Zone

Major announcements — like a government releasing monthly job numbers, or a central bank changing interest rates — can cause currency prices to swing wildly in seconds. These are the moments most traders lose money.

The bot has three rules for dealing with this:

```
┌──────────────────────────────────────────────────────────┐
│                   NEWS SAFETY RULES                       │
│                                                            │
│  Rule 1: If a major announcement is 30 minutes away →    │
│           Stop opening new trades on affected currencies  │
│                                                            │
│  Rule 2: 30 minutes after the announcement passes →      │
│           Resume normal trading                           │
│                                                            │
│  Rule 3: 20 minutes BEFORE a very high-impact            │
│           announcement → Check all open trades.          │
│           If AI thinks the risk is high enough,           │
│           close the trade early to protect capital.       │
└──────────────────────────────────────────────────────────┘
```

**Example:**
> Government jobs report is due at 1:30 PM.
> At 1:00 PM → bot suspends new trades on all US Dollar pairs.
> At 1:45 PM → bot resumes normal trading.
> At 1:15 PM (15 min before) → bot checks any open US Dollar trades. If the AI thinks the announcement could hurt them, it closes them first.

---

## Emergency Shutdown Systems

Beyond the normal rules, the bot has extreme safety nets:

```
┌───────────────────────────────────────────────────────────┐
│                  EMERGENCY TRIGGERS                        │
│                                                             │
│  Any single trade loses more than 15% of account →        │
│    Red alert. Flagged for your attention immediately.      │
│                                                             │
│  Total money at risk across ALL trades is too high →      │
│    Close all trades immediately.                           │
│                                                             │
│  Account is down 20% from its starting value →            │
│    Full shutdown. Kill switch activated.                    │
│    Bot stops everything and waits for you.                 │
│                                                             │
│  Account is down 5% today →                               │
│    No new trades for the rest of the day.                  │
│    Existing trades stay open with their safety exits.      │
└───────────────────────────────────────────────────────────┘
```

---

## Telegram: Your Control Panel

You can message the bot directly on Telegram to check on things or take control:

| What you type | What it does |
|---|---|
| `/stop` | Immediately halts all trading. Emergency off switch. |
| `/resume` | Turns trading back on after a `/stop`. |
| `/status` | Shows your balance, total profit/loss, and any open trades. |
| `/calendar` | Shows upcoming major announcements in the next 24 hours. |
| `/analyst` | Shows what the AI analyst decided for each currency pair. |
| `/reviewer` | Shows what the reviewing AI decided. |
| `/logs` | Shows today's activity log. |

---

## What Powers the Bot

The bot connects to several external services:

```
┌──────────────────────────────────────────────────────────┐
│                   EXTERNAL SERVICES                       │
│                                                            │
│  OANDA (Broker)                                           │
│    Where actual trades are placed.                        │
│    Also provides price history and live prices.           │
│    Default: uses a practice account (no real money).      │
│                                                            │
│  Groq (Primary AI)                                        │
│    Fast AI for the analyst and reviewer roles.            │
│                                                            │
│  Anthropic / Claude (Backup AI)                           │
│    Used if the primary AI is unavailable.                 │
│                                                            │
│  News Calendar API                                        │
│    Provides upcoming economic announcements and           │
│    recent headlines. Checked every hour.                  │
│                                                            │
│  Telegram                                                 │
│    Sends you alerts and receives your commands.           │
└──────────────────────────────────────────────────────────┘
```

---

## Full Decision Flow (One Currency Pair, One Hour)

```
                    ┌─────────────┐
                    │ Cycle Start  │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │Safety Checks│
                    └──────┬──────┘
                           │
             ┌─────────────▼──────────────┐
             │ Kill switch / Weekend /      │──► STOP → No trade
             │ Holiday / Daily loss limit? │
             └─────────────┬──────────────┘
                           │ All clear
                    ┌──────▼──────┐
                    │Collect Data  │
                    │Prices, News, │
                    │Announcements │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Run Math   │
                    │  Analysis   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────────────┐
                    │  AI Analyst         │
                    │  BUY / SELL /       │
                    │  DO NOTHING?        │
                    └──────┬──────────────┘
                           │
           ┌───────────────▼───────────────┐
           │ Confidence ≥ 60% AND          │
           │ signal is BUY or SELL?        │──► NO → Skip. No trade.
           └───────────────┬───────────────┘
                           │ YES
                    ┌──────▼──────────────┐
                    │  AI Reviewer        │
                    │  APPROVED /         │
                    │  ADJUSTED /         │
                    │  REJECTED?          │
                    └──────┬──────────────┘
                           │
           ┌───────────────▼───────────────┐
           │ Verdict = APPROVED            │
           │ or ADJUSTED?                  │──► NO (Rejected) → No trade.
           └───────────────┬───────────────┘
                           │ YES
                    ┌──────▼──────────────┐
                    │ Calculate Size      │
                    │ (2% risk rule)      │
                    └──────┬──────────────┘
                           │
                    ┌──────▼──────────────┐
                    │ Place Trade at      │
                    │ Broker with Safety  │
                    │ Exit + Profit Exit  │
                    └──────┬──────────────┘
                           │
                    ┌──────▼──────────────┐
                    │ Send Telegram       │
                    │ Notification        │
                    └──────┬──────────────┘
                           │
                    ┌──────▼──────────────┐
                    │ Wait 1 Hour,        │
                    │ Manage Open Trades  │
                    └─────────────────────┘
```

---

## A Real Example, From Start to Finish

> Account balance: $10,000. Bot starts at 1:00 PM.

| Time | What Happens |
|---|---|
| 1:00 PM | Cycle starts. Safety checks pass. |
| 1:02 PM | Fetches price history for Euro/Dollar. Current price: 1.0948. |
| 1:03 PM | Math tools note: price looks stretched upward. Momentum is weakening. |
| 1:04 PM | Recent news and interest rates favour the Dollar over the Euro. No major announcements for 4 hours. |
| 1:05 PM | AI Analyst says: **SELL Euro/Dollar. Confidence: 68%.** |
| 1:06 PM | 68% > 60% threshold. Signal is SELL. Proceeds to reviewer. |
| 1:07 PM | AI Reviewer checks: calendar is clear, reasoning is sound. **APPROVED.** |
| 1:08 PM | Bot calculates: 2% of $10k = $200 max loss. Safety exit at 50 units. Size = 40,000 units. |
| 1:09 PM | Trade placed: SELL 40,000 units at 1.0948. Safety exit at 1.1000. Profit exit at 1.0925. |
| 1:10 PM | Telegram message sent: "New trade opened — SELL Euro/Dollar." |
| 2:00 PM | Next cycle. Trade is 13 units in profit. Trailing protection not yet active (needs 20 units). |
| 3:00 PM | Price is now 25 units in profit. Trailing protection activates. Safety exit moves to 1.0933. |
| 3:30 PM | Price reaches 1.0925 — the profit exit. Trade closes automatically. |
| 3:31 PM | Telegram: "Trade closed at target. Profit: +$276. Account: $10,276." |

---

## Summary: Why This Bot Exists

Most people cannot watch currency markets 24 hours a day, do complex maths in real time, check news and announcements, and stay disciplined enough not to panic during losses. The bot does all of that automatically.

Its core design principles are:

1. **Never bet the house** — 2% risk limit per trade, every time
2. **Always have an exit** — Safety exit is placed at the broker the moment a trade opens
3. **Two AIs must agree** — One to find the opportunity, one to sanity-check it
4. **Avoid danger zones** — No new trades around major announcements
5. **Know when to stop** — Multiple automatic shutdown triggers if things go wrong
6. **Keep you informed** — Every significant event triggers a Telegram message

---

*For technical documentation, see the source code and inline comments in the `src/` directory.*
