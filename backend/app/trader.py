"""
Trade execution and position management — class-based.

Each Trade instance owns its full lifecycle:
    1. IOC buy at ask + slippage limit — fills instantly or not at all
    2. If buy fills (fully or partially), place limit sell for filled qty
    3. Schedule clean-window timeout — if sell hasn't filled in N seconds,
       cancel it and IOC sell to exit
    4. Stop loss armed via websocket ticks (on_price_tick)

TradeGroup wraps N Trades that share an undo_group_id — one button tap
in multi-market mode fires the whole group. Cancel on any member cancels
the group as a unit.

State machine (unchanged from prior implementation):
    "pending"         → instantiated, not yet executed
    "undo_window"     → buy filled, sell not yet placed (3s undo window)
    "open"            → buy filled, sell resting on book
    "filled"          → sell hit target (profit)
    "stopped"         → stop loss triggered (cut losses)
    "expired"         → clean-window timeout, forced exit
    "canceled"        → buy didn't fill, or no liquidity for sizing
    "canceled_by_user"→ user tapped undo
    "error"           → sell placement failed; needs manual cleanup
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime

from app import database as db
from app.constants import (
    CLEAN_WINDOW_SECONDS,
    STOP_LOSS_CENTS,
    UNDO_WINDOW_SECONDS,
)
from app.kalshi_client import kalshi

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"filled", "stopped", "expired", "canceled", "canceled_by_user", "error"}


# ---------------------------------------------------------------------------
# Order helpers
#
# dry_run is now passed as a kwarg (by Trade, which snapshots it from its
# session's settings). No more global DRY_RUN / USE_STOP_LOSS — one user's
# toggle can't flip another user's trades anymore.
# ---------------------------------------------------------------------------

def _fake_order_id() -> str:
    return f"sim-{uuid.uuid4().hex[:12]}"


def _place_order(*, dry_run: bool, **kwargs) -> dict:
    """Place an order. In dry_run, simulate (log + fake full fill at ask)."""
    if dry_run:
        oid = _fake_order_id()
        logger.info(
            f"[SIM] {kwargs.get('action','?').upper()} "
            f"{kwargs.get('quantity',0)} {kwargs.get('side','?')} "
            f"{kwargs.get('market_ticker','?')} "
            f"@${kwargs.get('price',0):.2f} "
            f"tif={kwargs.get('time_in_force','gtc')} → {oid}"
        )
        return {"order_id": oid, "status": "resting"}
    return kalshi.place_order(**kwargs)


def _cancel_order(order_id: str, *, dry_run: bool) -> dict:
    """Cancel an order. In dry_run, just log it."""
    if dry_run:
        logger.info(f"[SIM] CANCEL {order_id}")
        return {"order_id": order_id, "status": "canceled"}
    return kalshi.cancel_order(order_id)


def _get_fill_count(order_id: str | None) -> int:
    """Check how many contracts actually filled on a real (prod) order.

    Returns 0 if the order can't be found (IOC orders that don't fill are
    canceled immediately and return 404 on lookup — that's a legitimate
    'zero fills' result, not an error).
    """
    if not order_id or not kalshi.client:
        return 0
    try:
        order = kalshi.get_order(order_id, use_demo=False)
        if order is None:
            return 0
        count_str = getattr(order, "fill_count_fp", None) or "0"
        return int(float(count_str))
    except Exception as e:
        msg = str(e).lower()
        if "not_found" in msg or "404" in msg:
            return 0
        logger.error(f"Failed to check fill count for {order_id}: {e}")
        return 0


# ---------------------------------------------------------------------------
# Registries
#
# Sessions own per-(user, game) state and running aggregates. The module
# owns the by-id trade index (O(1) lookup is the index's job, not Session's).
# ---------------------------------------------------------------------------

_sessions: dict[tuple[str, int], "Session"] = {}
_trade_index: dict[str, "Trade"] = {}
_registry_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class Session:
    """Holds a single user's active trading state for one game.

    Groups live on the Session; trades are reachable via each group.
    Aggregates (realized_pnl, wins, resolved, total_trades) are kept as
    running totals updated by `on_trade_resolved` so `snapshot()` is O(1).

    `settings` is the per-user, per-session source of truth for trading
    parameters. PUT /settings writes here; PUT /settings also mirrors to
    Supabase for persistence across restarts. Replaces the old global
    `_settings` dict in market_selector.
    """

    def __init__(
        self,
        session_id: str,
        user_id: str,
        game_id: int,
        settings: dict | None = None,
    ):
        from app.market_selector import DEFAULT_SETTINGS
        self.id = session_id
        self.user_id = user_id
        self.game_id = game_id
        self.groups: dict[str, "TradeGroup"] = {}
        self.settings: dict = dict(DEFAULT_SETTINGS)
        if settings:
            self.settings.update(settings)
        # Counts button taps, not individual trades. A multi-market basket
        # increments once regardless of how many markets it fires.
        self.total_trades = 0
        self.realized_pnl = 0.0
        self.wins = 0
        self.resolved = 0
        self._lock = threading.Lock()

    def add_group(self, group: "TradeGroup") -> None:
        with self._lock:
            self.groups[group.id] = group
            self.total_trades += 1

    def on_trade_resolved(self, trade: "Trade") -> None:
        """Called from a Trade's terminal-state transition. Aggregates the
        whole group's P&L once every member is terminal."""
        with self._lock:
            group = self.groups.get(trade.undo_group_id)
            if not group:
                return
            if not all(t.status in _TERMINAL_STATUSES for t in group.trades):
                return
            # Idempotency guard: two concurrent timers finishing the last
            # two members of the group would otherwise both aggregate.
            if getattr(group, "_aggregated", False):
                return
            group._aggregated = True
            group_pnl = sum(t.realized_pnl for t in group.trades)
            self.realized_pnl += group_pnl
            self.resolved += 1
            if group_pnl > 0:
                self.wins += 1

    def active_trades(self) -> list["Trade"]:
        return [
            t for g in self.groups.values() for t in g.trades
            if t.status in ("undo_window", "open")
        ]

    def all_trades(self) -> list["Trade"]:
        return [t for g in self.groups.values() for t in g.trades]

    def snapshot(self) -> dict:
        with self._lock:
            pnl = {
                "realized": round(self.realized_pnl, 2),
                "total_trades": self.total_trades,
                "wins": self.wins,
                "resolved": self.resolved,
                "win_rate": (
                    round(self.wins / self.resolved, 2)
                    if self.resolved > 0 else 0.0
                ),
            }
        return {
            "positions": [t.to_dict() for t in self.all_trades()],
            "pnl": pnl,
        }


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class Trade:
    """Self-managing trade with full lifecycle."""

    def __init__(
        self,
        game_id: int,
        event: str,
        trade_info: dict,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        user_timestamp_ms: int | float | None = None,
        max_dollars: float = 500.0,
        max_slippage_cents: float = 1.0,
        use_undo_window: bool = True,
        game_state: dict | None = None,
        alpha: float | None = None,
        undo_group_id: str | None = None,
        settings: dict | None = None,
    ):
        from app.market_selector import DEFAULT_SETTINGS
        self.id: str = str(uuid.uuid4())
        self.game_id: int = game_id
        self.event: str = event
        self.trade_info: dict = trade_info
        self.session_id: str | None = session_id
        self.user_id: str | None = user_id
        self.user_timestamp_ms = user_timestamp_ms
        self.max_dollars: float = max_dollars
        self.max_slippage_cents: float = max_slippage_cents
        self.use_undo_window: bool = use_undo_window
        self.game_state: dict | None = game_state
        self.alpha: float | None = alpha
        # Always have a group id — single-market trades are a group of one.
        self.undo_group_id: str = undo_group_id or str(uuid.uuid4())

        # Snapshot the Session's settings at construction so the Trade's
        # execution + lifecycle decisions stay consistent even if the user
        # flips a toggle mid-flight. dry_run, use_stop_loss, stop_loss_cents
        # are all read from here — never from a module-level global.
        self._settings: dict = dict(DEFAULT_SETTINGS)
        if settings:
            self._settings.update(settings)

        # Derived from trade_info; concrete execution parameters.
        self.market_ticker: str = trade_info["market_ticker"]
        self.side: str = trade_info["side"]
        self.market_title: str = trade_info.get("market_title", "")
        self.market_type: str = trade_info.get("market_type", "moneyline")
        self.sell_target: float = trade_info.get("sell_target", 0.0)

        # Populated during lifecycle.
        self.status: str = "pending"
        self.buy_order_id: str | None = None
        self.sell_order_id: str | None = None
        self.entry_price: float = 0.0
        self.stop_loss: float = 0.0
        self.quantity: int = 0
        self.requested_quantity: int = 0
        self.realized_pnl: float = 0.0
        self.exit_price: float | None = None
        self.completed_at: datetime | None = None
        self.created_at: datetime = datetime.utcnow()
        self.trade_db_id: str | None = None          # Supabase row pk

        self._lock = threading.Lock()
        self._undo_timer: threading.Timer | None = None
        self._clean_timer: threading.Timer | None = None

        # Module-level index so by-id lookups are O(1). Registered here
        # rather than in execute() so zero-fill trades (which return
        # before timers start) are still findable for the history shim.
        with _registry_lock:
            _trade_index[self.id] = self

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize for API responses + SSE. Preserves the shape the
        frontend already consumes; adds undo_group_id."""
        # Live price only makes sense while the position is still on-book.
        current_price = None
        if self.status in ("open", "undo_window"):
            try:
                prices = kalshi.get_prices(self.market_ticker)
                current_price = prices["yes_bid"] if self.side == "YES" else prices["no_bid"]
            except Exception:
                pass
        return {
            # Dual id for backward compat — some frontend code reads `id`,
            # some reads `position_id`.
            "id": self.id,
            "position_id": self.id,
            "game_id": self.game_id,
            "event": self.event,
            "market_ticker": self.market_ticker,
            "market_title": self.market_title,
            "market_type": self.market_type,
            "side": self.side,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "sell_target": self.sell_target,
            "stop_loss": self.stop_loss,
            "current_price": current_price,
            "exit_price": self.exit_price,
            "buy_order_id": self.buy_order_id,
            "sell_order_id": self.sell_order_id,
            "status": self.status,
            "realized_pnl": self.realized_pnl,
            "clean_window_seconds": CLEAN_WINDOW_SECONDS,
            "created_at": self.created_at.isoformat() + "Z",
            "completed_at": self.completed_at.isoformat() + "Z" if self.completed_at else None,
            "undo_group_id": self.undo_group_id,
            # Supabase primary key — lets the frontend dedupe in-memory live
            # positions against historical rows from /history.
            "trade_db_id": self.trade_db_id,
        }

    # -- Lifecycle ----------------------------------------------------------

    def execute(self) -> "Trade":
        """Size → IOC buy → log → undo window (or activate immediately).

        Preserves the legacy contract: raises RuntimeError if sizing
        returns 0 contracts (no orderbook liquidity). A zero-fill IOC does
        NOT raise — it sets status="canceled" and returns.
        """
        # Already registered in _trade_index by __init__.

        sizing = kalshi.calculate_position_size(
            self.market_ticker, self.side,
            self.max_dollars, self.max_slippage_cents,
        )
        quantity = sizing["contracts"]
        if quantity == 0:
            # Preserve historical "no liquidity" error path.
            raise RuntimeError(
                f"No liquidity on {self.market_ticker} {self.side} "
                f"within {self.max_slippage_cents}¢ slippage"
            )

        self.requested_quantity = quantity
        self.entry_price = sizing["vwap"]
        stop_cents = self._settings.get("stop_loss_cents", STOP_LOSS_CENTS)
        self.stop_loss = max(0.01, round(self.entry_price - stop_cents / 100, 2))

        dry_run = bool(self._settings.get("dry_run", True))

        # --- IOC buy ---
        buy_price = round(sizing["best_ask"] + self.max_slippage_cents / 100, 2)
        buy_result = _place_order(
            market_ticker=self.market_ticker,
            action="buy",
            side=self.side,
            quantity=quantity,
            price=buy_price,
            time_in_force="ioc",
            dry_run=dry_run,
        )
        self.buy_order_id = buy_result.get("order_id")

        filled_qty = quantity if dry_run else _get_fill_count(self.buy_order_id)
        if filled_qty == 0:
            logger.info(f"Buy IOC got 0 fills for {self.event} {self.market_ticker} — no position")
            with self._lock:
                self.status = "canceled"
            self._log_to_db()
            self._notify_resolved()
            return self

        self.quantity = filled_qty
        with self._lock:
            self.status = "undo_window" if self.use_undo_window else "open"

        self._log_to_db()

        # Trade-time market snapshot — captures the board at decision time.
        try:
            from app.market_selector import snapshot_markets_for_game
            snapshot_markets_for_game(self.game_id)
        except Exception as e:
            logger.error(f"snapshot_markets_for_game failed: {e}")

        potential_profit = round((self.sell_target - self.entry_price) * filled_qty, 2)
        summary = (
            f"Trade plan [{self.event} {self.side} {self.market_ticker}] "
            f"BUY {filled_qty}@${self.entry_price:.2f} "
            f"(${sizing['total_cost']:.2f}) → "
            f"SELL @${self.sell_target:.2f} "
            f"(+${potential_profit:.2f} if filled) · stop=${self.stop_loss:.2f}"
        )

        if self.use_undo_window:
            self._undo_timer = threading.Timer(UNDO_WINDOW_SECONDS, self._activate)
            self._undo_timer.daemon = True
            self._undo_timer.start()
            logger.info(f"{summary} · sell in {UNDO_WINDOW_SECONDS}s (undo window)")
        else:
            self._activate()
            logger.info(f"{summary} · timeout={CLEAN_WINDOW_SECONDS}s")

        return self

    def _activate(self):
        """Place the limit sell and start the clean-window timer."""
        with self._lock:
            if self.status not in ("undo_window", "open"):
                return   # user canceled during the window
            self.status = "open"

        logger.info(f"Placing sell for {self.event} {self.market_ticker}")
        dry_run = bool(self._settings.get("dry_run", True))
        try:
            sell_result = _place_order(
                market_ticker=self.market_ticker,
                action="sell",
                side=self.side,
                quantity=self.quantity,
                price=self.sell_target,
                time_in_force="gtc",
                dry_run=dry_run,
            )
            self.sell_order_id = sell_result.get("order_id")
        except Exception as e:
            logger.error(f"Sell placement failed for {self.event} {self.market_ticker}: {e}")
            with self._lock:
                self.status = "error"
            self._update_db_status("error")
            self._notify_resolved()
            return

        self._update_db_status("open")

        self._clean_timer = threading.Timer(CLEAN_WINDOW_SECONDS, self._expire)
        self._clean_timer.daemon = True
        self._clean_timer.start()

    def _expire(self):
        """Clean-window fired. Cancel resting sell and IOC exit."""
        with self._lock:
            if self.status != "open":
                return
            self.status = "expired"

        logger.info(f"Clean window expired for {self.event} {self.market_ticker} — force exiting")
        dry_run = bool(self._settings.get("dry_run", True))

        if self.sell_order_id:
            try:
                _cancel_order(self.sell_order_id, dry_run=dry_run)
            except Exception as e:
                logger.error(f"Failed to cancel sell on expiry: {e}")

        try:
            _place_order(
                market_ticker=self.market_ticker,
                action="sell",
                side=self.side,
                quantity=self.quantity,
                price=0.01,
                time_in_force="ioc",
                dry_run=dry_run,
            )
        except Exception as e:
            logger.error(f"Expiry exit sell failed: {e}")

        prices = kalshi.get_prices(self.market_ticker)
        exit_bid = prices["yes_bid"] if self.side == "YES" else prices["no_bid"]
        with self._lock:
            self.realized_pnl = round((exit_bid - self.entry_price) * self.quantity, 2)
            self.exit_price = exit_bid
            self.completed_at = datetime.utcnow()

        self._update_db_status("expired", exit_bid, self.realized_pnl)
        self._notify_resolved()

    def cancel(self) -> dict | None:
        """User tapped undo during the undo window. IOC-exit held quantity.

        Raises ValueError if called outside the undo_window state — the
        historical contract. TradeGroup.cancel() swallows this so members
        that already activated don't abort the whole undo.
        """
        with self._lock:
            if self.status != "undo_window":
                raise ValueError(
                    f"Position {self.id} is '{self.status}' — can only cancel during undo window"
                )
            self.status = "canceled_by_user"

        if self._undo_timer:
            self._undo_timer.cancel()

        exit_price = 0.0
        dry_run = bool(self._settings.get("dry_run", True))
        if self.quantity > 0:
            try:
                _place_order(
                    market_ticker=self.market_ticker,
                    action="sell",
                    side=self.side,
                    quantity=self.quantity,
                    price=0.01,
                    time_in_force="ioc",
                    dry_run=dry_run,
                )
                prices = kalshi.get_prices(self.market_ticker)
                exit_price = prices["yes_bid"] if self.side == "YES" else prices["no_bid"]
            except Exception as e:
                logger.error(f"Cancel exit sell failed for {self.market_ticker}: {e}")

        with self._lock:
            self.realized_pnl = round((exit_price - self.entry_price) * self.quantity, 2)
            self.exit_price = exit_price
            self.completed_at = datetime.utcnow()

        self._update_db_status("canceled_by_user", exit_price, self.realized_pnl)
        self._notify_resolved()

        logger.info(
            f"Trade canceled (undo): {self.event} {self.market_ticker} "
            f"qty={self.quantity} pnl={self.realized_pnl}"
        )

        return {
            "position_id": self.id,
            "status": self.status,
            "quantity": self.quantity,
            "realized_pnl": self.realized_pnl,
            "undo_group_id": self.undo_group_id,
        }

    def on_price_tick(self, bid: float):
        """Called on every Kalshi tick for this ticker. Runs stop-loss check.

        TODO: detect sell fills from bid ≥ sell_target. Today the trader
        relies on Kalshi-side execution + the 45s clean-window timer; adding
        tick-side fill detection is a behavior change, not a refactor.
        """
        if not self._settings.get("use_stop_loss", True):
            return
        with self._lock:
            if self.status != "open":
                return
        if bid <= self.stop_loss:
            logger.warning(
                f"Stop loss triggered for {self.event}: "
                f"bid={bid:.2f} <= stop={self.stop_loss:.2f}"
            )
            self._execute_stop_loss(bid)

    def _execute_stop_loss(self, exit_price: float):
        with self._lock:
            if self.status != "open":
                return
            self.status = "stopped"

        dry_run = bool(self._settings.get("dry_run", True))

        if self._clean_timer:
            self._clean_timer.cancel()
        if self.sell_order_id:
            try:
                _cancel_order(self.sell_order_id, dry_run=dry_run)
            except Exception as e:
                logger.error(f"Failed to cancel limit sell {self.sell_order_id}: {e}")
        if self.buy_order_id:
            try:
                _cancel_order(self.buy_order_id, dry_run=dry_run)
            except Exception:
                pass   # likely already filled

        try:
            _place_order(
                market_ticker=self.market_ticker,
                action="sell",
                side=self.side,
                quantity=self.quantity,
                price=0.01,
                time_in_force="ioc",
                dry_run=dry_run,
            )
        except Exception as e:
            logger.error(f"Stop-loss sell failed for {self.market_ticker}: {e}")

        with self._lock:
            self.realized_pnl = round((exit_price - self.entry_price) * self.quantity, 2)
            self.exit_price = exit_price
            self.completed_at = datetime.utcnow()

        self._update_db_status("stopped", exit_price, self.realized_pnl)
        self._notify_resolved()

    # -- Supabase ----------------------------------------------------------

    def _log_to_db(self):
        """Insert the initial trades row. Populates self.trade_db_id.
        Single INSERT now that `log_trade` takes `undo_group_id` directly."""
        logger.info(
            f"Trade._log_to_db: {self.event} {self.market_ticker} "
            f"session_id={self.session_id} user_id={self.user_id}"
        )
        try:
            tid = db.log_trade(
                session_id=self.session_id,
                user_id=self.user_id,
                position=self,
                trade_info=self.trade_info,
                game_state=self.game_state,
                user_timestamp_ms=self.user_timestamp_ms,
                dry_run=bool(self._settings.get("dry_run", True)),
                alpha=self.alpha,
                undo_group_id=self.undo_group_id,
            )
            if tid:
                self.trade_db_id = tid
        except Exception as e:
            logger.error(f"Trade._log_to_db: {e}")

    def _notify_resolved(self):
        """Tell the Session this trade reached a terminal state. Aggregates
        are computed once the whole group is resolved.

        Looked up by (user_id, game_id) rather than held as a direct
        reference so Trade stays ignorant of the Session object."""
        with _registry_lock:
            session = _sessions.get((self.user_id, self.game_id)) if self.user_id else None
        if session is not None:
            try:
                session.on_trade_resolved(self)
            except Exception as e:
                logger.error(f"_notify_resolved: {e}")

    def _update_db_status(
        self,
        status: str,
        exit_price: float | None = None,
        pnl: float | None = None,
    ):
        logger.info(f"Trade._update_db_status: {self.event} → {status} (trade_db_id={self.trade_db_id})")
        try:
            db.update_trade_status(self.trade_db_id, status, exit_price, pnl)
        except Exception as e:
            logger.error(f"Trade._update_db_status: {e}")


# ---------------------------------------------------------------------------
# TradeGroup — multi-market basket
# ---------------------------------------------------------------------------

class TradeGroup:
    """N trades from a single button tap. Shared undo_group_id.

    Budget (max_dollars) is split proportional to EV across the basket.
    A zero-EV member would be zero-budget, so callers should pre-filter
    to positive-EV trades.
    """

    def __init__(
        self,
        game_id: int,
        event: str,
        trades_info: list[dict],
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        user_timestamp_ms: int | float | None = None,
        total_max_dollars: float,
        max_slippage_cents: float = 1.0,
        use_undo_window: bool = True,
        game_state: dict | None = None,
        alpha: float | None = None,
        settings: dict | None = None,
    ):
        self.id: str = str(uuid.uuid4())
        self.trades: list[Trade] = []

        # Proportional EV allocation. Fall back to equal split if EV sums
        # to ~0 (shouldn't happen after a positive-EV prefilter, but safe).
        evs = [max(float(t.get("ev_per_contract", 0)), 0.0) for t in trades_info]
        total_ev = sum(evs)
        n = len(trades_info)
        if total_ev <= 0 or n == 0:
            allocations = [total_max_dollars / max(1, n)] * n
        else:
            allocations = [total_max_dollars * (ev / total_ev) for ev in evs]

        for info, budget in zip(trades_info, allocations):
            self.trades.append(Trade(
                game_id=game_id,
                event=event,
                trade_info=info,
                session_id=session_id,
                user_id=user_id,
                user_timestamp_ms=user_timestamp_ms,
                max_dollars=budget,
                max_slippage_cents=max_slippage_cents,
                use_undo_window=use_undo_window,
                game_state=game_state,
                alpha=alpha,
                undo_group_id=self.id,
                settings=settings,
            ))

        # Note: group is NOT registered here. main.py calls
        # session.add_group(group) after construction so the Session owns
        # the registry of which groups belong to which (user, game).

    def execute(self) -> "TradeGroup":
        """Fire every trade sequentially. Members that can't size
        (RuntimeError) are logged and skipped; the rest still fire."""
        for t in self.trades:
            try:
                t.execute()
            except RuntimeError as e:
                logger.info(f"TradeGroup member skipped: {e}")
                with t._lock:
                    if t.status == "pending":
                        t.status = "canceled"
                # Best-effort DB row so "we tried and failed" is visible.
                t._log_to_db()
                t._notify_resolved()
        return self

    def cancel(self) -> list[dict]:
        """Cancel every trade in the group that's still in its undo window.
        Returns per-trade cancel results (empty if nothing was cancelable).
        """
        results: list[dict] = []
        for t in self.trades:
            try:
                res = t.cancel()
                if res:
                    results.append(res)
            except ValueError:
                # Already past undo window — expected for group cancel
                # where some members have activated. Don't abort the rest.
                continue
        return results


# ---------------------------------------------------------------------------
# Module API — Session-aware
#
# All lookups are O(1):
#   - (user_id, game_id) → Session       via _sessions
#   - trade_id           → Trade         via _trade_index
#   - (Trade).undo_group_id → TradeGroup via session.groups[gid]
# ---------------------------------------------------------------------------

def get_or_create_session(
    user_id: str,
    game_id: int,
    home_team: str | None = None,
    away_team: str | None = None,
    settings: dict | None = None,
) -> Session | None:
    """Return the in-memory Session for (user_id, game_id), creating it
    (and the Supabase row) on first touch. Subsequent calls are O(1) —
    this is the fix for the "Supabase round-trip on every /buy" pain point.

    Returns None if Supabase is down on the first call and we can't
    obtain a session_id — callers already handle None gracefully."""
    key = (user_id, game_id)
    with _registry_lock:
        cached = _sessions.get(key)
    if cached is not None:
        return cached

    # Cache miss — load the user's saved preferences from Supabase and seed
    # the new Session's settings dict with them. No more global mutation.
    from app.market_selector import DEFAULT_SETTINGS, clamp_settings
    seeded: dict = dict(DEFAULT_SETTINGS)
    try:
        saved_prefs = db.get_user_settings(user_id) or {}
        seeded.update(clamp_settings({
            k: v for k, v in saved_prefs.items() if k in _SETTINGS_WHITELIST
        }))
    except Exception as e:
        logger.error(f"get_or_create_session: failed to load saved settings: {e}")

    sid = db.get_or_create_session(user_id, game_id, home_team, away_team, seeded)
    if not sid:
        return None
    session = Session(sid, user_id, game_id, settings=seeded)
    with _registry_lock:
        # Double-checked: another thread may have just populated the cache.
        cached = _sessions.get(key)
        if cached is not None:
            return cached
        _sessions[key] = session
    return session


# Whitelist of keys allowed in session.settings / users.settings. Keep in
# sync with main.USER_SETTINGS_KEYS (a smoke-test below catches drift).
_SETTINGS_WHITELIST = frozenset({
    "alpha", "bet_size", "max_dollars", "max_slippage_cents",
    "use_undo_window", "use_stop_loss", "stop_loss_cents",
    "dry_run", "blowout_filter", "multi_market",
})


def update_session_settings(user_id: str, game_id: int, **kwargs) -> dict | None:
    """Apply a partial settings update to a specific user/game session.
    Returns the resulting settings dict, or None if no such session.

    Values are coerced / range-clamped by market_selector.clamp_settings
    before being merged, so caller-supplied junk (e.g. alpha=2.5) is
    sanitized to the same range the old update_settings enforced."""
    from app.market_selector import clamp_settings
    session = get_session(user_id, game_id)
    if session is None:
        return None
    clean = clamp_settings({k: v for k, v in kwargs.items() if k in _SETTINGS_WHITELIST})
    if not clean:
        return dict(session.settings)
    with session._lock:
        session.settings.update(clean)
        return dict(session.settings)


def update_user_settings_all_sessions(user_id: str, **kwargs) -> dict:
    """Apply a partial settings update to every open session for this user.
    Used by PUT /settings — changing a toggle in the UI fans out to all
    of the user's active games. Returns the resulting settings (from the
    last session touched, or the clamped input if no sessions exist yet)."""
    from app.market_selector import DEFAULT_SETTINGS, clamp_settings
    clean = clamp_settings({k: v for k, v in kwargs.items() if k in _SETTINGS_WHITELIST})
    with _registry_lock:
        mine = [s for s in _sessions.values() if s.user_id == user_id]
    if not mine:
        # No session yet — caller will still persist to DB; return the
        # defaults + clean merge so the response reflects the saved values.
        return {**DEFAULT_SETTINGS, **clean}
    latest = None
    for s in mine:
        with s._lock:
            s.settings.update(clean)
            latest = dict(s.settings)
    return latest


def get_session_settings(user_id: str, game_id: int) -> dict:
    """Return a copy of a specific session's settings. Falls back to
    DEFAULT_SETTINGS when no such session exists."""
    from app.market_selector import DEFAULT_SETTINGS
    session = get_session(user_id, game_id)
    if session is None:
        return dict(DEFAULT_SETTINGS)
    with session._lock:
        return dict(session.settings)


def get_effective_user_settings(user_id: str) -> dict:
    """Settings endpoint without a game context. Prefers the first active
    session's settings for this user; falls back to DEFAULT_SETTINGS. The
    user's DB-saved preferences are read separately by the caller and
    merged — this function only reflects in-memory state."""
    from app.market_selector import DEFAULT_SETTINGS
    with _registry_lock:
        mine = [s for s in _sessions.values() if s.user_id == user_id]
    if mine:
        with mine[0]._lock:
            return dict(mine[0].settings)
    return dict(DEFAULT_SETTINGS)


def get_first_session_for_game(game_id: int) -> Session | None:
    """For SSE-thread callers (compute_best_trades) that have only a
    game_id and need SOMEONE'S settings to produce a recommendation."""
    with _registry_lock:
        for s in _sessions.values():
            if s.game_id == game_id:
                return s
    return None


