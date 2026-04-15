#!/usr/bin/env python3
"""Fill-rate sensitivity to entry offset, broken down by event type.

Sweeps entry_offset from t-10s (10s ahead of MLB scorer log) to t+10s
(10s behind) at fixed α=0.65 / min_move=3¢. Re-runs sync at each offset
because fill_sim depends on when `entry_ts` lands. Raw game cache is
loaded once; only the sync step rerun per offset.
"""

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backtests.core.kalshi_sync import set_entry_offset
from backtests.sweeps._common import (
    load_raw_games,
    run_silent,
    sync_raw_games,
    widen_alphas,
)

ALPHA = 0.65
MIN_MOVE = 0.03
OFFSETS = list(range(10, -11, -1))  # 10..-10 → t-10s (ahead) → t+10s (behind)
EVENT_ORDER = ["HR", "2B", "1B", "BB", "DP", "OUT", "K"]


def fill_stats_by_event(trades):
    counts: dict[str, int] = defaultdict(int)
    fills: dict[str, int] = defaultdict(int)
    for t in trades:
        ev = t.get("event", "")
        counts[ev] += 1
        counts["TOTAL"] += 1
        if t.get("filled"):
            fills[ev] += 1
            fills["TOTAL"] += 1
    return counts, fills


def fmt_cell(count, filled):
    return "  — " if count == 0 else f"{100 * filled / count:>4.1f}"


def sign_label(offset: int) -> str:
    return f"t-{offset:>2}s" if offset >= 0 else f"t+{-offset:>2}s"


def main():
    widen_alphas()
    raw_games = load_raw_games()
    print(
        f"Loaded {len(raw_games)} cached games. α={ALPHA}, "
        f"min_move={int(MIN_MOVE*100)}¢\n"
    )

    hdr_cols = EVENT_ORDER + ["TOTAL"]

    # Sync once per offset, caching results so the count table doesn't rerun.
    results: list[dict] = []
    for offset in OFFSETS:
        set_entry_offset(offset)
        synced = sync_raw_games(raw_games)
        single, _ = run_silent(synced, ALPHA, min_move=MIN_MOVE)
        counts, fills = fill_stats_by_event(single)
        results.append({"offset": offset, "counts": counts, "fills": fills})

    header = f"{'Offset':>6}  " + "  ".join(f"{c:>5}" for c in hdr_cols) + "    n(total)"
    print("Fill rate % per event type at each entry offset:\n")
    print(header)
    print("-" * len(header))
    for r in results:
        counts, fills = r["counts"], r["fills"]
        row = f"  {sign_label(r['offset'])}  "
        row += "  ".join(fmt_cell(counts.get(c, 0), fills.get(c, 0)) for c in hdr_cols)
        row += f"     n={counts.get('TOTAL', 0):,}"
        print(row)

    print("\nTrade counts (same sweep, for scale context):")
    hdr2 = f"{'Offset':>6}  " + "  ".join(f"{c:>5}" for c in hdr_cols)
    print(hdr2)
    print("-" * len(hdr2))
    for r in results:
        counts = r["counts"]
        row = f"  {sign_label(r['offset'])}  "
        row += "  ".join(f"{counts.get(c, 0):>5}" for c in hdr_cols)
        print(row)

    # CSV alongside other sweep outputs in reports/.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    out_dir = Path(__file__).resolve().parents[1] / "reports"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"offset_fill_by_event_{stamp}.csv"
    fieldnames = ["offset_seconds", "event_type", "trades", "fills", "fill_rate_pct"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            counts, fills = r["counts"], r["fills"]
            for c in hdr_cols:
                n = counts.get(c, 0)
                k = fills.get(c, 0)
                writer.writerow({
                    "offset_seconds": r["offset"],
                    "event_type": c,
                    "trades": n,
                    "fills": k,
                    "fill_rate_pct": round(100 * k / n, 2) if n else "",
                })
    print(f"\nSaved CSV: {csv_path}")


if __name__ == "__main__":
    main()
