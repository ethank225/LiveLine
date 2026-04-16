"""
Tests for C3: exit_qty is persisted on every terminal trade.

The bug: update_trade_status() only patched {status, exit_price, pnl,
gross_pnl, entry_fee, exit_fee}. On partial IOC fills (Kalshi ate 80 of
100 contracts), the row kept quantity=100 while realized_pnl was computed
on 80, so any reconciliation via (exit - entry) * quantity disagreed.

The fix:
  - database.update_trade_status accepts exit_qty kwarg
  - Trade.exit_qty is set on every terminal resolution path
  - Trade._update_db_status threads self.exit_qty through
  - _ioc_exit returns (price, qty) so partial IOCs surface their real qty

Run:  cd backend && python -m pytest tests/test_exit_qty_persisted.py -v
"""

import threading
from unittest.mock import MagicMock

import pytest

from app import trader, market_selector
from app.fees import maker_fee, taker_fee
from app.trader import (
    Trade,
    _active_tickers,
    _active_tickers_lock,
    _killed_users,
    _registry_lock,
    _trade_by_sell_order_id,
    _trade_index,
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
    quantity: int = 100,
    status: str = "open",
    sell_order_id: str | None = "sell-exitqty",
    dry_run: bool = True,
) -> Trade:
    t = Trade.__new__(Trade)
    t._lock = threading.Lock()
    t._clean_timer = None
    t._undo_timer = None
    t.id = f"trade-{sell_order_id or 'none'}"
    t.event = "HR"
    t.user_id = "user-test"
    t.game_id = 1
    t.market_ticker = "TEST-MKT"
    t.side = "YES"
    t.quantity = quantity
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
    t.exit_qty = None
    t.completed_at = None
    t._owns_ticker_claim = False
    t._settings = {"dry_run": dry_run}
    t.trade_db_id = None
    t._last_fill_probe_ts = 0.0
    t.undo_group_id = None
    t.trade_info = {"entry_price": t.entry_price}
    t.requested_quantity = quantity
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
# DB layer — update_trade_status accepts exit_qty
# ---------------------------------------------------------------------------

class TestDatabaseLayer:
    def test_update_trade_status_signature_accepts_exit_qty(self):
        """Smoke: the kwarg exists and doesn't raise with a valid client
        stub. Real Supabase isn't hit; we just confirm the call reaches
        the .update(patch) branch with 'exit_qty' present in the patch."""
        from app import database

        patches: list[dict] = []

        # Build a minimal Supabase client that records .update(patch) calls.
        class _Chain:
            def update(self, patch):
                patches.append(patch)
                return self
            def eq(self, *a, **k):
                return self
            def execute(self):
                return MagicMock(data=[{}])

        class _Client:
            def table(self, name):
                return _Chain()

        # database._get_client is the module-level accessor every write
        # funnels through. Patching it avoids hitting real Supabase.
        import app.database as db_mod
        real_get_client = db_mod._get_client
        db_mod._get_client = lambda: _Client()
        try:
            database.update_trade_status(
                "trade-id-123", "filled", 0.60, 4.25,
                gross_pnl=5.00, entry_fee=0.50, exit_fee=0.25,
                exit_qty=80,
            )
        finally:
            db_mod._get_client = real_get_client

        assert len(patches) == 1
        assert patches[0]["exit_qty"] == 80
        assert patches[0]["status"] == "filled"


# ---------------------------------------------------------------------------
# Target fill
# ---------------------------------------------------------------------------

class TestTargetFill:
    def test_full_fill_records_full_qty(self):
        t = _make_trade(quantity=100, dry_run=True)
        t._mark_filled_at_target()

        assert t.status == "filled"
        assert t.exit_qty == 100
        # Sanity: (0.60 - 0.50) * 100 = $10 gross, net is positive after fees.
        assert t.realized_pnl is not None
        # DB write must include exit_qty — _update_db_status wraps update,
        # so we assert the Mock was called and trust the real path via the
        # DB-layer test above.
        assert t._update_db_status.called

    def test_partial_fill_at_target(self):
        """A resting limit sell can partial-fill if the book thinned out
        after placement. exit_qty reflects the reported qty, not the
        requested size."""
        t = _make_trade(quantity=100, dry_run=False)
        t._mark_filled_at_target(actual_price=0.60, actual_qty=80)

        assert t.exit_qty == 80
        # Realized P&L must also be on 80, not 100 — reconciliation test.
        expected_gross = (0.60 - 0.50) * 80
        expected_net = (
            expected_gross - taker_fee(80, 0.50) - maker_fee(80, 0.60)
        )
        assert t.realized_pnl == pytest.approx(expected_net)
        assert t.gross_pnl == pytest.approx(expected_gross)


# ---------------------------------------------------------------------------
# IOC exit paths — expire, stop-loss, kill, sell-fail, undo
# ---------------------------------------------------------------------------

