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
from app.pnl import compute_net

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"filled", "stopped", "expired", "canceled", "canceled_by_user", "error"}


# ---------------------------------------------------------------------------
# Log-format helpers
# ---------------------------------------------------------------------------

def _short_ticker(ticker: str) -> str:
    """Collapse Kalshi MLB tickers into a compact form for log lines.

    Examples:
        KXMLBGAME-26APR121920CLEATL-CLE    → ML-CLE
        KXMLBTOTAL-26APR121610TEXLAD-8     → OU-8
        KXMLBSPREAD-26APR121410CWSKC-KC2   → SP-KC2

    Unknown tickers fall back to the raw string so nothing silently
    loses identity in the logs."""
    if not ticker:
        return ticker
    parts = ticker.split("-")
    if len(parts) < 2:
        return ticker
    head = parts[0]
    label = parts[-1]
    if head.startswith("KXMLBGAME"):
        return f"ML-{label}"
    if head.startswith("KXMLBTOTAL"):
        return f"OU-{label}"
    if head.startswith("KXMLBSPREAD"):
        return f"SP-{label}"
    # Generic fallback — keep prefix-label for unfamiliar series so
    # log readers can still eyeball the market.
    return f"{head}-{label}"


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
    """Place an order. Silent — callers handle the [TRADE ...] log line.
    Raw POST body is captured by the client-level hook for diagnostics."""
    if dry_run:
        return {"order_id": _fake_order_id(), "status": "resting"}
    return kalshi.place_order(**kwargs)


def _cancel_order(order_id: str, *, dry_run: bool) -> dict:
    """Cancel an order. Silent — callers handle the [TRADE ...] log line."""
    if dry_run:
        return {"order_id": order_id, "status": "canceled"}
    return kalshi.cancel_order(order_id)


def _actual_fill(
    response: dict,
    action: str = "buy",
    side: str = "YES",
) -> tuple[float, int] | None:
    """Return `(per_contract_price, filled_qty)` from a Kalshi order
    response, or `None` if the order did not fill.

    Buys: use `taker_fill_cost_dollars / fill_count_fp` (VWAP across
    filled levels), which matches what we actually paid on the bought
    side.

    Sells: `taker_fill_cost_dollars` semantics aren't reliable across
    YES vs. NO sells, so read `yes_price_dollars` from the response
    instead. Kalshi always echoes it, and it's unambiguous:
      - SELL YES → revenue = yes_price_dollars
      - SELL NO  → revenue = 1.00 - yes_price_dollars

    Logs every field we read so a schema-drift bug is visible in prod
    without new instrumentation. Dry-run / simulated responses don't
    carry fill data and return None.
    """
    if not isinstance(response, dict):
        return None
    qty_raw = response.get("fill_count_fp")
    cost_raw_taker = response.get("taker_fill_cost_dollars")
    cost_raw_maker = response.get("maker_fill_cost_dollars")
    yes_price_raw = response.get("yes_price_dollars")
    order_id = response.get("order_id")

    if qty_raw is None:
        return None
    try:
        qty = int(float(qty_raw))
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None

    if action == "sell":
        if yes_price_raw is None:
            logger.error(
                f"[kalshi] _actual_fill sell order_id={order_id} missing "
                f"yes_price_dollars in response; cannot derive exit price"
            )
            return None
        try:
            yes_price = float(yes_price_raw)
        except (TypeError, ValueError):
            return None
        per_contract = round(yes_price if side == "YES" else 1.0 - yes_price, 4)
        logger.debug(
            f"[kalshi] _actual_fill order_id={order_id} action=sell side={side} "
            f"fill_count_fp={qty_raw!r} yes_price_dollars={yes_price_raw!r} "
            f"→ qty={qty} per_contract=${per_contract:.4f}"
        )
        return (per_contract, qty)

    # --- buy path (unchanged): VWAP via taker_fill_cost_dollars / qty ---
    cost_raw = cost_raw_taker if cost_raw_taker is not None else cost_raw_maker
    if cost_raw is None:
        return None
    try:
        cost = float(cost_raw)
    except (TypeError, ValueError):
        return None

    per_contract = round(cost / qty, 4)
    logger.debug(
        f"[kalshi] _actual_fill order_id={order_id} action=buy side={side} "
        f"fill_count_fp={qty_raw!r} "
        f"taker_fill_cost_dollars={cost_raw_taker!r} "
        f"maker_fill_cost_dollars={cost_raw_maker!r} "
        f"→ qty={qty} total_cost=${cost:.4f} per_contract=${per_contract:.4f}"
    )
    # Sanity check: Kalshi contract prices are $0.01-$0.99. A
    # per-contract number outside that range almost certainly means
    # `*_fill_cost_dollars` is per-contract and we should NOT divide.
    # Flag loudly and fall back to using the raw cost as the price.
    if per_contract > 1.0 or per_contract <= 0:
        logger.error(
            f"[kalshi] per-contract price ${per_contract:.4f} is out of range "
            f"[0.01, 0.99] — assuming taker_fill_cost_dollars is already "
            f"per-contract. Using ${cost:.4f} as fill price."
        )
        return (round(cost, 4), qty)
    return (per_contract, qty)


def _fmt_pnl(pnl: float) -> str:
    """Format P&L as +$0.15 / -$0.72 / $0.00 for log lines."""
    if pnl > 0:
        return f"+${pnl:.2f}"
    if pnl < 0:
        return f"-${abs(pnl):.2f}"
    return "$0.00"


