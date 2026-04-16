import sys
sys.path.insert(0, ".")
from app.kalshi_client import kalshi  # noqa

kalshi.connect()
ev = sys.argv[1]
mkts = kalshi.get_markets(ev)
print(f"{ev}: {len(mkts)} markets")
for m in mkts:
    print(
        f"  {m['ticker']:<50}  status={m.get('status'):<6}  "
        f"vol={m.get('volume')}  "
        f"yes_bid={m.get('yes_bid')}  yes_ask={m.get('yes_ask')}"
    )
