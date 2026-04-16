"""
Tests for C2: the per-user kill switch.

Covers:
  - is_kill_switch_armed / _arm_kill_switch / disarm_kill_switch wiring
  - Trade.execute() raises KillSwitchArmedError when the user is armed
  - Trade.force_kill() flattens an open trade: cancels timers, cancels
    resting sell, IOC-exits held qty, writes DB, emits SSE
  - arm_kill_switch(user_id) walks _trade_index and flattens every
    non-terminal trade owned by that user, skipping others
  - disarm_kill_switch clears the flag
  - Fresh session creation (get_or_create_session) auto-disarms the user

Run:  cd backend && python -m pytest tests/test_kill_switch.py -v
"""

import threading
from unittest.mock import MagicMock

import pytest

from app import trader, market_selector
from app.trader import (
    KillSwitchArmedError,
    Trade,
    _active_tickers,
    _active_tickers_lock,
    _killed_users,
    _registry_lock,
    _trade_by_sell_order_id,
    _trade_index,
    arm_kill_switch,
    disarm_kill_switch,
    is_kill_switch_armed,
)


@pytest.fixture(autouse=True)
def mock_sse(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(market_selector, "notify_sse_positions_changed", mock)
    return mock


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    with _registry_lock:
        _trade_by_sell_order_id.clear()
        _trade_index.clear()
    _killed_users.clear()
    with _active_tickers_lock:
        _active_tickers.clear()


def _make_trade(
    *,
    user_id: str = "user-A",
    status: str = "open",
    sell_order_id: str | None = "sell-1",
    dry_run: bool = True,
    trade_id: str | None = None,
) -> Trade:
    t = Trade.__new__(Trade)
    t._lock = threading.Lock()
    t._clean_timer = None
    t._undo_timer = None
    t.id = trade_id or f"trade-{user_id}-{sell_order_id or 'none'}"
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
    t.sell_order_id = sell_order_id
    t.buy_order_id = "buy-xyz"
    t.realized_pnl = None
    t.gross_pnl = 0.0
    t.entry_fee = 0.0
    t.exit_fee = 0.0
    t.exit_price = None
    t.completed_at = None
    t._owns_ticker_claim = False
    t._settings = {"dry_run": dry_run}
    t.trade_db_id = None
    t._last_fill_probe_ts = 0.0
    t.undo_group_id = None
    # Fields Trade.execute reads past the kill-switch check. Harmless on
    # force_kill paths which don't hit them.
    t.trade_info = {"entry_price": t.entry_price}
    t.requested_quantity = 0
    t.max_slippage_cents = 1.0
    t.max_dollars = 500.0
    t.use_undo_window = True

    t._update_db_status = MagicMock()
    t._log_to_db = MagicMock()

    with _registry_lock:
        _trade_index[t.id] = t
        if sell_order_id:
            _trade_by_sell_order_id[sell_order_id] = t

    return t


# ---------------------------------------------------------------------------
# Flag wiring
# ---------------------------------------------------------------------------

class TestFlagPrimitives:
    def test_flag_starts_disarmed(self):
        assert is_kill_switch_armed("anyone") is False

    def test_disarm_clears_flag(self, monkeypatch):
        trader._arm_kill_switch("user-1")
        assert is_kill_switch_armed("user-1") is True

        disarm_kill_switch("user-1")
        assert is_kill_switch_armed("user-1") is False

    def test_flag_is_per_user(self):
        trader._arm_kill_switch("user-A")
        assert is_kill_switch_armed("user-A") is True
        assert is_kill_switch_armed("user-B") is False


# ---------------------------------------------------------------------------
# Trade.execute guard
# ---------------------------------------------------------------------------

class TestExecuteGuard:
    def test_armed_user_execute_raises(self, monkeypatch):
        """Core contract: Trade.execute() must bail with
        KillSwitchArmedError (NOT reach calculate_position_size) when the
        owning user is armed. /buy catches this and returns 503."""
        trader._arm_kill_switch("user-A")
        t = _make_trade(user_id="user-A", status="pending", sell_order_id=None)

        # Sentinels: sizing should never be called in the armed path.
        called_sizing = MagicMock()
        monkeypatch.setattr(trader.kalshi, "calculate_position_size", called_sizing)

        with pytest.raises(KillSwitchArmedError):
            t.execute()

        called_sizing.assert_not_called()
        assert t.status == "canceled"

    def test_other_user_unaffected(self, monkeypatch):
        """Arming user-A doesn't block user-B."""
        trader._arm_kill_switch("user-A")
        t_b = _make_trade(user_id="user-B", status="pending", sell_order_id=None)

        # Make sizing return zero so execute returns without placing orders
        # (we're only verifying the kill-switch check lets us past).
        monkeypatch.setattr(
            trader.kalshi, "calculate_position_size",
            lambda *a, **k: {"contracts": 0, "vwap": 0.0, "best_ask": 0.0},
        )

        with pytest.raises(RuntimeError) as exc_info:
            t_b.execute()
        # Should be the "no liquidity" RuntimeError, not KillSwitchArmedError.
        assert not isinstance(exc_info.value, KillSwitchArmedError)


# ---------------------------------------------------------------------------
# Trade.force_kill
# ---------------------------------------------------------------------------

class TestForceKill:
    def test_open_trade_ioc_flattens(self, monkeypatch):
        t = _make_trade(status="open", dry_run=True)

        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.58, "no_bid": 0.42},
        )
        # In dry_run force_kill should NOT call cancel_order (no real order).
        cancel_mock = MagicMock()
        monkeypatch.setattr(trader.kalshi, "cancel_order", cancel_mock)
        # _place_order only gets called for the IOC exit itself.
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        result = t.force_kill()

        assert result is True
        assert t.status == "canceled_by_kill"
        assert t.exit_price == pytest.approx(0.58)
        assert t.realized_pnl is not None
        assert t.completed_at is not None
        t._update_db_status.assert_called_with(
            "canceled_by_kill", t.exit_price, t.realized_pnl,
        )
        # Dry-run: no REST cancel.
        cancel_mock.assert_not_called()

    def test_live_trade_cancels_sell_before_ioc(self, monkeypatch):
        t = _make_trade(status="open", dry_run=False, sell_order_id="sell-live")

        cancel_log = []
        monkeypatch.setattr(
            trader.kalshi, "cancel_order",
            lambda oid, **kw: cancel_log.append(oid) or {"order_id": oid},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.55, "no_bid": 0.45},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_live_positions",
            lambda *a, **k: [],
        )
        ioc_calls = []
        def fake_place(**kw):
            ioc_calls.append(kw)
            return {
                "order_id": "ioc-kill",
                "fill_count_fp": t.quantity,
                "yes_price_dollars": 0.55,
                "taker_fill_cost_dollars": 55.0,
            }
        monkeypatch.setattr(trader, "_place_order", fake_place)

        t.force_kill()

        assert cancel_log == ["sell-live"]
        assert len(ioc_calls) == 1
        assert ioc_calls[0]["time_in_force"] == "ioc"
        assert t.status == "canceled_by_kill"

    def test_cancel_failure_does_not_block_ioc(self, monkeypatch):
        """Cancelling the resting sell may 404 (already filled) or error.
        Either way the IOC flatten must still run — otherwise we leave a
        resting sell in the book when we meant to fully exit."""
        t = _make_trade(status="open", dry_run=False, sell_order_id="sell-err")

        monkeypatch.setattr(
            trader.kalshi, "cancel_order",
            MagicMock(side_effect=RuntimeError("order not found")),
        )
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.55, "no_bid": 0.45},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_live_positions",
            lambda *a, **k: [],
        )
        ioc_called = []
        monkeypatch.setattr(
            trader, "_place_order",
            lambda **kw: ioc_called.append(kw) or {
                "fill_count_fp": 100, "yes_price_dollars": 0.55,
            },
        )

        t.force_kill()

        assert len(ioc_called) == 1
        assert t.status == "canceled_by_kill"

    def test_terminal_trade_skipped(self):
        t = _make_trade(status="filled")
        result = t.force_kill()
        assert result is False
        assert t.status == "filled"  # unchanged

    def test_pending_timers_canceled(self, monkeypatch):
        t = _make_trade(status="open", dry_run=True)
        t._clean_timer = MagicMock()
        t._undo_timer = MagicMock()

        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.58, "no_bid": 0.42},
        )
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        t.force_kill()

        t._clean_timer.cancel.assert_called_once()
        t._undo_timer.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# arm_kill_switch — module-level orchestration
