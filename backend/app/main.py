"""
LiveLine API — MLB win expectancy engine + Kalshi trading.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import database as db
from app.auth import CurrentUser, get_current_user
from app.engine import GameState, compute_deltas, get_win_expectancy, ALL_OU_LINES, ALL_SPREAD_LINES
from app.game_state import get_game_state, get_todays_games
from app.kalshi_client import kalshi
from app.market_selector import (
    DEFAULT_SETTINGS,
    _evaluate_market,
    _notify_sse,
    _game_markets,
    _pick_ou_market,
    _pick_spread_market,
    compute_best_trades,
    discover_markets,
    get_cached_trades,
    notify_sse_game_state,
    on_price_update,
    set_event_loop,
    set_game_deltas,
    set_game_scores,
    sse_subscriber_count,
    subscribe_sse,
    unsubscribe_sse,
)
from app import trader
from app.trader import (
    Trade,
    TradeGroup,
    cancel_position,
    check_stop_losses,
    execute_trade,
    get_pnl,
    get_positions,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: start/stop Kalshi connection alongside the app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Store the running event loop so the websocket thread can push SSE events
    set_event_loop(asyncio.get_running_loop())

    try:
        kalshi.connect()
        kalshi.on_tick(on_price_update)
        kalshi.on_tick(check_stop_losses)
        logger.info("Kalshi integration active")
    except Exception as e:
        logger.warning(f"Kalshi connection failed (trading disabled): {e}")

    # Surface Supabase status at startup so it's easy to see the logging
    # layer is wired up (or not) without needing to trigger a trade.
    if db.is_available():
        logger.info("Supabase logging active")
    else:
        logger.warning("Supabase logging DISABLED — trades will execute but not be logged")

    yield

    kalshi.disconnect()


app = FastAPI(
    title="LiveLine",
    description="MLB win expectancy engine + Kalshi trading",
    lifespan=lifespan,
)

# Comma-separated list of allowed origins. Set CORS_ORIGINS in Railway/prod
# to the Vercel URL plus any custom domains, e.g.:
#   CORS_ORIGINS=https://liveline.vercel.app,https://liveline.app
cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===================================================================
# Health check — unauthenticated so Railway / load balancers can hit it
# ===================================================================

@app.get("/health")
async def health():
    return {"status": "ok"}


# ===================================================================
# Auth
# ===================================================================

@app.get("/auth/me")
async def auth_me(user: CurrentUser = Depends(get_current_user)):
    """Return the authenticated user."""
    return {"id": user.id, "email": user.email}


@app.post("/auth/logout", status_code=204)
async def auth_logout(user: CurrentUser = Depends(get_current_user)):
    """No-op server-side — Supabase session is cleared client-side.
    Kept for symmetry; returns 204 so the frontend can await it."""
    return None


# ===================================================================
# MLB game state endpoints
# ===================================================================

@app.get("/games")
async def list_games(user: CurrentUser = Depends(get_current_user)):
    """List today's MLB games with IDs, teams, scores, and status."""
    games = await asyncio.to_thread(get_todays_games)
    return {"date": date.today().isoformat(), "games": games}


_last_game_state_cache: dict[int, dict] = {}


