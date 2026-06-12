"""Backtest performance metrics and report formatting."""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .simulator import SimTrade


def compute_metrics(
    trades: List[SimTrade],
    equity: List[Tuple[datetime, float]],
    initial_balance: float,
) -> dict:
    """Compute summary statistics for a set of closed trades + equity curve."""
    if not trades:
        return {
            'trades': 0, 'win_rate': 0.0, 'profit_factor': 0.0,
            'avg_r': 0.0, 'median_r': 0.0, 'expectancy_usd': 0.0,
            'total_pnl': 0.0, 'return_pct': 0.0, 'max_drawdown_pct': 0.0,
            'longest_loss_streak': 0, 'avg_bars_held': 0.0,
            'exit_reasons': {},
        }

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd < 0]
    gross_win = sum(t.pnl_usd for t in wins)
    gross_loss = abs(sum(t.pnl_usd for t in losses))
    total_pnl = sum(t.pnl_usd for t in trades)

    r_values = sorted(t.r_multiple for t in trades)
    n = len(r_values)
    median_r = (
        r_values[n // 2] if n % 2 == 1
        else (r_values[n // 2 - 1] + r_values[n // 2]) / 2
    )

    # Longest losing streak (chronological)
    streak = longest = 0
    for t in sorted(trades, key=lambda t: t.exit_time):
        if t.pnl_usd < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    # Max drawdown on the equity curve
    max_dd = 0.0
    peak = initial_balance
    for _, value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    exit_reasons: Dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    return {
        'trades': len(trades),
        'win_rate': len(wins) / len(trades),
        'profit_factor': (gross_win / gross_loss) if gross_loss > 0 else float('inf'),
        'avg_r': sum(r_values) / n,
        'median_r': median_r,
        'expectancy_usd': total_pnl / len(trades),
        'total_pnl': total_pnl,
        'return_pct': total_pnl / initial_balance if initial_balance else 0.0,
        'max_drawdown_pct': max_dd,
        'longest_loss_streak': longest,
        'avg_bars_held': sum(t.bars_held for t in trades) / len(trades),
        'exit_reasons': exit_reasons,
    }


def format_report(
    trades: List[SimTrade],
    equity: List[Tuple[datetime, float]],
    initial_balance: float,
    final_balance: float,
    gate_rejections: Optional[Dict[str, int]] = None,
    signals_seen: int = 0,
    title: str = "BACKTEST REPORT",
) -> str:
    """Human-readable report: combined + per-pair + most recent 12 months."""
    lines = [
        "=" * 72,
        title,
        "=" * 72,
    ]

    if equity:
        lines.append(
            f"Period: {equity[0][0]:%Y-%m-%d} -> {equity[-1][0]:%Y-%m-%d} | "
            f"Balance: ${initial_balance:,.2f} -> ${final_balance:,.2f}"
        )

    lines.append("")
    lines.append(_metrics_block("COMBINED", compute_metrics(trades, equity, initial_balance)))

    # Per-pair breakdown (equity curve is account-level; per-pair DD omitted)
    pairs = sorted({t.pair for t in trades})
    for pair in pairs:
        pair_trades = [t for t in trades if t.pair == pair]
        m = compute_metrics(pair_trades, [], initial_balance)
        lines.append(_metrics_block(pair, m, include_dd=False))

    # Most recent 12 months — regime check
    if equity:
        cutoff = equity[-1][0] - timedelta(days=365)
        recent_trades = [t for t in trades if t.exit_time >= cutoff]
        recent_equity = [(ts, v) for ts, v in equity if ts >= cutoff]
        start_balance = recent_equity[0][1] if recent_equity else initial_balance
        lines.append(_metrics_block(
            "LAST 12 MONTHS", compute_metrics(recent_trades, recent_equity, start_balance)
        ))

    if signals_seen or gate_rejections:
        lines.append("-" * 72)
        lines.append(f"Signals generated: {signals_seen}")
        if gate_rejections:
            rej = ", ".join(f"{k}={v}" for k, v in sorted(gate_rejections.items()))
            lines.append(f"Gate rejections:   {rej}")

    lines.append("=" * 72)
    return "\n".join(lines)


def _metrics_block(label: str, m: dict, include_dd: bool = True) -> str:
    pf = m['profit_factor']
    pf_str = f"{pf:.2f}" if pf != float('inf') else "inf"
    rows = [
        f"--- {label} ---",
        f"  Trades: {m['trades']:>5}   Win rate: {m['win_rate']:>6.1%}   "
        f"Profit factor: {pf_str}",
        f"  Total P/L: ${m['total_pnl']:>10,.2f}   Return: {m['return_pct']:>7.1%}   "
        f"Expectancy: ${m['expectancy_usd']:.2f}/trade",
        f"  Avg R: {m['avg_r']:>5.2f}   Median R: {m['median_r']:>5.2f}   "
        f"Longest loss streak: {m['longest_loss_streak']}   "
        f"Avg bars held: {m['avg_bars_held']:.0f}",
    ]
    if include_dd:
        rows.append(f"  Max drawdown: {m['max_drawdown_pct']:.1%}")
    if m['exit_reasons']:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(m['exit_reasons'].items()))
        rows.append(f"  Exits: {reasons}")
    rows.append("")
    return "\n".join(rows)
