"""
Market selector: discovers Kalshi markets for MLB games and computes
the best trade (market + side) for each batting event based on EV.

EV = alpha * |delta| - spread

where delta is the win expectancy shift from engine.py and spread is
the bid-ask spread on the chosen side of the Kalshi contract.

Supports three market types:
  - Moneyline (KXMLBGAME): trade the home-team-wins contract
  - Over/Under (KXMLBTOTAL): dynamically pick the line closest to
    current total runs + 0.5
  - Spread (KXMLBSPREAD): dynamically pick the line closest above
    the current score margin
"""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
# Ticker prefix must use the same "MLB day" that game_state.py used to
# fetch the schedule, otherwise a 7 PM PT game on UTC hosts gets a
# next-day prefix and never matches any Kalshi event. Single source of
# truth: game_state.mlb_today().
from app.game_state import mlb_today

from app.constants import (
    ALL_OU_LINES, ALL_SPREAD_LINES,
    DEFAULT_ALPHA, DEFAULT_BET_SIZE, DEFAULT_MAX_DOLLARS,
    BLOWOUT_THRESHOLD, STOP_LOSS_CENTS,
)
from app import database as db
from app.bet_label import compute_display_label
from app.engine import EventDelta
from app.fees import taker_fee, maker_fee
from app.kalshi_client import kalshi
from app.line_selection import pick_ou_line, pick_spread_line

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event loop reference (set from main.py lifespan so the websocket thread
# can push into asyncio queues via call_soon_threadsafe)
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop

# ---------------------------------------------------------------------------
# Settings
#
# DEFAULT_SETTINGS is the frozen fallback — a brand-new session's settings
# start as a copy of this. The runtime authority is Session.settings
# (in app/trader.py); this module no longer holds a mutable global.
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    "alpha": DEFAULT_ALPHA,
    "bet_size": DEFAULT_BET_SIZE,
    "max_dollars": DEFAULT_MAX_DOLLARS,
    "max_slippage_cents": 1,
    "use_undo_window": True,
    "use_stop_loss": True,
    "stop_loss_cents": STOP_LOSS_CENTS,
    "dry_run": True,
    "blowout_filter": True,
    # Multi-market: one tap fires every positive-EV market for the event.
    # Budget is split proportionally to EV across the basket.
    "multi_market": False,
    # Minimum expected entry→target move (cents) for a trade to clear
    # Kalshi round-trip fees. Filters at the eval layer so sub-threshold
    # trades never reach the button grid.
    "min_move_cents": 4,
}


def clamp_settings(update: dict) -> dict:
    """Coerce / range-clamp a partial settings update. Returns only the keys
    present in `update` so callers can merge into a session's dict cleanly.
    Unknown keys are dropped."""
    out: dict = {}
    if "alpha" in update and update["alpha"] is not None:
        out["alpha"] = max(0.0, min(1.0, float(update["alpha"])))
    if "bet_size" in update and update["bet_size"] is not None:
        out["bet_size"] = max(1, int(update["bet_size"]))
    if "max_dollars" in update and update["max_dollars"] is not None:
        out["max_dollars"] = max(1.0, float(update["max_dollars"]))
    if "max_slippage_cents" in update and update["max_slippage_cents"] is not None:
        out["max_slippage_cents"] = max(0, int(update["max_slippage_cents"]))
    if "use_undo_window" in update and update["use_undo_window"] is not None:
        out["use_undo_window"] = bool(update["use_undo_window"])
    if "use_stop_loss" in update and update["use_stop_loss"] is not None:
        out["use_stop_loss"] = bool(update["use_stop_loss"])
    if "stop_loss_cents" in update and update["stop_loss_cents"] is not None:
        out["stop_loss_cents"] = max(1, int(update["stop_loss_cents"]))
    if "dry_run" in update and update["dry_run"] is not None:
        out["dry_run"] = bool(update["dry_run"])
    if "blowout_filter" in update and update["blowout_filter"] is not None:
        out["blowout_filter"] = bool(update["blowout_filter"])
    if "min_move_cents" in update and update["min_move_cents"] is not None:
        out["min_move_cents"] = max(0, int(update["min_move_cents"]))
    if "multi_market" in update and update["multi_market"] is not None:
        out["multi_market"] = bool(update["multi_market"])
    return out


# ---------------------------------------------------------------------------
# Structured market info
# ---------------------------------------------------------------------------

@dataclass
class MarketInfo:
    """A single Kalshi market with its type, line, and ticker."""
    ticker: str
    market_type: str  # "moneyline", "over_under", "spread"
    line: float | None  # e.g. 8.5 for O/U, 3.5 or -2.5 for spread
    delta_key: str  # key into EventDelta.over_under / .spread dicts
    flip: bool  # True if delta needs to be negated (away-side spread)
    label: str  # human-readable label
    title: str = ""


# ---------------------------------------------------------------------------
# Caches  (all guarded by their own locks)
# ---------------------------------------------------------------------------

# game_id -> list of MarketInfo (structured, all market types)
_game_markets: dict[int, list[MarketInfo]] = {}
_market_lock = threading.Lock()

# game_ids already discovered (skip re-discovery API calls)
_discovered_games: set[int] = set()

