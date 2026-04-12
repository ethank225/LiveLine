"""Kalshi client creation, event ticker discovery, and market enumeration."""

import os
from datetime import date
from pathlib import Path

from backtests.mlb import MarketSpec


def get_kalshi_client():
    """Create a Kalshi client from env vars, or return None."""
    try:
        from pykalshi import KalshiClient as KC
    except ImportError:
        return None

    env = os.getenv("KALSHI_ENV", "demo").lower()
    demo = env != "prod"
    prefix = "KALSHI_DEMO" if demo else "KALSHI_PROD"

    api_key = os.getenv(f"{prefix}_API_KEY_ID")
    key_path = os.getenv(f"{prefix}_PRIVATE_KEY_PATH")
    if not api_key or not key_path:
        return None

    key_file = Path(key_path)
    if not key_file.is_absolute():
        key_file = Path(__file__).parent.parent / key_path

    return KC(api_key_id=api_key, private_key_path=str(key_file), demo=demo)


_kalshi_event_cache: list | None = None


def _get_kalshi_events(client) -> list[str]:
    """Fetch all KXMLBGAME event tickers once and cache."""
    global _kalshi_event_cache
    if _kalshi_event_cache is not None:
        return _kalshi_event_cache

    try:
        events = client.get_events(series_ticker="KXMLBGAME", fetch_all=True)
        _kalshi_event_cache = [getattr(ev, "event_ticker", "") for ev in events]
        print(f"Cached {len(_kalshi_event_cache)} KXMLBGAME events from Kalshi")
    except Exception as e:
        print(f"Failed to fetch Kalshi events: {e}")
        _kalshi_event_cache = []

    return _kalshi_event_cache


def find_kalshi_event_ticker(client, target_date: date, home_abbr: str, away_abbr: str) -> str | None:
    """Search cached KXMLBGAME events for a game matching teams and date."""
    date_prefix = target_date.strftime("%y%b%d").upper()
    tickers = _get_kalshi_events(client)

    for ticker in tickers:
        if date_prefix not in ticker:
            continue
        t = ticker.upper()
        if away_abbr.upper() in t and home_abbr.upper() in t:
            return ticker
    return None


def discover_game_markets(client, game_event_ticker: str, home_abbr: str, away_abbr: str) -> list[MarketSpec]:
    """Given a KXMLBGAME event ticker, discover moneyline + O/U + spread markets."""
    suffix = game_event_ticker.split("-", 1)[1]
    home = home_abbr.upper()
    away = away_abbr.upper()

    specs: list[MarketSpec] = []

    # Moneyline
    specs.append(MarketSpec(
        ticker=f"KXMLBGAME-{suffix}-{home}",
        market_type="moneyline", line=None,
        delta_key="ml", flip=False, label=f"ML {home}",
    ))

    # Over/Under — all available lines
    ou_event = f"KXMLBTOTAL-{suffix}"
    try:
        for m in client.get_markets(event_ticker=ou_event):
            n_str = m.ticker.rsplit("-", 1)[-1]
            try:
                n = int(n_str)
            except ValueError:
                continue
            line = n - 0.5
            specs.append(MarketSpec(
                ticker=m.ticker, market_type="over_under", line=line,
                delta_key=f"ou_{line}", flip=False, label=f"O/U {line}",
            ))
    except Exception:
        pass

    # Spread — all available lines
    sp_event = f"KXMLBSPREAD-{suffix}"
    try:
        for m in client.get_markets(event_ticker=sp_event):
            mkt_suffix = m.ticker.rsplit("-", 1)[-1].upper()
            for abbr, is_away in [(home, False), (away, True)]:
                if mkt_suffix.startswith(abbr):
                    n_str = mkt_suffix[len(abbr):]
                    try:
                        n = int(n_str)
                    except ValueError:
                        continue
                    line = n - 0.5
                    if is_away:
                        delta_key = f"sp_{-line}"
                        label_line = f"{abbr} -{line}"
                    else:
                        delta_key = f"sp_{line}"
                        label_line = f"{abbr} -{line}"
                    specs.append(MarketSpec(
                        ticker=m.ticker, market_type="spread",
                        line=line if not is_away else -line,
                        delta_key=delta_key, flip=is_away,
                        label=f"SPR {label_line}",
                    ))
                    break
    except Exception:
        pass

    return specs
