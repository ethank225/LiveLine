"""
P&L math — single source of truth for both the live trader and the backtest.

Pure functions: no DB, no logging, no state. Prices are expected in
**traded-side units** (callers must flip `1.0 - yes_price` for NO trades
before calling anything here). Rounding is to 4 decimals, matching the
backtest CSV and the live `realized_pnl` column.

Fee schedule lives in `app.fees`. Entry is always taker (IOC buys cross
the book); exit is maker when the resting limit sell fills at target,
taker when the trader IOCs out (expire / stop-loss / undo).
"""

from app.fees import maker_fee, taker_fee


def compute_gross(entry_price: float, exit_price: float, quantity: int) -> float:
    """(exit − entry) × qty, rounded to 4 dp."""
    return round((exit_price - entry_price) * quantity, 4)


def compute_fees(
    quantity: int,
    entry_price: float,
    exit_price: float,
    exit_type: str,
) -> tuple[float, float]:
    """Return (entry_fee, exit_fee). `exit_type` ∈ {"maker", "taker"}."""
    entry_fee = taker_fee(quantity, entry_price)
    if exit_type == "maker":
        exit_fee = maker_fee(quantity, exit_price)
    elif exit_type == "taker":
        exit_fee = taker_fee(quantity, exit_price)
    else:
        raise ValueError(
            f"exit_type must be 'maker' or 'taker', got {exit_type!r}"
        )
    return entry_fee, exit_fee


def compute_net(
    entry_price: float,
    exit_price: float,
    quantity: int,
    exit_type: str,
) -> dict:
    """Full P&L breakdown for one trade. All values in dollars, 4-dp rounded."""
    gross = compute_gross(entry_price, exit_price, quantity)
    entry_fee, exit_fee = compute_fees(quantity, entry_price, exit_price, exit_type)
    fees = round(entry_fee + exit_fee, 4)
    return {
        "gross": gross,
        "entry_fee": round(entry_fee, 4),
        "exit_fee": round(exit_fee, 4),
        "fees": fees,
        "net": round(gross - fees, 4),
    }