# game_id -> last-known EventDelta list (for background recompute on tick)
_game_deltas: dict[int, list[EventDelta]] = {}
_delta_lock = threading.Lock()

# Last logged compute_best_trades signature per game, so per-subscriber
# SSE recomputes don't re-print the same delta table every ~2s. The
# signature covers the deltas + market lines + scores; any real change
# yields a new signature and a new log line.
_last_compute_sig: dict[int, str] = {}
_last_compute_sig_lock = threading.Lock()

# game_id -> (home_score, away_score) for dynamic line selection on recompute
_game_scores: dict[int, tuple[int, int]] = {}
_score_lock = threading.Lock()

# game_id -> (home_abbr, away_abbr) so trade dicts can carry team info
# for the frontend's NO-side label flip (SEA -4.5 NO → "HOU +4.5").
_game_teams: dict[int, tuple[str, str]] = {}
_teams_lock = threading.Lock()

# game_id -> { event: trade_info_dict }
_trade_cache: dict[int, dict] = {}
_cache_lock = threading.Lock()

# Throttle for periodic market snapshots. compute_best_trades runs on every
# price tick (~5 Hz); writing a snapshot every call would flood Supabase.
# One entry per game so multi-game sessions don't starve each other.
_SNAPSHOT_INTERVAL_SECONDS = 30.0
_last_snapshot_time: dict[int, float] = {}
_snapshot_time_lock = threading.Lock()


def set_game_deltas(game_id: int, deltas: list[EventDelta]):
    """Store the latest engine deltas for a game (called from main.py)."""
    with _delta_lock:
        _game_deltas[game_id] = deltas


def set_game_scores(game_id: int, home_score: int, away_score: int):
    """Store the latest scores for a game (for dynamic line selection)."""
    with _score_lock:
        _game_scores[game_id] = (home_score, away_score)


def get_cached_trades(game_id: int) -> dict | None:
    """Return pre-computed best trades from cache, or None."""
    with _cache_lock:
        return _trade_cache.get(game_id)


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def _search_event_ticker(home_abbr: str, away_abbr: str) -> str | None:
    """
    Try Kalshi ticker patterns for an MLB game.

    Real format: KXMLBGAME-{YY}{MON}{DD}{HHMM}{AWAY}{HOME}
    """
    if not kalshi.is_connected:
        logger.warning("_search_event_ticker: Kalshi not connected — aborting")
        return None

    today = mlb_today()
    date_prefix = today.strftime("%y%b%d").upper()
    home_u = home_abbr.upper()
    away_u = away_abbr.upper()

    logger.info(
        f"_search_event_ticker: searching KXMLBGAME for "
        f"home={home_u} away={away_u} date_prefix={date_prefix} "
        f"(pacific date={today.isoformat()})"
    )

    # --- Strategy 1: list KXMLBGAME events + filter -----------------------
    try:
        # fetch_all=True so we don't miss late-day games if Kalshi returns
        # multi-day results that push today's slate past the page cap.
        events = list(kalshi.client.get_events(
            series_ticker="KXMLBGAME",
            fetch_all=True,
        ))
    except Exception as e:
        logger.error(f"_search_event_ticker: get_events failed: {e}")
        events = []

    all_tickers = [getattr(ev, "event_ticker", "") for ev in events]
    logger.info(
        f"_search_event_ticker: Kalshi returned {len(events)} events for KXMLBGAME; "
        f"sample: {all_tickers[:10]}"
    )

    # How many events match just the date (useful to distinguish a TZ bug
    # from a teams-not-matched bug).
    date_matches = [t for t in all_tickers if date_prefix in t]
    logger.info(
        f"_search_event_ticker: {len(date_matches)} events match date_prefix={date_prefix}; "
        f"sample: {date_matches[:10]}"
    )

    for ticker in all_tickers:
        if date_prefix not in ticker:
            continue
        tu = ticker.upper()
        if away_u in tu and home_u in tu:
            logger.info(f"_search_event_ticker: matched {ticker} (strategy=list)")
            return ticker

    logger.warning(
        f"_search_event_ticker: no list-match for home={home_u} away={away_u} "
        f"date_prefix={date_prefix}; falling back to brute-force time scan"
    )

    # --- Strategy 2: brute-force common start-time slots ------------------
    candidates = []
    for hh in range(12, 24):
        for mm in ["00", "05", "10", "15", "20", "30", "35", "40", "45"]:
            candidates.append(
                f"KXMLBGAME-{date_prefix}{hh:02d}{mm}{away_u}{home_u}"
            )

    for ticker in candidates:
        try:
            markets = kalshi.get_markets(ticker)
            if markets:
                logger.info(f"_search_event_ticker: matched {ticker} (strategy=brute)")
                return ticker
        except Exception:
            continue

    logger.warning(
        f"_search_event_ticker: no match found after brute-force "
        f"(home={home_u} away={away_u} date_prefix={date_prefix})"
    )
    return None