async def _build_game_response(game_id: int) -> dict:
    """Shared logic for game-state and refresh endpoints."""
    try:
        info = await asyncio.to_thread(get_game_state, game_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Game not found: {e}")

    response = {
        "game_id": info.game_id,
        "away_team": info.away_team,
        "home_team": info.home_team,
        "away_abbreviation": info.away_abbreviation,
        "home_abbreviation": info.home_abbreviation,
        "away_score": info.away_score,
        "home_score": info.home_score,
        "status": info.status,
    }

    if info.state is None:
        response["state"] = None
        response["win_expectancy"] = None
        response["deltas"] = {}
        return response

    state = info.state
    we = get_win_expectancy(state)

    response["state"] = asdict(state)
    response["win_expectancy"] = round(we, 4)

    deltas = compute_deltas(
        state,
        home_score=info.home_score,
        away_score=info.away_score,
        ou_lines=ALL_OU_LINES,
        spread_lines=ALL_SPREAD_LINES,
    )
    response["deltas"] = {
        d.event: {
            "win_expectancy_before": d.win_expectancy_before,
            "win_expectancy_after": d.win_expectancy_after,
            "delta": d.delta,
            "over_under": d.over_under,
            "spread": d.spread,
        }
        for d in deltas
    }

    # Cache deltas and scores so the market selector can recompute on price ticks
    set_game_deltas(game_id, deltas)
    set_game_scores(game_id, info.home_score, info.away_score)

    # Cache the game-state snapshot so /buy can stamp it onto the trade row
    # without a second MLB API call in the tap path.
    _last_game_state_cache[game_id] = {
        **asdict(state),
        "home_score": info.home_score,
        "away_score": info.away_score,
        "home_team": info.home_team,
        "away_team": info.away_team,
        "home_abbreviation": info.home_abbreviation,
        "away_abbreviation": info.away_abbreviation,
    }

    return response


@app.get("/game-state/{game_id}")
async def game_state(game_id: int, user: CurrentUser = Depends(get_current_user)):
    """Get current game state with win expectancy and event deltas."""
    return await _build_game_response(game_id)


@app.post("/refresh/{game_id}")
async def refresh_game(game_id: int, user: CurrentUser = Depends(get_current_user)):
    """Re-fetch game state from MLB API, recompute deltas AND trades."""
    response = await _build_game_response(game_id)

    if not response.get("state"):
        return response

    # Discover + subscribe to Kalshi markets if not already done.
    # This is critical: without this, on_price_update has no market mappings
    # and price ticks won't trigger trade recomputation.
    if kalshi.is_connected:
        discover_markets(
            game_id=game_id,
            home_abbr=response["home_abbreviation"],
            away_abbr=response["away_abbreviation"],
        )

    # Recompute trades with fresh deltas + scores
    from app.engine import GameState
    deltas = compute_deltas(
        GameState(**response["state"]),
        home_score=response["home_score"],
        away_score=response["away_score"],
        ou_lines=ALL_OU_LINES,
        spread_lines=ALL_SPREAD_LINES,
    )
    trades = compute_best_trades(
        game_id, deltas,
        home_score=response["home_score"],
        away_score=response["away_score"],
    )
    response["trades"] = trades

    # Push game state to all SSE subscribers so other connected clients update
    notify_sse_game_state(game_id, {
        "away_abbreviation": response.get("away_abbreviation"),
        "home_abbreviation": response.get("home_abbreviation"),
        "away_score": response["away_score"],
        "home_score": response["home_score"],
        "state": response["state"],
        "status": response.get("status"),
        "win_expectancy": response.get("win_expectancy"),
    })

    return response


# ===================================================================
# SSE: live EV updates pushed to frontend
# ===================================================================

# ---------------------------------------------------------------------------
# MLB state polling — one background task per game, started on first SSE
# client, stopped when last client disconnects or game ends.
# ---------------------------------------------------------------------------

_polling_tasks: dict[int, asyncio.Task] = {}


async def _poll_mlb_state(game_id: int):
    """Poll MLB game state every 10s.  Push SSE events when state changes."""
    last_state = None
    while True:
        try:
            response = await _build_game_response(game_id)
            current_state = response.get("state")

            if current_state and current_state != last_state:
                last_state = current_state

                # compute_deltas populates _game_deltas via _build_game_response
                # above, so _notify_sse can pick them up per-subscriber with
                # the right settings. No need to pre-compute trades here —
                # the per-subscriber fan-out handles it.
                notify_sse_game_state(game_id, {
                    "away_abbreviation": response.get("away_abbreviation"),
                    "home_abbreviation": response.get("home_abbreviation"),
                    "away_score": response["away_score"],
                    "home_score": response["home_score"],
                    "state": current_state,
                    "status": response.get("status"),
                })
                _notify_sse(game_id)

            if response.get("status") == "Final":
                logger.info(f"Game {game_id} is Final — stopping poll")
                # Flush in-memory Sessions: writes each user's total_trades +
                # total_pnl to Supabase, sets ended_at, drops from registry.
                await asyncio.to_thread(trader.close_and_flush_sessions_for_game, game_id)
                # Safety net: also close any Supabase rows that were never
                # paired with an in-memory Session (e.g. pre-refactor rows).
                await asyncio.to_thread(db.close_open_sessions_for_game, game_id)
                break

            # Stop if no SSE subscribers remain
            if sse_subscriber_count(game_id) == 0:
                logger.info(f"No SSE subscribers for game {game_id} — stopping poll")
                break

        except Exception as e:
            logger.error(f"MLB poll error for game {game_id}: {e}")

        await asyncio.sleep(10)

    _polling_tasks.pop(game_id, None)


def _ensure_polling(game_id: int):
    """Start the MLB polling task for a game if not already running."""
    task = _polling_tasks.get(game_id)
    if task and not task.done():
        return
    _polling_tasks[game_id] = asyncio.create_task(_poll_mlb_state(game_id))
    logger.info(f"Started MLB polling for game {game_id}")


def _stop_polling_if_idle(game_id: int):
    """Cancel the polling task if no SSE subscribers remain."""
    if sse_subscriber_count(game_id) > 0:
        return
    task = _polling_tasks.pop(game_id, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"Stopped MLB polling for game {game_id} (no subscribers)")


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

@app.get("/stream/{game_id}")
async def stream_trades(
    game_id: int,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Server-sent events stream of recomputed trades.

    The frontend connects once; on every Kalshi price tick the backend
    pushes a new ``data:`` frame with the updated trades dict.  The
    initial frame contains whatever is already cached so the UI renders
    immediately.

    Also starts MLB state polling on first connection.

    EventSource can't set Authorization headers, so the JWT is passed
    as ?token=<jwt>.
    """
    # Create the Session BEFORE subscribing so the first SSE flush can
    # resolve this user's settings via get_session(). A race-window flush
    # that fires before the session lands falls back to DEFAULT_SETTINGS
    # — not catastrophic, just a one-tick blip on reconnect.
    cached_state = _last_game_state_cache.get(game_id, {})
    await asyncio.to_thread(
        trader.get_or_create_session,
        user.id,
        game_id,
        cached_state.get("home_team"),
        cached_state.get("away_team"),
    )

    # Attach user_id so per-subscriber recomputes see this user's settings.
    queue = subscribe_sse(game_id, user.id)
    _ensure_polling(game_id)

    async def event_generator():
        try:
            # Send current cached trades immediately so the UI renders without waiting
            cached = get_cached_trades(game_id)
            if cached:
                payload = json.dumps({"trades": cached})
                yield f"event: trades\ndata: {payload}\n\n"

            while True:
                msg = await queue.get()
                event_type = msg.get("type", "trades")
                payload = json.dumps(msg)
                yield f"event: {event_type}\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe_sse(game_id, queue)
            _stop_polling_if_idle(game_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ===================================================================
# Kalshi market + trading endpoints
# ===================================================================

@app.get("/markets/{game_id}")
async def list_markets(
    game_id: int,
    event_ticker: str | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Find Kalshi markets for a game.

    Pass ?event_ticker=KMLB-... to search a specific event, or omit to
    auto-discover by team abbreviation.
    """
    if not kalshi.is_connected:
        raise HTTPException(status_code=503, detail="Kalshi not connected. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH env vars.")

    try:
        info = await asyncio.to_thread(get_game_state, game_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Game not found: {e}")

    markets = discover_markets(
        game_id=game_id,
        home_abbr=info.home_abbreviation,
        away_abbr=info.away_abbreviation,
        event_ticker=event_ticker,
    )

    return {
        "game_id": game_id,
        "home_team": info.home_team,
        "away_team": info.away_team,
        "event_ticker": event_ticker,
        "markets": markets,
    }


@app.get("/trades/{game_id}")
async def list_trades(
    game_id: int,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Get the best trade for each of the 7 events.

    On first call for a game, discovers all Kalshi markets and subscribes
    to their orderbook websocket channels so that by the time the user
    taps a button, the local OrderbookManager has live depth.

    Returns from pre-computed cache if available (updated on every
    websocket price tick), otherwise computes fresh.
    """
    # Prefer this user's session settings; fall back to their saved prefs
    # merged over defaults. The frontend wants to know (a) what's active
    # right now and (b) use `multi_market` to pick an EventButton layout.
    settings = trader.get_session_settings(user.id, game_id)

    cached = get_cached_trades(game_id)
    if cached:
        return {
            "game_id": game_id,
            "settings": settings,
            "trades": cached,
            "source": "cache",
        }

    # Compute fresh: need game state + deltas + markets
    response = await _build_game_response(game_id)
    if not response.get("state"):
        return {
            "game_id": game_id,
            "settings": settings,
            "trades": {},
            "source": "no_game_state",
        }

    # Discover and subscribe to all markets for this game (if not already done).
    # This subscribes to both ticker AND orderbook_delta channels so the local
    # OrderbookManager is warm by the time the user clicks buy.
    if kalshi.is_connected:
        discover_markets(
            game_id=game_id,
            home_abbr=response["home_abbreviation"],
            away_abbr=response["away_abbreviation"],
        )

    deltas = compute_deltas(
        GameState(**response["state"]),
        home_score=response["home_score"],
        away_score=response["away_score"],
        ou_lines=ALL_OU_LINES,
        spread_lines=ALL_SPREAD_LINES,
    )
    trades = compute_best_trades(
        game_id, deltas,
        home_score=response["home_score"],
        away_score=response["away_score"],
        settings=settings,
    )

    return {
        "game_id": game_id,
        "settings": settings,
        "trades": trades,
        "source": "computed",
    }


@app.get("/position-size/{market_ticker}")
async def position_size(
    market_ticker: str,
    side: str = "YES",
    max_dollars: float = 500.0,
    max_slippage_cents: float = 1.0,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Calculate recommended position size based on live orderbook depth.
    Buys maximum the book allows, capped by dollar budget and slippage.
    """
    if not kalshi.is_connected:
        raise HTTPException(status_code=503, detail="Kalshi not connected")
    try:
        result = await asyncio.to_thread(
            kalshi.calculate_position_size,
            market_ticker, side.upper(), max_dollars, max_slippage_cents,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


class BuyBody(BaseModel):
    user_timestamp: float | None = None   # Date.now() from the client, ms epoch


@app.post("/buy/{game_id}/{event}")
async def buy_event(
    game_id: int,
    event: str,
    body: BuyBody | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Execute the pre-computed best trade for a batting event.

    Places a buy at ask, a limit sell at the target price, and arms
    a stop loss.
    """
    logger.info(f"/buy {game_id} {event} user={user.id}")
    if not kalshi.is_connected:
        raise HTTPException(status_code=503, detail="Kalshi not connected")

    event = event.upper()

    cached = get_cached_trades(game_id)
    if not cached or event not in cached:
        raise HTTPException(
            status_code=400,
            detail=f"No trade computed for event '{event}'. Call GET /trades/{game_id} first.",
        )

    trade_info = cached[event]

    if not trade_info.get("active"):
        raise HTTPException(
            status_code=400,
            detail=f"Event '{event}' has negative EV — spread eats the edge.",
        )

    if not trade_info.get("market_ticker"):
        raise HTTPException(
            status_code=400,
            detail=f"No Kalshi market found for event '{event}'.",
        )

    # Snapshot game state for the trade row. Prefer the polling-cached copy;
    # fall back to a fresh fetch if the cache is cold (first user interaction
    # before polling has populated it).
    cached_state = _last_game_state_cache.get(game_id)
    if cached_state is None:
        try:
            await _build_game_response(game_id)
            cached_state = _last_game_state_cache.get(game_id)
        except Exception:
            cached_state = None

    game_state_payload = None
    if cached_state:
        game_state_payload = {
            "inning": cached_state.get("inning"),
            "half": cached_state.get("half"),
            "outs": cached_state.get("outs"),
            "runners": cached_state.get("runners"),
            "home_score": cached_state.get("home_score"),
            "away_score": cached_state.get("away_score"),
        }

    # Fetch (or create on first touch) the in-memory Session for this
    # (user, game). Pulls the user's saved prefs from Supabase on first
    # touch; cached thereafter.
    session = await asyncio.to_thread(
        trader.get_or_create_session,
        user.id,
        game_id,
        (cached_state or {}).get("home_team"),
        (cached_state or {}).get("away_team"),
    )
    session_id = session.id if session is not None else None
    # All trading parameters for this tap come from the session. Fall back
    # to defaults only if Supabase was down and we couldn't create a
    # Session (get_or_create_session returned None).
    settings = dict(session.settings) if session is not None else dict(DEFAULT_SETTINGS)
    max_dollars = settings.get("max_dollars", 500.0)
    use_undo = settings.get("use_undo_window", True)
    alpha = settings.get("alpha")

    user_ts = body.user_timestamp if body else None
    multi_market = settings.get("multi_market", False)
    from app.constants import UNDO_WINDOW_SECONDS

    # Decide between single-market (one Trade) and multi-market (TradeGroup).
    # Single-mode is wrapped as a one-member TradeGroup so Session.add_group
    # sees every tap uniformly and total_trades counts taps, not trades.
    basket = [t for t in trade_info.get("all_trades", []) if t.get("ev_per_contract", 0) > 0]
    use_basket = multi_market and len(basket) > 1
    trades_info = basket if use_basket else [trade_info]

    try:
        def _fire() -> TradeGroup:
            group = TradeGroup(
                game_id=game_id,
                event=event,
                trades_info=trades_info,
                session_id=session_id,
                user_id=user.id,
                user_timestamp_ms=user_ts,
                total_max_dollars=max_dollars,
                use_undo_window=use_undo,
                game_state=game_state_payload,
                alpha=alpha,
                settings=settings,
            )
            if session is not None:
                session.add_group(group)
            return group.execute()
        group = await asyncio.to_thread(_fire)
        trades = group.trades
        undo_group_id = group.id
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Trade failed: {e}")

    # Response shape is uniform — frontend always gets a list of positions
    # plus the group id. Single-market mode is just a group of one.
    positions_payload = [t.to_dict() for t in trades]
    any_in_undo = any(p.get("status") == "undo_window" for p in positions_payload)
    return {
        "positions": positions_payload,
        "undo_group_id": undo_group_id,
        "event": event,
        "dry_run": bool(settings.get("dry_run", True)),
        "undo_window_seconds": UNDO_WINDOW_SECONDS if any_in_undo else 0,
        "cancel_url": f"/cancel/{trades[0].id}" if any_in_undo and trades else None,
    }


@app.post("/cancel/{position_id}")
async def cancel_trade(
    position_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """
    Cancel a trade during the 3-second undo window after buy.

    IOC sells all contracts at market and marks the position canceled.
    Only works while status is "undo_window".
    """
    try:
        result = await asyncio.to_thread(cancel_position, position_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cancel failed: {e}")
    return result


@app.get("/positions")
async def list_positions(
    game_id: int,
    user: CurrentUser = Depends(get_current_user),
):
    """Return this user's snapshot for the given game, plus live Kalshi
    positions. `game_id` is required so the backend doesn't leak trades
    across games/users."""
    snapshot = trader.get_positions(user.id, game_id)
    live = kalshi.get_live_positions(use_demo=False) if kalshi.is_connected else []
    return {
        **snapshot,
        "live_positions": live,
    }


@app.get("/history/{game_id}")
async def get_game_history(
    game_id: int,
    user: CurrentUser = Depends(get_current_user),
):
    """Every trade this user has logged on this game, newest-first.

    Lets the Trading page fill in history from earlier in the game (or
    from a previous session) — live in-memory positions take priority
    on the frontend since they hold current_price and active timers.
    """
    rows = await asyncio.to_thread(db.get_game_history, user.id, game_id)
    return rows


@app.get("/balance")
async def get_balance(user: CurrentUser = Depends(get_current_user)):
    """Return the real Kalshi (prod) account balance.

    When dry_run is on, trades are simulated against prod prices but no real
    orders are placed, so the displayed balance stays unchanged.
    """
    if not kalshi.is_connected:
        return {"balance": 0.0, "portfolio_value": 0.0, "total": 0.0, "connected": False}
    bal = await asyncio.to_thread(kalshi.get_balance, False)
    return {**bal, "connected": True}


class SettingsUpdate(BaseModel):
    alpha: float | None = None
    bet_size: int | None = None
    max_dollars: float | None = None
    max_slippage_cents: int | None = None
    use_undo_window: bool | None = None
    use_stop_loss: bool | None = None
    stop_loss_cents: int | None = None
    dry_run: bool | None = None
    blowout_filter: bool | None = None
    multi_market: bool | None = None


# Only these keys are ever read from or written to users.settings. Anything
# else in the global settings dict (or sneaking into a future PUT body)
# stays out of the DB. Never add credentials / secrets here.
USER_SETTINGS_KEYS = {
    "alpha", "bet_size", "max_dollars", "max_slippage_cents",
    "use_undo_window", "use_stop_loss", "stop_loss_cents",
    "dry_run", "blowout_filter", "multi_market",
}


def _whitelist_settings(raw: dict) -> dict:
    return {k: v for k, v in (raw or {}).items() if k in USER_SETTINGS_KEYS}


@app.get("/settings")
async def read_settings(user: CurrentUser = Depends(get_current_user)):
    """Return the user's current trading parameters.

    Resolution order:
      1. First active session's settings (if the user has any game open)
      2. User's saved prefs from Supabase merged over DEFAULT_SETTINGS
      3. DEFAULT_SETTINGS if both above are empty / unavailable

    This keeps the Settings page synced with whatever the user last saved,
    even right after a backend restart when no Session exists yet."""
    in_memory = trader.get_effective_user_settings(user.id)
    # If there's no active session, in_memory == DEFAULT_SETTINGS. Fold in
    # the user's saved DB prefs so the page shows their last toggles.
    saved = await asyncio.to_thread(db.get_user_settings, user.id)
    merged = {**in_memory, **_whitelist_settings(saved)}
    return merged


@app.put("/settings")
async def update_settings_endpoint(
    body: SettingsUpdate,
    user: CurrentUser = Depends(get_current_user),
):
    """Update trading parameters — applied to every active session for this
    user AND persisted to Supabase. The two writes together keep per-game
    state consistent and survive across restarts."""
    kwargs = body.model_dump(exclude_unset=True, exclude_none=True)
    # Fan out to all of this user's active sessions. If none exist yet, the
    # helper returns DEFAULT_SETTINGS merged with the clamped input.
    current = trader.update_user_settings_all_sessions(user.id, **kwargs)

    # Persist only whitelisted keys. Fire-and-forget on a thread so the
    # response doesn't block on a Supabase round-trip.
    await asyncio.to_thread(
        db.save_user_settings, user.id, _whitelist_settings(current),
    )
    return current


# ===================================================================
# Debug
# ===================================================================

@app.get("/debug/evaluate/{game_id}/{event}")
async def debug_evaluate(
    game_id: int,
    event: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Dump the per-market evaluation for every market for one event.

    Shows all moneyline, O/U (picked), and spread (picked) candidates
    with their EV, entry, target, and spread so you can see why one
    was selected.
    """
    event = event.upper()
    settings = trader.get_session_settings(user.id, game_id)
    alpha = settings["alpha"]
    bet_size = settings["bet_size"]

    info = await asyncio.to_thread(get_game_state, game_id)
    if info.state is None:
        raise HTTPException(400, "Game has no state")

    deltas = compute_deltas(
        info.state,
        home_score=info.home_score, away_score=info.away_score,
        ou_lines=ALL_OU_LINES, spread_lines=ALL_SPREAD_LINES,
    )
    d = next((x for x in deltas if x.event == event), None)
    if d is None:
        raise HTTPException(400, f"No delta for event {event}")

    markets = _game_markets.get(game_id, [])
    ml_markets = [m for m in markets if m.market_type == "moneyline"]
    total_runs = info.home_score + info.away_score
    margin = info.home_score - info.away_score
    ou_market = _pick_ou_market(total_runs, markets)
    sp_market = _pick_spread_market(margin, markets)

    def line_info(market, delta_value):
        prices = kalshi.get_prices(market.ticker)
        d_eff = -delta_value if market.flip else delta_value
        trade = _evaluate_market(market, delta_value, alpha, bet_size)
        return {
            "market": market.label,
            "ticker": market.ticker,
            "flip": market.flip,
            "model_delta": round(delta_value, 4),
            "effective_delta": round(d_eff, 4),
            "prices": prices,
            "yes_spread": round(prices["yes_ask"] - prices["yes_bid"], 4),
            "no_spread": round(prices["no_ask"] - prices["no_bid"], 4),
            "picked": {
                "side": trade["side"],
                "entry": trade["entry_price"],
                "target": trade["sell_target"],
                "spread_cost": trade["spread"],
                "ev_per_contract": trade["ev_per_contract"],
                "estimated_profit": trade["estimated_profit"],
            } if trade else None,
        }

    result = {
        "event": event,
        "game": f"{info.away_abbreviation} @ {info.home_abbreviation}",
        "score": f"{info.away_score}-{info.home_score}",
        "model_deltas": {
            "ml": round(d.delta, 4),
            **{f"ou_{line}": round(data["delta"], 4) for line, data in d.over_under.items()},
            **{f"sp_{line}": round(data["delta"], 4) for line, data in d.spread.items()},
        },
        "moneyline_candidates": [line_info(ml, d.delta) for ml in ml_markets],
    }

    if ou_market:
        ou_delta = d.over_under.get(str(ou_market.line), {}).get("delta", 0)
        result["ou_candidate"] = line_info(ou_market, ou_delta)
    if sp_market:
        sp_delta = d.spread.get(str(sp_market.line), {}).get("delta", 0)
        result["spread_candidate"] = line_info(sp_market, sp_delta)

    return result
