"""Human-readable bet label derivation.

Single source of truth so button grids, active position cards, and history
all render identical text for the same trade. Mirrors what the frontend
`betLabel` helper used to compute locally, now centralized server-side so
the frontend just reads `display_label`.
"""


def _spread_info(ticker: str) -> tuple[str, float] | None:
    """Parse `KXMLBSPREAD-...-{ABBR}{N}` → ({ABBR}, N-0.5). None if malformed."""
    if not ticker:
        return None
    last = ticker.rsplit("-", 1)[-1].upper()
    i = 0
    while i < len(last) and last[i].isalpha():
        i += 1
    abbr = last[:i]
    n_str = last[i:]
    if not abbr or not n_str:
        return None
    try:
        n = int(n_str)
    except ValueError:
        return None
    return abbr, n - 0.5


def _ou_line(ticker: str) -> str | None:
    """Parse `KXMLBTOTAL-...-{N}` → "{N}" (integer string)."""
    if not ticker:
        return None
    last = ticker.rsplit("-", 1)[-1]
    return last if last.isdigit() else None


def _ml_abbr(ticker: str) -> str | None:
    """Parse `KXMLBGAME-...-{HOME_ABBR}` → home abbr."""
    if not ticker:
        return None
    last = ticker.rsplit("-", 1)[-1].upper()
    return last if last.isalpha() and 2 <= len(last) <= 4 else None


def _infer_market_type(ticker: str) -> str | None:
    if not ticker:
        return None
    if ticker.startswith("KXMLBGAME"):
        return "moneyline"
    if ticker.startswith("KXMLBTOTAL"):
        return "over_under"
    if ticker.startswith("KXMLBSPREAD"):
        return "spread"
    return None


def compute_display_label(
    *,
    market_ticker: str,
    side: str,
    market_type: str | None = None,
    home_abbr: str = "",
    away_abbr: str = "",
) -> str:
    """Build the bet-meaning label. Flips team+sign on NO side so the
    user reads the side of the bet they actually placed.

    Spread: YES on `SEA -4.5` → "SEA -4.5"; NO → "HOU +4.5".
    Moneyline: YES on home ticker → "{HOME} wins"; NO → "{AWAY} wins".
    O/U: YES → "Over {N}"; NO → "Under {N}".
    """
    if not side:
        return ""
    mt = market_type or _infer_market_type(market_ticker) or ""
    home = (home_abbr or "").upper()
    away = (away_abbr or "").upper()

    if mt == "over_under":
        line = _ou_line(market_ticker)
        if line:
            return f"Over {line}" if side == "YES" else f"Under {line}"
        return "Over" if side == "YES" else "Under"

    if mt == "spread":
        info = _spread_info(market_ticker)
        if info:
            abbr, line = info
            line_str = f"{line:g}"
            if side == "YES":
                return f"{abbr} -{line_str}"
            other = home if abbr == away else (away if abbr == home else abbr)
            return f"{other} +{line_str}"

    if mt == "moneyline":
        abbr = _ml_abbr(market_ticker)
        if abbr:
            if side == "YES":
                return f"{abbr} wins"
            other = home if abbr == away else (away if abbr == home else None)
            return f"{other} wins" if other else f"{abbr} loses"
        return "Home wins" if side == "YES" else "Home loses"

    return side
