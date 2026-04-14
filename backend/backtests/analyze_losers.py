#!/usr/bin/env python3
"""
One-shot diagnostic: what happened on the unfilled trades in losing games?

Compares the 3 losing games (LAA@CIN, PHI@SF, ATL@LAA) against the best
winning game (COL@SD +$1,486) trade-by-trade. For each trade, looks up the
peak Kalshi price during the exit window from cached trade history and
classifies the unfilled trades into:

  - "never reached"   — peak price < target (model overpredicted move)
  - "touched but missed" — peak >= target but limit didn't fill (queue position)
  - "extreme entry"   — entry price already at >= 0.95 or <= 0.05 (price pinned)

Bid-ask spread isn't recoverable from historical trade data — Kalshi's
public API only logs trades. We use the (max - min) range across the
30s exit window as a "trading range" proxy.

Usage:
    python -m backtests.analyze_losers
    python -m backtests.analyze_losers --csv path/to/single_trades.csv
"""

import argparse
import csv
import glob
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# CLEAN_WINDOW_SECONDS lives on app.constants; the backtest's per-play
# `clean_cutoff` is also bounded by that. Using a flat 30s window here is
# a slight overstatement of the exit horizon for ABs that ended quickly,
# but matches the live trader's behavior, which is what we're auditing.
DEFAULT_EXIT_WINDOW = 30
ENTRY_OFFSET_DEFAULT = 5  # seconds before play_ts (matches current default)

CACHE_ROOT = Path(__file__).parent / "results" / "cache"
REPORTS_DIR = Path(__file__).parent / "reports"

# Worst 3 + best 1 from the last 3-day backtest (per_game_pnl_single CSV).
TARGETS = [
    ("824534 LAA@CIN", "2026-04-10", "loser"),
    ("823234 PHI@SF",  "2026-04-08", "loser"),
    ("824050 ATL@LAA", "2026-04-08", "loser"),
    ("823319 COL@SD",  "2026-04-09", "winner"),
]


def latest_single_trades_csv() -> Path:
    paths = sorted(glob.glob(str(REPORTS_DIR / "multi_market_single_trades_*.csv")))
    if not paths:
        raise SystemExit("No single_trades CSV found in reports/")
    return Path(paths[-1])


def load_trades_for_ticker(date: str, game_id: str, ticker: str) -> list[tuple[int, float]]:
    path = CACHE_ROOT / date / game_id / "trades" / f"{ticker}.json"
    if not path.exists():
        return []
    with open(path) as f:
        return [(int(t), float(p)) for t, p in json.load(f)]


def parse_iso(ts: str) -> int:
    """Convert ISO-8601 (with or without trailing Z) to unix seconds."""
    if not ts:
        return 0
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def window_stats(trades, t_start, t_end):
    """Return (peak_price, trough_price, range, n_trades) in [t_start, t_end].
    Direction-agnostic; caller decides whether peak or trough is the target."""
    in_window = [p for ts, p in trades if t_start <= ts <= t_end]
    if not in_window:
        return None, None, None, 0
    return max(in_window), min(in_window), max(in_window) - min(in_window), len(in_window)


def classify(row, peak, trough):
    """Bucket an unfilled trade by why it missed."""
    entry = float(row["entry_price"])
    target = float(row["sell_target"])
    pred = float(row["predicted_delta"])

    if entry >= 0.95 or entry <= 0.05:
        return "extreme_entry"
    if peak is None:
        return "no_market_data"
    # For positive-delta trades the target is ABOVE entry (we want price up).
    # For negative-delta (NO side) the target is BELOW entry (price down).
    if pred > 0:
        reached = peak >= target
        gap = peak - target
    else:
        reached = trough <= target
        gap = target - trough  # negative = trough went lower than target
    if reached:
        return "touched_but_missed"
    return "never_reached"


def gap_to_target(row, peak, trough):
    """Signed cents-to-target. Negative = price didn't reach target."""
    if peak is None:
        return None
    target = float(row["sell_target"])
    pred = float(row["predicted_delta"])
    if pred > 0:
        return round((peak - target) * 100, 2)
    return round((target - trough) * 100, 2)


