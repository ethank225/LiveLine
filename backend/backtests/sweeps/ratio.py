#!/usr/bin/env python3
"""Sweep `min_move_to_fee_ratio` on the cached backtest window.

The question: where does the price-aware min-move floor stop cutting
edge and start cutting real trades? The filter skips candidates whose
expected entry→target move is less than N× per-contract round-trip
fee. A flat `min_move_cents=3` is already applied alongside this ratio
gate (both must pass), so the sweep isolates the ratio's marginal
effect.

Ratio grid: 1.0 (≈ filter disabled — requires only breakeven vs fees),
1.5, 2.0, 2.5, 3.0, 3.5, 4.0. Other parameters are held at the current
best-known values from earlier sweeps (α=0.65, min_move=3¢; see
README.md §Tuning).

Output:
- stdout table per threshold: trades, gross EV, fees, net EV,
  net-per-trade, win rate, and a fee_pct distribution.
- CSV to `reports/ratio_sweep_<timestamp>.csv` with one row per cell.

Caveats (also printed at the top of the report):
- Fill model is simulated, not real. Directional only; cross-check
  against live dry-run data before changing production defaults.
- `min_move_cents=3` remains active during this sweep. If the ratio
  filter ends up doing most of the work, a follow-up sweep could A/B
  the absolute floor at 0.

Run:  cd backend && venv/bin/python -m backtests.sweeps.ratio
"""

import csv
from datetime import date

from backtests.reporting.report_io import timestamped_path
from backtests.sweeps._common import (
    load_and_sync,
    run_silent,
    stats_with_per_game,
    widen_alphas,
)


# Held-fixed parameters. 0.65 / 3¢ is the current best baseline per
# README.md. If those shift in the future, rerun this sweep to reconfirm.
BASELINE_ALPHA = 0.65
BASELINE_MIN_MOVE_CENTS = 3

# Filter values to test. 1.0 = "must at least break even vs fees"
# which is the weakest useful setting; 4.0 is aggressively strict.
RATIO_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]

# Widest cached window. 2026-04-13 has a single game but more data
# always beats less for this kind of threshold search.
SWEEP_DATES = [
    date(2026, 4, 8),
    date(2026, 4, 9),
    date(2026, 4, 10),
    date(2026, 4, 13),
]

# Buckets for the fee_pct distribution (fees / gross_pnl). Exhaustive
# over the interesting range; trades outside these buckets (negative
# gross, or gross=0) go into a dedicated "other" bucket.
FEE_PCT_BUCKETS = [
    ("<10%",   lambda p: p < 10),
    ("10-25%", lambda p: 10 <= p < 25),
    ("25-50%", lambda p: 25 <= p < 50),
    (">50%",   lambda p: p >= 50),
]


def _bucket_counts(trades: list[dict]) -> dict:
    """Return {bucket_label: count} plus 'other' for trades with
    non-positive gross (can't compute a meaningful percentage)."""
    counts = {label: 0 for label, _ in FEE_PCT_BUCKETS}
    counts["other"] = 0
    for t in trades:
        gross = t.get("gross_pnl") or 0.0
        fees = t.get("fees") or 0.0
        if gross <= 0:
            counts["other"] += 1
            continue
        pct = (fees / gross) * 100.0
        for label, predicate in FEE_PCT_BUCKETS:
            if predicate(pct):
                counts[label] += 1
                break
    return counts


def _aggregate_cell(trades: list[dict], per_game: dict) -> dict:
    """Pull per-trade aggregates out of the trade list. Complements
    `stats_with_per_game` (which is win/fill focused) with the fee
    accounting the ratio sweep actually cares about."""
    n = len(trades)
    gross = sum(t.get("gross_pnl") or 0.0 for t in trades)
    fees = sum(t.get("fees") or 0.0 for t in trades)
    net = sum(t.get("pnl") or 0.0 for t in trades)
    # Win rate: only count trades that actually resolved (filled=True).
    # Unfilled trades are clean-window misses, not wins/losses.
    resolved = [t for t in trades if t.get("filled")]
    wins = sum(1 for t in resolved if (t.get("pnl") or 0.0) > 0)
    win_rate = wins / len(resolved) * 100 if resolved else 0.0

    return {
        "n": n,
        "gross_ev": gross,
        "fees": fees,
        "net_ev": net,
        "net_per_trade": net / n if n > 0 else 0.0,
        "fill_rate": per_game["fill_rate"],
        "win_rate": win_rate,
        "per_game": per_game["per_game"],
        "worst": per_game["worst"],
        "buckets": _bucket_counts(trades),
    }


def eval_cell(all_synced, ratio: float) -> dict:
    single, _ = run_silent(
        all_synced, BASELINE_ALPHA,
        min_move=BASELINE_MIN_MOVE_CENTS / 100.0,
        min_move_to_fee_ratio=ratio,
    )
    stats = stats_with_per_game(single)
    return {"ratio": ratio, **_aggregate_cell(single, stats)}


