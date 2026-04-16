"""
Regression: periodic ticker refresh during MLB polling.

Context: `discover_markets` short-circuits after the first call per
game_id — correct for the hot path (/stream, /buy) but it froze the
spread ticker set. Kalshi lists higher spread / O/U lines incrementally
as the game state shifts; without a periodic refresh, `pick_spread_line`
pins on the tightest pre-game line once the margin exceeds what we
initially discovered. `refresh_markets` runs out-of-band on each MLB
poll cycle and adds any new tickers Kalshi has listed since.

Run:  cd backend && python -m pytest tests/test_refresh_markets.py -v
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from app import market_selector as ms
from app.market_selector import (
    MarketInfo,
    _discovered_games,
    _game_markets,
    _market_lock,
    refresh_markets,
)


GAME_ID = 99_999_042


@pytest.fixture(autouse=True)
def reset_registry():
    """Clean the module-level caches so each test starts fresh."""
    with _market_lock:
        _game_markets.pop(GAME_ID, None)
    _discovered_games.discard(GAME_ID)
    yield
    with _market_lock:
        _game_markets.pop(GAME_ID, None)
    _discovered_games.discard(GAME_ID)


def _seed_initial_markets():
    """Simulate the state `discover_markets` leaves behind after the
    initial /stream connect: a moneyline pair + a couple low spread
    lines. The refresh path then adds new tickers Kalshi lists later."""
    initial = [
        # Moneyline pair (2 tickers, stable).
        MarketInfo(
            ticker="KXMLBGAME-26APR15TEXATH-TEX",
            market_type="moneyline", line=None, delta_key="ml",
            flip=False, label="ML TEX",
        ),
        MarketInfo(
            ticker="KXMLBGAME-26APR15TEXATH-ATH",
            market_type="moneyline", line=None, delta_key="ml",
            flip=True, label="ML ATH",
        ),
        # Two opening spread lines per side (0.5, 1.5) — pre-game expected.
        MarketInfo(
            ticker="KXMLBSPREAD-26APR15TEXATH-TEX1",
            market_type="spread", line=0.5, delta_key="sp_0.5",
            flip=False, label="SPR TEX -0.5",
        ),
        MarketInfo(
            ticker="KXMLBSPREAD-26APR15TEXATH-TEX2",
            market_type="spread", line=1.5, delta_key="sp_1.5",
            flip=False, label="SPR TEX -1.5",
        ),
        MarketInfo(
            ticker="KXMLBSPREAD-26APR15TEXATH-ATH1",
            market_type="spread", line=-0.5, delta_key="sp_-0.5",
            flip=True, label="SPR ATH -0.5",
        ),
        MarketInfo(
            ticker="KXMLBSPREAD-26APR15TEXATH-ATH2",
            market_type="spread", line=-1.5, delta_key="sp_-1.5",
            flip=True, label="SPR ATH -1.5",
        ),
    ]
    with _market_lock:
        _game_markets[GAME_ID] = initial
    _discovered_games.add(GAME_ID)
    return initial


def _install_kalshi_mocks(monkeypatch, get_markets_impl, subscribe=None):
    """Wire up `kalshi.get_markets` and `kalshi.subscribe` so
    `refresh_markets` exercises its full flow without any network."""
    # `is_connected` is a property on KalshiManager — patch the class-
    # level descriptor so `ms.kalshi.is_connected` returns True without
    # needing real Kalshi state. Restores after the test.
    monkeypatch.setattr(
        type(ms.kalshi), "is_connected",
        property(lambda self: True),
    )
    monkeypatch.setattr(ms.kalshi, "get_markets", get_markets_impl)
    sub_mock = subscribe or MagicMock()
    monkeypatch.setattr(ms.kalshi, "subscribe", sub_mock)
    return sub_mock


class TestRefreshMarketsAddsNew:
    def test_adds_new_spread_tickers_from_kalshi(self, monkeypatch):
        """The production case: game opens with spread lines 0.5/1.5 on
        both sides, score moves to margin=3, Kalshi has added TEX3/TEX4
        and ATH3/ATH4 tickers. refresh_markets must subscribe them and
        extend _game_markets."""
        _seed_initial_markets()

        def fake_get_markets(event_ticker: str):
            if event_ticker == "KXMLBGAME-26APR15TEXATH":
                # ML is stable — same pair returns.
                return [
                    {"ticker": "KXMLBGAME-26APR15TEXATH-TEX", "title": ""},
                    {"ticker": "KXMLBGAME-26APR15TEXATH-ATH", "title": ""},
                ]
            if event_ticker == "KXMLBTOTAL-26APR15TEXATH":
                return []
            if event_ticker == "KXMLBSPREAD-26APR15TEXATH":
                # Original 0.5/1.5 plus new 2.5/3.5 (line = n − 0.5).
                return [
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-TEX1", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-TEX2", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-TEX3", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-TEX4", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-ATH1", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-ATH2", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-ATH3", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-ATH4", "title": ""},
                ]
            return []

        subscribe_mock = _install_kalshi_mocks(monkeypatch, fake_get_markets)

        added = refresh_markets(GAME_ID, "TEX", "ATH")

        assert sorted(added) == sorted([
            "KXMLBSPREAD-26APR15TEXATH-TEX3",
            "KXMLBSPREAD-26APR15TEXATH-TEX4",
            "KXMLBSPREAD-26APR15TEXATH-ATH3",
            "KXMLBSPREAD-26APR15TEXATH-ATH4",
        ])

        with _market_lock:
            cached = list(_game_markets[GAME_ID])
        cached_tickers = {m.ticker for m in cached}
        # Originals preserved.
        assert "KXMLBSPREAD-26APR15TEXATH-TEX1" in cached_tickers
        assert "KXMLBSPREAD-26APR15TEXATH-TEX2" in cached_tickers
        # New additions present.
        assert "KXMLBSPREAD-26APR15TEXATH-TEX3" in cached_tickers
        assert "KXMLBSPREAD-26APR15TEXATH-TEX4" in cached_tickers

        # New tickers were actually subscribed (price feed wired).
        subscribed = {call.args[0] for call in subscribe_mock.call_args_list}
        assert "KXMLBSPREAD-26APR15TEXATH-TEX3" in subscribed
        assert "KXMLBSPREAD-26APR15TEXATH-TEX4" in subscribed
        assert "KXMLBSPREAD-26APR15TEXATH-ATH3" in subscribed
        assert "KXMLBSPREAD-26APR15TEXATH-ATH4" in subscribed

        # Sanity: the new spread MarketInfo carries correct line + flip.
        added_tex3 = next(
            m for m in cached
            if m.ticker == "KXMLBSPREAD-26APR15TEXATH-TEX3"
        )
        assert added_tex3.line == 2.5
        assert added_tex3.flip is False
        added_ath3 = next(
            m for m in cached
            if m.ticker == "KXMLBSPREAD-26APR15TEXATH-ATH3"
        )
        assert added_ath3.line == -2.5
        assert added_ath3.flip is True

    def test_adds_new_ou_tickers_as_total_rises(self, monkeypatch):
        """O/U ladder extends as total runs climb. Same mechanism as
        spread — diff against the cached set, subscribe what's new."""
        _seed_initial_markets()
        # Add a low O/U to the cached set so the diff has a baseline.
        with _market_lock:
            _game_markets[GAME_ID].append(MarketInfo(
                ticker="KXMLBTOTAL-26APR15TEXATH-8",
                market_type="over_under", line=7.5,
                delta_key="ou_7.5", flip=False, label="O/U 7.5",
            ))

        def fake_get_markets(event_ticker: str):
            if event_ticker == "KXMLBGAME-26APR15TEXATH":
                return []
            if event_ticker == "KXMLBTOTAL-26APR15TEXATH":
                return [
                    {"ticker": "KXMLBTOTAL-26APR15TEXATH-8", "title": ""},
                    {"ticker": "KXMLBTOTAL-26APR15TEXATH-12", "title": ""},
                    {"ticker": "KXMLBTOTAL-26APR15TEXATH-14", "title": ""},
                ]
            if event_ticker == "KXMLBSPREAD-26APR15TEXATH":
                return []
            return []

        _install_kalshi_mocks(monkeypatch, fake_get_markets)

        added = refresh_markets(GAME_ID, "TEX", "ATH")

        assert sorted(added) == sorted([
            "KXMLBTOTAL-26APR15TEXATH-12",
            "KXMLBTOTAL-26APR15TEXATH-14",
        ])


