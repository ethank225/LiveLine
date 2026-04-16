"""
Regression: on the /buy path, the Kalshi IOC buy must fire BEFORE any
observability writes (Supabase `log_trade` insert, decision-log block).

Why this matters: on Railway, stdout logging and Supabase RTT each add
~50-200ms. Between "decision made" and "order sent" those milliseconds
are market-moving time. The decision log is pure observability derived
from the candidate dict; the `log_trade` insert creates a row that
`update_trade_status` later patches, and both `log_trade` and
`update_trade_status` already honor the "never raise" contract in
database.py — so a later insert is still safe.

Two invariants tested here:

  1. `Trade.execute` calls `_place_order` (the Kalshi POST) BEFORE
     calling `db.log_trade` (the initial Supabase row insert). This
     was already the case on the happy path before the reorder, but
     it's worth guarding against regression — a future refactor
     that moves `_log_to_db()` up to pre-buy would put DB latency
     back on the critical path silently.

  2. In `main.buy_event`, the `format_decision_log` call sits AFTER
     the `await asyncio.to_thread(_fire)` that executes the
     TradeGroup. Enforced via source inspection — the actual
     /buy endpoint needs a FastAPI client + auth + session +
     settings to exercise end-to-end, which is more scaffolding
     than this reorder warrants.

Run:  cd backend && python -m pytest tests/test_buy_ordering.py -v
"""

import inspect
import threading
import uuid
from unittest.mock import MagicMock

import pytest

from app import database as db, main, trader
from app.market_selector import DEFAULT_SETTINGS
from app.trader import (
    Trade, _active_tickers, _active_tickers_lock, _registry_lock,
    _trade_index,
)


@pytest.fixture(autouse=True)
def clean_registries():
    """Every test starts with empty in-flight-ticker + trade-index
    caches so dedupe gates don't carry state across cases."""
    yield
    with _registry_lock:
        _trade_index.clear()
    with _active_tickers_lock:
        _active_tickers.clear()


@pytest.fixture(autouse=True)
def mock_sse(monkeypatch):
    from app import market_selector
    monkeypatch.setattr(
        market_selector, "notify_sse_positions_changed", MagicMock(),
    )


def _make_trade(*, dry_run: bool = True) -> Trade:
    """Real `Trade.__init__` — the code-under-test (`execute`) reads
    many attributes that `Trade.__new__` bypass-construction wouldn't
    populate. The trade_info dict is the minimal valid shape the
    picker produces."""
    trade_info = {
        "event": "HR",
        "market_ticker": f"TEST-{uuid.uuid4().hex[:8]}",
        "market_type": "moneyline",
        "market_title": "Test Moneyline",
        "side": "YES",
        "entry_price": 0.50,
        "sell_target": 0.60,
        "spread": 0.02,
        "ev_per_contract": 0.04,
        "total_ev": 20.0,
        "estimated_fill_qty": 100,
        "quantity": 100,
        "estimated_cost": 50.0,
        "estimated_profit": 10.0,
        "entry_fee_est": 0.0,
        "exit_fee_est": 0.0,
        "net_expected_profit": 10.0,
    }
    return Trade(
        game_id=123,
        event="HR",
        trade_info=trade_info,
        session_id=None,
        user_id="user-ordering-test",
        settings={**DEFAULT_SETTINGS, "dry_run": dry_run, "use_undo_window": False},
        use_undo_window=False,
    )