def _print_caveats():
    print("=" * 84)
    print("min_move_to_fee_ratio sweep")
    print("=" * 84)
    print(f"Held fixed: alpha={BASELINE_ALPHA}  min_move={BASELINE_MIN_MOVE_CENTS}¢")
    print(f"Dates: {', '.join(d.isoformat() for d in SWEEP_DATES)}")
    print()
    print("Caveats:")
    print("  - Fill model is simulated, not real. Results are directionally")
    print("    informative; cross-check against live dry-run before changing")
    print("    production defaults.")
    print("  - min_move_cents=3 absolute floor is active alongside the ratio")
    print("    gate. If the ratio filter dominates, a follow-up sweep could")
    print("    A/B the absolute floor at 0.")
    print()


def _print_table(results: list[dict]):
    hdr = (
        f"{'Ratio':>6}  {'Trades':>6}  {'Gross EV':>10}  {'Fees':>9}  "
        f"{'Net EV':>10}  {'Net/Trade':>9}  {'Fill%':>6}  {'Win%':>6}  "
        f"{'$/game':>8}  {'Worst':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(
            f"{r['ratio']:>5.1f}x  {r['n']:>6,}  "
            f"${r['gross_ev']:>+9,.0f}  ${r['fees']:>+8,.0f}  "
            f"${r['net_ev']:>+9,.0f}  ${r['net_per_trade']:>+8,.2f}  "
            f"{r['fill_rate']:>5.1f}%  {r['win_rate']:>5.1f}%  "
            f"${r['per_game']:>+7,.0f}  ${r['worst']:>+7,.0f}"
        )


def _print_bucket_distribution(results: list[dict]):
    print()
    print("Fee-pct distribution (fees as % of gross_pnl, trades with gross > 0):")
    bucket_labels = [label for label, _ in FEE_PCT_BUCKETS] + ["other"]
    hdr = f"{'Ratio':>6}  " + "  ".join(f"{b:>8}" for b in bucket_labels)
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        counts = r["buckets"]
        row = f"{r['ratio']:>5.1f}x  " + "  ".join(
            f"{counts[b]:>8,}" for b in bucket_labels
        )
        print(row)


def _write_csv(results: list[dict]) -> str:
    path = timestamped_path("ratio_sweep", "csv")
    bucket_labels = [label for label, _ in FEE_PCT_BUCKETS] + ["other"]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ratio", "n_trades", "gross_ev", "fees", "net_ev",
            "net_per_trade", "fill_rate_pct", "win_rate_pct",
            "per_game", "worst_game",
            *[f"bucket_{b}" for b in bucket_labels],
        ])
        for r in results:
            writer.writerow([
                r["ratio"], r["n"], round(r["gross_ev"], 2),
                round(r["fees"], 2), round(r["net_ev"], 2),
                round(r["net_per_trade"], 4),
                round(r["fill_rate"], 2), round(r["win_rate"], 2),
                round(r["per_game"], 2), round(r["worst"], 2),
                *[r["buckets"][b] for b in bucket_labels],
            ])
    return str(path)


def _summarize_bend(results: list[dict]):
    """Identify the threshold where net-per-trade stops improving and
    where total net EV starts dropping due to over-filtering. These are
    the two bends the user cares about."""
    if len(results) < 2:
        return
    # Net/trade peak (most profitable marginal trade).
    peak_npt = max(results, key=lambda r: r["net_per_trade"])
    # Total net EV peak (sweet spot before over-filtering kicks in).
    peak_total = max(results, key=lambda r: r["net_ev"])
    print()
    print("Bend analysis:")
    print(
        f"  Net-per-trade peak: ratio={peak_npt['ratio']:.1f}x "
        f"(${peak_npt['net_per_trade']:+.2f}/trade on {peak_npt['n']:,} trades)"
    )
    print(
        f"  Total net-EV peak:  ratio={peak_total['ratio']:.1f}x "
        f"(${peak_total['net_ev']:+,.0f} on {peak_total['n']:,} trades)"
    )
    # Cuts-real-edge detector: scan from the strictest threshold
    # downward; once net_ev drops by >5% from the peak, note it.
    sorted_desc = sorted(results, key=lambda r: r["ratio"], reverse=True)
    peak_ev = peak_total["net_ev"]
    for r in sorted_desc:
        if r["net_ev"] < peak_ev * 0.95:
            print(
                f"  First 5% net-EV drop from peak: ratio≥{r['ratio']:.1f}x "
                f"(net=${r['net_ev']:+,.0f}, {peak_ev - r['net_ev']:+,.0f} "
                f"vs peak)"
            )
            break


def main():
    widen_alphas([BASELINE_ALPHA])   # single-α sweep; only need 0.65 simulated
    all_synced = load_and_sync(SWEEP_DATES)

    _print_caveats()

    results = [eval_cell(all_synced, r) for r in RATIO_GRID]
    results.sort(key=lambda r: r["ratio"])

    _print_table(results)
    _print_bucket_distribution(results)
    _summarize_bend(results)

    csv_path = _write_csv(results)
    print()
    print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