class TestRefreshMarketsNoOp:
    def test_no_new_tickers_is_silent(self, monkeypatch, caplog):
        """When Kalshi returns only tickers already in the cache, we
        do nothing — no subscribe calls, no added-ticker log."""
        _seed_initial_markets()

        def fake_get_markets(event_ticker: str):
            if event_ticker == "KXMLBGAME-26APR15TEXATH":
                return [
                    {"ticker": "KXMLBGAME-26APR15TEXATH-TEX", "title": ""},
                    {"ticker": "KXMLBGAME-26APR15TEXATH-ATH", "title": ""},
                ]
            if event_ticker == "KXMLBSPREAD-26APR15TEXATH":
                return [
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-TEX1", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-TEX2", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-ATH1", "title": ""},
                    {"ticker": "KXMLBSPREAD-26APR15TEXATH-ATH2", "title": ""},
                ]
            return []

        subscribe_mock = _install_kalshi_mocks(monkeypatch, fake_get_markets)

        with caplog.at_level(logging.INFO, logger="app.market_selector"):
            added = refresh_markets(GAME_ID, "TEX", "ATH")

        assert added == []
        assert subscribe_mock.call_count == 0
        # No "added N new tickers" info line when nothing changed.
        assert not any(
            "added" in r.message and "new tickers" in r.message
            for r in caplog.records
        )

    def test_never_removes_existing_tickers(self, monkeypatch):
        """Near-resolution markets sometimes drop from the listing
        endpoint while still being tradeable — refresh_markets must
        leave them in the cache so the picker keeps seeing them."""
        _seed_initial_markets()

        def fake_get_markets(event_ticker: str):
            # Kalshi returns only a subset (simulated: near-final market
            # dropped the low-line tickers, only TEX3 visible now).
            if event_ticker == "KXMLBSPREAD-26APR15TEXATH":
                return [{"ticker": "KXMLBSPREAD-26APR15TEXATH-TEX3", "title": ""}]
            return []

        _install_kalshi_mocks(monkeypatch, fake_get_markets)

        refresh_markets(GAME_ID, "TEX", "ATH")

        with _market_lock:
            cached_tickers = {m.ticker for m in _game_markets[GAME_ID]}
        # All 6 originals stayed + the new TEX3 was added.
        assert "KXMLBSPREAD-26APR15TEXATH-TEX1" in cached_tickers
        assert "KXMLBSPREAD-26APR15TEXATH-TEX2" in cached_tickers
        assert "KXMLBSPREAD-26APR15TEXATH-ATH1" in cached_tickers
        assert "KXMLBSPREAD-26APR15TEXATH-ATH2" in cached_tickers
        assert "KXMLBSPREAD-26APR15TEXATH-TEX3" in cached_tickers