def discover_markets(
    game_id: int,
    home_abbr: str,
    away_abbr: str,
    event_ticker: str | None = None,
) -> list[MarketInfo]:
    """
    Find all Kalshi markets for a game: moneyline + O/U + spread.
    Subscribes to websocket for each market.
    """
    if not kalshi.is_connected:
        return []

    # Skip if already discovered — avoids redundant API calls on every refresh
    if game_id in _discovered_games:
        with _market_lock:
            return list(_game_markets.get(game_id, []))

    if not event_ticker:
        event_ticker = _search_event_ticker(home_abbr, away_abbr)

    if not event_ticker:
        logger.warning(f"No Kalshi event found for game {game_id}")
        return []

    suffix = event_ticker.split("-", 1)[1]
    home = home_abbr.upper()
    away = away_abbr.upper()

    infos: list[MarketInfo] = []

    # --- Moneyline ---
    # Each game has TWO moneyline contracts: one per team. The home contract's
    # YES = home wins, the away contract's YES = away wins. These are mirror
    # markets — same economic bet, different liquidity. We evaluate both and
    # let compute_best_trades pick the one with best EV after spread cost.
    ml_markets = kalshi.get_markets(event_ticker)
    for m in ml_markets:
        ticker = m["ticker"]
        ticker_upper = ticker.upper()

        if ticker_upper.endswith(f"-{home}"):
            # Home contract: YES = home wins, delta aligned with model
            infos.append(MarketInfo(
                ticker=ticker, market_type="moneyline", line=None,
                delta_key="ml", flip=False, label=f"ML {home}",
                title=m.get("title", ""),
            ))
        elif ticker_upper.endswith(f"-{away}"):
            # Away contract: YES = away wins, model delta must be inverted
            infos.append(MarketInfo(
                ticker=ticker, market_type="moneyline", line=None,
                delta_key="ml", flip=True, label=f"ML {away}",
                title=m.get("title", ""),
            ))

        kalshi.subscribe(ticker)

    # --- Over/Under ---
    ou_event = f"KXMLBTOTAL-{suffix}"
    try:
        ou_markets = kalshi.get_markets(ou_event)
        for m in ou_markets:
            ticker = m["ticker"]
            n_str = ticker.rsplit("-", 1)[-1]
            try:
                n = int(n_str)
            except ValueError:
                continue
            line = n - 0.5
            infos.append(MarketInfo(
                ticker=ticker, market_type="over_under", line=line,
                delta_key=f"ou_{line}", flip=False, label=f"O/U {line}",
                title=m.get("title", ""),
            ))
            kalshi.subscribe(ticker)
    except Exception as e:
        logger.debug(f"O/U discovery failed for {ou_event}: {e}")

    # --- Spread ---
    sp_event = f"KXMLBSPREAD-{suffix}"
    try:
        sp_markets = kalshi.get_markets(sp_event)
        for m in sp_markets:
            ticker = m["ticker"]
            mkt_suffix = ticker.rsplit("-", 1)[-1].upper()
            for abbr, is_away in [(home, False), (away, True)]:
                if mkt_suffix.startswith(abbr):
                    n_str = mkt_suffix[len(abbr):]
                    try:
                        n = int(n_str)
                    except ValueError:
                        continue
                    line = n - 0.5
                    if is_away:
                        delta_key = f"sp_{-line}"
                        label = f"SPR {abbr} -{line}"
                    else:
                        delta_key = f"sp_{line}"
                        label = f"SPR {abbr} -{line}"
                    infos.append(MarketInfo(
                        ticker=ticker, market_type="spread",
                        line=line if not is_away else -line,
                        delta_key=delta_key, flip=is_away,
                        label=label, title=m.get("title", ""),
                    ))
                    kalshi.subscribe(ticker)
                    break
    except Exception as e:
        logger.debug(f"Spread discovery failed for {sp_event}: {e}")

    logger.info(
        f"Discovered {len(infos)} markets for game {game_id}: "
        f"{sum(1 for m in infos if m.market_type == 'moneyline')} ML, "
        f"{sum(1 for m in infos if m.market_type == 'over_under')} O/U, "
        f"{sum(1 for m in infos if m.market_type == 'spread')} SPR"
    )

    with _market_lock:
        _game_markets[game_id] = infos
    with _teams_lock:
        _game_teams[game_id] = (home_abbr.upper(), away_abbr.upper())
    _discovered_games.add(game_id)

    return infos


# ---------------------------------------------------------------------------
# Dynamic line selection (delegates to shared app/line_selection.py)
# ---------------------------------------------------------------------------

def _pick_ou_market(total_runs: int, markets: list[MarketInfo]) -> MarketInfo | None:
    ou_markets = [m for m in markets if m.market_type == "over_under"]
    return pick_ou_line(total_runs, ou_markets)


def _pick_spread_markets_both_sides(
    margin: int, markets: list[MarketInfo]
) -> list[MarketInfo]:
    sp = [m for m in markets if m.market_type == "spread"]
    home_side = pick_spread_line(margin, [m for m in sp if not m.flip])
    away_side = pick_spread_line(margin, [m for m in sp if m.flip])
    return [m for m in (home_side, away_side) if m is not None]


# ---------------------------------------------------------------------------
# EV computation
# ---------------------------------------------------------------------------

