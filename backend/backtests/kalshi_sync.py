"""Kalshi trade fetching, price lookup, sync, fill simulation, and dynamic market selection."""

from datetime import datetime

from backtests.mlb import PlayRecord, MarketSpec
from backtests.constants import (
    EXIT_WINDOWS, ENTRY_OFFSET, CLEAN_BUFFER, WINDOW_SECONDS,
    CLEAN_WINDOW_SECONDS,
    ALPHAS, STOP_LEVELS,
    HIGH_LEV_ML_THRESHOLD, HIGH_LEV_OU_PROXIMITY,
)
from app.line_selection import pick_ou_line, pick_spread_line

# ---------------------------------------------------------------------------
# Mutable flags (toggled by CLI args)
# ---------------------------------------------------------------------------

TRACE_MODE = False
STOP_LOSS_ANALYSIS = False

# Overrides ENTRY_OFFSET when set (via set_entry_offset). Value is in
# seconds *before* the play timestamp, so positive = look-ahead (cheat),
# negative = look-back / reaction delay (realistic). None → use default.
_ENTRY_OFFSET_OVERRIDE: int | None = None


def _entry_offset() -> int:
    return ENTRY_OFFSET if _ENTRY_OFFSET_OVERRIDE is None else _ENTRY_OFFSET_OVERRIDE


def set_entry_offset(seconds: int | None):
    global _ENTRY_OFFSET_OVERRIDE
    _ENTRY_OFFSET_OVERRIDE = seconds


def set_stop_loss_analysis(enabled: bool):
    global STOP_LOSS_ANALYSIS
    STOP_LOSS_ANALYSIS = enabled


def set_trace_mode(enabled: bool):
    global TRACE_MODE
    TRACE_MODE = enabled


# ---------------------------------------------------------------------------
# Trade fetching
# ---------------------------------------------------------------------------

