"""
Regression: `_pick_spread_markets_both_sides` must pair the away-side
ticker whose line magnitude mirrors the home-side pick, not run a
separate `pick_spread_line` on the away bucket.

Prior bug: independent away-side selection ran `pick_spread_line` on a
bucket of tickers whose `line` is stored negated (e.g. [-0.5, -1.5, -2.5,
…]). Its "smallest positive dist above margin" rule never matches for
negative lines, so it fell through to the closest-abs-to-margin fallback
and picked a non-mirror line — e.g. home WSH-2 (line=1.5) paired with
away PIT-1 (line=-0.5). The economic complement ticker PIT-2 (line=-1.5,
whose NO side is WSH +1.5) never entered the candidate set and the decision
log showed only one side of the pair.

Fix: after picking the home-side line X, look up the away-side ticker
whose `line == -X` directly. Gracefully skip away if no mirror exists.

Run:  cd backend && python -m pytest tests/test_spread_line_mirror.py -v
"""

import logging

import pytest

from app.market_selector import MarketInfo, _pick_spread_markets_both_sides


def _spread(line: float, *, flip: bool, ticker: str) -> MarketInfo:
    """Build a spread MarketInfo matching discover_markets' shape —
    home-side stores positive line with flip=False, away-side stores
    negated line with flip=True."""
    if flip:
        # Away-side: `line` on the MarketInfo is already stored negated.
        return MarketInfo(
            ticker=ticker,
            market_type="spread",
            line=line,   # caller passes the already-negated value
            delta_key=f"sp_{line}",
            flip=True,
            label=f"SPR {ticker.rsplit('-', 1)[-1]} -{-line}",
        )
    return MarketInfo(
        ticker=ticker,
        market_type="spread",
        line=line,
        delta_key=f"sp_{line}",
        flip=False,
        label=f"SPR {ticker.rsplit('-', 1)[-1]} -{line}",
    )


def _full_market_set() -> list[MarketInfo]:
    """Symmetric WSH vs PIT coverage at lines ±{0.5, 1.5, 2.5}."""
    markets = []
    for n in (0.5, 1.5, 2.5):
        markets.append(_spread(n, flip=False, ticker=f"KXMLBSPREAD-TEST-WSH{int(n + 0.5)}"))
        markets.append(_spread(-n, flip=True, ticker=f"KXMLBSPREAD-TEST-PIT{int(n + 0.5)}"))
    return markets


class TestMatchedPair:
    def test_margin_zero_pairs_home_and_away_at_same_line_magnitude(self):
        """Game tied (margin=0). Home-side pick is the smallest positive
        line above 0 → 0.5. The mirror away-side ticker must be the one
        with stored line=-0.5 (the PIT-1 ticker). Together they form the
        matched moneyline-equivalent pair."""
        picks = _pick_spread_markets_both_sides(0, _full_market_set())

        assert len(picks) == 2
        home, away = picks
        assert home.flip is False
        assert away.flip is True
        # Line magnitudes match — this is the invariant that was broken.
        assert home.line == 0.5
        assert away.line == -0.5

    def test_margin_one_picks_matched_pair_at_line_1_5(self):
        """Home leading by 1. pick_spread_line picks line 1.5 (smallest
        dist > 1). Away mirror is line=-1.5 (the PIT-2 ticker) — the one
        whose NO side is the economic WSH +1.5 contract that the prior
        bug excluded from candidates."""
        picks = _pick_spread_markets_both_sides(1, _full_market_set())

        assert len(picks) == 2
        home, away = picks
        assert home.line == 1.5
        assert away.line == -1.5
        # And crucially, the away ticker is the PIT-2 one (suffix "PIT2"
        # on the ticker), not PIT-1 which the old fallback would pick.
        assert away.ticker.endswith("-PIT2")

    def test_margin_two_picks_matched_pair_at_line_2_5(self):
        picks = _pick_spread_markets_both_sides(2, _full_market_set())

        assert len(picks) == 2
        home, away = picks
        assert home.line == 2.5
        assert away.line == -2.5
        assert home.ticker.endswith("-WSH3")
        assert away.ticker.endswith("-PIT3")


class TestAsymmetricCoverage:
    def test_away_missing_returns_home_only(self, caplog):
        """Kalshi coverage sometimes has no away-side ticker at the
        matching line (heavy favorite → no market for opponent cover).
        The selector must emit the home candidate and skip away without
        crashing or substituting a non-mirror line."""
        markets = [
            _spread(0.5, flip=False, ticker="KXMLBSPREAD-TEST-WSH1"),
            _spread(1.5, flip=False, ticker="KXMLBSPREAD-TEST-WSH2"),
            # Away-side: only line=-0.5 exists; no -1.5 mirror.
            _spread(-0.5, flip=True, ticker="KXMLBSPREAD-TEST-PIT1"),
        ]

        with caplog.at_level(logging.DEBUG, logger="app.market_selector"):
            picks = _pick_spread_markets_both_sides(1, markets)

        # Home side picks line=1.5; away mirror (-1.5) absent.
        assert len(picks) == 1
        assert picks[0].flip is False
        assert picks[0].line == 1.5
        # A debug note makes the skip visible if it starts happening
        # often — signals asymmetric Kalshi coverage rather than silently
        # dropping candidates.
        assert any(
            "no away-side mirror" in r.message
            for r in caplog.records
        ), f"expected debug log about missing mirror, got {[r.message for r in caplog.records]!r}"

    def test_home_missing_returns_empty(self):
        """If no home-side line is pickable, the function returns []
        rather than trying to pair an away with nothing."""
        markets = [
            _spread(-0.5, flip=True, ticker="KXMLBSPREAD-TEST-PIT1"),
            _spread(-1.5, flip=True, ticker="KXMLBSPREAD-TEST-PIT2"),
        ]

        picks = _pick_spread_markets_both_sides(0, markets)

        assert picks == []

    def test_no_spread_markets_returns_empty(self):
        """Games whose market discovery returned no spread tickers at
        all (rare, but possible on Kalshi) must not crash."""
        assert _pick_spread_markets_both_sides(0, []) == []
