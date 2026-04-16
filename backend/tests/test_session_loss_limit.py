"""
Tests for C5: session loss limit.

Spec: when Session.realized_pnl <= settings.session_loss_limit (negative),
  - arm the user's kill switch so /buy returns 503
  - emit a session_limit_reached SSE event ONCE per session
  - existing unresolved trades left alone (they'll flatten via the
    kill-switch sweep if any, or resolve normally)

Two trip points:
  1. A trade resolves and pushes session.realized_pnl over the floor
     (Session.on_trade_resolved fires SSE + arms kill).
  2. A user taps /buy after the floor is already crossed
     (Trade.execute re-checks and also arms kill, even if on_trade_resolved
     missed it — defense in depth).

Run:  cd backend && python -m pytest tests/test_session_loss_limit.py -v
"""

import threading
from unittest.mock import MagicMock

import pytest

from app import trader, market_selector
from app.market_selector import clamp_settings
from app.trader import (
    KillSwitchArmedError,
    Session,
    Trade,
    TradeGroup,
    _active_tickers,
    _active_tickers_lock,
    _killed_users,
    _registry_lock,
    _sessions,
    _trade_by_sell_order_id,
    _trade_index,
    is_kill_switch_armed,
)


@pytest.fixture(autouse=True)
def mock_sse_positions(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(market_selector, "notify_sse_positions_changed", mock)
    return mock


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    with _registry_lock:
        _trade_by_sell_order_id.clear()
        _trade_index.clear()
        _sessions.clear()
    _killed_users.clear()
    with _active_tickers_lock:
        _active_tickers.clear()


def _make_session(*, loss_limit: float = -100.0, user_id: str = "user-loss") -> Session:
    """Insert a Session into the global registry so Trade.execute finds it."""
    sess = Session(
        session_id="sess-1",
        user_id=user_id,
        game_id=1,
        settings={"session_loss_limit": loss_limit, "dry_run": True},
    )
    with _registry_lock:
        _sessions[(user_id, 1)] = sess
    return sess


def _make_trade(
    *,
    user_id: str = "user-loss",
    status: str = "pending",
    undo_group_id: str | None = "group-1",
) -> Trade:
    t = Trade.__new__(Trade)
    t._lock = threading.Lock()
    t._clean_timer = None
    t._undo_timer = None
    t.id = f"trade-{user_id}-{undo_group_id}"
    t.event = "HR"
    t.user_id = user_id
    t.game_id = 1
    t.market_ticker = "TEST-MKT"
    t.side = "YES"
    t.quantity = 100
    t.entry_price = 0.50
    t.sell_target = 0.60
    t.stop_loss = 0.40
    t.status = status
    t.sell_order_id = None
    t.buy_order_id = "buy-1"
    t.realized_pnl = 0.0
    t.gross_pnl = 0.0
    t.entry_fee = 0.0
    t.exit_fee = 0.0
    t.exit_price = None
    t.exit_qty = None
    t.completed_at = None
    t._owns_ticker_claim = False
    t._settings = {"dry_run": True}
    t.trade_db_id = None
    t._last_fill_probe_ts = 0.0
    t.undo_group_id = undo_group_id
    t.trade_info = {"entry_price": t.entry_price}
    t.requested_quantity = 100
    t.max_slippage_cents = 1.0
    t.max_dollars = 500.0
    t.use_undo_window = True

    t._update_db_status = MagicMock()
    t._log_to_db = MagicMock()

    with _registry_lock:
        _trade_index[t.id] = t

    return t


# ---------------------------------------------------------------------------
# Settings plumbing
# ---------------------------------------------------------------------------

class TestSettings:
    def test_default_setting_is_negative(self):
        """Default floor is a loss, not a profit — otherwise the first
        $1 win would trip the guard."""
        assert market_selector.DEFAULT_SETTINGS["session_loss_limit"] < 0

    def test_clamp_enforces_negative_floor(self):
        """Coerce accidental positives to 0 so the guard never fires on
        a winning day."""
        assert clamp_settings({"session_loss_limit": 500})["session_loss_limit"] == 0.0

    def test_clamp_enforces_lower_bound(self):
        """Absurd floors get floored at -10000 so integer overflow /
        config typos don't produce an unreachable threshold."""
        assert clamp_settings({"session_loss_limit": -999999})["session_loss_limit"] == -10000.0

    def test_clamp_keeps_valid_value(self):
        assert clamp_settings({"session_loss_limit": -500})["session_loss_limit"] == -500.0


# ---------------------------------------------------------------------------
# Trade.execute guard
# ---------------------------------------------------------------------------

class TestExecuteGuard:
    def test_pnl_at_limit_rejects(self, monkeypatch):
        """pnl == limit is treated as hit (exact boundary = reject)."""
        sess = _make_session(loss_limit=-100.0)
        sess.realized_pnl = -100.0

        t = _make_trade()
        sizing = MagicMock()
        monkeypatch.setattr(trader.kalshi, "calculate_position_size", sizing)

        with pytest.raises(KillSwitchArmedError) as exc_info:
            t.execute()

        assert "Session loss limit reached" in str(exc_info.value)
        # Sizing never runs — the guard short-circuits before any kalshi work.
        sizing.assert_not_called()
        assert is_kill_switch_armed(t.user_id) is True

    def test_pnl_below_limit_rejects(self, monkeypatch):
        sess = _make_session(loss_limit=-100.0)
        sess.realized_pnl = -150.0

        t = _make_trade()
        monkeypatch.setattr(
            trader.kalshi, "calculate_position_size", MagicMock(),
        )

        with pytest.raises(KillSwitchArmedError):
            t.execute()

    def test_pnl_just_above_limit_passes_guard(self, monkeypatch):
        """pnl = limit + $0.01: the guard should NOT trip. Use a sizing
        stub that raises 'No liquidity' so we know execution reached
        sizing — i.e. we got past both kill-switch and loss-limit checks."""
        sess = _make_session(loss_limit=-100.0)
        sess.realized_pnl = -99.99

        t = _make_trade()
        monkeypatch.setattr(
            trader.kalshi, "calculate_position_size",
            lambda *a, **k: {"contracts": 0, "vwap": 0.0, "best_ask": 0.0},
        )

        with pytest.raises(RuntimeError) as exc_info:
            t.execute()

        assert not isinstance(exc_info.value, KillSwitchArmedError)
        assert is_kill_switch_armed(t.user_id) is False

    def test_zero_limit_disables_guard(self, monkeypatch):
        """limit=0 means 'no floor'. Even very negative pnl should not
        trip the guard — the operator explicitly opted out."""
        sess = _make_session(loss_limit=0.0)
        sess.realized_pnl = -5000.0

        t = _make_trade()
        monkeypatch.setattr(
            trader.kalshi, "calculate_position_size",
            lambda *a, **k: {"contracts": 0, "vwap": 0.0, "best_ask": 0.0},
        )

        with pytest.raises(RuntimeError) as exc_info:
            t.execute()
        assert not isinstance(exc_info.value, KillSwitchArmedError)
        assert is_kill_switch_armed(t.user_id) is False


# ---------------------------------------------------------------------------
# on_trade_resolved proactive trip
# ---------------------------------------------------------------------------

class TestOnResolveTrip:
    def _resolved_trade(self, sess: Session, *, pnl: float) -> Trade:
        """A fake trade that counts as the only member of its group and
        has already reached a terminal state with `pnl` booked."""
        t = _make_trade(user_id=sess.user_id, status="filled")
        t.realized_pnl = pnl
        # Register a single-member group on the session so the aggregation
        # quorum is satisfied on the first resolve.
        group = TradeGroup.__new__(TradeGroup)
        group.id = "group-trip"
        group.trades = [t]
        group._aggregated = False
        t.undo_group_id = group.id
        with sess._lock:
            sess.groups[group.id] = group
        return t

    def test_resolve_that_crosses_floor_arms_and_emits(self, monkeypatch):
        """A losing trade pushes session pnl past the floor. on_trade_resolved
        must arm the kill switch and emit session_limit_reached exactly once."""
        sess = _make_session(loss_limit=-100.0)
        sess.realized_pnl = -95.0  # one tick away

        sse_calls = []
        monkeypatch.setattr(
            market_selector, "notify_sse_session_limit",
            lambda game_id, user_id, **kw: sse_calls.append({
                "game_id": game_id, "user_id": user_id, **kw,
            }),
        )

        losing = self._resolved_trade(sess, pnl=-10.0)
        sess.on_trade_resolved(losing)

        assert sess.realized_pnl == pytest.approx(-105.0)
        assert is_kill_switch_armed(sess.user_id) is True
        assert len(sse_calls) == 1
        assert sse_calls[0]["user_id"] == sess.user_id
        assert sse_calls[0]["pnl"] == pytest.approx(-105.0)
        assert sse_calls[0]["limit"] == pytest.approx(-100.0)

    def test_emit_is_once_per_session(self, monkeypatch):
        """Two consecutive losing trades that both sit below the floor
        must only emit ONE SSE event — frontend renders a persistent
        banner, so re-emitting would just flicker it."""
        sess = _make_session(loss_limit=-100.0)
        sess.realized_pnl = -95.0

        sse_calls = []
        monkeypatch.setattr(
            market_selector, "notify_sse_session_limit",
            lambda *a, **k: sse_calls.append(1),
        )

        first = self._resolved_trade(sess, pnl=-10.0)
        sess.on_trade_resolved(first)
        second = self._resolved_trade(sess, pnl=-20.0)
        sess.on_trade_resolved(second)

        assert sess.realized_pnl == pytest.approx(-125.0)
        assert len(sse_calls) == 1

    def test_winning_trade_above_floor_no_trip(self, monkeypatch):
        sess = _make_session(loss_limit=-100.0)
        sess.realized_pnl = -50.0

        sse_calls = []
        monkeypatch.setattr(
            market_selector, "notify_sse_session_limit",
            lambda *a, **k: sse_calls.append(1),
        )

        winner = self._resolved_trade(sess, pnl=+30.0)
        sess.on_trade_resolved(winner)

        assert sess.realized_pnl == pytest.approx(-20.0)
        assert is_kill_switch_armed(sess.user_id) is False
        assert sse_calls == []


# ---------------------------------------------------------------------------
# End-to-end: resolve trips, next /buy sees 503 via kill switch
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_trip_then_buy_is_blocked(self, monkeypatch):
        """After on_trade_resolved trips the limit, the NEXT Trade.execute
        is blocked by the kill-switch check (not the session-limit check
        specifically) — either path must produce KillSwitchArmedError so
        /buy renders 503."""
        sess = _make_session(loss_limit=-100.0)
        sess.realized_pnl = -95.0
        monkeypatch.setattr(
            market_selector, "notify_sse_session_limit",
            lambda *a, **k: None,
        )

        loser = _make_trade(status="filled", undo_group_id="g-trip")
        loser.realized_pnl = -10.0
        group = TradeGroup.__new__(TradeGroup)
        group.id = loser.undo_group_id
        group.trades = [loser]
        group._aggregated = False
        with sess._lock:
            sess.groups[group.id] = group
        sess.on_trade_resolved(loser)

        # Now a fresh /buy attempt should be blocked.
        next_trade = _make_trade(status="pending", undo_group_id="g-next")
        with pytest.raises(KillSwitchArmedError):
            next_trade.execute()
