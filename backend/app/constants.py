"""
Shared constants for LiveLine live trading and backtesting.

Single source of truth — both app/ and backtests/ import from here.
"""

# ---------------------------------------------------------------------------
# Trading parameters
# ---------------------------------------------------------------------------

DEFAULT_ALPHA = 0.6          # target capture — backtest-optimized
ALPHAS = [0.3, 0.4, 0.5, 0.6]  # alpha grid for backtest sweep

DEFAULT_BET_SIZE = 100       # contracts per trade
DEFAULT_MAX_DOLLARS = 500.0  # max dollar spend per trade

CLEAN_WINDOW_SECONDS = 45   # auto-exit if sell hasn't filled (live timer)
UNDO_WINDOW_SECONDS = 3     # delay before sell placement (live undo)

STOP_LOSS_CENTS = 10         # cents below entry price
STOP_LEVELS = [0.02, 0.03, 0.05, 0.08, 0.10]  # dollars — for backtest sweep

# ---------------------------------------------------------------------------
# Market lines
# ---------------------------------------------------------------------------

# Full set of lines computed by the engine for live trading + backtest
ALL_OU_LINES = [2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5, 12.5, 13.5]
ALL_SPREAD_LINES = [-4.5, -3.5, -2.5, -1.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]

# Default subset (used when full lines aren't needed, e.g. API response)
DEFAULT_OU_LINES = [7.5, 8.5, 9.5]
DEFAULT_SPREAD_LINES = [-1.5, 1.5]

# ---------------------------------------------------------------------------
# Filters & thresholds
# ---------------------------------------------------------------------------

BLOWOUT_THRESHOLD = 5        # |score_diff| >= this skips moneyline
HIGH_LEV_ML_THRESHOLD = 0.01
HIGH_LEV_OU_PROXIMITY = 3

# ---------------------------------------------------------------------------
# Backtest-specific
# ---------------------------------------------------------------------------

ENTRY_OFFSET = 5             # seconds before MLB event timestamp for entry
CLEAN_BUFFER = 5             # seconds before next pitch to stop measuring
WINDOW_SECONDS = 30          # default single-window comparison

EXIT_WINDOWS = [5, 10, 15, 30, 45, 60, 90, 120, 180, 300]
WINDOW_PCTS = [0.10, 0.25, 0.50, 0.75, 1.0]
WINDOW_LABELS = ["0-10%", "10-25%", "25-50%", "50-75%", "75-100%"]

EVENT_ORDER = ["HR", "2B", "1B", "BB", "DP", "OUT", "K"]

# ---------------------------------------------------------------------------
# Helpers (used by backtest reporting)
# ---------------------------------------------------------------------------


def get_clean_window(s: dict) -> float | None:
    """Return the clean window in seconds for a synced record, or None."""
    ttn = s.get("time_to_next_pitch")
    if ttn is not None and isinstance(ttn, (int, float)) and ttn > CLEAN_BUFFER:
        return ttn - CLEAN_BUFFER
    return None


def price_at_window_pct(
    moves: dict,
    clean_window: float,
    pct: float,
) -> float | None:
    """
    Look up the price move at clean_window × pct seconds.

    Uses the EXIT_WINDOWS moves dict — finds the largest fixed window
    that fits within the target time.
    """
    target_seconds = clean_window * pct
    best_move = None
    for w in EXIT_WINDOWS:
        if w <= target_seconds and w in moves:
            best_move = moves[w]
    return best_move
