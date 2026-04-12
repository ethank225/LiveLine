"""Shared constants and helpers for backtest modules.

Re-exports from app/constants.py (single source of truth) plus any
backtest-only values.
"""

from app.constants import (  # noqa: F401 — re-export
    # Trading parameters
    DEFAULT_ALPHA, ALPHAS, DEFAULT_BET_SIZE,
    CLEAN_WINDOW_SECONDS, UNDO_WINDOW_SECONDS,
    STOP_LOSS_CENTS, STOP_LEVELS,
    # Market lines
    ALL_OU_LINES, ALL_SPREAD_LINES,
    DEFAULT_OU_LINES, DEFAULT_SPREAD_LINES,
    # Filters
    BLOWOUT_THRESHOLD, HIGH_LEV_ML_THRESHOLD, HIGH_LEV_OU_PROXIMITY,
    # Backtest-specific
    ENTRY_OFFSET, CLEAN_BUFFER, WINDOW_SECONDS,
    EXIT_WINDOWS, WINDOW_PCTS, WINDOW_LABELS, EVENT_ORDER,
    # Helpers
    get_clean_window, price_at_window_pct,
)

# Backtest-only constants (not used by live bot)
CONTRACTS = DEFAULT_BET_SIZE  # alias for backward compatibility

# Thin-liquidity thresholds (backtest reporting only)
THIN_THRESHOLDS = {"moneyline": 20, "over_under": 5, "spread": 5}
