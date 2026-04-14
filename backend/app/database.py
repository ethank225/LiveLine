"""
Supabase integration — durable logging of sessions, trades, and
market snapshots.

Every public function in this module is best-effort:
    - wraps its body in try/except
    - logs failures
    - returns None (or a safe default) on error
    - NEVER raises

This is load-bearing. A Supabase outage must never prevent a trade
from executing. See verification step 7 in the plan.

The client is initialized with the SERVICE role key, which bypasses
RLS. End-user reads go through the frontend's anon-key client.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

_client = None
_disabled = False


def _get_client():
    """Lazy-init the Supabase client. Disables itself on any failure."""
    global _client, _disabled
    if _disabled:
        return None
    if _client is not None:
        return _client
    try:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        # Strip surrounding quotes/whitespace — common when pasted into .env
        if url:
            url = url.strip().strip('"').strip("'")
        if key:
            key = key.strip().strip('"').strip("'")
        if not url or not key:
            logger.warning(
                f"Supabase disabled: SUPABASE_URL={'set' if url else 'MISSING'} "
                f"SUPABASE_SERVICE_KEY={'set' if key else 'MISSING'}"
            )
            _disabled = True
            return None
        from supabase import create_client
        _client = create_client(url, key)
        logger.info(f"Supabase client initialized (url={url})")
        return _client
    except Exception as e:
        logger.error(f"Supabase client init failed: {e}")
        _disabled = True
        return None


def is_available() -> bool:
    return _get_client() is not None


# Eager init at import time so the "supabase up?" answer lands in startup
# logs, not on the first user action. Failures still don't raise.
try:
    _get_client()
except Exception as e:
    logger.error(f"Supabase eager init raised: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _ts_from_ms(ms: int | float | None) -> str | None:
    """Convert a JS Date.now() epoch-ms value to an ISO timestamp."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def get_or_create_session(
    user_id: str,
    game_id: int,
    home_team: str | None,
    away_team: str | None,
    settings: dict | None,
) -> str | None:
    """
    Return the open session id for (user_id, game_id), or create one.
    Returns None on DB failure — callers must handle None gracefully.
    """
    logger.info(f"get_or_create_session called (user={user_id} game={game_id})")
    client = _get_client()
    if client is None:
        logger.warning("get_or_create_session: client unavailable")
        return None
    try:
        # Look for an existing open session first.
        existing = (
            client.table("sessions")
            .select("id")
            .eq("user_id", user_id)
            .eq("game_id", game_id)
            .is_("ended_at", "null")
            .limit(1)
            .execute()
        )
        if existing.data:
            sid = existing.data[0]["id"]
            logger.info(f"get_or_create_session: reusing session {sid}")
            return sid

        inserted = (
            client.table("sessions")
            .insert({
                "user_id": user_id,
                "game_id": game_id,
                "home_team": home_team,
                "away_team": away_team,
                "settings_snapshot": settings or {},
            })
            .execute()
        )
        if inserted.data:
            sid = inserted.data[0]["id"]
            logger.info(f"get_or_create_session: created session {sid}")
            return sid
        logger.warning(f"get_or_create_session: insert returned no data ({inserted})")
    except Exception as e:
        logger.error(f"get_or_create_session failed (user={user_id} game={game_id}): {e}")
    return None