# ---------------------------------------------------------------------------

class TestArmKillSwitch:
    def test_flattens_user_trades_only(self, monkeypatch):
        """arm_kill_switch for user-A flattens every non-terminal trade
        owned by user-A and does NOT touch user-B's trades."""
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.58, "no_bid": 0.42},
        )
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        t_a1 = _make_trade(user_id="user-A", sell_order_id="a1", trade_id="trade-A-1")
        t_a2 = _make_trade(user_id="user-A", sell_order_id="a2", trade_id="trade-A-2")
        t_b = _make_trade(user_id="user-B", sell_order_id="b1", trade_id="trade-B-1")

        summary = arm_kill_switch("user-A")

        assert is_kill_switch_armed("user-A") is True
        assert t_a1.status == "canceled_by_kill"
        assert t_a2.status == "canceled_by_kill"
        # user-B untouched.
        assert t_b.status == "open"
        assert set(summary["flattened"]) == {t_a1.id, t_a2.id}
        assert summary["skipped"] == []
        assert summary["errors"] == []

    def test_skips_terminal_trades(self, monkeypatch):
        """Already-filled trades aren't re-resolved (that would double-book
        P&L). They count toward `skipped`, not `flattened`."""
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.58, "no_bid": 0.42},
        )
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        t_open = _make_trade(user_id="user-A", sell_order_id="s-open", trade_id="t-open")
        t_filled = _make_trade(
            user_id="user-A", sell_order_id="s-filled",
            status="filled", trade_id="t-filled",
        )

        summary = arm_kill_switch("user-A")

        assert t_open.status == "canceled_by_kill"
        assert t_filled.status == "filled"  # unchanged
        assert summary["flattened"] == [t_open.id]
        # Terminal trades are filtered OUT of `victims` and never seen by
        # arm_kill_switch, so they don't appear in skipped either.
        assert t_filled.id not in summary["skipped"]

    def test_idempotent(self, monkeypatch):
        """Arming twice is a no-op on the second call (nothing to flatten)."""
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.58, "no_bid": 0.42},
        )
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        _make_trade(user_id="user-A", sell_order_id="s1", trade_id="t1")

        first = arm_kill_switch("user-A")
        second = arm_kill_switch("user-A")

        assert len(first["flattened"]) == 1
        assert second["flattened"] == []
        assert is_kill_switch_armed("user-A") is True


# ---------------------------------------------------------------------------
# Disarm on fresh session
# ---------------------------------------------------------------------------

class TestSessionCreationDisarms:
    def test_new_session_clears_kill_flag(self, monkeypatch):
        """Starting a new game clears any prior kill flag (per spec).
        get_or_create_session() is the natural reset point — by the time
        a user is creating a fresh session they've acknowledged the prior
        kill event."""
        trader._arm_kill_switch("user-A")
        assert is_kill_switch_armed("user-A") is True

        # Stub the DB write so we don't hit Supabase.
        monkeypatch.setattr(
            trader.db, "get_or_create_session",
            lambda *a, **k: "session-id-fresh",
        )
        monkeypatch.setattr(
            trader.db, "get_user_settings", lambda user_id: {},
        )

        session = trader.get_or_create_session("user-A", 99, "TEAMA", "TEAMB")

        assert session is not None
        assert is_kill_switch_armed("user-A") is False

        # Clean up the Session we just wrote to the registry.
        with _registry_lock:
            trader._sessions.pop(("user-A", 99), None)
