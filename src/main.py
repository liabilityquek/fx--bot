"""Entry point for the multi-agent FX trading bot.

Usage:
    python src/main.py --test              # Component check, print VoteResult per pair, no trades
    python src/main.py --live              # Full trading loop
    python src/main.py --live --dry-run   # Full loop but skip actual order placement
    python src/main.py --live --interval 300        # Override cycle interval (seconds)
    python src/main.py --live --cycle 1 --dry-run   # Single cycle
"""

import argparse
import sys
import os

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.monitoring.logger import get_logger
from src.monitoring.alerts import AlertManager
from src.broker.oanda import OandaBroker
from src.risk.kill_switch import KillSwitch
from src.risk.weekend_guard import WeekendGuard
from src.voting.engine import VotingEngine
from src.execution.engine import TradingEngine, _df_to_candle_list
from src.news import EventMonitor, EventImpact


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-agent FX trading bot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test", action="store_true", help="Component test mode")
    mode.add_argument("--live", action="store_true", help="Live trading loop")
    parser.add_argument("--dry-run", action="store_true", help="Skip actual order placement")
    parser.add_argument("--interval", type=int, default=None, help="Cycle interval in seconds")
    parser.add_argument("--cycle", type=int, default=None, help="Stop after N cycles")
    return parser.parse_args()


def run_test(broker: OandaBroker, voting_engine: VotingEngine, logger) -> bool:
    """Verify components and print a VoteResult per pair. No trades placed."""
    logger.info("="*60)
    logger.info("TEST MODE — component verification")
    logger.info("="*60)

    # Settings
    if not settings.validate():
        logger.error("Settings validation failed")
        return False
    logger.info("Settings: OK")

    # Broker connection
    if not broker.connect():
        logger.error("Broker connection failed")
        return False
    logger.info("Broker: connected")

    account = broker.get_account_info()
    if not account:
        logger.error("Could not fetch account info")
        return False
    logger.info(f"Account: balance={account.balance:.2f}")

    # Run vote for each pair
    all_ok = True
    for pair in settings.TRADING_PAIRS:
        logger.info(f"\n--- {pair} ---")
        try:
            candles_df = broker.get_historical_candles(
                pair, granularity=settings.TIMEFRAME, count=settings.CANDLE_COUNT
            )
            if candles_df is None or len(candles_df) == 0:
                logger.warning(f"{pair}: no candle data")
                all_ok = False
                continue

            candles = _df_to_candle_list(candles_df)

            price_info = broker.get_current_price(pair)
            if not price_info:
                logger.warning(f"{pair}: no price data")
                all_ok = False
                continue

            price = (price_info['bid'] + price_info['ask']) / 2
            result = voting_engine.run_vote(pair, candles, price)

            print(f"\n{pair} VoteResult:")
            print(f"  Final signal  : {result.final_signal.value}")
            print(f"  Buy score     : {result.buy_score:.4f}")
            print(f"  Sell score    : {result.sell_score:.4f}")
            print(f"  Consensus     : {result.consensus_score:.4f}")
            print(f"  LLM available : {result.llm_available}")
            print("  Votes:")
            for v in result.agent_votes:
                print(f"    {v.agent_name:<16} {v.signal.value:<4} ({v.confidence:.2f}) — {v.reasoning}")

        except Exception as exc:
            logger.error(f"{pair}: test failed: {exc}")
            all_ok = False

    return all_ok


def main():
    args = parse_args()
    logger = get_logger("main")

    logger.info("Multi-agent FX trading bot starting")

    # Validate settings (non-fatal errors are printed but don't block)
    settings.validate()

    # Build components
    broker = OandaBroker(logger)
    alert_manager = AlertManager(logger)
    kill_switch = KillSwitch(logger)
    weekend_guard = WeekendGuard(logger=logger)
    voting_engine = VotingEngine(logger, alert_manager=alert_manager)
    event_monitor = EventMonitor(logger)

    if args.test:
        if not broker.connect():
            logger.error("Cannot connect to broker — aborting test")
            sys.exit(1)
        success = run_test(broker, voting_engine, logger)
        sys.exit(0 if success else 1)

    # Live / dry-run mode
    if not broker.connect():
        logger.error("Cannot connect to broker — aborting")
        sys.exit(1)

    engine = TradingEngine(
        broker=broker,
        voting_engine=voting_engine,
        alert_manager=alert_manager,
        kill_switch=kill_switch,
        weekend_guard=weekend_guard,
        logger=logger,
        dry_run=args.dry_run,
    )

    # Start Telegram command poller
    def _md_escape(text: str) -> str:
        """Escape characters that break Telegram Markdown v1."""
        return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

    def _get_calendar_text() -> str:
        events = event_monitor.get_upcoming_events(
            hours_ahead=24,
            hours_behind=0,
            min_impact=EventImpact.MEDIUM,
        )
        if not events:
            return "📅 *Upcoming Events*\n\nNo upcoming events for the rest of today."
        lines = ["📅 *Upcoming Events*\n"]
        for e in sorted(events, key=lambda x: x.minutes_until):
            time_str = e.time.strftime("%H:%M UTC")
            h = int(e.minutes_until // 60)
            m = int(e.minutes_until % 60)
            countdown = f"{h}h {m}m" if h > 0 else f"{m}m"
            parts = [f"F:{e.forecast}"] if e.forecast not in ("0.0", "0", "") else []
            parts += [f"P:{e.previous}"] if e.previous not in ("0.0", "0", "") else []
            data_str = " | ".join(parts)
            line = f"*{time_str}* (in {countdown})\n{e.currency} — {_md_escape(e.event_name)}\nImpact: {e.impact.value.upper()}"
            if data_str:
                line += f" | {data_str}"
            lines.append(line)
        return "\n\n".join(lines)

    def _get_calhistory_text() -> str:
        events = event_monitor.get_upcoming_events(
            hours_ahead=0,
            hours_behind=24,
            min_impact=EventImpact.MEDIUM,
        )
        if not events:
            return "📅 *Calendar History*\n\nNo medium/high-impact events have occurred yet today."
        lines = ["📅 *Calendar History*\n"]
        for e in sorted(events, key=lambda x: x.minutes_until, reverse=True):
            time_str = e.time.strftime("%H:%M UTC")
            actual_str = f" | A:{e.actual}" if e.actual not in ("0.0", "0", "") else ""
            parts = [f"F:{e.forecast}"] if e.forecast not in ("0.0", "0", "") else []
            parts += [f"P:{e.previous}"] if e.previous not in ("0.0", "0", "") else []
            data_str = " | ".join(parts)
            line = f"{time_str} ✓ DONE\n{e.currency} — {_md_escape(e.event_name)}\nImpact: {e.impact.value.upper()}"
            if data_str:
                line += f" | {data_str}"
            if actual_str:
                line += actual_str
            lines.append(line)
        return "\n\n".join(lines)

    alert_manager.start_command_poller(
        kill_switch=kill_switch,
        get_status_fn=engine.get_status,
        get_calendar_fn=_get_calendar_text,
        get_calhistory_fn=_get_calhistory_text,
        get_credits_fn=voting_engine.get_llm_provider_status,
    )

    try:
        engine.start(
            interval_seconds=args.interval,
            max_cycles=args.cycle,
        )
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — stopping engine")
        engine.stop()


if __name__ == "__main__":
    main()