def get_user_settings(user_id: str) -> dict:
    """Return the user's saved settings jsonb, or {} if unset / DB down.
    Never raises."""
    client = _get_client()
    if client is None or not user_id:
        return {}
    try:
        resp = (
            client.table("users")
            .select("settings")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        if resp.data and isinstance(resp.data[0].get("settings"), dict):
            return resp.data[0]["settings"]
    except Exception as e:
        logger.error(f"get_user_settings failed (user={user_id}): {e}")
    return {}


def save_user_settings(user_id: str, settings: dict) -> None:
    """Overwrite the user's settings jsonb. Caller is responsible for
    whitelisting fields — this helper just persists whatever it's given."""
    client = _get_client()
    if client is None or not user_id:
        return
    try:
        client.table("users").update({"settings": settings}).eq("id", user_id).execute()
        logger.info(f"save_user_settings: user={user_id} keys={list(settings.keys())}")
    except Exception as e:
        logger.error(f"save_user_settings failed (user={user_id}): {e}")


def close_open_sessions_for_game(game_id: int) -> None:
    """Close every open session for a game (called when status flips to Final)."""
    client = _get_client()
    if client is None:
        return
    try:
        (
            client.table("sessions")
            .update({"ended_at": _iso(datetime.now(timezone.utc))})
            .eq("game_id", int(game_id))
            .is_("ended_at", "null")
            .execute()
        )
    except Exception as e:
        logger.error(f"close_open_sessions_for_game failed (game={game_id}): {e}")


def close_session(session_id: str, total_trades: int, total_pnl: float) -> None:
    client = _get_client()
    if client is None or not session_id:
        return
    try:
        (
            client.table("sessions")
            .update({
                "ended_at": _iso(datetime.now(timezone.utc)),
                "total_trades": int(total_trades),
                "total_pnl": float(total_pnl),
            })
            .eq("id", session_id)
            .execute()
        )
    except Exception as e:
        logger.error(f"close_session failed (id={session_id}): {e}")


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def log_trade(
    session_id: str | None,
    user_id: str | None,
    position: Any,
    trade_info: dict | None,
    game_state: dict | None,
    user_timestamp_ms: int | float | None,
    dry_run: bool,
    alpha: float | None,
    undo_group_id: str | None = None,
) -> str | None:
    """
    Insert a new row in `trades`. Returns the inserted id or None on failure.

    `position` is a trader.Position dataclass; `trade_info` is the
    compute_best_trades dict the trade was executed from (for
    predicted_delta / market_label fallback).
    """
    logger.info(
        f"log_trade called (session={session_id} user={user_id} "
        f"event={getattr(position,'event',None)} ticker={getattr(position,'market_ticker',None)})"
    )
    client = _get_client()
    if client is None or not session_id or not user_id:
        logger.warning(
            f"log_trade: skipping — client={client is not None} "
            f"session_id={bool(session_id)} user_id={bool(user_id)}"
        )
        return None
    try:
        trade_info = trade_info or {}
        row = {
            "session_id": session_id,
            "user_id": user_id,
            "game_id": position.game_id,
            "event_type": position.event,
            "market_ticker": position.market_ticker,
            "market_type": position.market_type,
            "market_label": trade_info.get("market_title") or position.market_title,
            "side": position.side,
            "entry_price": float(position.entry_price),
            "sell_target": float(position.sell_target),
            "quantity": int(position.quantity),
            "requested_quantity": int(position.requested_quantity),
            "total_cost": round(float(position.entry_price) * int(position.quantity), 4),
            "predicted_delta": trade_info.get("ev_per_contract"),
            "alpha": alpha,
            "status": position.status,
            "game_state_json": game_state or {},
            "user_timestamp": _ts_from_ms(user_timestamp_ms),
            "dry_run": bool(dry_run),
            "undo_group_id": undo_group_id,
            "entry_fee_est": trade_info.get("entry_fee_est"),
            "exit_fee_est": trade_info.get("exit_fee_est"),
            "net_expected_profit": trade_info.get("net_expected_profit"),
            "fee_adjusted": trade_info.get("fee_adjusted"),
            "gross_pick_ticker": trade_info.get("gross_pick_ticker"),
        }
        inserted = client.table("trades").insert(row).execute()
        if inserted.data:
            tid = inserted.data[0]["id"]
            logger.info(f"log_trade: inserted trade {tid}")
            return tid
        logger.warning(f"log_trade: insert returned no data ({inserted})")
    except Exception as e:
        logger.error(f"log_trade failed (session={session_id} event={getattr(position,'event',None)}): {e}")
    return None


def update_trade_status(
    trade_id: str | None,
    status: str,
    exit_price: float | None = None,
    pnl: float | None = None,
    *,
    gross_pnl: float | None = None,
    entry_fee: float | None = None,
    exit_fee: float | None = None,
) -> None:
    logger.info(f"update_trade_status called (trade_id={trade_id} status={status})")
    client = _get_client()
    if client is None or not trade_id:
        logger.warning(
            f"update_trade_status: skipping — client={client is not None} "
            f"trade_id={bool(trade_id)}"
        )
        return
    try:
        patch: dict = {"status": status}
        if exit_price is not None:
            patch["exit_price"] = float(exit_price)
        if pnl is not None:
            # realized_pnl is now net of fees (entry taker + exit maker/taker).
            # Gross and the per-leg fees are stored alongside in the columns
            # added by the schema migration below so nothing is lost.
            patch["realized_pnl"] = float(pnl)
        if gross_pnl is not None:
            patch["gross_pnl"] = float(gross_pnl)
        if entry_fee is not None:
            patch["entry_fee"] = float(entry_fee)
        if exit_fee is not None:
            patch["exit_fee"] = float(exit_fee)
        if status in ("filled", "expired", "stopped", "canceled", "canceled_by_user", "error"):
            patch["closed_at"] = _iso(datetime.now(timezone.utc))
        resp = client.table("trades").update(patch).eq("id", trade_id).execute()
        logger.info(f"update_trade_status: patched trade {trade_id} → {status} ({len(resp.data or [])} rows)")
    except Exception as e:
        logger.error(f"update_trade_status failed (id={trade_id} status={status}): {e}")


# ---------------------------------------------------------------------------
# Market snapshots
# ---------------------------------------------------------------------------

def get_game_history(user_id: str, game_id: int) -> list[dict]:
    """
    Return every trade this user has logged on this game, newest-first,
    pre-normalized to the shape PositionCard expects. Returns [] on any
    failure — frontend should still render live in-memory positions.
    """
    client = _get_client()
    if client is None:
        return []
    try:
        resp = (
            client.table("trades")
            .select("*")
            .eq("user_id", user_id)
            .eq("game_id", game_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logger.error(f"get_game_history failed (user={user_id} game={game_id}): {e}")
        return []

    # Late import: database module is imported by market_selector at load.
    from app.market_selector import _game_teams, _teams_lock
    from app.bet_label import compute_display_label

    out: list[dict] = []
    for r in rows:
        gid = r.get("game_id")
        with _teams_lock:
            home_abbr, away_abbr = _game_teams.get(gid, ("", ""))
        ticker = r.get("market_ticker") or ""
        side = r.get("side") or ""
        mtype = r.get("market_type")
        out.append({
            # Dual id to match to_dict's shape — frontend keys on id/position_id.
            "id": r.get("id"),
            "position_id": r.get("id"),
            "trade_db_id": r.get("id"),
            "game_id": gid,
            # Frontend uses `event`, DB column is `event_type`.
            "event": r.get("event_type"),
            "market_ticker": ticker,
            "market_title": r.get("market_label"),
            "market_type": mtype,
            "side": side,
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "display_label": compute_display_label(
                market_ticker=ticker, side=side, market_type=mtype,
                home_abbr=home_abbr, away_abbr=away_abbr,
            ),
            "entry_price": _as_float(r.get("entry_price")),
            "sell_target": _as_float(r.get("sell_target")),
            "quantity": r.get("quantity") or 0,
            # DB doesn't persist stop_loss; derive a reasonable 10¢-below-entry
            # value so PositionCard's stopped-exit line has something to show.
            "stop_loss": max(0.01, round(_as_float(r.get("entry_price")) - 0.10, 2)),
            "current_price": None,
            "exit_price": _as_float(r.get("exit_price")),
            "buy_order_id": None,
            "sell_order_id": None,
            "status": r.get("status"),
            "realized_pnl": _as_float(r.get("realized_pnl")),
            "clean_window_seconds": 45,
            "created_at": r.get("created_at"),
            # DB column is `closed_at`; frontend renders `completed_at`.
            "completed_at": r.get("closed_at"),
            "undo_group_id": r.get("undo_group_id"),
            "dry_run": r.get("dry_run"),
            # Optional extras the card doesn't use but may be handy later.
            "game_state_json": r.get("game_state_json"),
        })
    return out


def _as_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def log_market_snapshots(game_id: int, snapshots: list[dict]) -> None:
    """Batch-insert market snapshots. Each dict needs:
    market_ticker, yes_bid, yes_ask, market_type."""
    client = _get_client()
    if client is None or not snapshots:
        return
    try:
        rows = []
        for s in snapshots:
            try:
                yes_bid = s.get("yes_bid")
                yes_ask = s.get("yes_ask")
                spread = None
                if yes_bid is not None and yes_ask is not None:
                    spread = round(float(yes_ask) - float(yes_bid), 4)
                rows.append({
                    "game_id": int(game_id),
                    "market_ticker": s.get("market_ticker"),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "spread": spread,
                    "market_type": s.get("market_type"),
                })
            except Exception:
                continue
        if rows:
            client.table("market_snapshots").insert(rows).execute()
    except Exception as e:
        logger.error(f"log_market_snapshots failed (game={game_id}): {e}")
