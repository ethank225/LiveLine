"""
Dynamic O/U and spread line selection.

Shared by live trading (app/market_selector.py) and backtesting
(backtests/kalshi_sync.py). Single source of truth.
"""

import logging

logger = logging.getLogger(__name__)


def pick_ou_line(total_runs: int, candidates: list) -> object | None:
    """
    Pick the O/U market whose line is closest above current total + 0.5.

    If total_runs = 5, targets 5.5 -> picks O/U 6.5 (smallest line >= 5.5).
    Falls back to the highest available line if none is above the target.

    Accepts any list of objects with a .line attribute (MarketInfo, MarketSpec).
    """
    if not candidates:
        return None
    target = total_runs + 0.5
    above = [c for c in candidates if c.line >= target]
    if above:
        return min(above, key=lambda c: c.line)
    return max(candidates, key=lambda c: c.line)


def pick_spread_line(margin: int, candidates: list) -> object | None:
    """
    Pick the spread market whose line is closest above the current margin.

    If home leads 4-1 (margin=3), picks the smallest line > 3 (e.g. 3.5).
    Falls back to closest line overall if none is strictly above.

    Accepts any list of objects with a .line attribute (MarketInfo, MarketSpec).
    """
    if not candidates:
        return None
    best = None
    best_dist = float("inf")
    for c in candidates:
        dist = c.line - margin
        if dist <= 0:
            continue
        if dist < best_dist:
            best_dist = dist
            best = c
    if best is None:
        # Fallback: no candidate line is above the current margin. This
        # is usually a symptom of Kalshi adding higher-line spread
        # tickers mid-game that the initial `discover_markets` call
        # never saw (the live fix is `refresh_markets` on the MLB poll).
        # Warn so the stale-ticker-set case is visible — without this
        # the picker silently pins on the tightest available line.
        best = min(candidates, key=lambda c: abs(c.line - margin))
        logger.warning(
            f"pick_spread_line fallback: margin={margin} "
            f"candidates={[c.line for c in candidates]} "
            f"falling back to closest abs line={best.line}"
        )
    return best