def pull_kalshi_trades(client, market_ticker: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    """Pull individual Kalshi trades. Returns sorted list of (unix_ts, yes_price)."""
    try:
        market = client.get_market(market_ticker)
        raw = market.get_trades(min_ts=start_ts, max_ts=end_ts, fetch_all=True)

        trades = []
        for t in raw:
            price = float(t.yes_price_dollars or 0)
            if price <= 0:
                continue
            ts_str = t.created_time
            if not ts_str:
                continue
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            trades.append((int(dt.timestamp()), price))

        trades.sort()
        return trades
    except Exception as e:
        print(f"    Failed to pull trades for {market_ticker}: {e}")
        return []


# ---------------------------------------------------------------------------
# Price lookup helpers
# ---------------------------------------------------------------------------

def _find_price_at(trades: list[tuple[int, float]], ts: int) -> float | None:
    """Last trade price at or before ts. Binary search."""
    lo, hi = 0, len(trades) - 1
    result = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if trades[mid][0] <= ts:
            result = trades[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return result


def _find_price_in_window(trades: list[tuple[int, float]], ts_start: int, ts_end: int) -> float | None:
    """Last trade price in [ts_start, ts_end]."""
    lo, hi = 0, len(trades) - 1
    first_idx = len(trades)
    while lo <= hi:
        mid = (lo + hi) // 2
        if trades[mid][0] >= ts_start:
            first_idx = mid
            hi = mid - 1
        else:
            lo = mid + 1
    result = None
    for i in range(first_idx, len(trades)):
        if trades[i][0] > ts_end:
            break
        result = trades[i][1]
    return result


# ---------------------------------------------------------------------------
# Fill simulation
# ---------------------------------------------------------------------------

def _simulate_limit_order(trades, entry_ts, entry_price, predicted_delta, clean_deadline_ts, sign):
    results = {}

    lo, hi = 0, len(trades) - 1
    first_idx = len(trades)
    while lo <= hi:
        mid = (lo + hi) // 2
        if trades[mid][0] > entry_ts:
            first_idx = mid
            hi = mid - 1
        else:
            lo = mid + 1

    window_trades = []
    for i in range(first_idx, len(trades)):
        if trades[i][0] > clean_deadline_ts:
            break
        window_trades.append(trades[i])

    if not window_trades:
        return {a: {"filled": False, "pnl": 0.0, "fill_time": None} for a in ALPHAS}

    last_clean_price = window_trades[-1][1]
    forced_exit_pnl = (last_clean_price - entry_price) * sign

    for alpha in ALPHAS:
        target_move = alpha * abs(predicted_delta)
        if predicted_delta == 0:
            results[alpha] = {"filled": False, "pnl": round(forced_exit_pnl, 4), "fill_time": None}
            continue

        if (predicted_delta * sign) > 0:
            target_price = entry_price + target_move
            for ts, price in window_trades:
                if price >= target_price:
                    results[alpha] = {"filled": True, "pnl": round(target_move, 4), "fill_time": ts - entry_ts}
                    break
        else:
            target_price = entry_price - target_move
            for ts, price in window_trades:
                if price <= target_price:
                    results[alpha] = {"filled": True, "pnl": round(target_move, 4), "fill_time": ts - entry_ts}
                    break

        if alpha not in results:
            results[alpha] = {"filled": False, "pnl": round(forced_exit_pnl, 4), "fill_time": None}

    return results


def _simulate_stop_loss(trades, entry_ts, entry_price, predicted_delta, clean_deadline_ts, sign, alpha):
    """
    For each stop level, simulate: does the stop trigger before the target?
    If stopped, also check if the target would have been hit later (false stop).

    Returns dict keyed by stop_level (float):
        {"outcome": "target"|"stopped"|"expired", "pnl": float, "would_have_hit": bool}
    Also includes key "none" for no-stop-loss (timer only).
    """
    # Collect window trades (same logic as _simulate_limit_order)
    lo, hi = 0, len(trades) - 1
    first_idx = len(trades)
    while lo <= hi:
        mid = (lo + hi) // 2
        if trades[mid][0] > entry_ts:
            first_idx = mid
            hi = mid - 1
        else:
            lo = mid + 1

    window_trades = []
    for i in range(first_idx, len(trades)):
        if trades[i][0] > clean_deadline_ts:
            break
        window_trades.append(trades[i])

    if not window_trades or predicted_delta == 0:
        base = {"outcome": "expired", "pnl": 0.0, "would_have_hit": False,
                "price_at_stop": None, "min_price": None, "max_price": None}
        result = {sl: dict(base) for sl in STOP_LEVELS}
        result["none"] = dict(base)
        return result

    # Track price extremes in the window
    all_prices = [p for _, p in window_trades]
    min_price = min(all_prices)
    max_price = max(all_prices)

    last_clean_price = window_trades[-1][1]
    forced_exit_pnl = round((last_clean_price - entry_price) * sign, 4)

    target_move = alpha * abs(predicted_delta)
    going_up = (predicted_delta * sign) > 0

    if going_up:
        target_price = entry_price + target_move
    else:
        target_price = entry_price - target_move

    # Pre-scan: at which trade index does the target get hit?
    target_hit_idx = None
    for i, (ts, price) in enumerate(window_trades):
        if going_up and price >= target_price:
            target_hit_idx = i
            break
        elif not going_up and price <= target_price:
            target_hit_idx = i
            break

    # For each stop level, find when (if) the stop triggers
    result = {}

    for sl in STOP_LEVELS:
        if going_up:
            # We bought expecting price to go up. Stop if price drops.
            stop_price = entry_price - sl
            stop_hit_idx = None
            for i, (ts, price) in enumerate(window_trades):
                if price <= stop_price:
                    stop_hit_idx = i
                    break
        else:
            # We shorted / bought NO expecting price to drop. Stop if price rises.
            stop_price = entry_price + sl
            stop_hit_idx = None
            for i, (ts, price) in enumerate(window_trades):
                if price >= stop_price:
                    stop_hit_idx = i
                    break

        extras = {"min_price": min_price, "max_price": max_price}

        if stop_hit_idx is not None and (target_hit_idx is None or stop_hit_idx < target_hit_idx):
            would_have_hit = target_hit_idx is not None
            result[sl] = {
                "outcome": "stopped", "pnl": round(-sl, 4),
                "would_have_hit": would_have_hit,
                "price_at_stop": window_trades[stop_hit_idx][1],
                **extras,
            }
        elif target_hit_idx is not None:
            result[sl] = {
                "outcome": "target", "pnl": round(target_move, 4),
                "would_have_hit": False, "price_at_stop": None, **extras,
            }
        else:
            result[sl] = {
                "outcome": "expired", "pnl": forced_exit_pnl,
                "would_have_hit": False, "price_at_stop": None, **extras,
            }

    extras_none = {"min_price": min_price, "max_price": max_price, "price_at_stop": None}
    if target_hit_idx is not None:
        result["none"] = {"outcome": "target", "pnl": round(target_move, 4),
                          "would_have_hit": False, **extras_none}
    else:
        result["none"] = {"outcome": "expired", "pnl": forced_exit_pnl,
                          "would_have_hit": False, **extras_none}

    return result


# ---------------------------------------------------------------------------
# Dynamic market pickers (delegates to shared app/line_selection.py)
# ---------------------------------------------------------------------------

def _pick_spread_spec(margin: int, spread_specs: list[MarketSpec]) -> MarketSpec | None:
    return pick_spread_line(margin, spread_specs)


def _pick_spread_specs_both_sides(
    margin: int, spread_specs: list[MarketSpec]
) -> list[MarketSpec]:
    """Mirror of market_selector._pick_spread_markets_both_sides — returns
    one home-side and one away-side spread per play. Live picker considers
    both; backtest used to take only one, leaving roughly half the spread
    candidates out of the pool."""
    home_side = pick_spread_line(margin, [s for s in spread_specs if not s.flip])
    away_side = pick_spread_line(margin, [s for s in spread_specs if s.flip])
    return [s for s in (home_side, away_side) if s is not None]


def _pick_ou_spec(total_runs: int, ou_specs: list[MarketSpec]) -> MarketSpec | None:
    return pick_ou_line(total_runs, ou_specs)


# ---------------------------------------------------------------------------
# Core sync logic (shared by all sync functions)
# ---------------------------------------------------------------------------

def _build_synced_record(
    rec, play_ts, entry_ts, price_at, predicted, sign, spec,
    trades, clean_cutoff, time_to_next_ab, time_to_next_pitch,
    market_type_override=None, market_label_override=None,
):
    """Build a single synced dict for one play against one market."""
    moves = {}
    clean_moves = {}
    for w in EXIT_WINDOWS:
        p = _find_price_in_window(trades, play_ts + 1, play_ts + w)
        if p is not None:
            move = round((p - price_at) * sign, 4)
            moves[w] = move
            if w <= clean_cutoff:
                clean_moves[w] = move

    if not moves:
        return None

    default_move = moves.get(WINDOW_SECONDS, list(moves.values())[0])

    abs_ml = abs(rec.ml_delta)
    total_runs = rec.home_score + rec.away_score
    ou_line = spec.line if (spec and spec.market_type == "over_under" and spec.line) else 8.5
    is_high_lev_ml = abs_ml >= HIGH_LEV_ML_THRESHOLD
    is_high_lev_ou = abs(total_runs - ou_line) <= HIGH_LEV_OU_PROXIMITY

    clean_deadline_ts = play_ts + int(clean_cutoff) if clean_cutoff < 9999 else play_ts + 300
    fill_sim = _simulate_limit_order(trades, entry_ts, price_at, predicted, clean_deadline_ts, sign)

    stop_loss_sim = None
    if STOP_LOSS_ANALYSIS:
        # Run at best alpha (0.6) — can parameterize later
        for a in reversed(ALPHAS):
            stop_loss_sim = _simulate_stop_loss(
                trades, entry_ts, price_at, predicted, clean_deadline_ts, sign, a)
            break

    trace_data = None
    if TRACE_MODE:
        entry_trade = None
        for i in range(len(trades) - 1, -1, -1):
            if trades[i][0] <= entry_ts:
                entry_trade = trades[i]
                break
        lo_t, hi_t = 0, len(trades) - 1
        fi = len(trades)
        while lo_t <= hi_t:
            mid = (lo_t + hi_t) // 2
            if trades[mid][0] > entry_ts:
                fi = mid
                hi_t = mid - 1
            else:
                lo_t = mid + 1
        wt = []
        for i in range(fi, len(trades)):
            if trades[i][0] > clean_deadline_ts:
                break
            wt.append(trades[i])
        trace_data = {
            "entry_trade": entry_trade, "window_trades": wt,
            "entry_ts": entry_ts, "play_ts": play_ts,
            "clean_deadline_ts": clean_deadline_ts, "sign": sign,
            "home_score": rec.home_score, "away_score": rec.away_score,
            "runners": rec.runners_before, "outs": rec.outs_before,
        }

    mt = market_type_override or (spec.market_type if spec else "moneyline")
    ml = market_label_override or (spec.label if spec else "ML")
    tk = spec.ticker if spec else ""

    # Count trades in clean window for thin-liquidity flag
    n_window_trades = 0
    lo_c, hi_c = 0, len(trades) - 1
    fi_c = len(trades)
    while lo_c <= hi_c:
        mid = (lo_c + hi_c) // 2
        if trades[mid][0] > entry_ts:
            fi_c = mid
            hi_c = mid - 1
        else:
            lo_c = mid + 1
    for i in range(fi_c, len(trades)):
        if trades[i][0] > clean_deadline_ts:
            break
        n_window_trades += 1

    return {
        "timestamp": rec.timestamp,
        "inning": rec.inning,
        "half": rec.half,
        "outs": rec.outs_before,
        "runners": rec.runners_before,
        "home_score": rec.home_score,
        "away_score": rec.away_score,
        "event": rec.mapped_event,
        "mlb_event": rec.mlb_event,
        "description": rec.description,
        "predicted_ml_delta": round(predicted, 4),
        "we_before": round(rec.we_before, 4),
        "we_after": round(rec.we_after, 4),
        "actual_price_move": default_move,
        "error": round(default_move - predicted, 4),
        "price_before": round(price_at, 4),
        "moves": moves,
        "clean_moves": clean_moves,
        "time_to_next_ab": time_to_next_ab if time_to_next_ab < 9999 else None,
        "time_to_next_pitch": time_to_next_pitch if time_to_next_pitch < 9999 else None,
        "market_type": mt,
        "market_label": ml,
        "market_ticker": tk,
        "is_high_lev_ml": is_high_lev_ml,
        "is_high_lev_ou": is_high_lev_ou,
        "leverage_score": round(abs_ml, 4),
        "total_runs": total_runs,
        "n_window_trades": n_window_trades,
        "fill_sim": fill_sim,
        "stop_loss_sim": stop_loss_sim,
        "_trace": trace_data,
    }


def _parse_play_times(records):
    play_times = []
    for rec in records:
        if not rec.timestamp:
            continue
        try:
            dt = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
            play_times.append((int(dt.timestamp()), rec))
        except Exception:
            continue
    return play_times


def _compute_gaps(rec, play_ts, play_times, idx):
    if idx + 1 < len(play_times):
        time_to_next_ab = play_times[idx + 1][0] - play_ts
    else:
        time_to_next_ab = 9999
    if rec.next_pitch_ts is not None:
        time_to_next_pitch = rec.next_pitch_ts - play_ts
    else:
        time_to_next_pitch = 9999
    # Live trader force-exits via IOC at CLEAN_WINDOW_SECONDS regardless of
    # what the next pitch is doing; cap here so the backtest's fill window
    # never exceeds what live would actually allow. The next-pitch − 5 floor
    # still kicks in on busy ABs where the next pitch lands inside the
    # CLEAN_WINDOW (we don't want to be measuring price through a follow-up
    # event).
    if time_to_next_pitch < 9999:
        clean_cutoff = min(time_to_next_pitch - 5, CLEAN_WINDOW_SECONDS)
    else:
        clean_cutoff = CLEAN_WINDOW_SECONDS
    return time_to_next_ab, time_to_next_pitch, clean_cutoff


# ---------------------------------------------------------------------------
# Sync functions
# ---------------------------------------------------------------------------

def sync_plays_with_trades(records, trades, spec=None, is_away_contract=False):
    """Sync plays against a single fixed market."""
    if not trades:
        return []

    flip = spec.flip if spec else is_away_contract
    sign = -1.0 if flip else 1.0
    delta_key = spec.delta_key if spec else "ml"

    synced = []
    play_times = _parse_play_times(records)

    for idx, (play_ts, rec) in enumerate(play_times):
        entry_ts = play_ts - _entry_offset()
        price_at = _find_price_at(trades, entry_ts)
        if price_at is None:
            continue

        time_to_next_ab, time_to_next_pitch, clean_cutoff = _compute_gaps(rec, play_ts, play_times, idx)
        predicted = rec.model_deltas.get(delta_key, rec.ml_delta)

        result = _build_synced_record(
            rec, play_ts, entry_ts, price_at, predicted, sign, spec,
            trades, clean_cutoff, time_to_next_ab, time_to_next_pitch,
        )
        if result:
            synced.append(result)

    return synced


def sync_plays_dynamic_ou(records, ou_specs, ou_trades):
    """Sync plays against dynamically selected O/U market per play."""
    synced = []
    play_times = _parse_play_times(records)

    for idx, (play_ts, rec) in enumerate(play_times):
        total_runs = rec.home_score + rec.away_score
        spec = _pick_ou_spec(total_runs, ou_specs)
        if spec is None:
            continue

        trades = ou_trades.get(spec.ticker)
        if not trades:
            continue

        entry_ts = play_ts - _entry_offset()
        price_at = _find_price_at(trades, entry_ts)
        if price_at is None:
            continue

        time_to_next_ab, time_to_next_pitch, clean_cutoff = _compute_gaps(rec, play_ts, play_times, idx)
        predicted = rec.model_deltas.get(spec.delta_key, 0)

        result = _build_synced_record(
            rec, play_ts, entry_ts, price_at, predicted, 1.0, spec,
            trades, clean_cutoff, time_to_next_ab, time_to_next_pitch,
            market_type_override="over_under",
            market_label_override=f"O/U {spec.line} (dynamic)",
        )
        if result:
            synced.append(result)

    return synced


def sync_plays_dynamic_spread(records, spread_specs, spread_trades):
    """Sync plays against dynamically selected spread markets per play.

    Per play we evaluate up to TWO spread markets — one home-side, one
    away-side — matching market_selector._pick_spread_markets_both_sides
    in the live system. Each yields its own synced record so the candidate
    pool downstream sees both options.
    """
    synced = []
    play_times = _parse_play_times(records)

    for idx, (play_ts, rec) in enumerate(play_times):
        margin = rec.home_score - rec.away_score
        specs = _pick_spread_specs_both_sides(margin, spread_specs)
        if not specs:
            continue

        time_to_next_ab, time_to_next_pitch, clean_cutoff = _compute_gaps(rec, play_ts, play_times, idx)
        entry_ts = play_ts - _entry_offset()

        for spec in specs:
            trades = spread_trades.get(spec.ticker)
            if not trades:
                continue

            sign = -1.0 if spec.flip else 1.0
            price_at = _find_price_at(trades, entry_ts)
            if price_at is None:
                continue

            predicted = rec.model_deltas.get(spec.delta_key, 0)

            result = _build_synced_record(
                rec, play_ts, entry_ts, price_at, predicted, sign, spec,
                trades, clean_cutoff, time_to_next_ab, time_to_next_pitch,
                market_type_override="spread",
                market_label_override=f"SPR {spec.label.split()[-1]} (dynamic)",
            )
            if result:
                synced.append(result)

    return synced