def _parse_fp(val) -> int:
    """Parse a Kalshi fixed-point count string (e.g. '3.00') into int.
    Returns 0 on None / unparseable input — matches historical behavior."""
    if val is None:
        return 0
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0


def _lookup_position_qty(market_ticker: str) -> int:
    """Ask Kalshi whether we hold a position on this ticker right now.

    Used as a fallback when the POST /portfolio/orders response claims
    0 fills — a real position on the ticker means the order actually
    filled and the app was about to orphan it.

    Returns 0 on any lookup failure (we don't want this path to mask
    legitimate zero-fill IOCs)."""
    try:
        positions = kalshi.get_live_positions(use_demo=False)
    except Exception as e:
        logger.error(f"_lookup_position_qty({market_ticker}) failed: {e}")
        return 0
    for pos in positions:
        if pos.get("market_ticker") == market_ticker:
            try:
                return int(pos.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _get_fill_count(order_id: str | None) -> int:
    """Follow-up GET for an order's fill count. DIAGNOSTIC ONLY now —
    Trade.execute reads fill_count_fp from the POST response. This call
    almost always 404s for filled IOC orders (they leave the open-orders
    index once filled); kept so we can confirm that behavior in logs."""
    if not order_id or not kalshi.client:
        return 0
    try:
        order = kalshi.get_order(order_id, use_demo=False)
        if order is None:
            return 0
        return _parse_fp(getattr(order, "fill_count_fp", None))
    except Exception as e:
        msg = str(e).lower()
        if "not_found" in msg or "404" in msg:
            return 0
        logger.error(f"_get_fill_count({order_id}): {e}")
        return 0


# ---------------------------------------------------------------------------
# Registries
#
# Sessions own per-(user, game) state and running aggregates. The module
# owns the by-id trade index (O(1) lookup is the index's job, not Session's).
# ---------------------------------------------------------------------------

_sessions: dict[tuple[str, int], "Session"] = {}
_trade_index: dict[str, "Trade"] = {}
# Parallel index keyed by the resting limit-sell's Kalshi order_id. Lets
# the websocket `fill` dispatcher find the owning Trade in O(1) without
# scanning every session. Populated when _activate() places the sell;
# cleared on terminal transition via _notify_resolved().
_trade_by_sell_order_id: dict[str, "Trade"] = {}
_registry_lock = threading.Lock()

# Tickers with an in-flight (non-terminal) trade. Guards against
# double-firing on the same market — a second tap while a position is
# already open would otherwise create an overlapping trade, doubling
# exposure and complicating exit (two resting sells, two timers, etc).
_active_tickers: set[str] = set()
_active_tickers_lock = threading.Lock()


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
        home_team: str | None = None,
        away_team: str | None = None,
    ):
        from app.market_selector import DEFAULT_SETTINGS
        self.id = session_id
        self.user_id = user_id
        self.game_id = game_id
        self.home_team = home_team
        self.away_team = away_team
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
        # Wall-clock for the [SESSION ...] CLOSED summary's duration field.
        self.created_at: datetime = datetime.utcnow()
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
        # Gross = (exit - entry) * qty. Net = gross - entry_fee - exit_fee.
        # `realized_pnl` is the net number (what Kalshi actually credits);
        # `gross_pnl` and the two fee legs are kept alongside so the audit
        # log and analytics can separate fee drag from alpha.
        self.realized_pnl: float = 0.0
        self.gross_pnl: float = 0.0
        self.entry_fee: float = 0.0
        self.exit_fee: float = 0.0
        self.exit_price: float | None = None
        self.completed_at: datetime | None = None
        self.created_at: datetime = datetime.utcnow()
        self.trade_db_id: str | None = None          # Supabase row pk

        self._lock = threading.Lock()
        self._undo_timer: threading.Timer | None = None
        self._clean_timer: threading.Timer | None = None
        # Throttle for tick-driven fill probes; REST position calls are
        # rate-limited so we don't hammer /portfolio/positions on every
        # tick once bid >= sell_target.
        self._last_fill_probe_ts: float = 0.0
        # Set to True if this trade is the one that claimed the ticker
        # in the _active_tickers dedupe guard. Only the claimant releases
        # on terminal transition — a skipped-duplicate trade must not
        # discard the claim the original trade still holds.
        self._owns_ticker_claim: bool = False

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
        # Late import: trader → market_selector → trader would cycle at
        # module load; deferring to call time keeps the graph clean.
        from app.market_selector import _game_teams, _teams_lock
        from app.bet_label import compute_display_label
        with _teams_lock:
            home_abbr, away_abbr = _game_teams.get(self.game_id, ("", ""))
        display_label = compute_display_label(
            market_ticker=self.market_ticker,
            side=self.side,
            market_type=self.market_type,
            home_abbr=home_abbr,
            away_abbr=away_abbr,
        )
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
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "display_label": display_label,
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

    # -- Log helpers --------------------------------------------------------

    @property
    def _log_prefix(self) -> str:
        """Every lifecycle log line is scoped by `[TRADE {event} {id8}]`
        so you can grep a single trade end-to-end without seeing the
        rest of the session's traffic."""
        return f"[TRADE {self.event} {self.id[:8]}]"

    @property
    def _short(self) -> str:
        """Compact form of the market ticker for logs (e.g. ML-CLE)."""
        return _short_ticker(self.market_ticker)

    def _coid(self, leg: str) -> str:
        """Idempotency key sent to Kalshi on every order we place.

        Format: `liveline-<trade_id[:8]>-<leg>` where leg is `b` (IOC
        buy), `s` (resting sell), or `x` (IOC force-exit). All three
        share the `liveline-` prefix so startup cleanup can cancel only
        our orders without touching any other Kalshi activity on the
        same account. The `-<leg>` suffix keeps each order's key unique
        so Kalshi's idempotency guard doesn't reject a later leg of the
        same trade as a duplicate of an earlier one."""
        return f"liveline-{self.id[:8]}-{leg}"

    # -- Lifecycle ----------------------------------------------------------

    def execute(self) -> "Trade":
        """Size → IOC buy → log → undo window (or activate immediately).

        Preserves the legacy contract: raises RuntimeError if sizing
        returns 0 contracts (no orderbook liquidity). A zero-fill IOC does
        NOT raise — it sets status="canceled" and returns.
        """
        # Already registered in _trade_index by __init__.

        # Dedupe: if this ticker is already in-flight, bail out before
        # placing any order. Released in _notify_resolved on terminal
        # transitions. This is the simplest "no orphan" guarantee —
        # the second trade never touches Kalshi.
        with _active_tickers_lock:
            if self.market_ticker in _active_tickers:
                logger.info(
                    f"{self._log_prefix} SKIP {self._short} — ticker already in-flight"
                )
                with self._lock:
                    self.status = "canceled"
                self._log_to_db()
                self._notify_resolved()
                return self
            _active_tickers.add(self.market_ticker)
            self._owns_ticker_claim = True

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

        # Execution-time safety check. _evaluate_market already gates on
        # `sell_target > entry + 0.01`, but evaluation uses best-ask for
        # entry while execution uses VWAP across the filled levels. If
        # prices moved up between eval and fill (or we took a deep book)
        # VWAP can exceed the original sell_target — placing the buy
        # would lock in a guaranteed loss the moment the limit sell rests.
        # Abort before touching Kalshi.
        if self.sell_target <= self.entry_price + 0.01:
            logger.error(
                f"{self._log_prefix} ABORT sell_target=${self.sell_target:.2f} "
                f"<= entry=${self.entry_price:.2f} + 1¢ — "
                f"would lock in a loss; canceling before buy"
            )
            with self._lock:
                self.status = "canceled"
            self._log_to_db()
            self._notify_resolved()
            return self

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
            client_order_id=self._coid("b"),
        )
        self.buy_order_id = buy_result.get("order_id")

        if dry_run:
            filled_qty = quantity
        else:
            # Fill count comes from the POST response body. Follow-up GET
            # is unreliable for IOC (filled orders 404 out of the index).
            filled_qty = _parse_fp(buy_result.get("fill_count_fp"))
            # Last-ditch fallback: if POST says 0, sanity-check positions.
            # Only logs when it actually saves us from an orphan.
            if filled_qty == 0:
                pos_qty = _lookup_position_qty(self.market_ticker)
                if pos_qty > 0:
                    logger.warning(
                        f"{self._log_prefix} ORPHAN-SAVE {self._short} "
                        f"POST fill=0 but Kalshi position qty={pos_qty}"
                    )
                    filled_qty = pos_qty

        if filled_qty == 0:
            logger.info(f"{self._log_prefix} BUY {self._short} → 0-fill (canceled)")
            with self._lock:
                self.status = "canceled"
            self._log_to_db()
            self._notify_resolved()
            # Safety net: IOC "no fill" is the most suspect path for lost
            # trades. Confirm Kalshi agrees there's no position.
            self._verify_position_closed(reason="zero-fill IOC")
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

        logger.info(
            f"{self._log_prefix} BUY {filled_qty} {self.side} {self._short} "
            f"@${self.entry_price:.2f} → filled "
            f"(target=${self.sell_target:.2f}, stop=${self.stop_loss:.2f})"
        )

        if self.use_undo_window:
            self._undo_timer = threading.Timer(UNDO_WINDOW_SECONDS, self._activate)
            self._undo_timer.daemon = True
            self._undo_timer.start()
        else:
            self._activate()

        return self

    def _activate(self):
        """Place the limit sell and start the clean-window timer."""
        with self._lock:
            if self.status not in ("undo_window", "open"):
                return   # user canceled during the window
            self.status = "open"

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
                client_order_id=self._coid("s"),
            )
            self.sell_order_id = sell_result.get("order_id")
            if self.sell_order_id:
                # Index for the websocket fill dispatcher. Unregistered
                # in _notify_resolved on any terminal transition.
                with _registry_lock:
                    _trade_by_sell_order_id[self.sell_order_id] = self
            logger.info(
                f"{self._log_prefix} SELL {self.quantity} {self.side} {self._short} "
                f"@${self.sell_target:.2f} → resting"
            )
        except Exception as e:
            logger.error(
                f"{self._log_prefix} SELL placement FAILED: {e} — "
                f"buy filled qty={self.quantity} but sell did not rest; verifying position"
            )
            with self._lock:
                self.status = "error"
            self._update_db_status("error")
            self._notify_resolved()
            # Orphan risk: buy filled, sell failed. Confirm against Kalshi.
            self._verify_position_closed(reason="sell placement failed")
            return

        self._update_db_status("open")

        self._clean_timer = threading.Timer(CLEAN_WINDOW_SECONDS, self._expire)
        self._clean_timer.daemon = True
        self._clean_timer.start()

    def _expire(self):
        """Clean-window fired. If the limit sell already filled, finalize
        as 'filled' at target. Otherwise cancel the sell and IOC-exit."""
        with self._lock:
            if self.status != "open":
                return

        # Before force-exiting, check whether the limit sell already
        # executed. Cancel-404 or position=0 are both definitive. Selling
        # contracts we don't hold creates a phantom fill / short position.
        if self._sell_already_filled():
            resolved = self._resolve_actual_exit_price()
            if resolved is None:
                self._mark_filled_at_target()
            else:
                price, qty = resolved
                self._mark_filled_at_target(actual_price=price, actual_qty=qty)
            return

        with self._lock:
            self.status = "expired"

        logger.info(f"{self._log_prefix} TIMER {CLEAN_WINDOW_SECONDS}s expired — force exit")
        dry_run = bool(self._settings.get("dry_run", True))

        # Sell cancel already attempted by _sell_already_filled() (dry_run
        # skips it entirely). No need to cancel again here.

        exit_price = self._ioc_exit(reason="expired", dry_run=dry_run)
        with self._lock:
            self._finalize_pnl(
                exit_price=exit_price, qty=self.quantity, exit_role="taker",
            )
            self.exit_price = exit_price
            self.completed_at = datetime.utcnow()

        logger.info(
            f"{self._log_prefix} RESOLVED expired pnl={_fmt_pnl(self.realized_pnl)} "
            f"(gross={_fmt_pnl(self.gross_pnl)} fees=${self.entry_fee + self.exit_fee:.2f})"
        )
        self._update_db_status("expired", exit_price, self.realized_pnl)
        self._notify_resolved()
        self._verify_position_closed(reason="expired")

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
        logger.info(f"{self._log_prefix} UNDO user tapped cancel (qty={self.quantity})")

        if self._undo_timer:
            self._undo_timer.cancel()

        dry_run = bool(self._settings.get("dry_run", True))
        exit_price = 0.0
        if self.quantity > 0:
            exit_price = self._ioc_exit(reason="canceled_by_user", dry_run=dry_run)

        with self._lock:
            self._finalize_pnl(
                exit_price=exit_price, qty=self.quantity, exit_role="taker",
            )
            self.exit_price = exit_price
            self.completed_at = datetime.utcnow()

        logger.info(
            f"{self._log_prefix} RESOLVED canceled_by_user pnl={_fmt_pnl(self.realized_pnl)} "
            f"(gross={_fmt_pnl(self.gross_pnl)} fees=${self.entry_fee + self.exit_fee:.2f})"
        )
        self._update_db_status("canceled_by_user", exit_price, self.realized_pnl)
        self._notify_resolved()
        self._verify_position_closed(reason="canceled_by_user")

        return {
            "position_id": self.id,
            "status": self.status,
            "quantity": self.quantity,
            "realized_pnl": self.realized_pnl,
            "undo_group_id": self.undo_group_id,
        }

    def on_price_tick(self, bid: float):
        """Called on every Kalshi tick for this ticker.

        Runs two checks:
          - Fill probe: if bid has reached sell_target, confirm against
            live positions (throttled). Catches the limit sell filling
            before the 45s clean-window timer fires.
          - Stop-loss: if bid dropped below stop, force exit.
        """
        with self._lock:
            if self.status != "open":
                return

        # Fill probe — only when bid actually reaches the target, and at
        # most once every ~2s so a rapid flurry of ticks doesn't hammer
        # /portfolio/positions.
        if bid >= self.sell_target and not bool(self._settings.get("dry_run", True)):
            import time as _time
            now = _time.monotonic()
            if now - self._last_fill_probe_ts >= 2.0:
                self._last_fill_probe_ts = now
                if _lookup_position_qty(self.market_ticker) == 0:
                    # Guard against racing with _expire / stop-loss.
                    with self._lock:
                        if self.status != "open":
                            return
                    logger.info(
                        f"{self._log_prefix} FILL tick-detected "
                        f"bid=${bid:.2f} ≥ target=${self.sell_target:.2f}"
                    )
                    resolved = self._resolve_actual_exit_price()
                    if resolved is None:
                        self._mark_filled_at_target()
                    else:
                        price, qty = resolved
                        self._mark_filled_at_target(actual_price=price, actual_qty=qty)
                    return

        if not self._settings.get("use_stop_loss", True):
            return
        if bid <= self.stop_loss:
            logger.warning(
                f"{self._log_prefix} STOP-LOSS bid=${bid:.2f} ≤ stop=${self.stop_loss:.2f}"
            )
            self._execute_stop_loss(bid)

    def _execute_stop_loss(self, exit_price: float):
        with self._lock:
            if self.status != "open":
                return

        # Rare but possible: stop-loss fires after the limit sell already
        # filled at target. If so, record the profitable fill rather than
        # selling phantom contracts.
        if self._sell_already_filled():
            resolved = self._resolve_actual_exit_price()
            if resolved is None:
                self._mark_filled_at_target()
            else:
                price, qty = resolved
                self._mark_filled_at_target(actual_price=price, actual_qty=qty)
            return

        with self._lock:
            self.status = "stopped"

        dry_run = bool(self._settings.get("dry_run", True))

        if self._clean_timer:
            self._clean_timer.cancel()
        # Sell cancel already attempted inside _sell_already_filled().

        # Trigger bid is just an estimate; use the real IOC fill price
        # instead so P&L reflects what actually executed on Kalshi.
        actual_exit = self._ioc_exit(reason="stopped", dry_run=dry_run, fallback_price=exit_price)

        with self._lock:
            self._finalize_pnl(
                exit_price=actual_exit, qty=self.quantity, exit_role="taker",
            )
            self.exit_price = actual_exit
            self.completed_at = datetime.utcnow()

        logger.info(
            f"{self._log_prefix} RESOLVED stopped pnl={_fmt_pnl(self.realized_pnl)} "
            f"(gross={_fmt_pnl(self.gross_pnl)} fees=${self.entry_fee + self.exit_fee:.2f})"
        )
        self._update_db_status("stopped", actual_exit, self.realized_pnl)
        self._notify_resolved()
        self._verify_position_closed(reason="stopped")

    # -- Fill detection ----------------------------------------------------

    def _sell_already_filled(self) -> bool:
        """Return True only when the resting limit sell has definitely
        executed on Kalshi. Exit-path sanity check — before any `_expire`
        or stop-loss force-exit, confirm whether the sell is still resting.

        Signal hierarchy:
          - Cancel returns 200 → sell was resting and we just canceled it.
            Contracts are still held; caller must IOC-exit. Return False.
            (A stale positions lookup here previously caused false-positive
            'filled' reports — never cross-check on a successful cancel.)
          - Cancel raises 404 / not_found → order left the open-orders
            index. For a GTC limit sell the only way that happens is a
            fill. Cross-check positions: qty==0 confirms fill → True;
            qty>0 means we're in a weird partial / race state → False
            (caller proceeds with IOC-exit on what remains).
          - Any other exception → re-raise. Don't guess; let the caller
            (clean-window timer) surface the problem rather than act on
            ambiguous state.

        Dry-run never actually fills, so we short-circuit False there."""
        if bool(self._settings.get("dry_run", True)):
            return False
        if not self.sell_order_id:
            return False

        try:
            _cancel_order(self.sell_order_id, dry_run=False)
            # 200 — the sell was still resting when we canceled it.
            # We still hold the contracts; IOC exit is required.
            logger.info(f"{self._log_prefix} CANCEL sell → 200 OK (was resting)")
            return False
        except Exception as e:
            msg = str(e).lower()
            if "not_found" not in msg and "404" not in msg:
                raise
            logger.info(f"{self._log_prefix} CANCEL sell → 404 (already gone)")
            qty = _lookup_position_qty(self.market_ticker)
            logger.info(f"{self._log_prefix} POSITION CHECK → {qty} contracts")
            if qty == 0:
                return True
            logger.warning(
                f"{self._log_prefix} partial/race — sell 404 but qty={qty}; caller will IOC-exit"
            )
            return False

    def _ioc_exit(
        self,
        *,
        reason: str,
        dry_run: bool,
        fallback_price: float | None = None,
    ) -> float:
        """Place the $0.01 IOC force-exit sell and return the per-contract
        exit price that actually executed on Kalshi.

        Kalshi fills IOC sells at whatever bid was available (not the
        $0.01 limit), so the real exit price comes from the POST response:
        `taker_fill_cost_dollars / fill_count_fp`. This is what P&L must
        use — using the $0.01 limit would over-report losses by the
        entire bid value.

        If the IOC doesn't fill at all (no bids at ≥$0.01 — rare), we
        fall back to `fallback_price` if provided, otherwise the current
        bid on the trade's side. Dry-run always uses the current bid."""
        limit_price = 0.01
        try:
            response = _place_order(
                market_ticker=self.market_ticker,
                action="sell",
                side=self.side,
                quantity=self.quantity,
                price=limit_price,
                time_in_force="ioc",
                dry_run=dry_run,
                client_order_id=self._coid("x"),
            )
        except Exception as e:
            logger.error(f"{self._log_prefix} EXIT IOC sell failed: {e}")
            return self._fallback_exit_price(fallback_price)

        if dry_run:
            # Sim doesn't report real fills — approximate with current bid.
            exit_price = self._fallback_exit_price(fallback_price)
            logger.info(
                f"{self._log_prefix} EXIT IOC sell {self.quantity} → "
                f"filled @${exit_price:.2f} [sim] (limit was ${limit_price:.2f})"
            )
            return exit_price

        fill = _actual_fill(response, action="sell", side=self.side)
        if fill is None:
            # IOC returned no fills — no bids at ≥$0.01 (very rare). Use
            # the fallback / current bid so P&L still reflects something.
            exit_price = self._fallback_exit_price(fallback_price)
            logger.warning(
                f"{self._log_prefix} EXIT IOC sell {self.quantity} → 0 fills "
                f"(limit was ${limit_price:.2f}); using fallback ${exit_price:.2f}"
            )
            return exit_price

        per_contract, filled_qty = fill
        tag = "filled" if filled_qty == self.quantity else f"partial {filled_qty}/{self.quantity}"
        logger.info(
            f"{self._log_prefix} EXIT IOC sell {self.quantity} → "
            f"{tag} @${per_contract:.2f} (limit was ${limit_price:.2f})"
        )
        return per_contract

    def _fallback_exit_price(self, fallback_price: float | None) -> float:
        """Used when an IOC exit returns no fill data (sim, zero-fill,
        or placement error). Prefer the caller-supplied fallback (e.g.
        the bid that triggered a stop-loss), then the current bid on
        this trade's side, then 0.0 as last resort."""
        if fallback_price is not None:
            return fallback_price
        try:
            prices = kalshi.get_prices(self.market_ticker)
            return prices["yes_bid"] if self.side == "YES" else prices["no_bid"]
        except Exception:
            return 0.0

    def _resolve_actual_exit_price(self, ws_msg=None) -> tuple[float, int] | None:
        """Return (per_contract_vwap, qty) for the resting limit sell's
        execution, in this trade's side-units. None if the real fill data
        isn't retrievable — caller falls back to sell_target.

        Source priority:
          1. REST /portfolio/fills via `kalshi.get_order_fill_vwap` —
             authoritative, captures partial fills at multiple levels and
             maker price improvement.
          2. WS FillMessage payload (`yes_price_dollars` / `count_fp`) —
             single-fill fallback when REST is unavailable; fine for the
             common case where the limit sell filled in one chunk.

        Dry-run short-circuits to None so sim behavior is unchanged.
        """
        if bool(self._settings.get("dry_run", True)):
            return None
        if not self.sell_order_id:
            return None

        try:
            vwap_qty = kalshi.get_order_fill_vwap(self.sell_order_id, self.side)
        except Exception as e:
            logger.error(f"{self._log_prefix} get_order_fill_vwap failed: {e}")
            vwap_qty = None
        if vwap_qty is not None:
            return vwap_qty

        if ws_msg is not None:
            try:
                yes_price_raw = getattr(ws_msg, "yes_price_dollars", None)
                count_raw = getattr(ws_msg, "count_fp", None)
                if yes_price_raw is not None and count_raw is not None:
                    yes_price = float(yes_price_raw)
                    qty = int(float(count_raw))
                    if qty > 0:
                        per_contract = round(
                            yes_price if self.side == "YES" else 1.0 - yes_price, 4
                        )
                        return (per_contract, qty)
            except (TypeError, ValueError) as e:
                logger.error(f"{self._log_prefix} parse ws fill payload: {e}")
        return None

    def _mark_filled_at_target(
        self, *, actual_price: float | None = None, actual_qty: int | None = None,
    ) -> None:
        """Finalize the trade as 'filled'. Prefers `actual_price` (real
        Kalshi execution) over `sell_target` (posted limit) so logged
        P&L matches the audit.

        Reachable from three threads now: the clean-window timer, the
        price-tick probe, and the websocket fill dispatcher. The first
        lock-holder wins via CAS (status must be 'open' to transition);
        any racing callers no-op. All side effects (timer cancel, DB
        write, SSE notify) happen OUTSIDE the lock to avoid deadlocks —
        _notify_resolved takes _registry_lock / _active_tickers_lock,
        and nesting those inside self._lock would invert the acquisition
        order used elsewhere.
        """
        if actual_price is None:
            exit_price = self.sell_target
            # Degraded row — execution price unavailable. Tag loudly so
            # CSV/Kalshi drift is grep-able without new instrumentation.
            if not bool(self._settings.get("dry_run", True)):
                logger.warning(
                    f"{self._log_prefix} FILL exit-price fallback sell_order_id="
                    f"{self.sell_order_id} — logging sell_target=${self.sell_target:.2f}"
                )
        else:
            exit_price = actual_price
        qty = actual_qty if actual_qty is not None else self.quantity

        with self._lock:
            if self.status != "open":
                # Another path (timer / stop-loss / websocket) already
                # claimed the transition. No-op.
                return
            self.status = "filled"
            # Limit sell at target → maker fee on the exit leg.
            self._finalize_pnl(exit_price=exit_price, qty=qty, exit_role="maker")
            self.exit_price = exit_price
            self.completed_at = datetime.utcnow()
        # Lock released — safe to do side-effects that may take other locks.
        if self._clean_timer:
            self._clean_timer.cancel()
        logger.info(
            f"{self._log_prefix} RESOLVED filled pnl={_fmt_pnl(self.realized_pnl)} "
            f"(qty={qty} @${exit_price:.2f} gross={_fmt_pnl(self.gross_pnl)} "
            f"fees=${self.entry_fee + self.exit_fee:.2f})"
        )
        self._update_db_status("filled", exit_price, self.realized_pnl)
        self._notify_resolved()

    def on_websocket_fill(self, msg) -> None:
        """Called from the Kalshi feed thread when a private fill message
        arrives whose order_id matches our resting sell. The cheap
        pre-check here avoids a log line when we're already resolved;
        the authoritative guard is the status-CAS inside
        _mark_filled_at_target.
        """
        oid = getattr(msg, "order_id", None)
        if not oid or oid != self.sell_order_id:
            return
        with self._lock:
            if self.status != "open":
                return
        logger.info(
            f"{self._log_prefix} FILL ws-detected order_id={oid}"
        )
        resolved = self._resolve_actual_exit_price(ws_msg=msg)
        if resolved is None:
            self._mark_filled_at_target()
        else:
            price, qty = resolved
            self._mark_filled_at_target(actual_price=price, actual_qty=qty)

    # -- Safety net --------------------------------------------------------

    def _verify_position_closed(self, *, reason: str) -> None:
        """After a terminal transition, confirm we don't have an orphaned
        position on Kalshi. Dry-run is skipped (no real position possible).

        Runs best-effort: exceptions are logged and swallowed so the
        verification never masks a real terminal-state transition.
        """
        if bool(self._settings.get("dry_run", True)):
            return
        try:
            positions = kalshi.get_live_positions(use_demo=False)
        except Exception as e:
            logger.error(
                f"_verify_position_closed: failed to fetch positions "
                f"(trade {self.id} {self.market_ticker}, reason={reason}): {e}"
            )
            return
        for pos in positions:
            if pos.get("market_ticker") != self.market_ticker:
                continue
            qty = int(pos.get("quantity", 0) or 0)
            if qty == 0:
                continue
            logger.error(
                f"{self._log_prefix} ORPHANED POSITION {self._short} qty={qty} "
                f"status={self.status} reason={reason} "
                f"buy_order_id={self.buy_order_id} sell_order_id={self.sell_order_id}"
            )

    # -- Supabase ----------------------------------------------------------

    def _log_to_db(self):
        """Insert the initial trades row. Populates self.trade_db_id.
        Single INSERT now that `log_trade` takes `undo_group_id` directly."""
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
        reference so Trade stays ignorant of the Session object.

        Also pushes a `positions_update` SSE event to the owning user so
        the UI flips from active → terminal immediately instead of waiting
        for the next 60s /positions poll. Every terminal-state path in
        Trade (and TradeGroup.execute's skip branch) routes through here,
        so one hook covers them all."""
        # Release the dedupe claim — but only if THIS trade is the one
        # that took it. A skipped-duplicate trade also reaches this path
        # and must not free the slot the original trade still holds.
        if self._owns_ticker_claim:
            with _active_tickers_lock:
                _active_tickers.discard(self.market_ticker)
            self._owns_ticker_claim = False

        # Drop the websocket-fill index entry. pop(..., None) is safe
        # if another path already removed it (double-resolve race) or
        # the sell never placed (error path, sell_order_id still None).
        if self.sell_order_id:
            with _registry_lock:
                _trade_by_sell_order_id.pop(self.sell_order_id, None)

        with _registry_lock:
            session = _sessions.get((self.user_id, self.game_id)) if self.user_id else None
        if session is not None:
            try:
                session.on_trade_resolved(self)
            except Exception as e:
                logger.error(f"_notify_resolved: {e}")

        # SSE push. Late import to avoid circular load.
        try:
            from app.market_selector import notify_sse_positions_changed
            notify_sse_positions_changed(self.game_id, self.user_id)
        except Exception as e:
            logger.error(f"_notify_resolved SSE push failed: {e}")

    def _finalize_pnl(
        self, *, exit_price: float, qty: int, exit_role: str,
    ) -> None:
        """Compute gross, fees, and net P&L for a closed trade and stash
        them on `self`. Caller must hold self._lock.

        Delegates the math to `app.pnl.compute_net` — same module the
        backtest calls, so the two systems can't drift. `exit_role` is
        "maker" when the resting limit sell executed at target, "taker"
        when we crossed the book on an IOC (expire / undo / stop-loss).
        Entry is always taker. Prices are in traded-side units (already
        NO-flipped upstream for NO trades).
        """
        pnl = compute_net(self.entry_price, exit_price, qty, exit_role)
        self.gross_pnl = pnl["gross"]
        self.entry_fee = pnl["entry_fee"]
        self.exit_fee = pnl["exit_fee"]
        self.realized_pnl = pnl["net"]

    def _update_db_status(
        self,
        status: str,
        exit_price: float | None = None,
        pnl: float | None = None,
    ):
        try:
            db.update_trade_status(
                self.trade_db_id, status, exit_price, pnl,
                gross_pnl=self.gross_pnl if self.completed_at else None,
                entry_fee=self.entry_fee if self.completed_at else None,
                exit_fee=self.exit_fee if self.completed_at else None,
            )
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
                logger.info(f"{t._log_prefix} SKIP {t._short} — {e}")
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
    session = Session(
        sid, user_id, game_id, settings=seeded,
        home_team=home_team, away_team=away_team,
    )
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
    "dry_run", "blowout_filter", "multi_market", "min_move_cents",
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


def update_user_settings_all_sessions(
    user_id: str, *, saved_prefs: dict | None = None, **kwargs,
) -> dict:
    """Apply a partial settings update to every open session for this user.
    Used by PUT /settings — changing a toggle in the UI fans out to all
    of the user's active games. Returns the resulting settings (from the
    last session touched, or defaults+saved_prefs+clean when no sessions
    exist yet).

    `saved_prefs` is the user's persisted preferences from Supabase. It
    MUST be passed when there are no active sessions — otherwise the
    no-session branch returns a fresh copy of DEFAULT_SETTINGS with only
    the one changed field overlaid, resetting every other preference
    the user had saved. The endpoint does the DB read (via
    asyncio.to_thread) and hands the result in so this function stays
    sync and I/O-free."""
    from app.market_selector import DEFAULT_SETTINGS, clamp_settings
    clean = clamp_settings({k: v for k, v in kwargs.items() if k in _SETTINGS_WHITELIST})
    with _registry_lock:
        mine = [s for s in _sessions.values() if s.user_id == user_id]
    if not mine:
        # Base = defaults, overlaid with the user's persisted prefs, then
        # the clamped new change. Without saved_prefs, any key the user
        # had toggled away from its default would snap back on every
        # subsequent change — e.g. use_undo_window=False quietly
        # reverting to True when the user toggles dry_run.
        saved_clean = clamp_settings({
            k: v for k, v in (saved_prefs or {}).items()
            if k in _SETTINGS_WHITELIST
        })
        return {**DEFAULT_SETTINGS, **saved_clean, **clean}
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


# Public alias for callers outside this module (e.g. the debug endpoint).
find_trade = _find_trade


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


def dispatch_websocket_fill(msg):
    """Websocket fill handler. Called on the Kalshi feed thread for every
    private fill event on the account. Looks up the owning Trade by the
    message's order_id and routes to Trade.on_websocket_fill.

    Must not raise: the feed listener runs every registered handler in a
    tight loop and an uncaught exception here could interfere with
    sibling handlers or be logged at a confusing site. All errors are
    swallowed and logged locally.
    """
    try:
        if msg is None:
            return
        oid = getattr(msg, "order_id", None)
        if not oid:
            return
        with _registry_lock:
            trade = _trade_by_sell_order_id.get(oid)
        if trade is None:
            # Not one of ours (our buy fill, someone else's demo account,
            # or a stale fill after we already resolved the trade).
            return
        trade.on_websocket_fill(msg)
    except Exception as e:
        logger.error(f"dispatch_websocket_fill: {e}")


def check_stop_losses(market_ticker: str, prices: dict):
    """Websocket tick callback. Iterates active trades via sessions so we
    skip the huge tail of terminal trades that can't react to ticks.

    Always fires `on_price_tick`, which internally runs:
      - fill probe (always) — detects a limit sell hitting target
      - stop-loss check (gated on the session's use_stop_loss)

    Previously this was gated up front by use_stop_loss, which also
    disabled fill detection. Keep the gate inside on_price_tick."""
    with _registry_lock:
        sessions = list(_sessions.values())
    for s in sessions:
        for t in s.active_trades():
            if t.market_ticker != market_ticker or t.status != "open":
                continue
            bid = prices["yes_bid"] if t.side == "YES" else prices["no_bid"]
            t.on_price_tick(bid)


# ---------------------------------------------------------------------------
# Startup cleanup
# ---------------------------------------------------------------------------

LIVELINE_COID_PREFIX = "liveline-"
_MLB_TICKER_MARKER = "KXMLB"


def cleanup_orphaned_liveline_orders() -> dict:
    """Called once at startup (after kalshi.connect()). Two jobs:

      1. Cancel every resting order whose client_order_id starts with
         `liveline-`. These are orders LiveLine placed before a crash/
         restart; the in-memory Trade objects that were tracking them
         are gone, so they're unmanaged. Only our orders are touched —
         the prefix filter means any other Kalshi activity on the same
         account (hand-placed trades, other tools) is left alone.
      2. Warn about any open MLB position. Kalshi doesn't tag positions
         with the order that created them, so we can't tell which ones
         are ours. Flatten-on-startup would be dangerous (could sell
         someone else's position); instead we log loudly so the operator
         can reconcile manually.

    Returns a summary dict for logging / tests. Never raises — startup
    must proceed even if Kalshi is flaky."""
    summary = {"orders_canceled": 0, "orders_failed": 0, "positions_flagged": 0}

    # --- orders ---
    try:
        orders = kalshi.get_open_orders(use_demo=False)
    except Exception as e:
        logger.error(f"[STARTUP] Failed to list open orders: {e}")
        orders = []

    for o in orders:
        coid = o.get("client_order_id") or ""
        if not coid.startswith(LIVELINE_COID_PREFIX):
            continue
        order_id = o.get("order_id")
        ticker = o.get("ticker")
        logger.warning(
            f"[STARTUP] Orphaned LiveLine order: {order_id} "
            f"coid={coid} ticker={ticker} — cancelling"
        )
        try:
            kalshi.cancel_order(order_id)
            summary["orders_canceled"] += 1
        except Exception as e:
            # 404 = order already gone (filled / canceled during the gap).
            # Anything else is real — log it but keep going.
            msg = str(e).lower()
            if "not_found" in msg or "404" in msg:
                logger.info(f"[STARTUP] Order {order_id} already gone (404)")
                continue
            summary["orders_failed"] += 1
            logger.error(f"[STARTUP] Failed to cancel {order_id}: {e}")

    # --- positions ---
    try:
        positions = kalshi.get_live_positions(use_demo=False)
    except Exception as e:
        logger.error(f"[STARTUP] Failed to list positions: {e}")
        positions = []

    for pos in positions:
        ticker = pos.get("market_ticker") or ""
        qty = int(pos.get("quantity", 0) or 0)
        if qty == 0:
            continue
        if _MLB_TICKER_MARKER not in ticker:
            continue
        summary["positions_flagged"] += 1
        logger.warning(
            f"[STARTUP] Open MLB position: {ticker} qty={qty} — "
            f"manual review recommended (not auto-selling; "
            f"can't distinguish LiveLine from hand-placed positions)"
        )

    logger.info(
        f"[STARTUP] Cleanup complete: canceled={summary['orders_canceled']} "
        f"failed={summary['orders_failed']} "
        f"positions_flagged={summary['positions_flagged']}"
    )
    return summary


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

        # Session summary line — one per user per game, at close.
        dur_min = max(0, int((datetime.utcnow() - session.created_at).total_seconds() // 60))
        teams = (
            f"{session.away_team}@{session.home_team}"
            if session.home_team and session.away_team
            else "?@?"
        )
        logger.info(
            f"[SESSION {session.id[:8]}] CLOSED game={game_id} {teams} "
            f"trades={session.total_trades} wins={session.wins} "
            f"resolved={session.resolved} "
            f"pnl={_fmt_pnl(session.realized_pnl)} duration={dur_min}m"
        )

        with _registry_lock:
            _sessions.pop(key, None)
