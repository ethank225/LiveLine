"""
Tests for the websocket fill dispatch system (trader.py + kalshi_client.py).

Exercises dispatch_websocket_fill end-to-end with the REAL
_trade_by_sell_order_id index, real _mark_filled_at_target, and real
locks. DB writes and SSE pushes are mocked to track call counts — we
don't want to hit Supabase or actually wake up an SSE queue.

Run:  cd backend && python -m pytest tests/test_websocket_fill.py -v
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import trader, market_selector
from app.trader import (
    Trade,
    _registry_lock,
    _trade_by_sell_order_id,
    _trade_index,
    dispatch_websocket_fill,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_sse(monkeypatch):
    """Replace the SSE broadcaster that _notify_resolved late-imports.

    Patched on the market_selector module (the import source) so the
    `from app.market_selector import notify_sse_positions_changed`
    inside _notify_resolved picks up the mock.
    """
    mock = MagicMock()
    monkeypatch.setattr(
        market_selector, "notify_sse_positions_changed", mock
    )
    return mock


@pytest.fixture(autouse=True)
def clean_registries():
    """Drain the module-level trade registries between tests.

    Tests register fake trades into _trade_by_sell_order_id directly; we
    don't want one test's leftovers to be seen by the next one.
    """
    yield
    with _registry_lock:
        _trade_by_sell_order_id.clear()
        _trade_index.clear()


def _make_open_trade(
    sell_order_id: str = "sim-abc123",
    *,
    status: str = "open",
    dry_run: bool = True,
    user_id: str = "user-test",
    game_id: int = 1,
    register_in_index: bool = True,
) -> Trade:
    """Construct a Trade skipping __init__ (which talks to Kalshi/DB).

    Sets every field _mark_filled_at_target, on_websocket_fill, and
    _notify_resolved read. `_update_db_status` and `_log_to_db` are
    replaced with MagicMocks on the instance so we can assert call
    counts without touching Supabase.
    """
    t = Trade.__new__(Trade)
    t._lock = threading.Lock()
    t._clean_timer = None
    t.id = f"trade-{sell_order_id}"
    t.event = "HR"
    t.user_id = user_id
    t.game_id = game_id
    t.market_ticker = "TEST-MKT"
    t.side = "YES"
    t.quantity = 100
    t.entry_price = 0.50
    t.sell_target = 0.60
    t.stop_loss = 0.40
    t.status = status
    t.sell_order_id = sell_order_id
    t.realized_pnl = None
    t.exit_price = None
    t.completed_at = None
    t.buy_order_id = "buy-xyz"
    t._owns_ticker_claim = False
    t._settings = {"dry_run": dry_run}
    t.trade_db_id = None
    t._last_fill_probe_ts = 0.0
    t.undo_group_id = None

    # Mock the DB side of the state machine. _mark_filled_at_target runs
    # for real but its DB write becomes a no-op we can introspect.
    t._update_db_status = MagicMock()
    t._log_to_db = MagicMock()

    if register_in_index and sell_order_id:
        with _registry_lock:
            _trade_by_sell_order_id[sell_order_id] = t

    return t


def _fake_msg(order_id, *, yes_price_dollars=None, count_fp=None, side=None):
    """Minimal stand-in for pykalshi's FillMessage. Dispatcher only
    touches .order_id; the resolver additionally reads yes_price_dollars
    and count_fp when falling back to the WS payload as the price source."""
    return SimpleNamespace(
        order_id=order_id,
        yes_price_dollars=yes_price_dollars,
        count_fp=count_fp,
        side=side,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_dispatch_transitions_to_filled(self, mock_sse):
        t = _make_open_trade("sim-happy-1")
        dispatch_websocket_fill(_fake_msg("sim-happy-1"))

        assert t.status == "filled"
        # P&L = (sell_target - entry_price) * quantity
        assert t.realized_pnl == pytest.approx(
            (0.60 - 0.50) * 100, rel=1e-6
        )
        assert t.exit_price == 0.60
        assert t.completed_at is not None

    def test_db_and_sse_called_exactly_once(self, mock_sse):
        t = _make_open_trade("sim-happy-2")
        dispatch_websocket_fill(_fake_msg("sim-happy-2"))

        t._update_db_status.assert_called_once_with(
            "filled", 0.60, t.realized_pnl
        )
        mock_sse.assert_called_once_with(t.game_id, t.user_id)

    def test_index_cleared_on_resolve(self):
        t = _make_open_trade("sim-happy-3")
        assert "sim-happy-3" in _trade_by_sell_order_id  # sanity

        dispatch_websocket_fill(_fake_msg("sim-happy-3"))

        with _registry_lock:
            assert "sim-happy-3" not in _trade_by_sell_order_id


class TestIdempotency:
    def test_duplicate_dispatch_is_noop(self, mock_sse):
        t = _make_open_trade("sim-dup")
        dispatch_websocket_fill(_fake_msg("sim-dup"))

        # Second dispatch — the index is already cleared, so dispatch
        # returns on lookup miss. DB/SSE shouldn't fire again.
        dispatch_websocket_fill(_fake_msg("sim-dup"))

        assert t.status == "filled"
        t._update_db_status.assert_called_once()
        mock_sse.assert_called_once()

    def test_duplicate_dispatch_while_still_indexed(self, mock_sse):
        """Even if the trade somehow stayed in the index, the CAS inside
        _mark_filled_at_target prevents a second resolve."""
        t = _make_open_trade("sim-dup2")
        dispatch_websocket_fill(_fake_msg("sim-dup2"))

        # Forcibly re-insert (simulating a race where _notify_resolved
        # hasn't cleaned up yet). The CAS must still hold.
        with _registry_lock:
            _trade_by_sell_order_id["sim-dup2"] = t

        dispatch_websocket_fill(_fake_msg("sim-dup2"))

        t._update_db_status.assert_called_once()
        mock_sse.assert_called_once()


class TestUnknownMessages:
    def test_unknown_order_id_is_noop(self, mock_sse):
        t = _make_open_trade("sim-known")
        dispatch_websocket_fill(_fake_msg("sim-NOT-IN-INDEX"))

        assert t.status == "open"
        assert t.realized_pnl is None
        t._update_db_status.assert_not_called()
        mock_sse.assert_not_called()

    def test_none_message_is_noop(self, mock_sse):
        t = _make_open_trade("sim-none")
        dispatch_websocket_fill(None)
        assert t.status == "open"
        t._update_db_status.assert_not_called()
        mock_sse.assert_not_called()

    def test_message_missing_order_id_attr(self, mock_sse):
        t = _make_open_trade("sim-missing-attr")
        dispatch_websocket_fill(SimpleNamespace())  # no order_id
        assert t.status == "open"
        t._update_db_status.assert_not_called()
        mock_sse.assert_not_called()

    def test_message_with_none_order_id(self, mock_sse):
        t = _make_open_trade("sim-none-oid")
        dispatch_websocket_fill(SimpleNamespace(order_id=None))
        assert t.status == "open"
        t._update_db_status.assert_not_called()
        mock_sse.assert_not_called()

    def test_message_raising_on_attribute_access(self, mock_sse):
        """Any exception inside dispatch must be swallowed; the feed
        listener runs every registered handler in a tight loop and an
        escaping exception could impact siblings."""
        class Boom:
            @property
            def order_id(self):
                raise RuntimeError("boom")

        # Should not raise.
        dispatch_websocket_fill(Boom())


class TestWrongStatus:
    @pytest.mark.parametrize(
        "status",
        ["expired", "stopped", "canceled", "canceled_by_user", "filled", "error"],
    )
    def test_non_open_trade_does_not_transition(self, mock_sse, status):
        """Stale fill arriving after the trade already resolved via some
        other path (timer expire, stop-loss, user cancel) must be a
        no-op — the terminal state stays as-is."""
        t = _make_open_trade(f"sim-stale-{status}", status=status)
        dispatch_websocket_fill(_fake_msg(f"sim-stale-{status}"))

        assert t.status == status
        assert t.realized_pnl is None  # we didn't set one in the fixture
        t._update_db_status.assert_not_called()
        mock_sse.assert_not_called()


class TestMissingSellOrderId:
    def test_undo_window_with_no_sell_order_id(self, mock_sse):
        """Trade is still in the undo window — the limit sell hasn't been
        placed yet, so it's never registered in _trade_by_sell_order_id.
        A fill event (for something else entirely) must not resolve it."""
        t = _make_open_trade(
            sell_order_id=None,
            status="undo_window",
            register_in_index=False,
        )
        # A stray fill for some unrelated order — whatever it is, it can't
        # match a trade whose sell_order_id is None.
        dispatch_websocket_fill(_fake_msg("some-other-order"))

        assert t.status == "undo_window"
        assert t.sell_order_id is None
        t._update_db_status.assert_not_called()
        mock_sse.assert_not_called()


class TestConcurrency:
    def test_fifty_concurrent_dispatches_produce_one_transition(self, mock_sse):
        """CAS + lock semantics guarantee exactly-once resolution even
        under a flood of racing callers. Mirrors the race a real
        websocket burst (duplicate delivery, retries) could produce."""
        t = _make_open_trade("sim-race")
        msg = _fake_msg("sim-race")

        barrier = threading.Barrier(50)

        def hit():
            barrier.wait()  # all 50 launch at once
            dispatch_websocket_fill(msg)

        threads = [threading.Thread(target=hit) for _ in range(50)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert t.status == "filled"
        assert t._update_db_status.call_count == 1
        assert mock_sse.call_count == 1

    def test_race_between_dispatch_and_direct_mark(self, mock_sse):
        """Simulate timer _expire calling _mark_filled_at_target directly
        while the websocket dispatch fires in parallel. Exactly one
        resolution should happen regardless of which side wins."""
        t = _make_open_trade("sim-race2")
        msg = _fake_msg("sim-race2")

        barrier = threading.Barrier(50)

        def ws():
            barrier.wait()
            dispatch_websocket_fill(msg)

        def timer_path():
            barrier.wait()
            t._mark_filled_at_target()

        threads = []
        for i in range(25):
            threads.append(threading.Thread(target=ws))
            threads.append(threading.Thread(target=timer_path))
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert t.status == "filled"
        assert t._update_db_status.call_count == 1
        assert mock_sse.call_count == 1


class TestActualExitPrice:
    """The resting limit sell can fill above sell_target (maker price
    improvement) or across multiple levels (partial fills). `exit_price`
    / `realized_pnl` should reflect the real Kalshi execution, not the
    intent price. Dry-run skips the lookup entirely (no real order to
    ask about) — these tests run with dry_run=False."""

    def test_rest_vwap_overrides_sell_target(self, monkeypatch, mock_sse):
        t = _make_open_trade("sim-live-1", dry_run=False)
        monkeypatch.setattr(
            trader.kalshi, "get_order_fill_vwap",
            lambda order_id, side: (0.65, 100),
        )
        dispatch_websocket_fill(_fake_msg("sim-live-1"))

        assert t.status == "filled"
        assert t.exit_price == 0.65
        assert t.realized_pnl == pytest.approx((0.65 - 0.50) * 100)
        t._update_db_status.assert_called_once_with("filled", 0.65, t.realized_pnl)

    def test_rest_vwap_partial_fill_qty_wins(self, monkeypatch, mock_sse):
        """If the resting sell partial-filled (80 of 100), Kalshi's fills
        endpoint reports qty=80 and we book P&L on those 80 contracts,
        not self.quantity."""
        t = _make_open_trade("sim-live-2", dry_run=False)
        monkeypatch.setattr(
            trader.kalshi, "get_order_fill_vwap",
            lambda order_id, side: (0.62, 80),
        )
        dispatch_websocket_fill(_fake_msg("sim-live-2"))

        assert t.exit_price == 0.62
        assert t.realized_pnl == pytest.approx((0.62 - 0.50) * 80)

    def test_ws_payload_used_when_rest_unavailable(self, monkeypatch, mock_sse):
        """get_order_fill_vwap returning None (transient REST failure)
        falls back to the WS message's own price/count."""
        t = _make_open_trade("sim-live-3", dry_run=False)
        monkeypatch.setattr(
            trader.kalshi, "get_order_fill_vwap", lambda order_id, side: None
        )
        msg = _fake_msg("sim-live-3", yes_price_dollars="0.63", count_fp="100")
        dispatch_websocket_fill(msg)

        assert t.exit_price == 0.63
        assert t.realized_pnl == pytest.approx((0.63 - 0.50) * 100)

    def test_no_side_flips_yes_price(self, monkeypatch, mock_sse):
        """WS payload carries yes_price_dollars; for a NO trade, exit is
        stored in NO units (1 - yes_price) to match entry_price units."""
        t = _make_open_trade("sim-live-4", dry_run=False)
        t.side = "NO"
        t.entry_price = 0.63
        t.sell_target = 0.70
        monkeypatch.setattr(
            trader.kalshi, "get_order_fill_vwap", lambda order_id, side: None
        )
        # Real fill: YES=0.28 → NO = 0.72.
        msg = _fake_msg("sim-live-4", yes_price_dollars="0.28", count_fp="100")
        dispatch_websocket_fill(msg)

        assert t.exit_price == 0.72
        assert t.realized_pnl == pytest.approx((0.72 - 0.63) * 100)

    def test_full_fallback_emits_warning(self, monkeypatch, mock_sse, caplog):
        """Both REST and WS payload unavailable — book at sell_target and
        log a warning so the degraded row is grep-able."""
        import logging
        t = _make_open_trade("sim-live-5", dry_run=False)
        monkeypatch.setattr(
            trader.kalshi, "get_order_fill_vwap", lambda order_id, side: None
        )
        with caplog.at_level(logging.WARNING, logger="app.trader"):
            dispatch_websocket_fill(_fake_msg("sim-live-5"))

        assert t.exit_price == 0.60  # sell_target
        assert any(
            "exit-price fallback" in r.message for r in caplog.records
        )


