"""
EAGAIN retry tests for app.database write helpers.

Pins the behavior that Supabase writes retry on OSError(errno=11) but
re-raise every other exception into the outer try/except unchanged — and
that `log_orderbook_snapshot` retries exactly once (not twice), since it
runs inside daemon snapshot threads that already iterate over N sibling
tickers.

Regression target: the Railway-only dropped-write bug where `log_trade`,
`log_orderbook_snapshot`, and `update_trade_status("open")` fire
concurrently after a /buy fan-out and at least one lost the socket race
on transient EAGAIN. Before the retry wrapper the DB row stayed stuck at
its pre-open status until a later lifecycle write happened to succeed.

Run:  cd backend && python -m pytest tests/test_database_retry.py -v
"""

from types import SimpleNamespace

import pytest

from app import database as db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position(**overrides):
    """Minimal object with just the attributes `log_trade` reads off a
    Position. Using SimpleNamespace avoids pulling in trader.Trade and
    its full constructor dependency chain."""
    defaults = {
        "game_id": 111,
        "event": "HR",
        "market_ticker": "KXMLBGAME-TEST-AZ",
        "market_type": "moneyline",
        "market_title": "AZ wins",
        "side": "YES",
        "entry_price": 0.42,
        "sell_target": 0.50,
        "quantity": 100,
        "requested_quantity": 100,
        "status": "pending",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _ChainableMock:
    """Mimic the supabase-py query-builder chain:

        client.table(...).insert(...).execute()
        client.table(...).update(...).eq(...).execute()

    Every chain step returns self so the test is agnostic to which
    intermediate calls the helper makes. `execute()` pulls from
    `side_effects` left-to-right: each entry is either a BaseException
    (raised) or a plain value (returned). `execute_calls` tracks how
    many times `execute()` was invoked across the whole test.
    """

    def __init__(self):
        self.side_effects: list = []
        self.execute_calls = 0

    def table(self, *_a, **_kw): return self
    def insert(self, *_a, **_kw): return self
    def update(self, *_a, **_kw): return self
    def eq(self, *_a, **_kw): return self

    def execute(self):
        self.execute_calls += 1
        if not self.side_effects:
            raise AssertionError("execute() called more times than side_effects supplied")
        effect = self.side_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect


def _insert_response(rows):
    """Shape of supabase-py's APIResponse — helpers only read `.data`."""
    return SimpleNamespace(data=rows)


def _retry_warnings(caplog):
    return [r for r in caplog.records
            if r.levelname == "WARNING" and "EAGAIN" in r.getMessage()]


@pytest.fixture
def mock_client(monkeypatch):
    """Swap `db._client` with the chainable mock and force `_disabled=False`
    so `_get_client()` returns the mock instead of trying to init a real
    Supabase connection from test-env credentials."""
    mock = _ChainableMock()
    monkeypatch.setattr(db, "_client", mock)
    monkeypatch.setattr(db, "_disabled", False)
    return mock


# ---------------------------------------------------------------------------
# log_trade — EAGAIN retries=2 (default)
# ---------------------------------------------------------------------------

def test_log_trade_recovers_after_eagain(mock_client, caplog):
    """(a) First .execute() raises EAGAIN, second succeeds — log_trade
    returns the inserted id and emits exactly one retry warning."""
    caplog.set_level("WARNING", logger="app.database")
    mock_client.side_effects = [
        OSError(11, "Resource temporarily unavailable"),
        _insert_response([{"id": "trade-1"}]),
    ]
    tid = db.log_trade(
        session_id="sess-1", user_id="user-1",
        position=_make_position(), trade_info={},
        game_state=None, user_timestamp_ms=None,
        dry_run=True, alpha=0.6,
    )
    assert tid == "trade-1"
    assert mock_client.execute_calls == 2
    assert len(_retry_warnings(caplog)) == 1


def test_log_trade_does_not_retry_non_eagain(mock_client, caplog):
    """(b) A ValueError is NOT retried — the outer try/except catches
    it on the first attempt and `log_trade` returns None. Verifies that
    the retry wrapper isn't silently converting every exception into a
    retry loop."""
    caplog.set_level("WARNING", logger="app.database")
    mock_client.side_effects = [ValueError("unrelated failure")]
    tid = db.log_trade(
        session_id="sess-1", user_id="user-1",
        position=_make_position(), trade_info={},
        game_state=None, user_timestamp_ms=None,
        dry_run=True, alpha=0.6,
    )
    assert tid is None
    assert mock_client.execute_calls == 1
    assert _retry_warnings(caplog) == []


def test_log_trade_eagain_exhaustion(mock_client, caplog):
    """(c) All three attempts raise EAGAIN. `log_trade` returns None,
    the pre-existing final-failure error log fires once, and
    `.execute()` was called exactly 3 times (initial + retries=2)."""
    caplog.set_level("WARNING", logger="app.database")
    mock_client.side_effects = [
        OSError(11, "Resource temporarily unavailable"),
        OSError(11, "Resource temporarily unavailable"),
        BlockingIOError(11, "Resource temporarily unavailable"),
    ]
    tid = db.log_trade(
        session_id="sess-1", user_id="user-1",
        position=_make_position(), trade_info={},
        game_state=None, user_timestamp_ms=None,
        dry_run=True, alpha=0.6,
    )
    assert tid is None
    assert mock_client.execute_calls == 3
    error_logs = [r for r in caplog.records
                  if r.levelname == "ERROR" and "log_trade failed" in r.getMessage()]
    assert len(error_logs) == 1
    # Two retry warnings (after attempts 1 and 2); the third attempt's
    # failure is surfaced via the final-failure error log, not a warning.
    assert len(_retry_warnings(caplog)) == 2


# ---------------------------------------------------------------------------
# log_orderbook_snapshot — retries=1 contract
# ---------------------------------------------------------------------------

_BOOK = {
    "bids": [(0.42, 100)],
    "asks": [(0.44, 80)],
    "total_bid_depth": 100,
    "total_ask_depth": 80,
}


def test_snapshot_retries_exactly_once_on_recovery(mock_client, caplog):
    """(d) Snapshot writes recover on a single retry: first .execute()
    raises EAGAIN, second succeeds. Exactly one warning log; exactly
    two `.execute()` calls."""
    caplog.set_level("WARNING", logger="app.database")
    mock_client.side_effects = [
        BlockingIOError(11, "Resource temporarily unavailable"),
        _insert_response([{"id": "snap-1"}]),
    ]
    db.log_orderbook_snapshot(
        trade_id="trade-1", game_id=111,
        market_ticker="KXMLBGAME-TEST-AZ", side="YES",
        book=_BOOK, market_type="moneyline",
    )
    assert mock_client.execute_calls == 2
    assert len(_retry_warnings(caplog)) == 1


def test_snapshot_does_not_retry_twice(mock_client, caplog):
    """(d continued) Pins the retries=1 upper bound. Even with three
    EAGAIN attempts available, `.execute()` is called exactly twice
    (initial + 1 retry) and then the exception propagates to the outer
    swallow. A future refactor bumping the default to retries=2 would
    fail this test."""
    caplog.set_level("WARNING", logger="app.database")
    mock_client.side_effects = [
        BlockingIOError(11, "Resource temporarily unavailable"),
        BlockingIOError(11, "Resource temporarily unavailable"),
        BlockingIOError(11, "Resource temporarily unavailable"),
    ]
    db.log_orderbook_snapshot(
        trade_id="trade-1", game_id=111,
        market_ticker="KXMLBGAME-TEST-AZ", side="YES",
        book=_BOOK, market_type="moneyline",
    )
    assert mock_client.execute_calls == 2  # NOT 3 — retries=1 cap
    # One retry warning for the first failure; the second failure goes
    # straight to the outer try/except (error-level log, not warning).
    assert len(_retry_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# _is_eagain: chained-cause detection
# ---------------------------------------------------------------------------

def test_is_eagain_walks_exception_chain():
    """supabase-py wraps socket-level OSErrors in httpx exception types.
    The detector must walk `__cause__` / `__context__` to find the
    underlying EAGAIN; a top-level RuntimeError is not EAGAIN by itself."""
    inner = BlockingIOError(11, "Resource temporarily unavailable")
    outer = RuntimeError("httpx transport error")
    outer.__cause__ = inner
    assert db._is_eagain(outer) is True
    assert db._is_eagain(RuntimeError("something else")) is False