class TestTradeExecuteOrdering:
    def test_place_order_fires_before_log_trade(self, monkeypatch):
        """On the happy path, `Trade.execute` calls Kalshi's place-order
        POST before it calls `db.log_trade`. If a refactor inverts that,
        the DB insert's RTT lands on the critical path between decision
        and order."""
        call_order: list[str] = []

        def fake_place(**kwargs):
            # Only the IOC buy fires inside execute(); the GTC sell is
            # placed later in _activate (which we skip by setting
            # use_undo_window=False is still fine — _activate runs after
            # return, but the test mock stays installed).
            if kwargs.get("action") == "buy":
                call_order.append("place_order_buy")
            else:
                call_order.append(f"place_order_{kwargs.get('action')}")
            return {
                "order_id": f"kalshi-{uuid.uuid4().hex[:8]}",
                # dry_run short-circuits parse_fp; quantity is set from
                # the sizing result. Non-dry-run would use fill_count_fp.
            }

        def fake_log_trade(**kwargs):
            call_order.append("log_trade")
            return f"db-{uuid.uuid4().hex[:8]}"

        def fake_update_trade_status(*args, **kwargs):
            call_order.append("update_trade_status")

        def fake_sizing(*args, **kwargs):
            return {
                "contracts": 100, "vwap": 0.50, "best_ask": 0.50,
                "slippage": 0.0, "total_cost": 50.0, "depth_at_limit": 200,
                "budget_limited": False, "source": "local",
            }

        def fake_get_prices(*args, **kwargs):
            # Keeps the dry-run pre-buy freshness check happy.
            return {"yes_ask": 0.50, "yes_bid": 0.48,
                    "no_ask": 0.52, "no_bid": 0.50}

        monkeypatch.setattr(trader, "_place_order", fake_place)
        monkeypatch.setattr(db, "log_trade", fake_log_trade)
        monkeypatch.setattr(db, "update_trade_status", fake_update_trade_status)
        monkeypatch.setattr(trader.kalshi, "calculate_position_size", fake_sizing)
        monkeypatch.setattr(trader.kalshi, "get_prices", fake_get_prices)
        monkeypatch.setattr(
            trader.kalshi, "get_orderbook_snapshot",
            lambda *a, **k: {
                "bids": [], "asks": [],
                "total_bid_depth": 0, "total_ask_depth": 0,
            },
        )
        # _activate places a resting GTC sell on a Timer. With
        # use_undo_window=False the activate runs synchronously inside
        # execute(); we still want to observe the ordering that matters
        # (buy → log_trade) before activate's sell placement fires.
        # Patching _activate to a no-op keeps the test focused on the
        # pre-activate invariant.
        monkeypatch.setattr(Trade, "_activate", lambda self: None)

        trade = _make_trade(dry_run=True)
        trade.execute()

        assert "place_order_buy" in call_order, (
            f"Kalshi buy never fired. call_order={call_order}"
        )
        assert "log_trade" in call_order, (
            f"DB insert never fired. call_order={call_order}"
        )
        buy_idx = call_order.index("place_order_buy")
        log_idx = call_order.index("log_trade")
        assert buy_idx < log_idx, (
            f"Kalshi buy must fire before db.log_trade. "
            f"Order was: {call_order} "
            f"(buy_idx={buy_idx}, log_idx={log_idx})"
        )


class TestDecisionLogDeferredInBuyEndpoint:
    def test_format_decision_log_called_after_fire(self):
        """Source-level invariant: in `main.buy_event`, the
        `format_decision_log` emission sits AFTER the
        `await asyncio.to_thread(_fire)` line that executes the
        TradeGroup. This is what keeps the multi-line formatted log
        off the critical path between decision and Kalshi order.

        A full end-to-end test via TestClient would need FastAPI auth,
        a Session, a cached trade dict, and Kalshi stubs — more
        scaffolding than a pure-reorder change warrants. The source
        check is narrow but catches the specific regression the
        reorder was designed to prevent."""
        src = inspect.getsource(main.buy_event)

        fire_marker = "await asyncio.to_thread(_fire)"
        decision_marker = "format_decision_log(decision)"

        assert fire_marker in src, (
            f"expected {fire_marker!r} in buy_event source — did the "
            f"TradeGroup execution shape change?"
        )
        assert decision_marker in src, (
            f"expected {decision_marker!r} in buy_event source — was the "
            f"decision log removed entirely?"
        )
        fire_idx = src.index(fire_marker)
        decision_idx = src.index(decision_marker)
        assert decision_idx > fire_idx, (
            f"format_decision_log must be called AFTER "
            f"await asyncio.to_thread(_fire). "
            f"fire_idx={fire_idx}, decision_idx={decision_idx} — "
            f"the decision log has drifted back onto the pre-buy path."
        )