class TestIocExitRecordsExitQty:
    def _install_ioc(
        self, monkeypatch, *, filled_qty: int, yes_price: float = 0.55,
    ):
        """Mock the IOC branch to simulate Kalshi filling `filled_qty`
        contracts at yes_price_dollars=yes_price."""
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": yes_price, "no_bid": 1.0 - yes_price},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_live_positions",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            trader.kalshi, "cancel_order",
            lambda oid, **kw: {"order_id": oid},
        )

        def fake_place(**kw):
            if kw.get("time_in_force") == "ioc":
                return {
                    "order_id": "ioc-real",
                    "fill_count_fp": filled_qty,
                    "yes_price_dollars": yes_price,
                    "taker_fill_cost_dollars": yes_price * filled_qty,
                }
            return {"order_id": "gtc-ok"}
        monkeypatch.setattr(trader, "_place_order", fake_place)

    def test_expire_full_ioc_fill_records_full_qty(self, monkeypatch):
        t = _make_trade(quantity=100, dry_run=False)
        self._install_ioc(monkeypatch, filled_qty=100, yes_price=0.55)
        # _expire calls _sell_already_filled first — for this test we skip
        # the "already filled" branch by ensuring cancel succeeds and
        # get_order returns None (order already gone → treated as "not
        # already filled"). Simplest: stub _sell_already_filled directly.
        t._sell_already_filled = lambda: False

        t._expire()

        assert t.status == "expired"
        assert t.exit_qty == 100
        assert t.exit_price == pytest.approx(0.55)

    def test_expire_partial_ioc_fill(self, monkeypatch):
        """Kalshi IOC ate 60/100 (thin book). exit_qty must be 60, not 100."""
        t = _make_trade(quantity=100, dry_run=False)
        self._install_ioc(monkeypatch, filled_qty=60, yes_price=0.55)
        t._sell_already_filled = lambda: False

        t._expire()

        assert t.exit_qty == 60
        # Realized P&L on 60 contracts, taker exit fees.
        expected_gross = (0.55 - 0.50) * 60
        assert t.gross_pnl == pytest.approx(expected_gross)

    def test_stop_loss_partial_ioc(self, monkeypatch):
        t = _make_trade(quantity=100, dry_run=False)
        self._install_ioc(monkeypatch, filled_qty=40, yes_price=0.35)
        t._sell_already_filled = lambda: False

        t._execute_stop_loss(0.35)

        assert t.status == "stopped"
        assert t.exit_qty == 40
        # Loss on 40 contracts: (0.35 - 0.50) * 40 = -$6 gross
        assert t.gross_pnl == pytest.approx(-6.0)

    def test_force_kill_records_filled_qty(self, monkeypatch):
        t = _make_trade(quantity=100, dry_run=False)
        self._install_ioc(monkeypatch, filled_qty=70, yes_price=0.52)

        t.force_kill()

        assert t.status == "canceled_by_kill"
        assert t.exit_qty == 70

    def test_sell_fail_ioc_records_qty(self, monkeypatch):
        """Sell placement (gtc) raises → IOC flatten fills 50. exit_qty=50."""
        t = _make_trade(quantity=100, dry_run=False)
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.52, "no_bid": 0.48},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_live_positions", lambda *a, **k: [],
        )

        def fake_place(**kw):
            if kw.get("time_in_force") == "gtc":
                raise RuntimeError("simulated sell placement error")
            if kw.get("time_in_force") == "ioc":
                return {
                    "order_id": "ioc-sellfail",
                    "fill_count_fp": 50,
                    "yes_price_dollars": 0.52,
                    "taker_fill_cost_dollars": 26.0,
                }
            return {}
        monkeypatch.setattr(trader, "_place_order", fake_place)

        t._activate()

        assert t.status == "error"
        assert t.exit_qty == 50

    def test_ioc_zero_fill_records_requested_qty(self, monkeypatch):
        """IOC returned no fill data (rare 0-bid book). P&L is booked on
        the requested size at the fallback bid — conservative — and
        exit_qty matches so the reconciliation holds."""
        t = _make_trade(quantity=100, dry_run=False)
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.45, "no_bid": 0.55},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_live_positions", lambda *a, **k: [],
        )
        monkeypatch.setattr(
            trader.kalshi, "cancel_order", lambda *a, **k: {"order_id": "x"},
        )

        # IOC returns a response with no fill_count_fp → _actual_fill → None.
        def fake_place(**kw):
            return {"order_id": "no-fill", "fill_count_fp": 0}
        monkeypatch.setattr(trader, "_place_order", fake_place)
        t._sell_already_filled = lambda: False

        t._expire()

        assert t.exit_qty == 100  # requested size, matches P&L booking
        assert t.exit_price == pytest.approx(0.45)