class TestRefreshMarketsErrorIsolation:
    def test_kalshi_api_error_is_logged_and_state_preserved(
        self, monkeypatch, caplog,
    ):
        """A Kalshi lookup failure must not corrupt _game_markets or
        abort the MLB poll loop — log a warning per market type that
        failed and return whatever partial results succeeded."""
        _seed_initial_markets()
        pre_snapshot = [m.ticker for m in _game_markets[GAME_ID]]

        def fake_get_markets(event_ticker: str):
            # All three lookups raise — worst case.
            raise RuntimeError("simulated Kalshi 502")

        _install_kalshi_mocks(monkeypatch, fake_get_markets)

        with caplog.at_level(logging.WARNING, logger="app.market_selector"):
            added = refresh_markets(GAME_ID, "TEX", "ATH")

        assert added == []
        # State preserved — existing tickers untouched.
        with _market_lock:
            post_snapshot = [m.ticker for m in _game_markets[GAME_ID]]
        assert post_snapshot == pre_snapshot
        # At least one warning emitted so the silent-no-op case is
        # distinguishable from the "Kalshi broke" case in the logs.
        assert any(
            "refresh_markets" in r.message and "failed" in r.message
            for r in caplog.records
        )

    def test_not_yet_discovered_game_is_a_noop(self, monkeypatch):
        """If refresh fires before discover_markets has seeded state
        (race on startup, or a caller ordering bug), return cleanly
        rather than attempting to rebuild from empty state."""
        # Deliberately do NOT seed — game_id is absent from registry.
        subscribe_mock = _install_kalshi_mocks(
            monkeypatch, lambda *_: [], subscribe=MagicMock(),
        )

        added = refresh_markets(GAME_ID, "TEX", "ATH")

        assert added == []
        assert subscribe_mock.call_count == 0

    def test_kalshi_disconnected_is_a_noop(self, monkeypatch):
        """If Kalshi isn't connected, refresh is a no-op — the poll
        loop will retry on its next cycle."""
        _seed_initial_markets()
        monkeypatch.setattr(
            type(ms.kalshi), "is_connected",
            property(lambda self: False),
        )
        # get_markets should never be called — set it to raise if it is.
        monkeypatch.setattr(
            ms.kalshi, "get_markets",
            lambda *a, **k: pytest.fail(
                "get_markets must not be called when Kalshi is disconnected"
            ),
        )

        added = refresh_markets(GAME_ID, "TEX", "ATH")

        assert added == []


