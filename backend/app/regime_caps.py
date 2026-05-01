"""Regime-aware caps on model predictions where the engine's output systematically
diverges from market reality. See investigation findings: compute_spread_probability
is mathematically correct, but in late-tied-game regimes the order book prices off
tighter conditional run distributions than the engine assumes.

Each cap here represents an empirical ceiling derived from cached Kalshi data,
not a derived physical ceiling. The numbers are meant to be re-validated as
market conditions evolve.
"""

from app.constants import (
    SPR_LATE_TIED_CAP_ENABLED,
    SPR_LATE_TIED_CAP_INNING,
    SPR_LATE_TIED_CAP_MARGIN,
    SPR_LATE_TIED_CAP_VALUE,
)


def cap_spread_predicted_move(
    raw_predicted: float,
    inning: int,
    margin: int,
    market_type: str,
) -> float:
    """Cap |raw_predicted| at the empirical ceiling for late-tied SPR games.

    The cap deflates the prediction so downstream EV/min_move/fee_ratio gates
    operate on calibrated expected moves. Trades whose calibrated edge falls
    below the existing fee threshold are naturally rejected by those gates;
    trades whose calibrated edge still clears proceed with realistic targets
    and sizing.

    Sign is preserved. The cap only reduces magnitude.
    """
    if not SPR_LATE_TIED_CAP_ENABLED:
        return raw_predicted
    if market_type != "spread":
        return raw_predicted
    if inning < SPR_LATE_TIED_CAP_INNING:
        return raw_predicted
    if abs(margin) > SPR_LATE_TIED_CAP_MARGIN:
        return raw_predicted

    sign = 1.0 if raw_predicted >= 0 else -1.0
    return sign * min(abs(raw_predicted), SPR_LATE_TIED_CAP_VALUE)
