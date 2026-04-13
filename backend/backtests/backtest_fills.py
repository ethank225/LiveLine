#!/usr/bin/env python3
"""
Limit-order fill simulation analysis.

Reads *_synced.csv files that contain fill simulation columns
(fill_{alpha}_hit, fill_{alpha}_pnl, fill_{alpha}_time) written
by backtest.py, and reports fill rates, P&L, and optimal alpha.

Usage:
    python backtests/backtest_fills.py
    python backtests/backtest_fills.py --filter "823482"
    python backtests/backtest_fills.py --contracts 200
    python backtests/backtest_fills.py --alphas 0.3,0.4,0.5,0.6,0.7,0.8
"""

import argparse
import csv
import glob
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtests.constants import (
    ALPHAS as DEFAULT_ALPHAS, EVENT_ORDER, EXIT_WINDOWS,
    WINDOW_PCTS, WINDOW_LABELS, CONTRACTS,
    get_clean_window,
)
from backtests.report_io import capture_report

RESULTS_DIR = Path(__file__).parent / "results"


def load_synced_files(results_dir: Path, filter_str: str | None = None,
                      alphas: list[float] | None = None):
    pattern = str(results_dir / "*_synced.csv")
    files = sorted(glob.glob(pattern))
    if filter_str:
        files = [f for f in files if filter_str in Path(f).name]

    if alphas is None:
        alphas = DEFAULT_ALPHAS

    rows = []
    for filepath in files:
        with open(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                row["predicted_ml_delta"] = float(
                    row.get("predicted_delta") or row.get("predicted_ml_delta") or 0
                )
                row["price_before"] = float(row.get("price_before") or 0)
                row["event"] = row.get("event", "")
                row["market_type"] = row.get("market_type", "moneyline")
                row["market_label"] = row.get("market_label", "")
                row["is_high_lev_ml"] = row.get("is_high_lev_ml", "").lower() == "true"
                row["is_high_lev_ou"] = row.get("is_high_lev_ou", "").lower() == "true"

                # Parse fill sim columns
                fill_sim = {}
                for a in alphas:
                    hit_val = row.get(f"fill_{a}_hit", "")
                    pnl_val = row.get(f"fill_{a}_pnl", "")
                    time_val = row.get(f"fill_{a}_time", "")

                    if hit_val == "" and pnl_val == "":
                        continue

                    fill_sim[a] = {
                        "filled": hit_val == "True",
                        "pnl": float(pnl_val) if pnl_val != "" else 0.0,
                        "fill_time": (int(float(time_val))
                                      if time_val not in ("", "None", "None") else None),
                    }

                row["fill_sim"] = fill_sim

                # Parse gap fields
                ttn_raw = row.get("time_to_next_pitch", "")
                row["time_to_next_pitch"] = (
                    float(ttn_raw) if ttn_raw not in ("", "None") else None
                )

                row["_file"] = Path(filepath).stem
                rows.append(row)

    return rows, files, alphas


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def print_alpha_summary(plays: list[dict], alphas: list[float], contracts: int):
    """Per-alpha fill rate, P&L, EV."""
    print(f"\n{'Alpha':>6} {'Trades':>6} {'Fill%':>6} {'Avg Fill':>9} "
          f"{'Avg Miss':>9} {'EV/Trade':>9} {'Total P&L':>10} {'Avg Fill t':>11}")
    print("-" * 80)

    best_alpha = None
    best_total = float("-inf")

    for alpha in alphas:
        with_data = [s for s in plays if alpha in s["fill_sim"]]
        if not with_data:
            continue

        fills = [s for s in with_data if s["fill_sim"][alpha]["filled"]]
        misses = [s for s in with_data if not s["fill_sim"][alpha]["filled"]]

        fill_pnls = [s["fill_sim"][alpha]["pnl"] for s in fills]
        miss_pnls = [s["fill_sim"][alpha]["pnl"] for s in misses]
        all_pnls = [s["fill_sim"][alpha]["pnl"] for s in with_data]

        n = len(with_data)
        fill_rate = 100 * len(fills) / n
        avg_fill = sum(fill_pnls) / len(fill_pnls) if fill_pnls else 0
        avg_miss = sum(miss_pnls) / len(miss_pnls) if miss_pnls else 0
        ev = sum(all_pnls) / n
        total = sum(p * contracts for p in all_pnls)

        fill_times = [s["fill_sim"][alpha]["fill_time"] for s in fills
                      if s["fill_sim"][alpha]["fill_time"] is not None]
        avg_ft = f"{sum(fill_times) / len(fill_times):.0f}s" if fill_times else "n/a"

        print(f"{alpha:>6.1f} {n:>6} {fill_rate:>5.1f}% {avg_fill:>+9.4f} "
              f"{avg_miss:>+9.4f} {ev:>+9.4f} ${total:>+9.0f} {avg_ft:>11s}")

        if total > best_total:
            best_total = total
            best_alpha = alpha

    return best_alpha, best_total


def print_event_breakdown(plays: list[dict], alpha: float, contracts: int):
    """Per-event breakdown at a given alpha."""
    by_event: dict[str, list] = {}
    for s in plays:
        if alpha in s["fill_sim"]:
            by_event.setdefault(s["event"], []).append(s)

    print(f"\n  Per-event at α={alpha}:")
    print(f"  {'Event':>5} {'Trades':>6} {'Fill%':>6} {'Avg P&L':>8} "
          f"{'Total':>8} {'Win%':>6} {'Avg Fill t':>10}")
    print(f"  {'-' * 58}")

    for event in EVENT_ORDER:
        evs = by_event.get(event)
        if not evs:
            continue
        fills = [s for s in evs if s["fill_sim"][alpha]["filled"]]
        all_pnls = [s["fill_sim"][alpha]["pnl"] for s in evs]
        n = len(evs)
        fr = 100 * len(fills) / n
        avg_pnl = sum(all_pnls) / n
        total = sum(p * contracts for p in all_pnls)
        winners = sum(1 for p in all_pnls if p > 0)
        wp = 100 * winners / n
        ft = [s["fill_sim"][alpha]["fill_time"] for s in fills
              if s["fill_sim"][alpha]["fill_time"] is not None]
        avg_ft = f"{sum(ft) / len(ft):.0f}s" if ft else "n/a"
        print(f"  {event:>5} {n:>6} {fr:>5.0f}% {avg_pnl:>+8.4f} "
              f"${total:>+7.0f} {wp:>5.0f}% {avg_ft:>10s}")


def print_market_breakdown(plays: list[dict], alpha: float, contracts: int):
    """Per market type breakdown at a given alpha."""
    by_mt: dict[str, list] = {}
    for s in plays:
        if alpha in s["fill_sim"]:
            by_mt.setdefault(s["market_type"], []).append(s)

    if len(by_mt) <= 1:
        return

    print(f"\n  Per market type at α={alpha}:")
    print(f"  {'Market':>12} {'Trades':>6} {'Fill%':>6} {'EV/Trade':>9} "
          f"{'Total P&L':>10} {'Win%':>6}")
    print(f"  {'-' * 55}")

    for mt in ["moneyline", "over_under", "spread"]:
        items = by_mt.get(mt)
        if not items:
            continue
        all_pnls = [s["fill_sim"][alpha]["pnl"] for s in items]
        fills = [s for s in items if s["fill_sim"][alpha]["filled"]]
        n = len(items)
        fr = 100 * len(fills) / n
        ev = sum(all_pnls) / n
        total = sum(p * contracts for p in all_pnls)
        wp = 100 * sum(1 for p in all_pnls if p > 0) / n
        print(f"  {mt:>12} {n:>6} {fr:>5.0f}% {ev:>+9.4f} "
              f"${total:>+9.0f} {wp:>5.0f}%")


def print_leverage_comparison(plays: list[dict], alpha: float, contracts: int):
    """Compare high-leverage vs all plays."""
    with_data = [s for s in plays if alpha in s["fill_sim"]]
    hl_ml = [s for s in with_data if s["is_high_lev_ml"]]
    hl_ou = [s for s in with_data if s["is_high_lev_ou"]]

    print(f"\n  Leverage filter at α={alpha}:")
    print(f"  {'Filter':>20} {'Trades':>6} {'Fill%':>6} {'EV/Trade':>9} "
          f"{'Total P&L':>10} {'Win%':>6}")
    print(f"  {'-' * 62}")

    for label, subset in [("All plays", with_data),
                           ("High lev ML", hl_ml),
                           ("High lev O/U", hl_ou)]:
        if not subset:
            continue
        all_pnls = [s["fill_sim"][alpha]["pnl"] for s in subset]
        fills = [s for s in subset if s["fill_sim"][alpha]["filled"]]
        n = len(subset)
        fr = 100 * len(fills) / n
        ev = sum(all_pnls) / n
        total = sum(p * contracts for p in all_pnls)
        wp = 100 * sum(1 for p in all_pnls if p > 0) / n
        print(f"  {label:>20} {n:>6} {fr:>5.0f}% {ev:>+9.4f} "
              f"${total:>+9.0f} {wp:>5.0f}%")


def print_fill_time_distribution(plays: list[dict], alpha: float):
    """Distribution of fill times as absolute seconds AND as % of clean window."""
    abs_times = []
    pct_times = []  # fill_time / clean_window * 100

    for s in plays:
        if alpha not in s["fill_sim"]:
            continue
        fd = s["fill_sim"][alpha]
        if not fd["filled"] or fd["fill_time"] is None:
            continue

        ft = fd["fill_time"]
        abs_times.append(ft)

        # Clean window = time_to_next_pitch - 5
        ttn = s.get("time_to_next_pitch")
        if ttn is not None and isinstance(ttn, (int, float)) and ttn > 5:
            clean_window = ttn - 5
            pct_times.append(min(ft / clean_window * 100, 100.0))

    if not abs_times:
        return

    abs_times.sort()
    n = len(abs_times)

    print(f"\n  Fill time distribution at α={alpha} ({n} fills):")
    print(f"    Absolute:  Median={abs_times[n // 2]}s  P25={abs_times[n // 4]}s  "
          f"P75={abs_times[3 * n // 4]}s  Max={abs_times[-1]}s")

    if pct_times:
        pct_times.sort()
        np = len(pct_times)
        print(f"    % of window: Median={pct_times[np // 2]:.0f}%  "
              f"P25={pct_times[np // 4]:.0f}%  P75={pct_times[3 * np // 4]:.0f}%")

        buckets = [(0, 10), (10, 25), (25, 50), (50, 75), (75, 100.01)]
        print(f"\n    {'Window %':>10} {'Fills':>5} {'%':>6} {'Cumul%':>7}  Interpretation")
        print(f"    {'-' * 65}")
        cumul = 0
        for lo, hi in buckets:
            count = sum(1 for p in pct_times if lo <= p < hi)
            cumul += count
            pct = 100 * count / np
            cpct = 100 * cumul / np
            label = f"{lo:.0f}-{hi:.0f}%"
            if hi <= 10.01:
                note = "instant fill — price moved fast"
            elif hi <= 25.01:
                note = "early fill — strong signal"
            elif hi <= 50.01:
                note = "mid-window — comfortable"
            elif hi <= 75.01:
                note = "late fill — cutting it close"
            else:
                note = "barely made it — risky"
            print(f"    {label:>10} {count:>5} {pct:>5.1f}% {cpct:>6.1f}%  {note}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with capture_report("backtest_fills") as report_path:
        _main_inner()
        print(f"\nReport saved: {report_path}")


def _main_inner():
    parser = argparse.ArgumentParser(
        description="Analyze limit-order fill simulation from backtest CSVs")
    parser.add_argument("--results-dir", type=str, default=str(RESULTS_DIR),
                        help="Directory containing *_synced.csv files")
    parser.add_argument("--filter", type=str, default=None,
                        help="Only include files matching this string")
    parser.add_argument("--contracts", type=int, default=100,
                        help="Contracts per trade (default: 100)")
    parser.add_argument("--alphas", type=str, default=None,
                        help="Comma-separated alpha values (default: 0.3,0.4,0.5,0.6)")
    args = parser.parse_args()

    alphas = ([float(a) for a in args.alphas.split(",")]
              if args.alphas else DEFAULT_ALPHAS)

    results_dir = Path(args.results_dir)
    rows, files, alphas = load_synced_files(results_dir, args.filter, alphas)

    # Only include plays with fill sim data and nonzero predicted delta
    plays = [r for r in rows
             if r["fill_sim"] and r["predicted_ml_delta"] != 0]

    if not plays:
        print(f"No fill simulation data found in {results_dir}")
        print(f"Run backtest.py first to generate synced CSVs with fill data.")
        return

    print(f"Loaded {len(plays)} tradeable plays from {len(files)} files")
    print(f"  (filtered from {len(rows)} total rows — "
          f"excluded {len(rows) - len(plays)} with zero delta or no fill data)")
    if args.filter:
        print(f"Filter: '{args.filter}'")
    print(f"Alphas: {alphas}")
    print(f"Contracts per trade: {args.contracts}")

    print(f"\n{'=' * 80}")
    print(f"LIMIT ORDER FILL SIMULATION")
    print(f"Entry at t-5s, target = entry + α × predicted_delta")
    print(f"Exit at target OR last clean trade before next pitch")
    print(f"{'=' * 80}")

    best_alpha, best_total = print_alpha_summary(plays, alphas, args.contracts)

    if best_alpha is not None:
        print(f"\n  → Best alpha: {best_alpha} (${best_total:+.0f} total P&L)")
        print_event_breakdown(plays, best_alpha, args.contracts)
        print_market_breakdown(plays, best_alpha, args.contracts)
        print_leverage_comparison(plays, best_alpha, args.contracts)
        print_fill_time_distribution(plays, best_alpha)

    print("\nDone.")


if __name__ == "__main__":
    main()
