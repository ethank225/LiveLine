"""
Regression tests for C4: sell-placement failure must auto-flatten.

The bug: when the limit sell POST raised (Kalshi REST error, timeout,
schema issue), `_activate` set status="error", logged "verifying position",
and returned. No IOC exit was placed. The naked long held until the next
startup orphan sweep — which only cancels resting orders, never flattens
positions. At $500/play, that's a fully-exposed long with no stop, no
target, no timer.

The fix: in the sell-placement except branch, _ioc_exit(reason=...) so the
position is force-flattened at market before returning. DB row then carries
a real exit_price and realized_pnl instead of bare "error".

Run:  cd backend && python -m pytest tests/test_sell_fail_flattens.py -v
"""

import threading
from unittest.mock import MagicMock

import pytest

from app import trader, market_selector
from app.trader import Trade, _registry_lock, _trade_by_sell_order_id, _trade_index


@pytest.fixture(autouse=True)
def mock_sse(monkeypatch):
    """Swap out the SSE broadcaster so _notify_resolved doesn't hit an
    event queue that isn't wired up in tests."""
    mock = MagicMock()
    monkeypatch.setattr(market_selector, "notify_sse_positions_changed", mock)
    return mock


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """_activate now sleeps between sell-placement retries. Patch it to a
    no-op so the full-retry test doesn't take 1.5s per case."""
    monkeypatch.setattr(trader.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    with _registry_lock:
        _trade_by_sell_order_id.clear()
        _trade_index.clear()


def _make_buy_filled_trade(*, dry_run: bool = True, side: str = "YES") -> Trade:
    """Construct a Trade with the buy already filled — the state _activate
    starts from. Status is 'open' so the status-gate at the top of _activate
    lets execution proceed into the sell placement."""
    t = Trade.__new__(Trade)
    t._lock = threading.Lock()
    t._clean_timer = None
    t._undo_timer = None
    t.id = "trade-sell-fail"
    t.event = "HR"
    t.user_id = "user-test"
    t.game_id = 1
    t.market_ticker = "TEST-MKT"
    t.side = side
    t.quantity = 100
    t.entry_price = 0.50
    t.sell_target = 0.60
    t.stop_loss = 0.40
    t.status = "open"
    t.sell_order_id = None
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

    # DB writes become introspectable no-ops; real trader.py logic runs.
    t._update_db_status = MagicMock()
    t._log_to_db = MagicMock()

    return t


def _install_place_order(
    monkeypatch,
    *,
    gtc_raises: bool,
    ioc_response: dict | Exception | None,
    gtc_fail_attempts: int | None = None,
    gtc_success_response: dict | None = None,
):
    """Install a fake trader._place_order.

    - `gtc_raises=True` with `gtc_fail_attempts=None`: every GTC call raises
      (persistent failure → drives _activate through all 4 attempts + IOC).
    - `gtc_fail_attempts=N`: first N GTC calls raise; the (N+1)th returns
      `gtc_success_response` (transient failure → retry succeeds).
    - `gtc_raises=False`: GTC succeeds on attempt 1.

    IOC behavior is unchanged: returns `ioc_response` or raises it.

    Returns the call_log list so tests can assert call order / args."""
    call_log: list[dict] = []
    gtc_attempts = 0

    def fake_place(**kwargs):
        nonlocal gtc_attempts
        call_log.append(kwargs)
        tif = kwargs.get("time_in_force")
        if tif == "gtc":
            gtc_attempts += 1
            if gtc_fail_attempts is not None:
                if gtc_attempts <= gtc_fail_attempts:
                    raise RuntimeError("simulated Kalshi sell placement error")
                return gtc_success_response or {"order_id": f"gtc-{gtc_attempts}"}
            if gtc_raises:
                raise RuntimeError("simulated Kalshi sell placement error")
            return gtc_success_response or {"order_id": f"gtc-{gtc_attempts}"}
        if tif == "ioc":
            if isinstance(ioc_response, Exception):
                raise ioc_response
            return ioc_response or {}
        # Non-gtc/ioc calls aren't expected in _activate's flow.
        return {}

    monkeypatch.setattr(trader, "_place_order", fake_place)
    return call_log


def _install_fallback_bid(monkeypatch, *, yes_bid: float = 0.55, no_bid: float = 0.45):
    """Mock kalshi.get_prices so _fallback_exit_price returns a real bid
    instead of 0.0 sentinel. Also mock get_live_positions for the
    _verify_position_closed path in the live branch (dry_run bypasses it)."""
    monkeypatch.setattr(
        trader.kalshi, "get_prices",
        lambda *a, **k: {"yes_bid": yes_bid, "no_bid": no_bid},
    )
    monkeypatch.setattr(
        trader.kalshi, "get_live_positions",
        lambda *a, **k: [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDryRunSellFailFlattens:
    def test_sell_failure_triggers_ioc_exit(self, monkeypatch, mock_sse):
        """Persistent sell failure → 4 GTC attempts, then IOC force-exit."""
        t = _make_buy_filled_trade(dry_run=True)
        _install_fallback_bid(monkeypatch, yes_bid=0.55)
        calls = _install_place_order(monkeypatch, gtc_raises=True, ioc_response={})

        t._activate()

        # 4 GTC attempts (initial + 3 retries) then a single IOC exit.
        assert len(calls) == 5
        assert [c["time_in_force"] for c in calls[:4]] == ["gtc"] * 4
        assert calls[4]["time_in_force"] == "ioc"
        assert calls[4]["action"] == "sell"
        assert calls[4]["quantity"] == t.quantity

    def test_transient_sell_failure_recovers(self, monkeypatch, mock_sse):
        """Transient failure resolves within retries → sell rests, no IOC."""
        t = _make_buy_filled_trade(dry_run=True)
        _install_fallback_bid(monkeypatch, yes_bid=0.55)
        calls = _install_place_order(
            monkeypatch,
            gtc_raises=False,
            ioc_response={},
            gtc_fail_attempts=2,  # attempts 1 and 2 raise, 3 succeeds
            gtc_success_response={"order_id": "gtc-resting-ok"},
        )

        t._activate()

        # 3 GTC calls (2 failures + 1 success), NO IOC exit.
        assert len(calls) == 3
        assert all(c["time_in_force"] == "gtc" for c in calls)
        assert t.status == "open"
        assert t.sell_order_id == "gtc-resting-ok"
        assert t._clean_timer is not None
        # Tidy up the started timer so it doesn't fire during the test run.
        t._clean_timer.cancel()

    def test_trade_resolves_with_populated_pnl(self, monkeypatch, mock_sse):
        """Post-flatten: status=error, exit_price set, realized_pnl computed,
        completed_at set. No more naked-long sitting open."""
        t = _make_buy_filled_trade(dry_run=True)
        _install_fallback_bid(monkeypatch, yes_bid=0.55)
        _install_place_order(monkeypatch, gtc_raises=True, ioc_response={})

        t._activate()

        assert t.status == "error"
        assert t.exit_price == pytest.approx(0.55)
        assert t.realized_pnl is not None
        # (0.55 - 0.50) * 100 = $5 gross, minus taker both legs. Positive here.
        assert t.gross_pnl == pytest.approx(5.00)
        assert t.completed_at is not None

    def test_db_patched_with_exit_price_and_pnl(self, monkeypatch, mock_sse):
        """_update_db_status must be called with the final ("error",
        exit_price, realized_pnl) tuple — not the bare "error" of the old
        code path that left exit_price / realized_pnl NULL."""
        t = _make_buy_filled_trade(dry_run=True)
        _install_fallback_bid(monkeypatch, yes_bid=0.55)
        _install_place_order(monkeypatch, gtc_raises=True, ioc_response={})

        t._activate()

        t._update_db_status.assert_called_with("error", t.exit_price, t.realized_pnl)

    def test_sse_notified_on_resolve(self, monkeypatch, mock_sse):
        """Frontend must see the trade resolve — otherwise the position
        card sits in 'open' forever with no way for the user to know."""
        t = _make_buy_filled_trade(dry_run=True)
        _install_fallback_bid(monkeypatch, yes_bid=0.55)
        _install_place_order(monkeypatch, gtc_raises=True, ioc_response={})

        t._activate()

        mock_sse.assert_called_once_with(t.game_id, t.user_id)

    def test_no_clean_timer_started(self, monkeypatch, mock_sse):
        """The clean-window timer is only supposed to arm after a
        successful sell placement. On the sell-fail branch we should
        never start it — the trade is already terminal."""
        t = _make_buy_filled_trade(dry_run=True)
        _install_fallback_bid(monkeypatch, yes_bid=0.55)
        _install_place_order(monkeypatch, gtc_raises=True, ioc_response={})

        t._activate()

        assert t._clean_timer is None


class TestLiveSellFailFlattens:
    """Same coverage with dry_run=False so the IOC response path exercises
    _actual_fill (not the dry-run shortcut to _fallback_exit_price)."""

    def test_live_ioc_reads_actual_fill(self, monkeypatch, mock_sse):
        t = _make_buy_filled_trade(dry_run=False, side="YES")
        _install_fallback_bid(monkeypatch)
        # IOC response: filled 100 @ yes_price_dollars=0.52
        ioc_response = {
            "order_id": "ioc-live-1",
            "fill_count_fp": 100,
            "yes_price_dollars": 0.52,
            "taker_fill_cost_dollars": 52.0,
        }
        _install_place_order(monkeypatch, gtc_raises=True, ioc_response=ioc_response)

        t._activate()

        assert t.status == "error"
        assert t.exit_price == pytest.approx(0.52)
        # gross = (0.52 - 0.50) * 100 = $2
        assert t.gross_pnl == pytest.approx(2.00)
        assert t.realized_pnl is not None

    def test_live_ioc_no_side_flips_price(self, monkeypatch, mock_sse):
        """NO-side sells are stored in NO-units (1 - yes_price_dollars)."""
        t = _make_buy_filled_trade(dry_run=False, side="NO")
        t.entry_price = 0.40  # NO @ 0.40 = YES @ 0.60
        t.sell_target = 0.45
        _install_fallback_bid(monkeypatch)
        ioc_response = {
            "order_id": "ioc-live-2",
            "fill_count_fp": 100,
            # Kalshi echoes YES price even for NO fills; _actual_fill flips it.
            "yes_price_dollars": 0.57,  # → NO fill at 0.43
            "taker_fill_cost_dollars": 43.0,
        }
        _install_place_order(monkeypatch, gtc_raises=True, ioc_response=ioc_response)

        t._activate()

        assert t.exit_price == pytest.approx(0.43)

    def test_live_ioc_also_fails_uses_fallback_bid(self, monkeypatch, mock_sse):
        """If the IOC placement itself raises, _fallback_exit_price returns
        the current bid. Exit still records a real number — no $0.00
        sentinel bleeding into the DB."""
        t = _make_buy_filled_trade(dry_run=False)
        _install_fallback_bid(monkeypatch, yes_bid=0.53)
        _install_place_order(
            monkeypatch,
            gtc_raises=True,
            ioc_response=RuntimeError("IOC also failed"),
        )

        t._activate()

        assert t.status == "error"
        assert t.exit_price == pytest.approx(0.53)
        assert t.realized_pnl is not None
