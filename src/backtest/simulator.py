"""Event-driven backtest simulator for the SuperTrend+EMA200 strategy mode.

Parity rules:
- Signals are evaluated by the REAL SuperTrendEmaStrategy.vote() on a window of
  exactly settings.H1_CANDLE_COUNT bars (window length changes EMA/ADX values,
  so this matches what the live engine sees).
- Entry gates are the live functions imported from src.execution.engine
  (_is_adx_trending, _htf_trend_aligned, _count_indicator_confluences,
  _get_min_confidence_for_setup) — no formula duplication.
- SL/TP geometry uses the live _calc_atr/_get_atr_multiplier formulas; trade
  management mirrors TradeManager (BE at 0.5R, 50% partial at 1R then BE,
  ATR-trailing after 1R, market-hours time stop), with SL moves taking effect
  on the NEXT bar exactly like the live monitoring loop.

Known deltas vs live (documented in README): no M15 momentum gate, no news
suspensions, no USD-correlation guard (sim holds max one position per pair),
mid-price candles with spread modeled as half typical_spread per side, and
H4 bars resampled UTC-anchored (OANDA's H4 candles are NY-17:00 anchored).

Conservative intra-bar rules: if a bar touches both SL and TP, SL fills first;
gaps beyond SL/TP fill at the bar open, not at the level.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import settings
from config.pairs import PAIR_INFO
from src.agents.base import Signal
from src.agents.indicators import to_dataframe
from src.agents.tech_agent import TechAgent
from src.agents.trend_agent import TrendAgent
from src.agents.momentum_agent import MomentumAgent
from src.execution.engine import (
    _calc_atr,
    _count_indicator_confluences,
    _get_atr_multiplier,
    _get_min_confidence_for_setup,
    _htf_trend_aligned,
    _is_adx_trending,
)
from src.execution.trade_manager import _market_hours_elapsed
from src.risk.position_sizer import PositionSizer
from src.strategies import SuperTrendEmaStrategy
from .data import _parse_time


@dataclass
class SimPosition:
    pair: str
    side: str                  # 'long' | 'short'
    units: int                 # remaining units (mutable on partial close)
    initial_units: int
    entry_price: float
    entry_time: datetime
    sl: float
    tp: float
    initial_sl_distance: float
    risk_usd: float            # dollars at risk at entry (for R multiples)
    realized_pnl: float = 0.0  # accumulated incl. partial closes
    peak: float = 0.0
    trough: float = 0.0
    be_triggered: bool = False
    partial_done: bool = False
    bars_held: int = 0

    @property
    def is_long(self) -> bool:
        return self.side == 'long'


@dataclass
class SimTrade:
    pair: str
    side: str
    units: int
    entry_price: float
    entry_time: datetime
    exit_price: float
    exit_time: datetime
    exit_reason: str           # sl|tp|time_stop|flip_exit|end_of_data
    pnl_usd: float
    r_multiple: float
    bars_held: int
    partial_taken: bool


@dataclass
class BacktestConfig:
    pairs: List[str]
    start: datetime
    end: datetime
    balance: float = 10_000.0
    spread_mult: float = 1.0
    granularity: str = 'H1'


@dataclass
class BacktestResult:
    trades: List[SimTrade]
    equity: List[Tuple[datetime, float]]
    final_balance: float
    gate_rejections: Dict[str, int]
    signals_seen: int


class BacktestSimulator:
    """Bar-by-bar simulator with a single shared account across pairs."""

    def __init__(self, config: BacktestConfig, logger: Optional[logging.Logger] = None):
        self.config = config
        self.logger = logger or logging.getLogger('backtest_sim')
        self.strategy = SuperTrendEmaStrategy(self.logger)
        self._tech = TechAgent(self.logger)
        self._trend = TrendAgent(self.logger)
        self._momentum = MomentumAgent(self.logger)
        self._sizer = PositionSizer(self.logger)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, candles_by_pair: Dict[str, List[Dict]]) -> BacktestResult:
        cfg = self.config
        window_len = settings.H1_CANDLE_COUNT
        bar_seconds = {'M15': 900, 'M30': 1800, 'H1': 3600, 'H4': 14400}.get(
            cfg.granularity, 3600
        )

        pair_data: Dict[str, dict] = {}
        for pair, candles in candles_by_pair.items():
            if len(candles) < window_len + 2:
                self.logger.warning(f"{pair}: only {len(candles)} candles — skipped")
                continue
            df = to_dataframe(candles)
            times = [_parse_time(c['time']) for c in candles]
            pair_data[pair] = {
                'candles': candles,
                'times': times,
                'atr14': _rolling_atr(df, 14),
                'candidates': _candidate_bars(
                    df,
                    settings.STRATEGY_SUPERTREND_PERIOD,
                    settings.STRATEGY_SUPERTREND_MULTIPLIER,
                    settings.STRATEGY_SIGNAL_VALIDITY_BARS,
                ),
                'h4': _resample_h4(df, times),
                'idx': 0,
                'position': None,        # SimPosition
                'pending': None,         # action scheduled for the next bar
                'cooldown_until': None,  # datetime
            }

        if not pair_data:
            return BacktestResult([], [], cfg.balance, {}, 0)

        timeline = sorted({t for d in pair_data.values() for t in d['times']})

        balance = cfg.balance
        trades: List[SimTrade] = []
        equity: List[Tuple[datetime, float]] = []
        rejections: Dict[str, int] = {}
        signals_seen = 0

        consecutive_losses = 0
        halted_today = False
        day_str: Optional[str] = None
        day_start_nav = balance

        for ts in timeline:
            # Daily rollover — mirrors live engine resets
            ts_day = ts.strftime('%Y-%m-%d')
            if ts_day != day_str:
                day_str = ts_day
                consecutive_losses = 0
                halted_today = False
                day_start_nav = balance + self._total_unrealized(pair_data)

            for pair, d in pair_data.items():
                idx = d['idx']
                if idx >= len(d['times']) or d['times'][idx] != ts:
                    continue
                d['idx'] = idx + 1
                candles = d['candles']
                bar = candles[idx]
                hs = _half_spread(pair, cfg.spread_mult)

                # ---- 1. Execute action scheduled on the previous bar (fills at open)
                pending, d['pending'] = d['pending'], None
                if pending is not None:
                    gap_seconds = (ts - pending['decided_at']).total_seconds()
                    # Discard stale pendings (weekend gap) and out-of-session fills
                    if gap_seconds <= 2 * bar_seconds and _in_session(ts):
                        open_px = float(bar['open'])
                        if pending['kind'] == 'flip_exit' and d['position'] is not None:
                            balance += self._close_position(
                                d, trades, open_px, ts, 'flip_exit', hs
                            )
                        if pending['enter'] and d['position'] is None and not halted_today:
                            d['position'] = self._open_position(
                                pair, pending, open_px, ts, hs, balance, consecutive_losses
                            )

                # ---- 2. Manage the open position against this bar's range
                if d['position'] is not None:
                    pnl_delta, exit_px, reason = self._manage_bar(
                        d['position'], bar, ts, hs, d['atr14'][idx]
                    )
                    balance += pnl_delta
                    if exit_px is not None:
                        trade = self._finalize_trade(d['position'], exit_px, ts, reason)
                        trades.append(trade)
                        d['position'] = None
                        if trade.pnl_usd < 0:
                            consecutive_losses += 1
                            d['cooldown_until'] = ts + timedelta(
                                hours=settings.LOSS_COOLDOWN_HOURS
                            )
                            if consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
                                halted_today = True
                        elif trade.pnl_usd > 0:
                            consecutive_losses = 0

                # ---- 3. Daily loss circuit breaker (on NAV)
                nav = balance + self._total_unrealized(pair_data)
                if day_start_nav > 0 and (day_start_nav - nav) / day_start_nav >= settings.MAX_DAILY_LOSS_PERCENT:
                    halted_today = True

                # ---- 4. Signal evaluation (candidate bars only — fresh ST flips)
                if idx not in d['candidates'] or idx < window_len - 1 or idx >= len(candles) - 1:
                    continue
                if halted_today:
                    continue
                # Live cooldown check returns before run_decision — no signal,
                # no flip exit during cooldown
                cd = d['cooldown_until']
                if cd is not None and ts < cd:
                    _bump(rejections, 'cooldown')
                    continue

                window = candles[idx - window_len + 1: idx + 1]
                close = float(bar['close'])

                indicators: dict = {}
                indicators.update(self._tech.get_indicators(pair, window, close))
                indicators.update(self._trend.get_indicators(pair, window, close))
                indicators.update(self._momentum.get_indicators(pair, window, close))

                vote = self.strategy.vote(pair, window, close, indicators)
                if vote.signal == Signal.HOLD:
                    continue
                signals_seen += 1
                if vote.confidence < settings.CONSENSUS_THRESHOLD:
                    _bump(rejections, 'confidence')
                    continue
                if vote.confidence < _get_min_confidence_for_setup(vote.setup_type):
                    _bump(rejections, 'setup_confidence')
                    continue

                is_long = vote.signal == Signal.BUY
                cur = d['position']
                flip = (
                    settings.STRATEGY_EXIT_ON_FLIP
                    and cur is not None
                    and cur.is_long != is_long
                )
                if cur is not None and not flip:
                    _bump(rejections, 'position_open')
                    continue

                enter = self._passes_gates(pair, indicators, is_long, close, d, idx, rejections)
                if flip or enter:
                    d['pending'] = {
                        'kind': 'flip_exit' if flip else 'entry',
                        'enter': enter,
                        'is_long': is_long,
                        'window': window,
                        'decided_at': ts,
                    }

            equity.append((ts, balance + self._total_unrealized(pair_data)))

        # Close anything still open at the end of data
        for pair, d in pair_data.items():
            if d['position'] is None:
                continue
            hs = _half_spread(pair, cfg.spread_mult)
            balance += self._close_position(
                d, trades, float(d['candles'][-1]['close']), d['times'][-1],
                'end_of_data', hs,
            )

        return BacktestResult(
            trades=trades,
            equity=equity,
            final_balance=balance,
            gate_rejections=rejections,
            signals_seen=signals_seen,
        )

    # ------------------------------------------------------------------
    # Gates — live functions from src.execution.engine
    # ------------------------------------------------------------------

    def _passes_gates(self, pair, indicators, is_long, close, d, idx, rejections) -> bool:
        if not _is_adx_trending(indicators):
            _bump(rejections, 'adx')
            return False

        if settings.HTF_ALIGNMENT_ENABLED:
            h4_window = _h4_window(d['h4'], d['times'][idx])
            if not _htf_trend_aligned(h4_window, is_long):
                _bump(rejections, 'h4_alignment')
                return False

        min_conf = (
            settings.MIN_CONFLUENCES_USD_CHF if pair == 'USD_CHF'
            else settings.MIN_CONFLUENCES
        )
        count, _ = _count_indicator_confluences(indicators, is_long, close)
        if count < min_conf:
            _bump(rejections, 'confluence')
            return False

        # RR gate: live TP is constructed as SL x DEFAULT_TAKE_PROFIT_RATIO, so
        # the per-trade check reduces to this config consistency condition
        if settings.DEFAULT_TAKE_PROFIT_RATIO < settings.MIN_RR_RATIO - 1e-9:
            _bump(rejections, 'rr_ratio')
            return False

        return True

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    def _open_position(
        self, pair, pending, open_px, ts, hs, balance, consecutive_losses
    ) -> Optional[SimPosition]:
        is_long = pending['is_long']
        entry = open_px + hs if is_long else open_px - hs

        window = pending['window']
        pip = PAIR_INFO.get(pair, {}).get('pip_value', 0.0001)
        atr_val = _calc_atr(window)
        if atr_val and atr_val > 0:
            sl_distance = atr_val * _get_atr_multiplier(atr_val, window)
        else:
            sl_distance = settings.DEFAULT_STOP_LOSS_PIPS * pip
        tp_distance = sl_distance * settings.DEFAULT_TAKE_PROFIT_RATIO

        sl = entry - sl_distance if is_long else entry + sl_distance
        tp = entry + tp_distance if is_long else entry - tp_distance

        risk_percent = None
        if consecutive_losses >= settings.CONSECUTIVE_LOSS_RISK_REDUCTION_AFTER:
            risk_percent = settings.MAX_RISK_PER_TRADE / 2

        size = self._sizer.calculate(
            pair=pair,
            account_balance=balance,
            stop_loss_pips=max(1, int(sl_distance / pip)),
            risk_percent=risk_percent,
            current_price=entry,
        )
        if not size or size.units < 1:
            return None

        return SimPosition(
            pair=pair,
            side='long' if is_long else 'short',
            units=size.units,
            initial_units=size.units,
            entry_price=entry,
            entry_time=ts,
            sl=sl,
            tp=tp,
            initial_sl_distance=sl_distance,
            risk_usd=_to_usd(pair, sl_distance * size.units, entry),
            peak=entry,
            trough=entry,
        )

    def _manage_bar(
        self, pos: SimPosition, bar: dict, ts, hs, atr_now
    ) -> Tuple[float, Optional[float], str]:
        """Apply one bar to the open position.

        Returns (realized_pnl_delta, exit_price_or_None, exit_reason).
        SL/TP used for exit checks were set BEFORE this bar (no lookahead);
        BE/trailing updates at the bottom take effect from the next bar.
        """
        o, h, l, c = (float(bar['open']), float(bar['high']),
                      float(bar['low']), float(bar['close']))
        pos.bars_held += 1
        long = pos.is_long
        pnl = 0.0

        def exit_at(level: float) -> float:
            return level - hs if long else level + hs

        # 1. Gap through SL/TP — fill at the open, not the level
        if (long and o <= pos.sl) or (not long and o >= pos.sl):
            px = exit_at(o)
            return self._fill(pos, px), px, 'sl'
        if (long and o >= pos.tp) or (not long and o <= pos.tp):
            px = exit_at(o)
            return self._fill(pos, px), px, 'tp'

        sl_hit = (long and l <= pos.sl) or (not long and h >= pos.sl)
        tp_hit = (long and h >= pos.tp) or (not long and l <= pos.tp)

        # 2. Both touched in one bar — conservative: SL first
        if sl_hit:
            px = exit_at(pos.sl)
            return self._fill(pos, px), px, 'sl'

        one_r = pos.initial_sl_distance * settings.PARTIAL_TP_RR_TARGET
        partial_level = pos.entry_price + one_r if long else pos.entry_price - one_r
        favorable = h if long else l
        partial_reached = (favorable >= partial_level) if long else (favorable <= partial_level)

        # 3. Full TP — price passed 1R en route, so take a pending partial first
        if tp_hit:
            if settings.PARTIAL_TP_ENABLED and not pos.partial_done and partial_reached:
                pnl += self._partial_close(pos, partial_level, hs)
            px = exit_at(pos.tp)
            return pnl + self._fill(pos, px), px, 'tp'

        # 4. Partial TP at 1R (then SL -> break-even, mirroring _check_partial_tp)
        if settings.PARTIAL_TP_ENABLED and not pos.partial_done and partial_reached:
            pnl += self._partial_close(pos, partial_level, hs)

        # 5. Break-even at 0.5R (mirroring _check_break_even)
        if not pos.be_triggered:
            profit_extreme = (favorable - pos.entry_price) if long else (pos.entry_price - favorable)
            if profit_extreme >= pos.initial_sl_distance * settings.BREAK_EVEN_TRIGGER_R:
                self._move_sl_to_be(pos)

        # 6. ATR trailing once peak profit >= activation R (mirroring _check_trailing_stop)
        pos.peak = max(pos.peak, h)
        pos.trough = min(pos.trough, l)
        peak_profit = (pos.peak - pos.entry_price) if long else (pos.entry_price - pos.trough)
        if (
            peak_profit >= pos.initial_sl_distance * settings.TRAILING_STOP_ACTIVATION_R
            and atr_now is not None and not np.isnan(atr_now) and atr_now > 0
        ):
            trail = atr_now * settings.TRAILING_ATR_MULTIPLIER
            pip = 0.01 if 'JPY' in pos.pair else 0.0001
            if long:
                candidate = min(pos.peak - trail, pos.tp - 2 * pip)
                if candidate > pos.sl:
                    pos.sl = candidate
            else:
                candidate = max(pos.trough + trail, pos.tp + 2 * pip)
                if candidate < pos.sl:
                    pos.sl = candidate

        # 7. Time stop — losing after TIME_STOP_HOURS market hours
        if settings.TIME_STOP_ENABLED:
            losing = (c < pos.entry_price) if long else (c > pos.entry_price)
            if losing and _market_hours_elapsed(pos.entry_time, ts) >= settings.TIME_STOP_HOURS:
                px = exit_at(c)
                return pnl + self._fill(pos, px), px, 'time_stop'

        return pnl, None, ''

    def _partial_close(self, pos: SimPosition, level: float, hs: float) -> float:
        units_to_close = int(abs(pos.units) * settings.PARTIAL_TP_RATIO)
        if units_to_close < 1:
            return 0.0
        exit_px = level - hs if pos.is_long else level + hs
        diff = (exit_px - pos.entry_price) if pos.is_long else (pos.entry_price - exit_px)
        pnl = _to_usd(pos.pair, diff * units_to_close, exit_px)
        pos.units -= units_to_close
        pos.realized_pnl += pnl
        pos.partial_done = True
        self._move_sl_to_be(pos)
        return pnl

    def _move_sl_to_be(self, pos: SimPosition) -> None:
        pip = 0.01 if 'JPY' in pos.pair else 0.0001
        buffer = settings.BREAK_EVEN_BUFFER_PIPS * pip
        new_sl = pos.entry_price + buffer if pos.is_long else pos.entry_price - buffer
        if (pos.is_long and new_sl > pos.sl) or (not pos.is_long and new_sl < pos.sl):
            pos.sl = new_sl
        pos.be_triggered = True

    def _fill(self, pos: SimPosition, exit_px: float) -> float:
        """Close all remaining units at exit_px; returns the realized pnl delta."""
        diff = (exit_px - pos.entry_price) if pos.is_long else (pos.entry_price - exit_px)
        pnl = _to_usd(pos.pair, diff * pos.units, exit_px)
        pos.realized_pnl += pnl
        pos.units = 0
        return pnl

    def _finalize_trade(self, pos: SimPosition, exit_price: float, ts, reason: str) -> SimTrade:
        r_mult = round(pos.realized_pnl / pos.risk_usd, 2) if pos.risk_usd else 0.0
        return SimTrade(
            pair=pos.pair,
            side=pos.side,
            units=pos.initial_units,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=exit_price,
            exit_time=ts,
            exit_reason=reason,
            pnl_usd=round(pos.realized_pnl, 2),
            r_multiple=r_mult,
            bars_held=pos.bars_held,
            partial_taken=pos.partial_done,
        )

    def _close_position(self, d, trades, price, ts, reason, hs) -> float:
        pos: SimPosition = d['position']
        exit_px = price - hs if pos.is_long else price + hs
        pnl = self._fill(pos, exit_px)
        trades.append(self._finalize_trade(pos, exit_px, ts, reason))
        d['position'] = None
        return pnl

    def _total_unrealized(self, pair_data) -> float:
        total = 0.0
        for d in pair_data.values():
            pos: Optional[SimPosition] = d['position']
            if pos is None:
                continue
            idx = max(0, min(d['idx'], len(d['candles'])) - 1)
            c = float(d['candles'][idx]['close'])
            diff = (c - pos.entry_price) if pos.is_long else (pos.entry_price - c)
            total += _to_usd(pos.pair, diff * pos.units, c)
        return total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bump(counter: Dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _half_spread(pair: str, spread_mult: float) -> float:
    info = PAIR_INFO.get(pair, {})
    pip = info.get('pip_value', 0.0001)
    return info.get('typical_spread', 1.0) * pip * spread_mult / 2


def _to_usd(pair: str, amount_quote: float, rate: float) -> float:
    """Convert a quote-currency amount to USD (account currency)."""
    quote = PAIR_INFO.get(pair, {}).get('quote_currency', 'USD')
    if quote == 'USD':
        return amount_quote
    return amount_quote / rate if rate > 0 else 0.0


def _in_session(ts: datetime) -> bool:
    if not settings.SESSION_FILTER_ENABLED:
        return True
    start, end = settings.SESSION_START_UTC_HOUR, settings.SESSION_END_UTC_HOUR
    if start <= end:
        return start <= ts.hour < end
    return ts.hour >= start or ts.hour < end


def _rolling_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean().to_numpy()


def _candidate_bars(df: pd.DataFrame, period: int, multiplier: float, validity: int) -> set:
    """Bars where a SuperTrend flip occurred within the last `validity` bars.

    Full-series pass used as a cheap pre-filter; the actual signal decision
    re-runs supertrend() on the live-sized window for exact parity.
    """
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(window=period).mean().to_numpy()
    hl2 = ((high + low) / 2).to_numpy()
    closes = close.to_numpy()
    ub = hl2 + multiplier * atr_series
    lb = hl2 - multiplier * atr_series

    n = len(df)
    start = period
    fu, fl = np.zeros(n), np.zeros(n)
    direction = np.zeros(n, dtype=int)
    fu[start], fl[start] = ub[start], lb[start]
    direction[start] = 1 if closes[start] > ub[start] else -1
    candidates: set = set()
    for i in range(start + 1, n):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or closes[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or closes[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] == 1:
            direction[i] = -1 if closes[i] < fl[i] else 1
        else:
            direction[i] = 1 if closes[i] > fu[i] else -1
        if direction[i] != direction[i - 1]:
            for age in range(validity):
                if i + age < n:
                    candidates.add(i + age)
    return candidates


def _resample_h4(df: pd.DataFrame, times: List[datetime]) -> List[Dict]:
    """Resample H1 candles to UTC-anchored H4 bars (flat-dict format)."""
    frame = df.copy()
    frame['time'] = pd.to_datetime(list(times), utc=True)
    frame = frame.set_index('time')
    h4 = frame.resample('4h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
        'volume': 'sum',
    }).dropna()
    return [
        {
            'time': ts.to_pydatetime(),
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': float(row['volume']),
        }
        for ts, row in h4.iterrows()
    ]


def _h4_window(h4_bars: List[Dict], now: datetime, count: int = 60) -> List[Dict]:
    """Last `count` completed H4 bars strictly before `now` (no lookahead)."""
    completed = [b for b in h4_bars if b['time'] + timedelta(hours=4) <= now]
    return completed[-count:]
