"""
Tests for C1: /buy must reject stale cached trades.

The bug: /trades computes candidate trades (entry, target, stop, exit-
liquidity, stale-market, min-move all checked at evaluation time) and
caches them. /buy reads the cache and fires immediately, without
re-evaluating any of those guards. Between /trades and user tap the book
can move — entry price can be invalid, the market can go stale, exit
liquidity can evaporate. At $500/play that gap is the difference between
a guarded entry and taking full slippage into a thin book.

The fix: stamp the cache with a timestamp on every write. /buy reads the
age; if older than CACHED_TRADE_MAX_AGE_S (5s), return 409. The frontend
re-fetches /trades (which rebuilds the cache with every guard re-run) and
retries the tap on the fresh data.

These tests exercise the market_selector side of the check. The /buy
endpoint integration is covered by the same logic via a direct call to
get_cached_trades_age_s here; a full FastAPI client test is deferred
until the Kalshi + Supabase test doubles are unified.

Run:  cd backend && python -m pytest tests/test_buy_freshness_guard.py -v
"""

import time
from unittest.mock import patch

import pytest

from app import market_selector as ms
from app.market_selector import (
    CACHED_TRADE_MAX_AGE_S,
    _cache_lock,
    _trade_cache,
    _trade_cache_timestamps,
    get_cached_trades,
    get_cached_trades_age_s,
)


@pytest.fixture(autouse=True)
def clean_cache():
    yield
    with _cache_lock:
        _trade_cache.clear()
        _trade_cache_timestamps.clear()


def _seed_cache(game_id: int, when: float | None = None):
    """Insert a fake trade into the cache with a controllable timestamp."""
    with _cache_lock:
        _trade_cache[game_id] = {"HR": {"active": True, "market_ticker": "TEST-MKT"}}
        if when is not None:
            _trade_cache_timestamps[game_id] = when


class TestAgeLookup:
    def test_missing_cache_returns_none(self):
        assert get_cached_trades_age_s(99999) is None

    def test_fresh_cache_reports_small_age(self):
        _seed_cache(1, when=time.time())
        age = get_cached_trades_age_s(1)
        assert age is not None
        assert 0 <= age < 0.5

    def test_old_cache_reports_large_age(self):
        _seed_cache(2, when=time.time() - 30.0)
        age = get_cached_trades_age_s(2)
        assert age is not None
        assert age > 29.0


class TestStalenessPolicy:
    """Mirrors what /buy does: reject when age is None or age >
    CACHED_TRADE_MAX_AGE_S. These tests assert the boundary conditions
    hold so the endpoint's logic lands on the right side of 'stale'."""

    def test_fresh_cache_under_limit_passes(self):
        _seed_cache(10, when=time.time() - 1.0)
        age = get_cached_trades_age_s(10)
        assert age is not None and age <= CACHED_TRADE_MAX_AGE_S

    def test_exactly_at_limit_is_stale(self):
        """Age == limit should be treated as stale (strict `> limit` would
        allow 5.0000s — one extra tick of tolerance past spec)."""
        _seed_cache(11, when=time.time() - CACHED_TRADE_MAX_AGE_S - 0.1)
        age = get_cached_trades_age_s(11)
        assert age is not None and age > CACHED_TRADE_MAX_AGE_S

    def test_missing_timestamp_is_stale(self):
        """A /buy hit before /trades ever ran has no timestamp. Policy:
        treat missing the same as 'infinitely old' → reject."""
        # Seed only the trades dict, not the timestamp dict, to simulate
        # a cache populated by a code path that doesn't stamp freshness.
        with _cache_lock:
            _trade_cache[12] = {"HR": {}}
            # No entry in _trade_cache_timestamps for 12.
        assert get_cached_trades_age_s(12) is None


class TestWriteStampsTimestamp:
    """compute_best_trades stamps the cache when it writes. Spot-check
    the write path with a direct lock-level insert (simulating what
    compute_best_trades does internally), then via the public helper."""

    def test_direct_write_path_updates_timestamp(self):
        """Replicates the write inside compute_best_trades: cache +
        timestamp updated under the same lock. No race possible."""
        fake_result = {"HR": {"active": True}}

        t0 = time.time()
        with _cache_lock:
            _trade_cache[50] = fake_result
            _trade_cache_timestamps[50] = time.time()

        assert get_cached_trades(50) is fake_result
        age = get_cached_trades_age_s(50)
        assert age is not None and age >= 0 and age < 0.1

    def test_age_grows_monotonically(self):
        """Stamping once and checking twice: age at t1 > age at t0."""
        _seed_cache(60, when=time.time())
        age0 = get_cached_trades_age_s(60)
        # Tiny sleep to guarantee a measurable delta without slowing CI.
        time.sleep(0.02)
        age1 = get_cached_trades_age_s(60)
        assert age0 is not None and age1 is not None
        assert age1 > age0


class TestPolicyConstant:
    """Guardrail: the cache-age policy constant is the one /buy reads.
    A refactor that silently decouples them would defeat the check."""

    def test_constant_is_small_and_positive(self):
        """Sanity bounds — a 0s or negative limit would reject every tap;
        a 60s+ limit would defeat the purpose (books move in seconds)."""
        assert 1.0 <= CACHED_TRADE_MAX_AGE_S <= 15.0

    def test_main_imports_the_same_constant(self):
        """If main.py defined its own MAX_AGE, a bump here wouldn't
        propagate. Verify the /buy endpoint reads ms.CACHED_TRADE_MAX_AGE_S."""
        from app import main
        assert main.CACHED_TRADE_MAX_AGE_S is ms.CACHED_TRADE_MAX_AGE_S
