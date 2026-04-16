"""
Regression: NO-side sizing must read the local book via book.yes, even when
book.no (NO-side bids) is empty.

The bug: `calculate_position_size`, `estimate_fill_qty`, `_get_ask_levels`,
and `get_orderbook_snapshot` gated the local-book fast path on
`book.best_ask is not None`. `best_ask` is derived from `book.no` alone
(`1 - max(book.no)`), so a fully-populated YES-side book with an empty
NO-side bid stack would falsely mark has_local=False and fall through to
REST — returning shallow or stale depth for NO-side candidates even though
the local book had 100k contracts of YES bids (= NO asks) already loaded.

The fix: gate on the side-specific dict that the query actually consumes
(YES sizing needs book.no; NO sizing needs book.yes).

Run:  cd backend && python -m pytest tests/test_sizing_no_side.py -v
"""

from pykalshi import OrderbookManager

from app.kalshi_client import kalshi


TICKER = "KXMLBGAME-26APR15WSHPIT-WSH"


def _install_yes_heavy_book(monkeypatch):
    """Build an OrderbookManager whose book.yes is fully populated with
    100k contracts of YES bids across three price levels, and book.no is
    empty (no NO-side bids at all). This reproduces the thin-NO-book shape
    that previously tripped the `best_ask is None` gate."""
    book = OrderbookManager(TICKER)
    book.apply_snapshot(
        yes_levels=[
            ("0.60", "100"),
            ("0.59", "50000"),
            ("0.58", "50000"),
        ],
        no_levels=[],
    )
    # Sanity: best_ask really is None because book.no is empty — that's
    # the exact condition the old gate tripped on.
    assert book.best_ask is None
    assert book.best_bid == "0.60"

    monkeypatch.setitem(kalshi._books, TICKER, book)
    # The REST fallback must NOT be consulted; wire it to raise so any
    # attempt to call it fails the test loudly.
    def boom(*a, **k):
        raise AssertionError(
            "REST fallback was called — local book read should have "
            "succeeded via book.yes"
        )
    monkeypatch.setattr(kalshi, "client", None)
    # calculate_position_size goes through _get_ask_levels which, on
    # local-book hit, returns before reaching the REST branch. But if
    # any gate regresses, `self.client is None` guarantees an early
    # empty return rather than a network call in CI.
    _ = boom  # silence unused warning


def _cleanup_book(monkeypatch):
    """monkeypatch.setitem auto-reverses, but best to re-assert after."""
    pass


class TestNoSideLocalRead:
    def test_calculate_position_size_reads_local_for_no_side(self, monkeypatch):
        """NO-side sizing: NO asks come from YES bids. A book with 100k
        YES bids should let us size a substantial NO position locally
        (tight slippage caps the depth actually taken, but the LOCAL path
        must be the one feeding the calculation)."""
        _install_yes_heavy_book(monkeypatch)

        sized = kalshi.calculate_position_size(
            TICKER, "NO", max_dollars=500.0, max_slippage_cents=2.0,
        )

        assert sized["source"] == "local", (
            f"expected local-book read for NO side, got source={sized['source']!r}"
        )
        # Best NO ask = 1 - 0.60 = 0.40, 100 contracts there.
        assert sized["best_ask"] == 0.40
        # Slippage window 2¢ (limit = 0.42) covers all three levels:
        # 0.40 (100), 0.41 (50000), 0.42 (50000) → 100100.
        assert sized["depth_at_limit"] == 100100
        # Budget $500 caps the take well below full depth — just prove we
        # took more than the 100 the bug returned.
        assert sized["contracts"] > 100, (
            f"expected > 100 contracts from 100k depth, got {sized['contracts']}"
        )

    def test_estimate_fill_qty_reads_local_for_no_side(self, monkeypatch):
        """Public entrypoint used by the picker. Was returning 100 for
        WSH NO when 100k contracts were locally available."""
        _install_yes_heavy_book(monkeypatch)

        qty = kalshi.estimate_fill_qty(
            TICKER, "NO", max_dollars=500.0, max_slippage_cents=2.0,
        )

        assert qty > 100, (
            f"estimate_fill_qty returned {qty}; expected substantial NO depth "
            f"from 100k YES bids in local book"
        )

    def test_yes_side_still_gated_on_no_bids(self, monkeypatch):
        """Symmetric sanity: YES sizing needs book.no (NO bids become YES
        asks). When book.no is empty, YES sizing should fall through to
        REST. We stub REST to an empty response so the function returns
        zero contracts rather than reading book.yes by mistake."""
        _install_yes_heavy_book(monkeypatch)

        # Force REST path to a controlled empty response.
        class _FakeBook:
            def model_dump(self):
                return {"orderbook": {"yes_dollars": [], "no_dollars": []}}

        class _FakeMarket:
            def get_orderbook(self, depth):
                return _FakeBook()

        class _FakeClient:
            def get_market(self, ticker):
                return _FakeMarket()

        monkeypatch.setattr(kalshi, "client", _FakeClient())

        sized = kalshi.calculate_position_size(
            TICKER, "YES", max_dollars=500.0, max_slippage_cents=2.0,
        )

        # book.no is empty → local gate fails for YES side → REST fallback
        # returns an empty ask ladder → zero contracts.
        assert sized["source"] == "rest"
        assert sized["contracts"] == 0


class TestDepthSnapshotNoSide:
    def test_get_orderbook_snapshot_reads_local_for_no_side(self, monkeypatch):
        """The decision log's `ask_depth` field goes through this helper.
        It should report the full 100k YES-bid depth as NO asks."""
        _install_yes_heavy_book(monkeypatch)

        snap = kalshi.get_orderbook_snapshot(TICKER, "NO", levels=5)

        assert snap["total_ask_depth"] == 100100, (
            f"expected total_ask_depth=100100 (sum of YES bids), "
            f"got {snap['total_ask_depth']}"
        )