class TestPickSpreadLineFallbackWarns:
    """When `pick_spread_line` can't find a candidate line above the
    current margin, it falls back to the closest absolute match. Until
    `refresh_markets` caught up we were on that fallback silently. The
    warning log makes the condition visible so operators can tell
    "stale ticker set" from "this is just the best we have." """

    def test_fallback_branch_emits_warning_with_context(self, caplog):
        from app.line_selection import pick_spread_line

        class _Line:
            def __init__(self, line):
                self.line = line

        # Margin = 4, candidates max out at 1.5 — the stale-set shape.
        candidates = [_Line(0.5), _Line(1.5)]

        with caplog.at_level(logging.WARNING, logger="app.line_selection"):
            pick = pick_spread_line(4, candidates)

        assert pick.line == 1.5   # closest abs to margin=4
        warnings = [
            r for r in caplog.records
            if "pick_spread_line fallback" in r.message
        ]
        assert warnings, (
            f"expected a fallback warning, got {[r.message for r in caplog.records]}"
        )
        msg = warnings[0].message
        # Warning includes enough context to diagnose the cause.
        assert "margin=4" in msg
        assert "1.5" in msg   # the picked line

    def test_normal_path_no_warning(self, caplog):
        from app.line_selection import pick_spread_line

        class _Line:
            def __init__(self, line):
                self.line = line

        # Margin = 1, candidates include a line strictly above — no fallback.
        candidates = [_Line(0.5), _Line(1.5), _Line(2.5)]

        with caplog.at_level(logging.WARNING, logger="app.line_selection"):
            pick = pick_spread_line(1, candidates)

        assert pick.line == 1.5
        assert not any(
            "fallback" in r.message for r in caplog.records
        )