def get_session(user_id: str, game_id: int) -> Session | None:
    with _registry_lock:
        return _sessions.get((user_id, game_id))


def get_positions(user_id: str, game_id: int) -> dict:
    """Return the user's snapshot for this game. Empty shape when the
    session hasn't been touched yet — shape stays stable for the frontend."""
    session = get_session(user_id, game_id)
    if session is None:
        return {
            "positions": [],
            "pnl": {
                "realized": 0.0,
                "total_trades": 0,
                "wins": 0,
                "resolved": 0,
                "win_rate": 0.0,
            },
        }
    return session.snapshot()


def get_pnl(user_id: str, game_id: int) -> dict:
    return get_positions(user_id, game_id)["pnl"]


def get_open_trades_for_game(game_id: int) -> list[Trade]:
    """Used by market_selector._notify_sse, which fans price updates out
    across all users for a game and therefore can't scope to a user_id."""
    out: list[Trade] = []
    with _registry_lock:
        sessions = [s for s in _sessions.values() if s.game_id == game_id]
    for s in sessions:
        out.extend(s.active_trades())
    return out


def _find_trade(trade_id: str) -> Trade | None:
    with _registry_lock:
        return _trade_index.get(trade_id)


def execute_trade(
    game_id: int,
    event: str,
    trade_info: dict,
    max_dollars: float = 500.0,
    max_slippage_cents: float = 1.0,
    use_undo_window: bool = True,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    user_timestamp_ms: int | float | None = None,
    game_state: dict | None = None,
    alpha: float | None = None,
    undo_group_id: str | None = None,
    settings: dict | None = None,
) -> Trade:
    """Single-market path. Returns a Trade. main.py wraps this in a
    TradeGroup-of-one and registers with the Session."""
    trade = Trade(
        game_id=game_id,
        event=event,
        trade_info=trade_info,
        session_id=session_id,
        user_id=user_id,
        user_timestamp_ms=user_timestamp_ms,
        max_dollars=max_dollars,
        max_slippage_cents=max_slippage_cents,
        use_undo_window=use_undo_window,
        game_state=game_state,
        alpha=alpha,
        undo_group_id=undo_group_id,
        settings=settings,
    )
    return trade.execute()


