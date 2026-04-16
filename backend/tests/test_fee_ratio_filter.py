"""
Regression: price-aware minimum-move filter in `_evaluate_market`.

A flat `min_move_cents` over-filters cheap contracts (tiny fees → a 2¢
move is profitable) and under-filters expensive ones (large fees → even
3¢ barely clears round-trip). The new `min_move_to_fee_ratio` gate
normalizes by per-contract round-trip fee. Both filters apply — a
candidate must satisfy min_move_cents AND min_move_to_fee_ratio.

Tests cover (numbers from an empirical sanity run on the actual
taker/maker formulas in app/fees.py):

  entry=0.10 target=0.13 → ratio 3.6x  → passes at 2.0
  entry=0.50 target=0.53 → ratio 1.4x  → fails at 2.0, passes at 1.0
  entry=0.20 target=0.90 → ratio 54.7x → passes (walk-off scale)
  entry=0.05 target=0.07 → ratio 4.4x  → ratio passes but abs floor
                                         (3¢) cuts the 2¢ move

Run:  cd backend && python -m pytest tests/test_fee_ratio_filter.py -v
"""

from unittest.mock import patch

import pytest

from app import market_selector as ms
from app.market_selector import MarketInfo, _evaluate_market


def _stub_kalshi(prices: dict):
    """Stub the price + staleness + liquidity surfaces so the eval path
    runs deterministically down to the filters under test."""
    return [
        patch.object(ms.kalshi, "is_market_stale", lambda *a, **k: False),
        patch.object(ms.kalshi, "has_exit_liquidity", lambda *a, **k: True),
        patch.object(ms.kalshi, "estimate_fill_qty", lambda *a, **k: 100),
        patch.object(
            ms.kalshi, "get_orderbook_snapshot",
            lambda *a, **k: {"total_bid_depth": 1000, "total_ask_depth": 1000},
        ),
        patch.object(ms.kalshi, "get_prices", lambda *a, **k: prices),
    ]


def _moneyline_market() -> MarketInfo:
    return MarketInfo(
        ticker="KXMLBGAME-TEST-WSH",
        market_type="moneyline",
        line=None,
        delta_key="ml",
        flip=False,
        label="ML WSH",
    )


def _prices(yes_ask: float, target_offset_cents: float) -> dict:
    """Build a price dict with YES ask at `yes_ask` and spreads wide
    enough that `_is_dead` is only hit for ceiling/floor violations.
    target_offset_cents isn't used here — the caller sets alpha to land
    the target at the desired point."""
    return {
        "yes_ask": yes_ask,
        "yes_bid": max(0.01, yes_ask - 0.02),  # 2¢ spread
        "no_ask": 1.0 - yes_ask + 0.02,
        "no_bid": 1.0 - yes_ask,
    }


def _run_eval(
    yes_ask: float,
    target_price: float,
    *,
    min_move_cents: int = 0,
    min_move_to_fee_ratio: float = 0.0,
    bet_size: int = 100,
) -> dict:
    """Drive _evaluate_market with prices that produce entry=yes_ask and
    sell_target=target_price (via alpha = target_move). delta=1.0 with
    alpha=(target - entry) places the target exactly at target_price."""
    market = _moneyline_market()
    move = target_price - yes_ask
    alpha = move   # delta=1.0 × alpha = move
    prices = _prices(yes_ask, 0)

    with _stub_kalshi(prices)[0], _stub_kalshi(prices)[1], \
         _stub_kalshi(prices)[2], _stub_kalshi(prices)[3], \
         _stub_kalshi(prices)[4]:
        return _evaluate_market(
            market, delta_value=1.0, alpha=alpha, bet_size=bet_size,
            min_move_cents=min_move_cents,
            min_move_to_fee_ratio=min_move_to_fee_ratio,
        )


class TestRatioGate:
    def test_ratio_3x_passes_at_default_2x(self):
        """entry=0.10, 3¢ move → ~0.83¢ round-trip per contract → 3.6x.
        Clears the 2.0 default."""
        result = _run_eval(
            yes_ask=0.10, target_price=0.13,
            min_move_cents=0, min_move_to_fee_ratio=2.0,
        )
        assert result.get("active") is not False, (
            f"expected viable candidate at ratio 3.6x, got rejection: "
            f"{result.get('rejection_reason')} — {result.get('rejection_detail')}"
        )
        assert result["side"] == "YES"
        assert result["entry_price"] == 0.10
        assert result["sell_target"] == 0.13

    def test_ratio_below_2x_fails_at_default(self):
        """entry=0.50, 3¢ move → ~2.19¢ round-trip → 1.37x. Below 2.0
        default → rejected with fee_ratio reason."""
        result = _run_eval(
            yes_ask=0.50, target_price=0.53,
            min_move_cents=0, min_move_to_fee_ratio=2.0,
        )
        assert result["active"] is False
        assert result["rejection_reason"] == "fee_ratio"
        detail = result["rejection_detail"]
        # Format: "move=3.0¢ fees=2.19¢ ratio=1.37x < 2.0x"
        assert "move=3.0¢" in detail, detail
        assert "ratio=" in detail and "x <" in detail, detail
        assert "2.0x" in detail, detail

    def test_walk_off_huge_move_always_passes(self):
        """70¢ move on a walk-off HR scenario clears any reasonable
        ratio threshold — per-contract fees top out around 1.75¢."""
        result = _run_eval(
            yes_ask=0.20, target_price=0.90,
            min_move_cents=5, min_move_to_fee_ratio=2.0,
        )
        assert result.get("active") is not False, (
            f"walk-off should pass, got {result}"
        )

    def test_ratio_1_0_effectively_disables_the_gate(self):
        """At min_move_to_fee_ratio=1.0 the gate demands only that move
        covers fees 1x (breakeven). entry=0.50 / 3¢ move is ratio 1.37x
        — rejected at 2.0, accepted at 1.0."""
        result = _run_eval(
            yes_ask=0.50, target_price=0.53,
            min_move_cents=0, min_move_to_fee_ratio=1.0,
        )
        assert result.get("active") is not False, (
            f"ratio 1.37x should pass at threshold 1.0, got rejection: "
            f"{result.get('rejection_reason')}"
        )

    def test_ratio_zero_disables_gate_entirely(self):
        """Explicit 0 threshold short-circuits the filter. Verifies
        the default path when the setting is turned off."""
        result = _run_eval(
            yes_ask=0.50, target_price=0.53,
            min_move_cents=0, min_move_to_fee_ratio=0.0,
        )
        assert result.get("active") is not False


class TestAbsoluteFloorStillFires:
    def test_cheap_entry_2c_move_blocked_by_absolute_floor(self):
        """entry=0.05 / 2¢ move has ratio ~4.3x (would pass any sane
        ratio gate) but the 3¢ absolute floor still cuts it. Both
        filters active, both must pass."""
        result = _run_eval(
            yes_ask=0.06, target_price=0.08,  # avoid 0.05 dead floor
            min_move_cents=3, min_move_to_fee_ratio=2.0,
        )
        assert result["active"] is False
        assert result["rejection_reason"] == "min_move", (
            f"expected absolute-floor rejection, got {result.get('rejection_reason')}: "
            f"{result.get('rejection_detail')}"
        )
        assert "move=2¢ < 3¢" in result["rejection_detail"]
