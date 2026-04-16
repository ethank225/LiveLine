"""
Tests for the instant-sell extension to Trade.cancel.

Before: cancel() raised ValueError for anything other than undo_window.
Now: open-state trades can also be canceled (cancels the resting limit
sell, then IOC-flattens at market). Realized P&L reflects the real
crossing-the-spread exit price.

Run:  cd backend && python -m pytest tests/test_instant_sell.py -v
"""

import threading
from unittest.mock import MagicMock

import pytest

from app import trader, market_selector
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


def _make_trade(*, status: str = "open", dry_run: bool = True) -> Trade:
    t = Trade.__new__(Trade)
    t._lock = threading.Lock()
    t._clean_timer = None
    t._undo_timer = None
    t.id = "trade-instant-sell"
    t.event = "HR"
    t.user_id = "user-test"
    t.game_id = 1
    t.market_ticker = "TEST-MKT"
    t.side = "YES"
    t.quantity = 100
    t.entry_price = 0.50
    t.sell_target = 0.60
    t.stop_loss = 0.40
    t.status = status
    t.sell_order_id = "sell-resting"
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
    t.requested_quantity = 100
    t.max_slippage_cents = 1.0
    t.max_dollars = 500.0
    t.use_undo_window = True

    t._update_db_status = MagicMock()
    t._log_to_db = MagicMock()

    with _registry_lock:
        _trade_index[t.id] = t
        if t.sell_order_id:
            _trade_by_sell_order_id[t.sell_order_id] = t
    return t


class TestOpenStateCancel:
    def test_open_trade_cancel_flattens(self, monkeypatch):
        """Cancel on an open trade must cancel the resting sell first,
        then IOC-exit. Realized P&L reflects the IOC fill price (at the
        bid), not the sell_target."""
        t = _make_trade(status="open", dry_run=False)

        cancel_log = []
        monkeypatch.setattr(
            trader.kalshi, "cancel_order",
            lambda oid, **kw: cancel_log.append(oid) or {"order_id": oid},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.48, "no_bid": 0.52},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_live_positions", lambda *a, **k: [],
        )

        place_log = []
        def fake_place(**kw):
            place_log.append(kw.get("time_in_force"))
            return {
                "order_id": "ioc-instant",
                "fill_count_fp": 100,
                "yes_price_dollars": 0.48,
                "taker_fill_cost_dollars": 48.0,
            }
        monkeypatch.setattr(trader, "_place_order", fake_place)

        result = t.cancel()

        # Order of operations: cancel the resting sell BEFORE the IOC.
        assert cancel_log == ["sell-resting"]
        assert place_log == ["ioc"]
        # Status + P&L + row are finalized as canceled_by_user.
        assert t.status == "canceled_by_user"
        assert t.exit_price == pytest.approx(0.48)
        assert t.exit_qty == 100
        # Loss: bought at 0.50, flattened at 0.48 → -2¢ * 100 = -$2.00 gross
        assert t.gross_pnl == pytest.approx(-2.00)
        assert t.realized_pnl is not None
        # DB + SSE wiring fired.
        t._update_db_status.assert_called_with(
            "canceled_by_user", t.exit_price, t.realized_pnl,
        )
        assert result is not None
        assert result["status"] == "canceled_by_user"

    def test_dry_run_skips_rest_cancel(self, monkeypatch):
        """Dry-run has no real Kalshi order to cancel; must not try."""
        t = _make_trade(status="open", dry_run=True)

        cancel_mock = MagicMock()
        monkeypatch.setattr(trader.kalshi, "cancel_order", cancel_mock)
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.48, "no_bid": 0.52},
        )
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        t.cancel()

        cancel_mock.assert_not_called()
        assert t.status == "canceled_by_user"
        assert t.exit_price == pytest.approx(0.48)

    def test_cancel_failure_still_ioc_exits(self, monkeypatch):
        """The resting-sell cancel_order may 404 (already filled) or
        error. Either way the IOC must still run — leaving a naked long
        because a cancel failed would defeat the whole instant-sell
        feature."""
        t = _make_trade(status="open", dry_run=False)

        monkeypatch.setattr(
            trader.kalshi, "cancel_order",
            MagicMock(side_effect=RuntimeError("already filled")),
        )
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.48, "no_bid": 0.52},
        )
        monkeypatch.setattr(
            trader.kalshi, "get_live_positions", lambda *a, **k: [],
        )
        ioc_calls = []
        monkeypatch.setattr(
            trader, "_place_order",
            lambda **kw: ioc_calls.append(kw) or {
                "fill_count_fp": 100, "yes_price_dollars": 0.48,
            },
        )

        t.cancel()

        assert len(ioc_calls) == 1
        assert ioc_calls[0]["time_in_force"] == "ioc"
        assert t.status == "canceled_by_user"

    def test_clean_timer_canceled(self, monkeypatch):
        """The 45s clean-window timer must be canceled so it doesn't
        fire AFTER the instant-sell has already resolved the trade."""
        t = _make_trade(status="open", dry_run=True)
        t._clean_timer = MagicMock()

        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.48, "no_bid": 0.52},
        )
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        t.cancel()

        t._clean_timer.cancel.assert_called_once()


class TestExistingUndoWindowStillWorks:
    def test_undo_window_cancel_unchanged(self, monkeypatch):
        """Regression: the classic 3s undo flow must still produce the
        same outcome it always has."""
        t = _make_trade(status="undo_window", dry_run=True)

        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_bid": 0.48, "no_bid": 0.52},
        )
        # No resting sell yet in undo_window; cancel_order shouldn't fire.
        cancel_mock = MagicMock()
        monkeypatch.setattr(trader.kalshi, "cancel_order", cancel_mock)
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {})

        t.cancel()

        cancel_mock.assert_not_called()
        assert t.status == "canceled_by_user"


class TestTerminalStillRaises:
    @pytest.mark.parametrize(
        "status", ["filled", "expired", "stopped", "canceled_by_user", "error"],
    )
    def test_terminal_cancel_raises(self, status):
        t = _make_trade(status=status)
        with pytest.raises(ValueError) as exc_info:
            t.cancel()
        assert "can only cancel" in str(exc_info.value)

    def test_pending_cancel_raises(self):
        t = _make_trade(status="pending")
        with pytest.raises(ValueError):
            t.cancel()