def _empty_trade(event: str, bet_size: int) -> dict:
    return {
        "event": event,
        "market_ticker": None,
        "market_title": None,
        "market_type": None,
        "side": None,
        "entry_price": 0,
        "sell_target": 0,
        "spread": 0,
        "ev_per_contract": 0,
        "quantity": bet_size,
        "estimated_cost": 0,
        "estimated_profit": 0,
        "active": False,
    }


def _evaluate_market(
    market: MarketInfo,
    delta_value: float,
    alpha: float,
    bet_size: int,
    event: str | None = None,
    home_abbr: str = "",
    away_abbr: str = "",
    min_move_cents: int = 0,
) -> dict | None:
    """Evaluate a single market for one event. Returns trade info or None."""
    prices = kalshi.get_prices(market.ticker)

    yes_ask = prices["yes_ask"]
    yes_bid = prices["yes_bid"]
    no_ask = prices["no_ask"]
    no_bid = prices["no_bid"]

    # For flipped markets (away-side spread), the delta sign is inverted:
    # a positive delta for the model means YES on the home side, but NO on
    # the away-side contract. The flip flag handles this.
    d = -delta_value if market.flip else delta_value

    yes_label = compute_display_label(
        market_ticker=market.ticker, side="YES", market_type=market.market_type,
        home_abbr=home_abbr, away_abbr=away_abbr,
    )
    no_label = compute_display_label(
        market_ticker=market.ticker, side="NO", market_type=market.market_type,
        home_abbr=home_abbr, away_abbr=away_abbr,
    )
    prefix = f"eval[{event or '-'}][{market.label}]"
    logger.info(
        f"{prefix} delta_in={delta_value:+.4f} d_eff={d:+.4f} flip={market.flip} "
        f"yes_bid={yes_bid} yes_ask={yes_ask} no_bid={no_bid} no_ask={no_ask}"
    )

    best = None
    yes_reason = "skipped: d<=0"
    no_reason = "skipped: d>=0"

    # Dead-market gates. Any side hitting these is a guaranteed loss or no
    # upside — skip it entirely so it never lands in all_trades for
    # multi-market mode to pick.
    #
    #   entry >= 0.95           — ceiling at $1; basically zero upside after fees
    #   entry <= 0.05           — floor at $0; same deal from the other side
    #   target <= entry + 0.01  — need at least 1¢ profit to clear fees and
    #                             ensure the trade isn't a guaranteed loss
    #                             after price moves between eval and fill.
    def _is_dead(entry: float, target: float) -> bool:
        return entry >= 0.95 or entry <= 0.05 or target <= entry + 0.01

    # YES side: profitable when delta > 0
    if d > 0 and yes_ask > 0 and yes_bid > 0:
        spread = yes_ask - yes_bid
        ev = alpha * d - spread
        sell_target = min(yes_ask + alpha * d, 0.99)
        if _is_dead(yes_ask, sell_target):
            yes_reason = (
                f"dead: entry={yes_ask:.2f} target={sell_target:.2f} "
                f"ev={ev:+.4f} spread_cost={spread:.4f}"
            )
        else:
            profit_per = sell_target - yes_ask
            best = {
                "side": "YES",
                "entry_price": round(yes_ask, 2),
                "sell_target": round(sell_target, 2),
                "spread": round(spread, 4),
                "ev_per_contract": round(ev, 4),
                "profit_per": profit_per,
            }
            yes_reason = (
                f"kept: entry={yes_ask:.2f} target={sell_target:.2f} "
                f"ev={ev:+.4f} spread_cost={spread:.4f}"
            )
    elif d > 0:
        yes_reason = f"skipped: no yes prices (bid={yes_bid} ask={yes_ask})"

    # NO side: profitable when delta < 0
    if d < 0 and no_ask > 0 and no_bid > 0:
        spread = no_ask - no_bid
        abs_d = abs(d)
        ev = alpha * abs_d - spread
        sell_target = min(no_ask + alpha * abs_d, 0.99)
        if _is_dead(no_ask, sell_target):
            no_reason = (
                f"dead: entry={no_ask:.2f} target={sell_target:.2f} "
                f"ev={ev:+.4f} spread_cost={spread:.4f}"
            )
        else:
            profit_per = sell_target - no_ask
            if best is None or ev > best["ev_per_contract"]:
                best = {
                    "side": "NO",
                    "entry_price": round(no_ask, 2),
                    "sell_target": round(sell_target, 2),
                    "spread": round(spread, 4),
                    "ev_per_contract": round(ev, 4),
                    "profit_per": profit_per,
                }
            no_reason = (
                f"kept: entry={no_ask:.2f} target={sell_target:.2f} "
                f"ev={ev:+.4f} spread_cost={spread:.4f}"
            )
    elif d < 0:
        no_reason = f"skipped: no no prices (bid={no_bid} ask={no_ask})"

    logger.info(f"{prefix}   YES ({yes_label}) {yes_reason}")
    logger.info(f"{prefix}   NO  ({no_label}) {no_reason}")

    if best is None:
        logger.info(f"{prefix} → no trade")
        return None
    # Round-trip fees eat profit on sub-threshold moves. Filter here so
    # the trade never reaches the button grid.
    move_cents = round(abs(best["sell_target"] - best["entry_price"]) * 100)
    if min_move_cents > 0 and move_cents < min_move_cents:
        picked_label = yes_label if best["side"] == "YES" else no_label
        logger.info(
            f"{prefix} → below min_move {picked_label} ({best['side']}) "
            f"move={move_cents}¢ < {min_move_cents}¢"
        )
        return None
    # Fee-aware net expected profit. Entry is Taker (we hit the book); exit is
    # Maker (our sell target is a resting limit). `bet_size` is contracts, not
    # dollars — see constants.DEFAULT_BET_SIZE.
    entry_fee = taker_fee(contracts=bet_size, price_dollars=best["entry_price"])
    exit_fee = maker_fee(contracts=bet_size, price_dollars=best["sell_target"])
    net_expected_profit = (
        (best["sell_target"] - best["entry_price"]) * bet_size - entry_fee - exit_fee
    )

    picked_label = yes_label if best["side"] == "YES" else no_label
    logger.info(
        f"{prefix} → trade {picked_label} ({best['side']}) "
        f"entry={best['entry_price']} target={best['sell_target']} "
        f"ev={best['ev_per_contract']:+.4f} "
        f"net_ev={net_expected_profit:+.4f} "
        f"fees={entry_fee:.3f}+{exit_fee:.3f}"
    )

    return {
        "event": None,  # filled by caller
        "market_ticker": market.ticker,
        "market_title": market.title or market.label,
        "market_type": market.market_type,
        "side": best["side"],
        "entry_price": best["entry_price"],
        "sell_target": best["sell_target"],
        "spread": best["spread"],
        "ev_per_contract": best["ev_per_contract"],
        "quantity": bet_size,
        "estimated_cost": round(best["entry_price"] * bet_size, 2),
        "estimated_profit": round(best["profit_per"] * bet_size, 2),
        "entry_fee_est": round(entry_fee, 4),
        "exit_fee_est": round(exit_fee, 4),
        "net_expected_profit": round(net_expected_profit, 4),
    }


