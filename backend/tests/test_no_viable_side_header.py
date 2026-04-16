"""
Regression: `no_viable_side` rejections must carry a `side` on the
rejection dict so the decision-log header reads consistently with
PICKED / REJECTED / other-SKIPPED rows (all of which include YES/NO).

Prior behavior: when both sides of a ticker failed — typically YES
skipped on the sign filter and NO marked dead — the rejection was
returned with `side=None`, and the decision-log header rendered as
bare `SKIPPED → NYY -1.5` (no YES/NO suffix). Inconsistent next to
`PICKED → Over 11.5 YES` and other rows.

Fix: `_evaluate_market` picks a "primary side" — the one that made
it further through evaluation — and stashes it on the rejection.
The `reason:` string is unchanged; it still carries both sides'
rejection reasons so the body of the log stays informative.

Run:  cd backend && python -m pytest tests/test_no_viable_side_header.py -v
"""

from unittest.mock import patch

import pytest

from app import market_selector as ms
from app.market_selector import MarketInfo, _evaluate_market, format_decision_log


def _stub_market_stale(value: bool = False):
    return patch.object(ms.kalshi, "is_market_stale", lambda *a, **k: value)


def _stub_prices(prices: dict):
    return patch.object(ms.kalshi, "get_prices", lambda *a, **k: prices)


def _spread_market(flip: bool = False) -> MarketInfo:
    return MarketInfo(
        ticker="KXMLBSPREAD-TEST-NYY2",
        market_type="spread",
        line=1.5 if not flip else -1.5,
        delta_key="sp_1.5" if not flip else "sp_-1.5",
        flip=flip,
        label="SPR NYY -1.5",
    )


class TestPrimarySideSelection:
    def test_no_dead_yes_skipped_on_sign_picks_no(self):
        """d < 0 → YES branch skipped on sign filter, NO branch evaluated
        and found dead (entry=0.96 near the 0.95 ceiling). Primary side
        should be NO — the one that got past the sign filter."""
        market = _spread_market(flip=False)
        prices = {
            "yes_ask": 0.04, "yes_bid": 0.03,  # YES side wouldn't matter
            "no_ask": 0.96, "no_bid": 0.93,    # NO ask in dead zone (>= 0.95)
        }
        with _stub_market_stale(False), _stub_prices(prices):
            result = _evaluate_market(
                market, delta_value=-0.05,  # d < 0 → evaluates NO branch
                alpha=0.6, bet_size=100,
            )

        assert result["active"] is False
        assert result["rejection_reason"] == "no_viable_side"
        assert result["side"] == "NO"
        # reason: string must still carry both sides' diagnostics.
        detail = result["rejection_detail"]
        assert "YES: skipped: d<=0" in detail
        assert "NO: dead:" in detail

    def test_yes_dead_no_skipped_on_sign_picks_yes(self):
        """Mirror case: d > 0 → NO skipped on sign, YES evaluated and
        dead (entry near 0.05 floor). Primary side should be YES."""
        market = _spread_market(flip=False)
        prices = {
            "yes_ask": 0.04, "yes_bid": 0.03,  # YES ask in dead zone (<= 0.05)
            "no_ask": 0.96, "no_bid": 0.95,
        }
        with _stub_market_stale(False), _stub_prices(prices):
            result = _evaluate_market(
                market, delta_value=0.05,  # d > 0 → evaluates YES branch
                alpha=0.6, bet_size=100,
            )

        assert result["active"] is False
        assert result["rejection_reason"] == "no_viable_side"
        assert result["side"] == "YES"
        detail = result["rejection_detail"]
        assert "YES: dead:" in detail
        assert "NO: skipped: d>=0" in detail

    def test_both_skipped_on_sign_picks_yes_deterministically(self):
        """d == 0 → neither branch evaluates past sign. Deterministic
        fallback is YES so log output stays stable across runs."""
        market = _spread_market(flip=False)
        prices = {
            "yes_ask": 0.50, "yes_bid": 0.48,
            "no_ask": 0.50, "no_bid": 0.48,
        }
        with _stub_market_stale(False), _stub_prices(prices):
            result = _evaluate_market(
                market, delta_value=0.0,  # d == 0
                alpha=0.6, bet_size=100,
            )

        assert result["active"] is False
        assert result["rejection_reason"] == "no_viable_side"
        assert result["side"] == "YES"


class TestDecisionLogHeaderRendering:
    def test_no_viable_side_header_includes_side_suffix(self):
        """End-to-end: the rendered decision log shows
        `SKIPPED → NYY -1.5 NO` rather than the bare `SKIPPED → NYY -1.5`
        that the old code produced. Confirms the format-level goal of
        this change actually lands in the log output."""
        market = _spread_market(flip=False)
        prices = {
            "yes_ask": 0.04, "yes_bid": 0.03,
            "no_ask": 0.96, "no_bid": 0.93,
        }
        with _stub_market_stale(False), _stub_prices(prices):
            rejection = _evaluate_market(
                market, delta_value=-0.05,
                alpha=0.6, bet_size=100,
            )

        # Feed the rejection into the formatter via a minimal decision dict.
        decision = {
            "event": "HR",
            "alpha": 0.6,
            "picked_ticker": None,
            "picked_side": None,
            "fee_adjusted": False,
            "candidates": [],
            "rejected": [rejection],
        }
        rendered = format_decision_log(decision)

        assert "SKIPPED → NYY -1.5 NO" in rendered, (
            "expected 'SKIPPED → NYY -1.5 NO' in rendered log, got:\n"
            + rendered
        )
        # reason: line still shows both sides' diagnostics — verify we
        # didn't accidentally drop that.
        assert "reason: no_viable_side" in rendered
        assert "YES: skipped: d<=0" in rendered
        assert "NO: dead:" in rendered
