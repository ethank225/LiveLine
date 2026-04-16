"""
Regression tests: decision-log labels must flip for NO-side trades.

The bug: spread NO on a "SEA -1.5" market was rendering as "SEA -1.5" in the
decision log instead of "DET +1.5". `compute_display_label` in bet_label.py
already does the flip correctly — but only when it receives non-empty
home_abbr / away_abbr. If `_game_teams` isn't populated for the game when
compute_best_trades runs (cold-start race where discover_markets hasn't
completed), the flip silently degrades and NO-side spread / moneyline labels
come out wrong.

These tests drive compute_best_trades with synthetic markets + deltas and
assert the decision log on the resulting trade dict contains the flipped
label. The price book is mocked so we control which side (YES/NO) wins.

Run:  cd backend && python -m pytest tests/test_decision_log_labels.py -v
"""

import logging

import pytest

from app import market_selector as ms
from app.engine import EventDelta
from app.market_selector import (
    MarketInfo,
    _game_markets,
    _game_teams,
    _market_lock,
    _teams_lock,
    compute_best_trades,
    format_decision_log,
)


GAME_ID = 99_999_001


@pytest.fixture(autouse=True)
def reset_state():
    """Wipe the module-level caches compute_best_trades reads from so each
    test starts clean. Also swap out the kalshi client calls the picker
    makes with stubs (we don't touch a real exchange in unit tests)."""
    yield
    with _market_lock:
        _game_markets.pop(GAME_ID, None)
    with _teams_lock:
        _game_teams.pop(GAME_ID, None)


@pytest.fixture
def no_kalshi(monkeypatch):
    """Feed the picker deterministic prices + depth + stale/liquidity
    passes. ask=0.50, bid=0.40 on both sides gives a viable trade; stale
    and liquidity return "fine" so the only guards in play are the
    min-move / dead-market ones that spread eval already handles."""
    def prices(_ticker):
        return {"yes_ask": 0.50, "yes_bid": 0.40, "no_ask": 0.50, "no_bid": 0.40}

    monkeypatch.setattr(ms.kalshi, "get_prices", prices)
    monkeypatch.setattr(ms.kalshi, "is_market_stale", lambda *a, **k: False)
    monkeypatch.setattr(ms.kalshi, "has_exit_liquidity", lambda *a, **k: True)
    monkeypatch.setattr(
        ms.kalshi, "estimate_fill_qty", lambda *a, **k: 100,
    )
    monkeypatch.setattr(
        ms.kalshi, "get_orderbook_snapshot",
        lambda *a, **k: {"total_bid_depth": 1000, "total_ask_depth": 1000},
    )


def _install_teams(home: str = "SEA", away: str = "DET"):
    with _teams_lock:
        _game_teams[GAME_ID] = (home, away)


def _install_markets(markets: list[MarketInfo]):
    with _market_lock:
        _game_markets[GAME_ID] = markets


def _settings(**overrides) -> dict:
    base = {
        "alpha": 0.6,
        "bet_size": 100,
        "blowout_filter": False,
        "min_move_cents": 0,
        "stale_market_seconds": 0,
        "exit_slippage_cents": 0,
        "max_dollars": 500.0,
        "max_slippage_cents": 5.0,
    }
    base.update(overrides)
    return base


def _picked_candidate(trades: dict, event: str) -> dict:
    """Pull the first-ranked candidate (the winner) out of the decision
    log for the given event."""
    t = trades[event]
    dl = t.get("decision_log")
    assert dl, f"no decision_log on event={event}"
    assert dl["candidates"], f"no candidates on event={event}"
    return dl["candidates"][0]


class TestSpreadNoFlip:
    def test_home_spread_no_flips_to_away_with_plus(self, no_kalshi):
        """NO on SEA's home-side spread ticker ("SEA -1.5") should render
        as "DET +1.5" — that's what compute_display_label does for the
        frontend, and the decision log must match."""
        _install_teams(home="SEA", away="DET")
        _install_markets([
            MarketInfo(
                ticker="KXMLBSPREAD-26APR15SEADET-SEA2",
                market_type="spread", line=1.5, delta_key="sp_1.5",
                flip=False, label="SPR SEA -1.5",
            ),
        ])
        # Negative spread delta for SEA-covers-1.5 → NO side wins EV.
        deltas = [EventDelta(
            event="HR",
            win_expectancy_before=0.5, win_expectancy_after=0.5,
            delta=0.0, over_under={},
            spread={"1.5": {"before": 0.5, "after": 0.4, "delta": -0.1}},
        )]

        trades = compute_best_trades(GAME_ID, deltas, settings=_settings())

        picked = _picked_candidate(trades, "HR")
        assert picked["side"] == "NO"
        assert picked["display_label"] == "DET +1.5", (
            f"expected 'DET +1.5', got {picked['display_label']!r}"
        )

    def test_away_spread_no_flips_to_home_with_plus(self, no_kalshi):
        """Symmetric case: NO on DET's away-side spread ticker ("DET -1.5")
        should render as "SEA +1.5". `_pick_spread_markets_both_sides` now
        requires a matching home-side ticker to anchor the pair — install
        both, but only supply a delta for the away-side so the home-side
        is skipped at `_evaluate_market` time and this test stays focused
        on the away-side's NO flip."""
        _install_teams(home="SEA", away="DET")
        _install_markets([
            MarketInfo(
                ticker="KXMLBSPREAD-26APR15SEADET-SEA2",
                market_type="spread", line=1.5, delta_key="sp_1.5",
                flip=False, label="SPR SEA -1.5",
            ),
            MarketInfo(
                ticker="KXMLBSPREAD-26APR15SEADET-DET2",
                market_type="spread", line=-1.5, delta_key="sp_-1.5",
                flip=True, label="SPR DET -1.5",
            ),
        ])
        # Only "-1.5" delta supplied → home-side (line=1.5) lookup returns
        # None and is skipped; away-side evaluates normally. Positive
        # delta flipped to negative d_eff → NO side wins.
        deltas = [EventDelta(
            event="HR",
            win_expectancy_before=0.5, win_expectancy_after=0.5,
            delta=0.0, over_under={},
            spread={"-1.5": {"before": 0.5, "after": 0.6, "delta": 0.1}},
        )]

        trades = compute_best_trades(GAME_ID, deltas, settings=_settings())

        picked = _picked_candidate(trades, "HR")
        assert picked["side"] == "NO"
        assert picked["display_label"] == "SEA +1.5", (
            f"expected 'SEA +1.5', got {picked['display_label']!r}"
        )


