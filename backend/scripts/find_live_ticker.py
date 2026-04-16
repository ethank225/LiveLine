"""Print currently-OPEN KXMLBGAME tickers for today, sorted by volume.

Uses `get_events(series_ticker=KXMLBGAME, fetch_all=True)` filtered to
today's date prefix (YYMONDD), then pulls markets per event. Much faster
than paginating every open market Kalshi serves."""
import sys
sys.path.insert(0, ".")

from app.game_state import mlb_today  # noqa: E402
from app.kalshi_client import kalshi  # noqa: E402

kalshi.connect()

today = mlb_today()
date_prefix = today.strftime("%y%b%d").upper()
print(f"Searching KXMLBGAME events with date prefix: {date_prefix}")

events = list(kalshi.client.get_events(
    series_ticker="KXMLBGAME", fetch_all=True,
))
print(f"Total KXMLBGAME events returned: {len(events)}")

todays_events = [
    ev for ev in events
    if date_prefix in getattr(ev, "event_ticker", "")
]
print(f"Events matching today ({date_prefix}): {len(todays_events)}")
for ev in todays_events[:5]:
    print(f"  event_ticker={getattr(ev, 'event_ticker', '?')}")

# Pull markets from each today's event
all_mlb = []
for ev in todays_events:
    et = getattr(ev, "event_ticker", "")
    mkts = kalshi.get_markets(et)
    for m in mkts:
        if m.get("status") == "open":
            all_mlb.append(m)

print(f"\nTotal open markets across today's events: {len(all_mlb)}")

def _vol(m):
    try:
        return int(m.get("volume") or 0)
    except Exception:
        return 0

all_mlb.sort(key=_vol, reverse=True)
print("\nTop 10 by volume:")
for m in all_mlb[:10]:
    print(
        f"  {m['ticker']:<55}  vol={_vol(m):>8}  "
        f"yes_bid={m.get('yes_bid')}  yes_ask={m.get('yes_ask')}"
    )
