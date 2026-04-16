#!/usr/bin/env python3
"""2D sweep over (min_move_cents, min_move_to_fee_ratio).

Extends `sweeps/ratio.py` with a second axis: min_move_cents at {0, 3}.
Tests whether the 3¢ absolute floor is still load-bearing once the
price-aware ratio filter is active, or whether it's over-filtering
trades the ratio gate would already pass.

Grid: 2 floors × 7 ratios = 14 cells. α=0.65 held fixed. Same cached
37-game window as `sweeps/ratio.py` (2026-04-08 through 2026-04-13).

Output:
- 2D table: ratio rows × floor columns, showing net EV and trade count.
- Per-ratio `Δ vs floor=3` column — positive means dropping the floor
  helped, negative means the floor was still cutting profitable trades.
- Summary paragraph interpreting the pattern.
- CSV to `reports/ratio_2d_sweep_<timestamp>.csv` (one row per cell).

Caveats (also printed):
- Fill model is simulated, not real. Directional only.
- Results are suggestive, not production-default-changing. Confirm
  against live dry-run data before any filter config change.

Run:  cd backend && venv/bin/python -m backtests.sweeps.ratio_2d
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


BASELINE_ALPHA = 0.65

MIN_MOVE_GRID = [0, 3]     # 0 = floor disabled; 3 = current production floor
RATIO_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]

SWEEP_DATES = [
    date(2026, 4, 8),
    date(2026, 4, 9),
    date(2026, 4, 10),
    date(2026, 4, 13),
]


def _aggregate(trades: list[dict], per_game: dict) -> dict:
    n = len(trades)
    gross = sum(t.get("gross_pnl") or 0.0 for t in trades)
    fees = sum(t.get("fees") or 0.0 for t in trades)
    net = sum(t.get("pnl") or 0.0 for t in trades)
    return {
        "n": n,
        "gross_ev": gross,
        "fees": fees,
        "net_ev": net,
        "net_per_trade": net / n if n > 0 else 0.0,
        "fill_rate": per_game["fill_rate"],
        "per_game": per_game["per_game"],
        "worst": per_game["worst"],
    }


def eval_cell(all_synced, floor_cents: int, ratio: float) -> dict:
    single, _ = run_silent(
        all_synced, BASELINE_ALPHA,
        min_move=floor_cents / 100.0,
        min_move_to_fee_ratio=ratio,
    )
    stats = stats_with_per_game(single)
    return {
        "floor_cents": floor_cents, "ratio": ratio,
        **_aggregate(single, stats),
    }


def _print_caveats():
    print("=" * 88)
    print("(min_move_cents, min_move_to_fee_ratio) 2D sweep")
    print("=" * 88)
    print(f"Held fixed: alpha={BASELINE_ALPHA}")
    print(f"Dates: {', '.join(d.isoformat() for d in SWEEP_DATES)}")
    print()
    print("Question: is the 3¢ absolute floor still load-bearing once")
    print("the ratio filter is active, or does it over-filter trades the")
    print("ratio gate would already pass?")
    print()
    print("Caveats:")
    print("  - Fill model is simulated, not real. Directional only;")
    print("    confirm against live dry-run before any default change.")
    print("  - Nothing here should change production defaults. Report only.")
    print()


def _print_2d_table(results: list[dict]):
    # Index by (floor, ratio) for fast lookup while printing.
    by_key = {(r["floor_cents"], r["ratio"]): r for r in results}

    hdr = (
        f"{'Ratio':>6}  "
        f"{'Net EV (floor=0)':>18}  {'Trades':>7}  "
        f"{'Net EV (floor=3)':>18}  {'Trades':>7}  "
        f"{'Δ (floor=0 - floor=3)':>22}"
    )
    print(hdr)
    print("-" * len(hdr))
    for ratio in RATIO_GRID:
        r0 = by_key[(0, ratio)]
        r3 = by_key[(3, ratio)]
        delta = r0["net_ev"] - r3["net_ev"]
        trade_delta = r0["n"] - r3["n"]
        print(
            f"{ratio:>5.1f}x  "
            f"${r0['net_ev']:>+15,.0f}   {r0['n']:>6,}   "
            f"${r3['net_ev']:>+15,.0f}   {r3['n']:>6,}   "
            f"${delta:>+13,.0f}  ({trade_delta:+,} trades)"
        )


def _print_full_cells(results: list[dict]):
    print()
    print("All 14 cells (sorted by net EV):")
    hdr = (
        f"{'Floor':>5}  {'Ratio':>5}  {'Trades':>6}  "
        f"{'Gross EV':>10}  {'Fees':>9}  {'Net EV':>10}  "
        f"{'Net/Trade':>9}  {'Fill%':>6}  {'$/game':>8}  {'Worst':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(results, key=lambda x: x["net_ev"], reverse=True):
        print(
            f"{r['floor_cents']:>4}¢  {r['ratio']:>4.1f}x  {r['n']:>6,}  "
            f"${r['gross_ev']:>+9,.0f}  ${r['fees']:>+8,.0f}  "
            f"${r['net_ev']:>+9,.0f}  ${r['net_per_trade']:>+8,.2f}  "
            f"{r['fill_rate']:>5.1f}%  "
            f"${r['per_game']:>+7,.0f}  ${r['worst']:>+7,.0f}"
        )


def _interpret(results: list[dict]):
    """Classify each ratio row as 'drop floor helped' / 'floor still
    doing work' / 'wash' based on the delta between floor=0 and floor=3.
    Thresholds are relative: a 2% swing of total net EV counts as noise;
    bigger than that is a real signal."""
    by_key = {(r["floor_cents"], r["ratio"]): r for r in results}
    print()
    print("=" * 88)
    print("Interpretation")
    print("=" * 88)

    total_peak = max(r["net_ev"] for r in results)
    noise_band = total_peak * 0.02   # 2% of peak net = "within noise"

    verdicts = []
    for ratio in RATIO_GRID:
        r0 = by_key[(0, ratio)]
        r3 = by_key[(3, ratio)]
        delta = r0["net_ev"] - r3["net_ev"]
        trade_delta = r0["n"] - r3["n"]
        if delta > noise_band:
            verdict = "FLOOR HURTS"   # dropping it helped — floor over-filtered
        elif delta < -noise_band:
            verdict = "FLOOR HELPS"   # keeping it helped — floor still earning
        else:
            verdict = "WASH"
        verdicts.append((ratio, delta, trade_delta, verdict))
        print(
            f"  ratio={ratio:.1f}x  Δ=${delta:>+6,.0f}  "
            f"({trade_delta:+,} trades)  →  {verdict}"
        )

    # One-line take for each regime.
    print()
    floor_helps_at = [v for v in verdicts if v[3] == "FLOOR HELPS"]
    floor_hurts_at = [v for v in verdicts if v[3] == "FLOOR HURTS"]
    wash_at = [v for v in verdicts if v[3] == "WASH"]

    if floor_hurts_at and all(v[0] >= 2.0 for v in floor_hurts_at):
        print(
            "  Pattern: at ratio ≥ 2.0 the floor over-filters; the ratio "
            "gate alone is sufficient in that regime."
        )
    elif floor_helps_at and all(v[0] >= 2.0 for v in floor_helps_at):
        print(
            "  Pattern: at ratio ≥ 2.0 the floor still cuts genuinely thin "
            "trades the ratio gate misses (likely execution-noise)."
        )
    elif len(wash_at) == len(verdicts):
        print(
            "  Pattern: floor effect is noise-level across all tested "
            "ratios. Can stay as cheap insurance; removing it wouldn't "
            "meaningfully change returns."
        )
    else:
        print(
            "  Pattern: mixed — interpretation depends on which ratio "
            "you deploy. See the per-ratio verdicts above."
        )
    print(
        f"  (noise band = 2% of peak net EV = ±${noise_band:,.0f})"
    )


def _write_csv(results: list[dict]) -> str:
    path = timestamped_path("ratio_2d_sweep", "csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "floor_cents", "ratio", "n_trades",
            "gross_ev", "fees", "net_ev", "net_per_trade",
            "fill_rate_pct", "per_game", "worst_game",
        ])
        for r in sorted(results, key=lambda x: (x["floor_cents"], x["ratio"])):
            writer.writerow([
                r["floor_cents"], r["ratio"], r["n"],
                round(r["gross_ev"], 2), round(r["fees"], 2),
                round(r["net_ev"], 2), round(r["net_per_trade"], 4),
                round(r["fill_rate"], 2), round(r["per_game"], 2),
                round(r["worst"], 2),
            ])
    return str(path)


def main():
    widen_alphas([BASELINE_ALPHA])
    all_synced = load_and_sync(SWEEP_DATES)

    _print_caveats()

    results = [
        eval_cell(all_synced, floor, ratio)
        for floor in MIN_MOVE_GRID for ratio in RATIO_GRID
    ]

    _print_2d_table(results)
    _print_full_cells(results)
    _interpret(results)

    csv_path = _write_csv(results)
    print()
    print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