class TestMoneylineNoFlip:
    def test_home_ml_no_flips_to_away_wins(self, no_kalshi):
        """NO on SEA's moneyline ticker should render as "DET wins"."""
        _install_teams(home="SEA", away="DET")
        _install_markets([
            MarketInfo(
                ticker="KXMLBGAME-26APR15SEADET-SEA",
                market_type="moneyline", line=None, delta_key="ml",
                flip=False, label="ML SEA",
            ),
        ])
        deltas = [EventDelta(
            event="HR",
            win_expectancy_before=0.55, win_expectancy_after=0.45,
            delta=-0.10, over_under={}, spread={},
        )]

        trades = compute_best_trades(GAME_ID, deltas, settings=_settings())

        picked = _picked_candidate(trades, "HR")
        assert picked["side"] == "NO"
        assert picked["display_label"] == "DET wins"


class TestFormattedOutput:
    """format_decision_log is the ultimate consumer — confirm the flipped
    label actually shows up in the rendered block, not just the dict."""

    def test_formatted_block_shows_flipped_spread_label(self, no_kalshi):
        _install_teams(home="SEA", away="DET")
        _install_markets([
            MarketInfo(
                ticker="KXMLBSPREAD-26APR15SEADET-SEA2",
                market_type="spread", line=1.5, delta_key="sp_1.5",
                flip=False, label="SPR SEA -1.5",
            ),
        ])
        deltas = [EventDelta(
            event="HR",
            win_expectancy_before=0.5, win_expectancy_after=0.5,
            delta=0.0, over_under={},
            spread={"1.5": {"before": 0.5, "after": 0.4, "delta": -0.1}},
        )]

        trades = compute_best_trades(GAME_ID, deltas, settings=_settings())
        rendered = format_decision_log(trades["HR"]["decision_log"])

        # Header uses the ticker-perspective label (strip "SPR " prefix)
        # + side. Two same-outcome candidates on different tickers stay
        # distinct because the ticker identity survives into the label.
        assert "PICKED → SEA -1.5 NO" in rendered, (
            "expected 'PICKED → SEA -1.5 NO' in formatted block, got:\n"
            + rendered
        )
        # display_label (frontend-facing) still flips to DET +1.5 for NO,
        # but the header line in the decision log must not — we want the
        # raw ticker perspective there so same-outcome candidates don't
        # collide on a single label.
        for line in rendered.splitlines():
            if line.lstrip().startswith(("│ PICKED →", "│ REJECTED →")):
                assert "DET +1.5" not in line, (
                    f"flipped DET +1.5 leaked into header line "
                    f"(should be ticker-perspective SEA -1.5): {line!r}"
                )


class TestEmptyTeamsRace:
    """When _game_teams hasn't been populated, compute_display_label can't
    flip. We can't fully recover the correct team, but we must (a) emit a
    warning so the race is visible in logs, and (b) not print a label
    that looks like a normal YES-side pick."""

    def test_warns_when_teams_empty(self, no_kalshi, caplog):
        _install_markets([
            MarketInfo(
                ticker="KXMLBSPREAD-26APR15SEADET-SEA2",
                market_type="spread", line=1.5, delta_key="sp_1.5",
                flip=False, label="SPR SEA -1.5",
            ),
        ])
        # Deliberately skip _install_teams — simulate the race.
        deltas = [EventDelta(
            event="HR",
            win_expectancy_before=0.5, win_expectancy_after=0.5,
            delta=0.0, over_under={},
            spread={"1.5": {"before": 0.5, "after": 0.4, "delta": -0.1}},
        )]

        with caplog.at_level(logging.WARNING, logger="app.market_selector"):
            compute_best_trades(GAME_ID, deltas, settings=_settings())

        messages = [r.message for r in caplog.records]
        assert any("_game_teams empty" in m for m in messages), (
            f"expected empty-teams warning in logs, got {messages!r}"
        )
