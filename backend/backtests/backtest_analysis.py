#!/usr/bin/env python3
"""
Analyze previously generated backtest results.

Reads all *_synced.csv files from backtests/results/ and prints the
timing analysis, accuracy summary, and P&L simulation using clean
price moves (capped before the next pitch contaminates).

Usage:
    python backtests/backtest_analysis.py
    python backtests/backtest_analysis.py --results-dir backtests/results
    python backtests/backtest_analysis.py --filter "NYY"
"""

import argparse
import csv
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtests.report_io import capture_report

RESULTS_DIR = Path(__file__).parent / "results"

WINDOWS = [5, 10, 15, 30, 45, 60, 90, 120, 180, 300]
WINDOW_COLS = [f"move_t+{w}s" for w in WINDOWS]
CLEAN_COLS = [f"clean_t+{w}s" for w in WINDOWS]

EVENT_ORDER = ["HR", "2B", "1B", "BB", "DP", "OUT", "K"]

DEFAULT_TARGET_EXIT = 40  # adaptive exit target (seconds)


def _set_target_exit(val: int):
    global DEFAULT_TARGET_EXIT
    DEFAULT_TARGET_EXIT = val


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_synced_files(results_dir: Path, filter_str: str | None = None):
    pattern = str(results_dir / "*_synced.csv")
    files = sorted(glob.glob(pattern))
    if filter_str:
        files = [f for f in files if filter_str in Path(f).name]

    rows = []
    for filepath in files:
        with open(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                row["predicted_ml_delta"] = float(
                    row.get("predicted_delta") or row.get("predicted_ml_delta") or 0
                )
                row["price_before"] = float(row.get("price_before") or 0)
                row["we_before"] = float(row.get("we_before") or 0)

                # All-window moves (may be contaminated)
                moves = {}
                for w, col in zip(WINDOWS, WINDOW_COLS):
                    val = row.get(col, "")
                    if val != "":
                        moves[w] = float(val)
                row["moves"] = moves

                # Clean moves (from backtest.py, if present)
                clean = {}
                for w, col in zip(WINDOWS, CLEAN_COLS):
                    val = row.get(col, "")
                    if val != "":
                        clean[w] = float(val)

                # If clean columns are missing, compute from time_to_next_pitch
                if not clean and moves:
                    ttn = row.get("time_to_next_pitch", "")
                    if ttn and ttn not in ("", "None"):
                        cutoff = float(ttn) - 5
                        clean = {w: v for w, v in moves.items() if w <= cutoff}
                    else:
                        clean = dict(moves)  # last play, all windows clean

                row["clean_moves"] = clean

                # Leverage fields
                row["is_high_lev_ml"] = row.get("is_high_lev_ml", "").lower() == "true"
                row["is_high_lev_ou"] = row.get("is_high_lev_ou", "").lower() == "true"
                row["leverage_score"] = float(row.get("leverage_score") or 0)
                row["total_runs"] = int(float(row.get("total_runs") or 0))
                row["market_type"] = row.get("market_type", "moneyline")
                row["market_label"] = row.get("market_label", "")

                # Gap fields
                row["time_to_next_ab"] = (
                    float(row["time_to_next_ab"])
                    if row.get("time_to_next_ab") not in ("", "None", None) else None
                )
                row["time_to_next_pitch"] = (
                    float(row["time_to_next_pitch"])
                    if row.get("time_to_next_pitch") not in ("", "None", None) else None
                )

                row["_file"] = Path(filepath).stem
                rows.append(row)

    return rows, files


def _adaptive_exit(row: dict, target: int = DEFAULT_TARGET_EXIT) -> float | None:
    """
    Adaptive exit: sell at min(target, latest clean window).
    Returns the exit P&L per contract, or None if no clean window.
    """
    clean = row.get("clean_moves", {})
    if not clean:
        return None

    # Find the largest clean window <= target
    best_w = None
    for w in sorted(clean.keys()):
        if w <= target:
            best_w = w
    # If no window <= target, use the smallest available clean window
    if best_w is None:
        best_w = min(clean.keys())

    return clean[best_w]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def print_accuracy(rows: list[dict]):
    """Accuracy using clean 30s window."""
    plays = [r for r in rows if 30 in r["clean_moves"]]
    if not plays:
        print("No plays with clean 30s window data.")
        return

    n = len(plays)
    errors = [r["clean_moves"][30] - r["predicted_ml_delta"] for r in plays]
    mae = sum(abs(e) for e in errors) / n
    rmse = (sum(e ** 2 for e in errors) / n) ** 0.5
    bias = sum(errors) / n
    dc = sum(1 for r in plays if r["predicted_ml_delta"] != 0
             and (r["predicted_ml_delta"] > 0) == (r["clean_moves"][30] > 0))
    dt = sum(1 for r in plays if r["predicted_ml_delta"] != 0)

    print(f"{'=' * 60}")
    print(f"ACCURACY SUMMARY (clean 30s window, {n} plays)")
    print(f"{'=' * 60}")
    print(f"Bias: {bias:+.4f}  |  MAE: {mae:.4f}  |  RMSE: {rmse:.4f}  |  "
          f"Dir: {100 * dc / dt:.1f}%" if dt else "n/a")

    print(f"\n{'Event':>5} {'Count':>5} {'Pred':>8} {'Actual':>8} {'MAE':>7} {'Dir%':>6}")
    print("-" * 48)
    by_ev: dict[str, list] = {}
    for r in plays:
        by_ev.setdefault(r["event"], []).append(r)
    for ev in EVENT_ORDER:
        evs = by_ev.get(ev)
        if not evs:
            continue
        pa = sum(r["predicted_ml_delta"] for r in evs) / len(evs)
        aa = sum(r["clean_moves"][30] for r in evs) / len(evs)
        em = sum(abs(r["clean_moves"][30] - r["predicted_ml_delta"]) for r in evs) / len(evs)
        edc = sum(1 for r in evs if r["predicted_ml_delta"] != 0
                  and (r["predicted_ml_delta"] > 0) == (r["clean_moves"][30] > 0))
        edt = sum(1 for r in evs if r["predicted_ml_delta"] != 0)
        edp = f"{100 * edc / edt:.0f}%" if edt else "n/a"
        print(f"{ev:>5} {len(evs):>5} {pa:>+8.4f} {aa:>+8.4f} {em:>7.4f} {edp:>6}")


def print_timing(rows: list[dict]):
    """Per-event timing analysis: ALL vs CLEAN side by side."""
    by_event: dict[str, list] = {}
    for r in rows:
        by_event.setdefault(r["event"], []).append(r)

    print(f"\n{'=' * 80}")
    print("TIMING ANALYSIS — ALL vs CLEAN (no next-pitch contamination)")
    print(f"{'=' * 80}")

    summary = []

    for event in EVENT_ORDER:
        items = by_event.get(event)
        if not items:
            continue

        n = len(items)
        pred_avg = sum(r["predicted_ml_delta"] for r in items) / n

        all_stats: dict[int, dict] = {}
        clean_stats: dict[int, dict] = {}

        for label, key, stats in [("all", "moves", all_stats),
                                   ("clean", "clean_moves", clean_stats)]:
            for w in WINDOWS:
                vals = [r[key][w] for r in items if w in r.get(key, {})]
                if not vals:
                    continue
                avg = sum(vals) / len(vals)
                dc = sum(1 for r in items
                         if w in r.get(key, {}) and r["predicted_ml_delta"] != 0
                         and (r["predicted_ml_delta"] > 0) == (r[key][w] > 0))
                dt = sum(1 for r in items
                         if w in r.get(key, {}) and r["predicted_ml_delta"] != 0)
                stats[w] = {"avg": avg, "count": len(vals),
                            "dir_correct": dc, "dir_total": dt}

        if not all_stats:
            continue

        all_peak_w = max(all_stats, key=lambda w: abs(all_stats[w]["avg"]))
        all_peak = all_stats[all_peak_w]["avg"]
        clean_peak_w = max(clean_stats, key=lambda w: abs(clean_stats[w]["avg"])) if clean_stats else 0
        clean_peak = clean_stats[clean_peak_w]["avg"] if clean_stats else 0

        print(f"\n{event} ({n} events):  predicted={pred_avg:+.4f}")
        print(f"  ALL peak: {all_peak:+.4f}@t+{all_peak_w}s  |  "
              f"CLEAN peak: {clean_peak:+.4f}@t+{clean_peak_w}s")
        print(f"  {'':>8} {'--- ALL ---':>24}    {'--- CLEAN ---':>24}")

        for w in WINDOWS:
            a = all_stats.get(w)
            c = clean_stats.get(w)
            parts = f"  t+{w:>3d}s:"
            if a:
                adp = (f"{100 * a['dir_correct'] / a['dir_total']:.0f}%"
                       if a["dir_total"] else "n/a")
                parts += f"  {a['avg']:>+7.4f} dir={adp:>4s} n={a['count']:<5d}"
            else:
                parts += f"  {'':>24}"
            if c and c["count"] > 0:
                cdp = (f"{100 * c['dir_correct'] / c['dir_total']:.0f}%"
                       if c["dir_total"] else "n/a")
                parts += f"  {c['avg']:>+7.4f} dir={cdp:>4s} n={c['count']}"
            else:
                parts += f"  {'---':>7}"
            print(parts)

        summary.append({
            "event": event, "n": n, "predicted": pred_avg,
            "all_peak": all_peak, "all_peak_w": all_peak_w,
            "clean_peak": clean_peak, "clean_peak_w": clean_peak_w,
        })

    if summary:
        print(f"\n{'=' * 70}")
        print("PEAK SUMMARY")
        print(f"{'=' * 70}")
        print(f"{'Event':>5} {'Count':>5} {'Predicted':>10} "
              f"{'All Peak':>12} {'Clean Peak':>14}")
        print("-" * 55)
        for r in summary:
            print(f"{r['event']:>5} {r['n']:>5} {r['predicted']:>+10.4f} "
                  f"{r['all_peak']:>+8.4f}@{r['all_peak_w']:>3d}s "
                  f"{r['clean_peak']:>+8.4f}@{r['clean_peak_w']:>3d}s")


def print_pnl_simulation(rows: list[dict]):
    """
    P&L simulation using clean moves only.
    Shows fixed-window exits AND adaptive exit strategy.
    """
    by_event: dict[str, list] = {}
    for r in rows:
        by_event.setdefault(r["event"], []).append(r)

    contracts = 100

    print(f"\n{'=' * 90}")
    print(f"P&L SIMULATION — {contracts} contracts, CLEAN moves only")
    print(f"{'=' * 90}")

    # Fixed-window columns
    header_wins = [w for w in WINDOWS if w <= 120]  # skip 180/300 for width
    print(f"{'Event':>5} {'Count':>5}", end="")
    for w in header_wins:
        print(f" {'t+' + str(w) + 's':>8}", end="")
    print(f" {'Adaptive':>10}")
    print("-" * (12 + 9 * len(header_wins) + 11))

    all_adaptive = []

    for event in EVENT_ORDER:
        items = by_event.get(event)
        if not items:
            continue

        print(f"{event:>5} {len(items):>5}", end="")

        for w in header_wins:
            vals = [r["clean_moves"][w] for r in items if w in r.get("clean_moves", {})]
            if vals:
                total_pnl = sum(v * contracts for v in vals)
                print(f" ${total_pnl:>+7.0f}", end="")
            else:
                print(f" {'---':>8}", end="")

        # Adaptive exit
        adaptive_vals = []
        for r in items:
            v = _adaptive_exit(r)
            if v is not None:
                adaptive_vals.append(v)
        all_adaptive.extend(adaptive_vals)

        if adaptive_vals:
            total = sum(v * contracts for v in adaptive_vals)
            print(f" ${total:>+8.0f}", end="")
        else:
            print(f" {'---':>10}", end="")

        print()

    # Totals
    print("-" * (12 + 9 * len(header_wins) + 11))
    print(f"{'TOTAL':>5} {len(rows):>5}", end="")
    for w in header_wins:
        vals = [r["clean_moves"][w] for r in rows if w in r.get("clean_moves", {})]
        if vals:
            print(f" ${sum(v * contracts for v in vals):>+7.0f}", end="")
        else:
            print(f" {'---':>8}", end="")

    if all_adaptive:
        print(f" ${sum(v * contracts for v in all_adaptive):>+8.0f}", end="")
    print()

    # Adaptive exit detail
    print(f"\nAdaptive exit strategy: sell at t+{DEFAULT_TARGET_EXIT}s or before next pitch")
    if all_adaptive:
        n = len(all_adaptive)
        avg_pnl = sum(all_adaptive) / n * contracts
        winners = sum(1 for v in all_adaptive if v > 0)
        losers = sum(1 for v in all_adaptive if v < 0)
        flat = n - winners - losers
        print(f"  Trades: {n}  |  Avg P&L/trade: ${avg_pnl:+.2f}  |  "
              f"Win/Loss/Flat: {winners}/{losers}/{flat} "
              f"({100 * winners / n:.0f}%/{100 * losers / n:.0f}%/{100 * flat / n:.0f}%)")

        # Per-event adaptive
        print(f"\n  {'Event':>5} {'Trades':>6} {'Total P&L':>10} {'Avg/Trade':>10} {'Win%':>6}")
        print(f"  {'-' * 42}")
        for event in EVENT_ORDER:
            items = by_event.get(event, [])
            vals = [v for r in items if (v := _adaptive_exit(r)) is not None]
            if not vals:
                continue
            total = sum(v * contracts for v in vals)
            avg = total / len(vals)
            wp = 100 * sum(1 for v in vals if v > 0) / len(vals)
            print(f"  {event:>5} {len(vals):>6} ${total:>+9.0f} ${avg:>+9.2f} {wp:>5.0f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with capture_report("backtest_analysis") as report_path:
        _main_inner()
        print(f"\nReport saved: {report_path}")


def _main_inner():
    parser = argparse.ArgumentParser(
        description="Analyze backtest results from synced CSV files")
    parser.add_argument("--results-dir", type=str,
                        default=str(RESULTS_DIR),
                        help="Directory containing *_synced.csv files")
    parser.add_argument("--filter", type=str, default=None,
                        help="Only include files matching this string")
    parser.add_argument("--target-exit", type=int, default=DEFAULT_TARGET_EXIT,
                        help=f"Adaptive exit target in seconds (default {DEFAULT_TARGET_EXIT})")
    args = parser.parse_args()

    _set_target_exit(args.target_exit)
    results_dir = Path(args.results_dir)
    rows, files = load_synced_files(results_dir, args.filter)

    if not rows:
        print(f"No synced data found in {results_dir}")
        return

    print(f"Loaded {len(rows)} plays from {len(files)} files")
    if args.filter:
        print(f"Filter: '{args.filter}'")
    print(f"Adaptive exit target: t+{DEFAULT_TARGET_EXIT}s (or before next pitch)")

    print_accuracy(rows)
    print_timing(rows)
    print_pnl_simulation(rows)

    print("\nDone.")


if __name__ == "__main__":
    main()