def compute_best_trades(
    game_id: int,
    deltas: list[EventDelta],
    home_score: int | None = None,
    away_score: int | None = None,
    settings: dict | None = None,
) -> dict:
    """
    For each event, find the market+side with highest EV across moneyline,
    O/U, and spread markets. Dynamically picks O/U and spread lines based
    on current scores.

    `settings` overrides the lookup path. When None, we pull from the first
    session attached to this game (there's no user context on the SSE tick
    thread, so we pick deterministically). When no session exists yet,
    DEFAULT_SETTINGS is used — which is what the /trades endpoint hits
    when the user hasn't opened the game's stream yet.

    Returns dict keyed by event name -> trade info.
    """
    if settings is None:
        # Late import: trader imports market_selector at module load, so we
        # can't reference the session registry at the top of this file.
        from app.trader import get_first_session_for_game
        s = get_first_session_for_game(game_id)
        settings = dict(s.settings) if s else dict(DEFAULT_SETTINGS)

    alpha = settings.get("alpha", DEFAULT_SETTINGS["alpha"])
    bet_size = settings.get("bet_size", DEFAULT_SETTINGS["bet_size"])
    blowout_filter = settings.get("blowout_filter", DEFAULT_SETTINGS["blowout_filter"])
    min_move_cents = int(settings.get("min_move_cents", DEFAULT_SETTINGS["min_move_cents"]))

    with _market_lock:
        markets = list(_game_markets.get(game_id, []))
    with _teams_lock:
        home_abbr, away_abbr = _game_teams.get(game_id, ("", ""))

    # If scores not passed, try cached scores
    if home_score is None or away_score is None:
        with _score_lock:
            cached_scores = _game_scores.get(game_id)
        if cached_scores:
            home_score, away_score = cached_scores
        else:
            home_score, away_score = 0, 0

    total_runs = home_score + away_score
    margin = home_score - away_score
    is_blowout = abs(margin) >= BLOWOUT_THRESHOLD

    # Dynamically pick O/U and spread markets
    ou_market = _pick_ou_market(total_runs, markets)
    sp_markets_picked = _pick_spread_markets_both_sides(margin, markets)
    ml_markets = [m for m in markets if m.market_type == "moneyline"]

    sp_candidates = sorted(
        (m.line for m in markets if m.market_type == "spread"),
        key=lambda x: x,
    )
    sp_picks_labels = [m.label for m in sp_markets_picked]
    logger.info(
        f"Game {game_id}: score {away_score}-{home_score} "
        f"margin={margin:+d} total={total_runs} "
        f"ou_pick={ou_market.label if ou_market else None} "
        f"sp_picks={sp_picks_labels} "
        f"sp_lines={sp_candidates}"
    )

    # Per-event delta table — useful for debugging, but fires once per
    # subscriber per recompute (every ~2s). Dedupe on a signature of
    # (deltas, lines, scores) so we only print when something changed.
    ou_key = str(ou_market.line) if ou_market else None
    sp_keys = [str(m.line) for m in sp_markets_picked]
    sig_parts: list[str] = [
        f"ou={ou_key}", f"sp={','.join(sp_keys)}",
        f"hs={home_score}", f"as={away_score}",
    ]
    for d in deltas:
        ou_d = d.over_under.get(ou_key, {}).get("delta") if ou_key else None
        sp_ds = [d.spread.get(k, {}).get("delta") for k in sp_keys]
        sig_parts.append(f"{d.event}:{d.delta:+.4f}:{ou_d}:{sp_ds}")
    sig = "|".join(sig_parts)

    with _last_compute_sig_lock:
        prev_sig = _last_compute_sig.get(game_id)
        changed = sig != prev_sig
        if changed:
            _last_compute_sig[game_id] = sig

    if changed:
        logger.info(
            f"compute_best_trades game={game_id} "
            f"ou_line={ou_key} sp_lines={sp_keys} "
            f"ml_markets={len(ml_markets)} total_runs={total_runs} margin={margin}"
        )
        for d in deltas:
            ou_delta = d.over_under.get(ou_key, {}).get("delta") if ou_key else None
            sp_deltas = {k: d.spread.get(k, {}).get("delta") for k in sp_keys}
            sp_delta_str = ", ".join(
                f"{k}={'None' if v is None else f'{v:+.4f}'}"
                for k, v in sp_deltas.items()
            ) or "None"
            logger.info(
                f"  event={d.event:<3} ml_delta={d.delta:+.4f} "
                f"ou_delta={ou_delta if ou_delta is None else f'{ou_delta:+.4f}'} "
                f"sp_deltas=[{sp_delta_str}]"
            )

    result: dict[str, dict] = {}
    _seen_trade_ids: dict[int, str] = {}   # probes accidental dict sharing

    for d in deltas:
        # Collect every viable candidate (any side, any market) for this event.
        # We keep them all so multi-market mode can fire the full positive-EV
        # basket; single-market mode just picks the winner.
        candidates: list[dict] = []

        # --- Moneyline candidates ---
        if not (blowout_filter and is_blowout):
            for ml in ml_markets:
                trade = _evaluate_market(ml, d.delta, alpha, bet_size, event=d.event, home_abbr=home_abbr, away_abbr=away_abbr, min_move_cents=min_move_cents)
                if trade:
                    candidates.append(trade)

        # --- O/U candidate ---
        if ou_market:
            ou_delta_data = d.over_under.get(str(ou_market.line))
            if ou_delta_data:
                trade = _evaluate_market(ou_market, ou_delta_data["delta"], alpha, bet_size, event=d.event, home_abbr=home_abbr, away_abbr=away_abbr, min_move_cents=min_move_cents)
                if trade:
                    candidates.append(trade)

        # --- Spread candidates (home-side + away-side) ---
        for sp_market in sp_markets_picked:
            sp_delta_data = d.spread.get(str(sp_market.line))
            if sp_delta_data:
                trade = _evaluate_market(sp_market, sp_delta_data["delta"], alpha, bet_size, event=d.event, home_abbr=home_abbr, away_abbr=away_abbr, min_move_cents=min_move_cents)
                if trade:
                    candidates.append(trade)

        if candidates:
            # Two sorts: the old gross-EV rule and the new net-EV rule. We pick
            # by net EV but remember the gross pick so we can flag trades where
            # fee-awareness changed the choice.
            gross_sorted = sorted(
                candidates, key=lambda t: t["ev_per_contract"], reverse=True
            )
            net_sorted = sorted(
                candidates, key=lambda t: t["net_expected_profit"], reverse=True
            )
            gross_pick = gross_sorted[0]
            net_pick = net_sorted[0]
            fee_adjusted = (
                gross_pick["market_ticker"] != net_pick["market_ticker"]
                or gross_pick["side"] != net_pick["side"]
            )

            best_trade = dict(net_pick)
            best_trade["event"] = d.event
            best_trade["active"] = best_trade["net_expected_profit"] > 0
            best_trade["fee_adjusted"] = fee_adjusted
            best_trade["gross_pick_ticker"] = (
                gross_pick["market_ticker"] if fee_adjusted else None
            )
            best_trade["home_abbr"] = home_abbr
            best_trade["away_abbr"] = away_abbr
            best_trade["display_label"] = compute_display_label(
                market_ticker=best_trade["market_ticker"],
                side=best_trade["side"],
                market_type=best_trade["market_type"],
                home_abbr=home_abbr,
                away_abbr=away_abbr,
            )
            if fee_adjusted:
                logger.info(
                    f"compute_best_trades game={game_id} event={d.event} "
                    f"fee_adjusted=True: net_pick={net_pick['market_ticker']}/{net_pick['side']} "
                    f"(net_ev={net_pick['net_expected_profit']:+.4f}) "
                    f"was gross_pick={gross_pick['market_ticker']}/{gross_pick['side']} "
                    f"(ev={gross_pick['ev_per_contract']:+.4f}, "
                    f"net_ev={gross_pick['net_expected_profit']:+.4f})"
                )
            # all_trades is the full positive-net-EV basket. Frontend ignores
            # it in single-market mode; backend reads it in multi mode.
            best_trade["all_trades"] = [
                {
                    **c,
                    "event": d.event,
                    "home_abbr": home_abbr,
                    "away_abbr": away_abbr,
                    "display_label": compute_display_label(
                        market_ticker=c["market_ticker"],
                        side=c["side"],
                        market_type=c["market_type"],
                        home_abbr=home_abbr,
                        away_abbr=away_abbr,
                    ),
                }
                for c in candidates
                if c["net_expected_profit"] > 0
            ]
            result[d.event] = best_trade

            # --- DIAGNOSTIC ------------------------------------------------
            # Confirm the picked trade per event and detect accidental dict
            # sharing across events (id() collisions = same object).
            prev_event = _seen_trade_ids.get(id(best_trade))
            if prev_event:
                logger.error(
                    f"  SHARED best_trade dict between {prev_event} and {d.event} "
                    f"— this is a bug"
                )
            _seen_trade_ids[id(best_trade)] = d.event
            logger.info(
                f"  picked {d.event:<3} → {best_trade['market_type']:<10} "
                f"{best_trade.get('display_label', '')} ({best_trade['side']}) "
                f"{best_trade['market_ticker']} "
                f"entry={best_trade['entry_price']:.2f} "
                f"target={best_trade['sell_target']:.2f} "
                f"ev={best_trade['ev_per_contract']:+.4f} "
                f"(candidates={len(candidates)})"
            )
            # -------------------------------------------------------------
        else:
            empty = _empty_trade(d.event, bet_size)
            empty["all_trades"] = []
            result[d.event] = empty

    # Update cache
    with _cache_lock:
        _trade_cache[game_id] = result

    # Periodic throttled snapshot — gives us 30 s continuous market data
    # without flooding Supabase on every tick. Trade-time snapshots
    # (called from execute_trade) remain for exact-at-decision rows.
    _maybe_snapshot(game_id, markets)

    return result