def cancel_position(position_id: str) -> dict:
    """Cancel by trade id. O(1): trade → session → group.
    Raises ValueError if the single trade isn't in its undo window."""
    trade = _find_trade(position_id)
    if trade is None:
        raise ValueError(f"Position {position_id} not found")

    session = None
    if trade.user_id:
        session = get_session(trade.user_id, trade.game_id)

    group = session.groups.get(trade.undo_group_id) if session else None
    # Group-of-one also registers as a group on the Session; only expand
    # the cancel when it's actually a multi-trade basket.
    if group is not None and len(group.trades) > 1:
        results = group.cancel()
        if not results:
            raise ValueError(
                f"No positions in group {trade.undo_group_id} were cancelable "
                "(all already past the undo window)"
            )
        total_pnl = round(sum(r["realized_pnl"] for r in results), 2)
        return {
            "undo_group_id": trade.undo_group_id,
            "status": "canceled_by_user",
            "canceled": [r["position_id"] for r in results],
            "realized_pnl": total_pnl,
            "count": len(results),
        }

    # Single-trade path (raises ValueError if past undo window).
    return trade.cancel()


def check_stop_losses(market_ticker: str, prices: dict):
    """Websocket tick callback. Iterates active trades via sessions so we
    skip the huge tail of terminal trades that can't stop-out anyway.
    Each session's use_stop_loss setting is checked independently — one
    user's toggle doesn't disable stop-loss for anyone else."""
    with _registry_lock:
        sessions = list(_sessions.values())
    for s in sessions:
        if not s.settings.get("use_stop_loss", True):
            continue
        for t in s.active_trades():
            if t.market_ticker != market_ticker or t.status != "open":
                continue
            bid = prices["yes_bid"] if t.side == "YES" else prices["no_bid"]
            t.on_price_tick(bid)


def close_and_flush_sessions_for_game(game_id: int) -> None:
    """Called when a game goes Final. Writes each session's in-memory
    totals to Supabase via db.close_session, then drops them from the
    registry. Fixes the never-written total_trades / total_pnl columns."""
    with _registry_lock:
        victims = [
            ((uid, gid), s) for (uid, gid), s in _sessions.items()
            if gid == game_id
        ]
    for key, session in victims:
        try:
            db.close_session(session.id, session.total_trades, session.realized_pnl)
        except Exception as e:
            logger.error(f"close session {session.id} failed: {e}")
        with _registry_lock:
            _sessions.pop(key, None)