class TestIndexCleanup:
    @pytest.mark.parametrize("terminal_status", ["filled", "expired", "stopped"])
    def test_resolve_removes_from_index(self, mock_sse, terminal_status):
        """Every terminal-state path runs through _notify_resolved, which
        pops sell_order_id from _trade_by_sell_order_id. Verify cleanup
        is not specific to the websocket path."""
        t = _make_open_trade(f"sim-cleanup-{terminal_status}")
        assert f"sim-cleanup-{terminal_status}" in _trade_by_sell_order_id

        if terminal_status == "filled":
            # Going through the real code path.
            dispatch_websocket_fill(_fake_msg(f"sim-cleanup-{terminal_status}"))
        else:
            # Simulate a non-websocket terminal transition: flip status
            # under the lock, then call the same _notify_resolved hook
            # every other exit path uses.
            with t._lock:
                t.status = terminal_status
            t._notify_resolved()

        with _registry_lock:
            assert f"sim-cleanup-{terminal_status}" not in _trade_by_sell_order_id

    def test_double_cleanup_does_not_raise(self, mock_sse):
        """pop(..., None) must tolerate a racing second cleanup."""
        t = _make_open_trade("sim-double-cleanup")
        dispatch_websocket_fill(_fake_msg("sim-double-cleanup"))

        # Second cleanup attempt — would KeyError without the default.
        t._notify_resolved()  # re-entrant; pops from now-empty dict
