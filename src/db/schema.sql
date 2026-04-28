-- FX Trading Bot — closed trade store
-- Run once in Supabase SQL editor to provision the table.

CREATE TABLE IF NOT EXISTS trades (
    -- Identity
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id        TEXT UNIQUE NOT NULL,
    pair            TEXT NOT NULL,
    side            TEXT NOT NULL,              -- BUY / SELL

    -- Position sizing
    units           INTEGER NOT NULL,

    -- Price levels
    entry_price     NUMERIC(10,5) NOT NULL,
    close_price     NUMERIC(10,5),
    stop_loss       NUMERIC(10,5),
    take_profit     NUMERIC(10,5),

    -- Pip distances
    sl_pips         NUMERIC(8,1),              -- distance entry → SL
    tp_pips         NUMERIC(8,1),              -- distance entry → TP
    pips_gained     NUMERIC(8,1),              -- actual pips at close (negative = loss)

    -- P&L
    realized_pnl    NUMERIC(12,2),             -- realized in account currency

    -- Close context
    close_reason    TEXT,                       -- stop_loss | take_profit | user | news | emergency

    -- Entry decision context (for RAG retrieval)
    entry_reason    TEXT,                       -- LLM reasoning string (max 120 chars)
    confidence      NUMERIC(5,4),              -- final adjusted confidence 0.0–1.0
    setup_type      TEXT,                       -- BREAKOUT | PULLBACK | REVERSAL | LIQUIDITY_SWEEP | RANGE | NONE
    reviewer_verdict TEXT,                      -- APPROVED | ADJUSTED | REJECTED | SKIPPED | UNAVAILABLE
    reviewer_reason  TEXT,                      -- reviewer agent explanation

    -- Technical context
    strategy_name   TEXT,
    atr_value       NUMERIC(10,5),
    r_multiple      NUMERIC(6,2),              -- realized pips / sl_pips

    -- Timestamps
    open_time       TIMESTAMPTZ,
    close_time      TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common RAG retrieval patterns
CREATE INDEX IF NOT EXISTS trades_pair_idx          ON trades(pair);
CREATE INDEX IF NOT EXISTS trades_close_reason_idx  ON trades(close_reason);
CREATE INDEX IF NOT EXISTS trades_setup_type_idx    ON trades(setup_type);
CREATE INDEX IF NOT EXISTS trades_close_time_idx    ON trades(close_time DESC);
