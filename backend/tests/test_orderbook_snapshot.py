"""Tests for KalshiManager.get_orderbook_snapshot.

The method flips sides consistently with `_get_ask_levels` and
`has_exit_liquidity` — bids in traded-side units (same side as trade),
asks in traded-side units (`1 - p` of opposite side). These tests pin
that behavior so a later refactor of the flip logic fails loudly.
"""

from pykalshi.orderbook import OrderbookManager

from app.kalshi_client import kalshi


def _install_book(ticker: str, yes: dict, no: dict):
    book = OrderbookManager(ticker)
    book.yes = yes
    book.no = no
    kalshi._books[ticker] = book
    return book


def _uninstall_book(ticker: str):
    kalshi._books.pop(ticker, None)


class TestYesSide:
    """YES trade: bids = raw book.yes (desc), asks = 1 − book.no (asc)."""

    def test_top_levels_and_sort(self):
        t = "TEST-YES-1"
        # book.yes: YES bids at 0.48, 0.46, 0.45. Should return desc.
        # book.no: NO bids at 0.40, 0.38 → YES asks at 0.60, 0.62. Asc.
        _install_book(
            t,
            yes={"0.48": "50", "0.45": "30", "0.46": "20"},
            no={"0.40": "100", "0.38": "25"},
        )
        try:
            snap = kalshi.get_orderbook_snapshot(t, "YES", levels=5)
            assert snap["bids"] == [(0.48, 50), (0.46, 20), (0.45, 30)]
            assert snap["asks"] == [(0.60, 100), (0.62, 25)]
            assert snap["total_bid_depth"] == 100
            assert snap["total_ask_depth"] == 125
        finally:
            _uninstall_book(t)

    def test_levels_param_truncates(self):
        t = "TEST-YES-2"
        _install_book(
            t,
            yes={"0.50": "1", "0.49": "2", "0.48": "3", "0.47": "4"},
            no={"0.40": "1", "0.39": "2", "0.38": "3", "0.37": "4"},
        )
        try:
            snap = kalshi.get_orderbook_snapshot(t, "YES", levels=2)
            assert len(snap["bids"]) == 2
            assert len(snap["asks"]) == 2
            # Total depth still reflects the whole book, not the truncated list.
            assert snap["total_bid_depth"] == 10
            assert snap["total_ask_depth"] == 10
        finally:
            _uninstall_book(t)


class TestNoSide:
    """NO trade: bids = raw book.no (desc), asks = 1 − book.yes (asc).
    Symmetric mirror of the YES case."""

    def test_flip_is_symmetric(self):
        t = "TEST-NO-1"
        _install_book(
            t,
            yes={"0.60": "40"},  # ⇒ NO ask at 0.40
            no={"0.35": "50", "0.30": "15"},  # NO bids desc
        )
        try:
            snap = kalshi.get_orderbook_snapshot(t, "NO", levels=5)
            assert snap["bids"] == [(0.35, 50), (0.30, 15)]
            assert snap["asks"] == [(0.40, 40)]
            assert snap["total_bid_depth"] == 65
            assert snap["total_ask_depth"] == 40
        finally:
            _uninstall_book(t)


class TestEdges:
    def test_missing_book_returns_empty(self):
        snap = kalshi.get_orderbook_snapshot("DOES-NOT-EXIST", "YES")
        assert snap == {
            "bids": [], "asks": [], "total_bid_depth": 0, "total_ask_depth": 0,
        }

    def test_zero_qty_levels_dropped(self):
        """A level with qty=0 is book residue (canceled) and should not
        appear in the snapshot or contribute to depth totals."""
        t = "TEST-ZERO-1"
        _install_book(
            t,
            yes={"0.50": "10", "0.49": "0", "0.48": "5"},
            no={"0.40": "0"},
        )
        try:
            snap = kalshi.get_orderbook_snapshot(t, "YES", levels=5)
            prices = [p for p, _ in snap["bids"]]
            assert 0.49 not in prices
            assert snap["bids"] == [(0.50, 10), (0.48, 5)]
            assert snap["total_bid_depth"] == 15
            assert snap["asks"] == []
            assert snap["total_ask_depth"] == 0
        finally:
            _uninstall_book(t)

    def test_empty_dicts(self):
        """Local book exists but has no price levels — e.g. freshly cleared
        or pre-snapshot initialization. Should not crash; returns empties."""
        t = "TEST-EMPTY-1"
        _install_book(t, yes={}, no={})
        try:
            snap = kalshi.get_orderbook_snapshot(t, "YES")
            assert snap["bids"] == []
            assert snap["asks"] == []
            assert snap["total_bid_depth"] == 0
            assert snap["total_ask_depth"] == 0
        finally:
            _uninstall_book(t)
