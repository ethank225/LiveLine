"""Throwaway: subscribe orderbook_delta on ONE live MLB ticker and see
whether the feed still crashes / throws on empty snapshots or deltas.

If this runs cleanly for 30s with non-zero deltas and no exceptions, we
can drop the REST-based depth probes (estimate_fill_qty's 5s TTL cache,
calculate_position_size fallback) and stream depth instead.

Run:  python3 scripts/probe_orderbook_delta.py [--seconds 30] [--ticker TICKER]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from collections import Counter

# Ensure `app` package imports resolve when run from backend/
sys.path.insert(0, ".")

from app.kalshi_client import kalshi  # noqa: E402


def _pick_live_mlb_ticker() -> str | None:
    """First open market under any KXMLBGAME event. Returns None if none."""
    try:
        events = list(kalshi.client.get_events(
            series_ticker="KXMLBGAME", fetch_all=True,
        ))
    except Exception as e:
        print(f"get_events failed: {e}")
        return None
    for ev in events:
        ev_ticker = getattr(ev, "event_ticker", "")
        if not ev_ticker:
            continue
        markets = kalshi.get_markets(ev_ticker)
        for m in markets:
            if m.get("status") == "open":
                return m["ticker"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--ticker", default=None,
                    help="Override auto-pick with a specific market ticker")
    args = ap.parse_args()

    # Surface feed-internal errors — the prior crash was internal to pykalshi,
    # so raw logs matter more than our own print counts.
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    print("Connecting Kalshi...")
    kalshi.connect()

    ticker = args.ticker or _pick_live_mlb_ticker()
    if not ticker:
        print("No open MLB market found. Pass --ticker TICKER to force one.")
        return 2
    print(f"Probing ticker: {ticker}")

    counts: Counter[str] = Counter()
    errors: list[str] = []

    def _listen(msg):
        name = type(msg).__name__
        counts[name] += 1
        # Log a couple of each kind so we can see the shape
        if counts[name] <= 2:
            try:
                print(f"  [{name}] {msg!r}")
            except Exception as e:
                errors.append(f"repr {name}: {e}")

    # Register an ADDITIONAL orderbook_delta handler on the feed. Can't
    # just reassign kalshi._on_orderbook — the feed already captured a
    # bound reference to it at connect() time.
    def _raw_handler(msg):
        try:
            _listen(msg)
        except Exception:
            errors.append("listener: " + traceback.format_exc())

    kalshi.feed.on("orderbook_delta", _raw_handler)

    try:
        kalshi.feed.subscribe("orderbook_delta", market_ticker=ticker)
        print(f"Subscribed orderbook_delta for {ticker}")
    except Exception:
        print("subscribe() raised:")
        traceback.print_exc()
        return 1

    print(f"Listening {args.seconds}s...")
    t0 = time.time()
    try:
        while time.time() - t0 < args.seconds:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    print("\n=== RESULT ===")
    print(f"elapsed:  {time.time() - t0:.1f}s")
    print(f"message counts: {dict(counts)}")
    print(f"errors:   {len(errors)}")
    for e in errors[:5]:
        print("---")
        print(e)
    verdict = (
        "PASS — feed stayed up"
        if counts and not errors
        else "FAIL — errors or no messages (check logs above)"
    )
    print(verdict)
    return 0 if counts and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