def analyze(csv_path: Path, exit_window: int):
    by_tag = defaultdict(list)
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            tag = row["game_tag"]
            for target_tag, _, _ in TARGETS:
                if tag == target_tag:
                    by_tag[tag].append(row)
                    break

    print(f"Loaded {sum(len(v) for v in by_tag.values())} trades across "
          f"{len(by_tag)} target games from {csv_path.name}")
    print(f"Exit window: {exit_window}s | entry offset: t-{ENTRY_OFFSET_DEFAULT}s\n")

    for tag, date, kind in TARGETS:
        rows = by_tag.get(tag, [])
        if not rows:
            print(f"=== {tag} ({kind}) — no trades found ===\n")
            continue
        game_id = tag.split()[0]
        print(f"{'=' * 100}")
        print(f"{tag}  ({kind})  — {len(rows)} trades, "
              f"{sum(1 for r in rows if r['filled'].lower()=='true')} filled")
        print(f"{'=' * 100}")
        print(
            f"  {'event':<5} {'mkt':<4} {'entry':>5} {'target':>6} {'move':>5} "
            f"{'peak':>5} {'trough':>6} {'gap¢':>5} {'range¢':>6} "
            f"{'fill':<5} {'class':<20}"
        )
        print(f"  {'-'*88}")

        cls_counts = defaultdict(int)
        ranges_unfilled = []
        gaps_unfilled = []
        extreme_entry_count = 0
        for r in rows:
            ticker = r["market_ticker"]
            entry_iso = r["timestamp"]  # this is the play timestamp
            play_ts = parse_iso(entry_iso)
            entry_ts = play_ts - ENTRY_OFFSET_DEFAULT
            window_end = entry_ts + exit_window
            trades = load_trades_for_ticker(date, game_id, ticker)
            peak, trough, rng, n = window_stats(trades, entry_ts, window_end)

            entry = float(r["entry_price"])
            target = float(r["sell_target"])
            move_c = round((target - entry) * 100, 1)
            gap = gap_to_target(r, peak, trough)
            mt_short = {"moneyline": "ML", "over_under": "O/U", "spread": "SPR"}.get(r["market_type"], "?")
            filled = r["filled"].lower() == "true"
            cls = "FILLED" if filled else classify(r, peak, trough)
            cls_counts[cls] += 1
            if not filled:
                if rng is not None:
                    ranges_unfilled.append(rng * 100)
                if gap is not None:
                    gaps_unfilled.append(gap)
            if entry >= 0.90 or entry <= 0.10:
                extreme_entry_count += 1

            peak_s = f"{peak:.2f}" if peak is not None else "—"
            tro_s = f"{trough:.2f}" if trough is not None else "—"
            gap_s = f"{gap:+.1f}" if gap is not None else "—"
            rng_s = f"{rng*100:.1f}" if rng is not None else "—"
            print(
                f"  {r['event']:<5} {mt_short:<4} "
                f"{entry:>5.2f} {target:>6.2f} {move_c:>+5.1f} "
                f"{peak_s:>5} {tro_s:>6} {gap_s:>5} {rng_s:>6} "
                f"{'✓' if filled else 'X':<5} {cls:<20}"
            )

        n = len(rows)
        unfilled = sum(1 for r in rows if r["filled"].lower() != "true")
        print(f"\n  Summary:")
        for cls in ["FILLED", "touched_but_missed", "never_reached", "extreme_entry", "no_market_data"]:
            c = cls_counts.get(cls, 0)
            if c:
                print(f"    {cls:<22} {c:>3}  ({100*c/n:>5.1f}%)")
        if ranges_unfilled:
            avg_range = sum(ranges_unfilled) / len(ranges_unfilled)
            print(f"    avg trading range (unfilled trades, 30s window): {avg_range:.1f}¢")
        if gaps_unfilled:
            avg_gap = sum(gaps_unfilled) / len(gaps_unfilled)
            print(f"    avg distance to target (unfilled): {avg_gap:+.1f}¢")
        print(f"    trades with entry near extreme (>=0.90 or <=0.10): "
              f"{extreme_entry_count}/{n} ({100*extreme_entry_count/n:.0f}%)")
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=None)
    ap.add_argument("--window", type=int, default=DEFAULT_EXIT_WINDOW)
    args = ap.parse_args()
    csv_path = args.csv or latest_single_trades_csv()
    analyze(csv_path, args.window)


if __name__ == "__main__":
    main()
