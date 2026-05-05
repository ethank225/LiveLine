"""
Regression: Trade.execute writes TWO orderbook_snapshots rows per trade —
one phase='pre' captured immediately before the IOC fires (so we can later
reconstruct the book the sizer saw) and one phase='post' captured after
fill. The pre-row's captured_at must precede the post-row's captured_at.

Pinning this prevents a future refactor from reverting to single-snapshot
or to reordering capture back to post-only (the bug this fix corrected).

Run:  cd backend && python -m pytest tests/test_orderbook_pre_post.py -v
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from pykalshi.orderbook import OrderbookManager

from app import database as db, trader
from app.kalshi_client import kalshi
from app.market_selector import DEFAULT_SETTINGS
from app.trader import (
    Trade, _active_tickers, _active_tickers_lock, _registry_lock,
    _trade_index,
)


@pytest.fixture(autouse=True)
def clean_registries():
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


def _make_trade(ticker: str) -> Trade:
    """Match the trade_info shape compute_best_trades produces."""
    trade_info = {
        "event": "HR",
        "market_ticker": ticker,
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
        game_id=999,
        event="HR",
        trade_info=trade_info,
        session_id=None,
        user_id="user-pre-post-test",
        settings={**DEFAULT_SETTINGS, "dry_run": True, "use_undo_window": False},
        use_undo_window=False,
    )


class TestPrePostSnapshots:
    def test_two_rows_pre_then_post(self, monkeypatch):
        """End-to-end: dry-run Trade.execute → two log_orderbook_snapshot
        calls, the first phase='pre' and the second phase='post', with
        pre's captured_at strictly earlier than post's."""
        ticker = f"TEST-PREPOST-{uuid.uuid4().hex[:8]}"

        # Install a populated local book so both pre and post reads
        # succeed via the local path (the prompt's correctness criterion:
        # pre snapshot must read from the in-memory book, no network).
        book = OrderbookManager(ticker)
        book.yes = {"0.50": "200", "0.49": "100"}
        book.no = {"0.49": "200", "0.48": "100"}  # → YES asks at 0.51, 0.52
        kalshi._books[ticker] = book

        snapshots: list[dict] = []

        def fake_log_snapshot(*args, **kwargs):
            # Mirror the real signature: (trade_id, game_id, market_ticker,
            # side, book, market_type=None, phase='post', captured_at=None)
            snapshots.append({
                "trade_id": args[0],
                "market_ticker": args[2],
                "side": args[3],
                "book": args[4],
                "phase": kwargs.get("phase", "post"),
                "captured_at": kwargs.get("captured_at"),
            })

        def fake_place(**kwargs):
            return {"order_id": f"kalshi-{uuid.uuid4().hex[:8]}"}

        def fake_log_trade(**kwargs):
            return f"db-{uuid.uuid4().hex[:8]}"

        def fake_sizing(*args, **kwargs):
            return {
                "contracts": 100, "vwap": 0.50, "best_ask": 0.50,
                "slippage": 0.0, "total_cost": 50.0, "depth_at_limit": 200,
                "budget_limited": False, "source": "local",
            }

        def fake_get_prices(*args, **kwargs):
            return {"yes_ask": 0.50, "yes_bid": 0.48,
                    "no_ask": 0.52, "no_bid": 0.50}

        monkeypatch.setattr(trader, "_place_order", fake_place)
        monkeypatch.setattr(db, "log_trade", fake_log_trade)
        monkeypatch.setattr(db, "update_trade_status", lambda *a, **k: None)
        monkeypatch.setattr(db, "log_orderbook_snapshot", fake_log_snapshot)
        monkeypatch.setattr(trader.db, "log_orderbook_snapshot", fake_log_snapshot)
        monkeypatch.setattr(trader.kalshi, "calculate_position_size", fake_sizing)
        monkeypatch.setattr(trader.kalshi, "get_prices", fake_get_prices)
        # Skip cross-market sibling capture — focus on the traded ticker.
        monkeypatch.setattr(
            trader, "_snapshot_sibling_markets", lambda *a, **k: None,
        )
        # _activate places a resting GTC sell on a Timer; the snapshot
        # writes have nothing to do with it. Stub to keep the test scoped.
        monkeypatch.setattr(Trade, "_activate", lambda self: None)

        try:
            trade = _make_trade(ticker)
            trade.execute()

            # Daemon threads dispatch the writes. They run synchronously
            # against our fake (no real network), but we still need to
            # wait briefly for the threads to start + finish.
            import time as _time
            for _ in range(50):
                if len(snapshots) >= 2:
                    break
                _time.sleep(0.02)

            phases = [s["phase"] for s in snapshots]
            assert phases == ["pre", "post"], (
                f"expected exactly one pre and one post snapshot in order "
                f"['pre', 'post']; got {phases}"
            )

            pre, post = snapshots[0], snapshots[1]
            assert pre["trade_id"] == post["trade_id"], (
                "both rows must share the same trade_id"
            )
            assert pre["captured_at"] is not None and post["captured_at"] is not None, (
                "both rows must carry an explicit captured_at — the schema's "
                "default now() lands at insert time, which can re-order writes "
                "relative to actual capture moments."
            )
            assert pre["captured_at"] < post["captured_at"], (
                f"pre captured_at must precede post captured_at; "
                f"pre={pre['captured_at']!r} post={post['captured_at']!r}"
            )
            # The pre snapshot must reflect the local book the sizer saw —
            # in particular, it must contain non-empty ladders since the
            # local book was populated when execute() ran.
            assert pre["book"]["bids"], (
                "pre snapshot bid ladder is empty; the capture should have "
                "read the local book and seen the installed levels."
            )
            assert pre["book"]["asks"], (
                "pre snapshot ask ladder is empty; same as above for asks."
            )
        finally:
            kalshi._books.pop(ticker, None)

    def test_pre_skipped_when_local_book_empty(self, monkeypatch):
        """If the local book is empty (freshly subscribed, no snapshot yet),
        the pre-trade capture must skip with a warning rather than blocking
        on REST. Only the post snapshot ends up written."""
        ticker = f"TEST-PREPOST-EMPTY-{uuid.uuid4().hex[:8]}"

        # Empty local book — bids and asks both empty dicts. local_only=True
        # in get_orderbook_snapshot must return empties without REST.
        empty_book = OrderbookManager(ticker)
        empty_book.yes = {}
        empty_book.no = {}
        kalshi._books[ticker] = empty_book

        snapshots: list[dict] = []

        def fake_log_snapshot(*args, **kwargs):
            snapshots.append({"phase": kwargs.get("phase", "post")})

        # The post-snapshot path also calls get_orderbook_snapshot (without
        # local_only). With an empty book and no real Kalshi client wired
        # up, we want it to also return empties (skipping post too) — that
        # mirrors the real "empty book" behavior and exercises the skip
        # branches symmetrically.
        monkeypatch.setattr(trader, "_place_order", lambda **kw: {"order_id": "x"})
        monkeypatch.setattr(db, "log_trade", lambda **kw: f"db-{uuid.uuid4().hex[:8]}")
        monkeypatch.setattr(db, "update_trade_status", lambda *a, **k: None)
        monkeypatch.setattr(db, "log_orderbook_snapshot", fake_log_snapshot)
        monkeypatch.setattr(trader.db, "log_orderbook_snapshot", fake_log_snapshot)
        monkeypatch.setattr(
            trader.kalshi, "calculate_position_size",
            lambda *a, **k: {
                "contracts": 100, "vwap": 0.50, "best_ask": 0.50,
                "slippage": 0.0, "total_cost": 50.0, "depth_at_limit": 200,
                "budget_limited": False, "source": "local",
            },
        )
        monkeypatch.setattr(
            trader.kalshi, "get_prices",
            lambda *a, **k: {"yes_ask": 0.50, "yes_bid": 0.48,
                             "no_ask": 0.52, "no_bid": 0.50},
        )
        monkeypatch.setattr(trader, "_snapshot_sibling_markets", lambda *a, **k: None)
        monkeypatch.setattr(Trade, "_activate", lambda self: None)

        try:
            trade = _make_trade(ticker)
            trade.execute()

            import time as _time
            _time.sleep(0.1)  # let any daemon threads finish

            phases = [s["phase"] for s in snapshots]
            assert "pre" not in phases, (
                "pre snapshot must be skipped when local book is empty — "
                "REST fallback would put network latency on the order path. "
                f"snapshots={phases}"
            )
        finally:
            kalshi._books.pop(ticker, None)