def _maybe_snapshot(game_id: int, markets: list["MarketInfo"]) -> None:
    """Write a snapshot for this game iff >= 30 s since the last one."""
    if not markets:
        return
    import time
    now = time.monotonic()
    with _snapshot_time_lock:
        last = _last_snapshot_time.get(game_id, 0.0)
        if (now - last) < _SNAPSHOT_INTERVAL_SECONDS:
            return
        _last_snapshot_time[game_id] = now

    rows: list[dict] = []
    for m in markets:
        try:
            prices = kalshi.get_prices(m.ticker)
            rows.append({
                "market_ticker": m.ticker,
                "yes_bid": prices.get("yes_bid"),
                "yes_ask": prices.get("yes_ask"),
                "market_type": m.market_type,
            })
        except Exception:
            continue
    if not rows:
        return
    t = threading.Thread(
        target=db.log_market_snapshots,
        args=(game_id, rows),
        daemon=True,
    )
    t.start()


def snapshot_markets_for_game(game_id: int) -> None:
    """
    Snapshot the current orderbook of every market for a game.

    Called from trader.execute_trade AFTER a buy fills, so each row in
    market_snapshots represents the board state at a trading decision
    point — entry prices, spreads, and the alternatives the user didn't
    pick. Roughly 20 rows per trade, not per tick.

    Writes run on a daemon thread so Supabase latency never blocks the
    caller (which is on the /buy request path).
    """
    with _market_lock:
        markets = list(_game_markets.get(game_id, []))
    if not markets:
        return
    rows: list[dict] = []
    for m in markets:
        try:
            prices = kalshi.get_prices(m.ticker)
            rows.append({
                "market_ticker": m.ticker,
                "yes_bid": prices.get("yes_bid"),
                "yes_ask": prices.get("yes_ask"),
                "market_type": m.market_type,
            })
        except Exception:
            continue
    if not rows:
        return
    t = threading.Thread(
        target=db.log_market_snapshots,
        args=(game_id, rows),
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Websocket callback — recompute on price changes (with tick coalescing)
# ---------------------------------------------------------------------------
#
# Kalshi can fire many ticks per second across all subscribed markets for a
# game. Instead of recomputing + pushing SSE on every single tick, we batch
# ticks within a 200ms window: mark affected game_ids as "pending", schedule
# a single flush on the event loop, and when the flush fires we do one
# recompute + one SSE push per game. This caps the user-visible update rate
# at 5/sec and collapses duplicate work.

_COALESCE_SECONDS = 0.2

_pending_lock = threading.Lock()
_pending_recompute: set[int] = set()
_flush_scheduled = False


def on_price_update(market_ticker: str, _prices: dict):
    """Called on every websocket tick. Queues affected games for coalesced flush."""
    with _market_lock:
        affected = [
            gid
            for gid, markets in _game_markets.items()
            if any(m.ticker == market_ticker for m in markets)
        ]

    if not affected:
        return

    global _flush_scheduled
    with _pending_lock:
        for gid in affected:
            _pending_recompute.add(gid)
        should_schedule = not _flush_scheduled
        if should_schedule:
            _flush_scheduled = True

    # Schedule the flush on the event loop (we're on the Kalshi WS thread).
    if should_schedule and _loop is not None:
        _loop.call_soon_threadsafe(_arm_flush_timer)


def _arm_flush_timer():
    """Runs on the event loop thread. Arms the delayed flush."""
    if _loop is not None:
        _loop.call_later(_COALESCE_SECONDS, _flush_pending)


def _flush_pending():
    """Fan out per-subscriber trade recomputes for each pending game.
    compute_best_trades is now settings-sensitive, so _notify_sse handles
    the per-user loop (with a settings-keyed cache to avoid redundant
    compute when subscribers share settings)."""
    global _flush_scheduled
    with _pending_lock:
        games = list(_pending_recompute)
        _pending_recompute.clear()
        _flush_scheduled = False

    for gid in games:
        try:
            _notify_sse(gid)
        except Exception as e:
            logger.error(f"Flush failed for game {gid}: {e}")


# ---------------------------------------------------------------------------
# SSE (server-sent events) pub/sub
#
# Subscribers carry user_id so per-user trade views can be computed from
# their Session's settings. A subscriber without a user_id (legacy path)
# falls back to DEFAULT_SETTINGS.
# ---------------------------------------------------------------------------

# Each value is a list of (queue, user_id_or_None) tuples.
_sse_queues: dict[int, list[tuple[asyncio.Queue, str | None]]] = {}
_sse_lock = threading.Lock()


def subscribe_sse(game_id: int, user_id: str | None = None) -> asyncio.Queue:
    """Register a new SSE subscriber. Attach user_id so flushes can compute
    trades against that user's session settings."""
    q: asyncio.Queue = asyncio.Queue()
    with _sse_lock:
        _sse_queues.setdefault(game_id, []).append((q, user_id))
    return q


def unsubscribe_sse(game_id: int, q: asyncio.Queue):
    """Remove an SSE subscriber by queue."""
    with _sse_lock:
        subs = _sse_queues.get(game_id, [])
        _sse_queues[game_id] = [(sq, uid) for (sq, uid) in subs if sq is not q]


def sse_subscriber_count(game_id: int) -> int:
    """Return the number of active SSE subscribers for a game."""
    with _sse_lock:
        return len(_sse_queues.get(game_id, []))


def notify_sse_game_state(game_id: int, game_state: dict):
    """Push a game_state update to all SSE subscribers (user-agnostic)."""
    _push_sse(game_id, {"type": "game_state", "game_state": game_state})


def notify_sse_positions_changed(game_id: int, user_id: str | None = None) -> None:
    """Tell connected clients that positions for this (game, user) changed.

    Used on terminal-state transitions (filled / expired / stopped /
    canceled) so the UI updates instantly instead of waiting for the
    next 60s /positions poll. Scoped to `user_id` when provided so
    we don't wake up every viewer of the game for one user's fill.
    Omitting user_id broadcasts to everyone on the game (used when the
    target user isn't known, e.g. batch closes).
    """
    if _loop is None:
        return
    payload = {"type": "positions_update", "game_id": game_id}
    with _sse_lock:
        subs = list(_sse_queues.get(game_id, []))
    for queue, uid in subs:
        if user_id is not None and uid != user_id:
            continue
        _loop.call_soon_threadsafe(queue.put_nowait, payload)


def _notify_sse(game_id: int):
    """Recompute trades **per subscriber** using their session settings,
    and push over each subscriber's queue. Subscribers with identical
    settings share one compute via the per-call cache — the common case
    (one user, friends on the same alpha) computes exactly once.

    position_prices is user-agnostic (same live bid for every watcher)
    so it's computed once and reused across subscribers.
    """
    # Late imports avoid circular load with trader + DEFAULT_SETTINGS below.
    from app.trader import get_open_trades_for_game, get_session

    with _sse_lock:
        subs = list(_sse_queues.get(game_id, []))
    if not subs:
        return

    with _delta_lock:
        deltas = _game_deltas.get(game_id)
    if not deltas:
        return
    with _score_lock:
        scores = _game_scores.get(game_id, (0, 0))

    # Shared across all subscribers.
    position_prices: dict[str, dict] = {}
    for t in get_open_trades_for_game(game_id):
        try:
            prices = kalshi.get_prices(t.market_ticker)
            bid = prices["yes_bid"] if t.side == "YES" else prices["no_bid"]
            position_prices[t.id] = {"current_price": bid}
        except Exception:
            continue

    # Per-settings compute cache. Key is a frozenset of the settings items;
    # all our setting values (bool / int / float / str) are hashable.
    compute_cache: dict[frozenset, dict] = {}

    for queue, user_id in subs:
        session = get_session(user_id, game_id) if user_id else None
        settings = dict(session.settings) if session else dict(DEFAULT_SETTINGS)
        key = frozenset(settings.items())
        trades = compute_cache.get(key)
        if trades is None:
            try:
                trades = compute_best_trades(
                    game_id, deltas, scores[0], scores[1], settings=settings,
                )
            except Exception as e:
                logger.error(f"compute_best_trades failed in _notify_sse: {e}")
                continue
            compute_cache[key] = trades

        payload = {
            "type": "trades",
            "trades": trades,
            "position_prices": position_prices,
        }
        if _loop is not None:
            _loop.call_soon_threadsafe(queue.put_nowait, payload)


def _push_sse(game_id: int, payload: dict):
    """Broadcast a shared payload (game_state etc.) to every subscriber."""
    if _loop is None:
        return
    with _sse_lock:
        subs = list(_sse_queues.get(game_id, []))
    for queue, _user_id in subs:
        _loop.call_soon_threadsafe(queue.put_nowait, payload)
