#!/usr/bin/env python3
"""2D sweep over (alpha, min_move_to_fee_ratio).

Both filters sit on the "which trades to take" side of the pipeline
and have a shared interaction: alpha scales the predicted move, and
the ratio gate compares that move against per-contract fees. Sweeping
them independently leaves the interaction uncharted — a higher alpha
widens the expected move, which can push fee-ratios above threshold
for trades a lower alpha would have filtered out.

Grid: 6 alphas × 7 ratios = 42 cells. `min_move_cents=3` held fixed
(the `ratio_2d` sweep established that floor is neutral-to-helpful at
ratio ≤ 2.5 and only over-filters at ratio ≥ 3.0 — kept on for this
sweep to match current production). Same cached 37-game window as the
prior ratio sweeps.

Output:
- 2D table: alpha columns × ratio rows, showing net EV per cell.
- Top-5 cells + Pareto frontier on (per_game, worst_game) so the
  risk-adjusted picks are visible alongside the raw-EV winners.
- CSV to `reports/alpha_ratio_sweep_<timestamp>.csv`.

Caveats (also printed):
- Fill model is simulated. Directional only; cross-check against live
  dry-run before changing production defaults.
- Results are reports; not tuning production.

Run:  cd backend && venv/bin/python -m backtests.sweeps.alpha_ratio
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


ALPHA_GRID = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
RATIO_GRID = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
BASELINE_MIN_MOVE_CENTS = 3

SWEEP_DATES = [
    date(2026, 4, 8),
    date(2026, 4, 9),
    date(2026, 4, 10),
    date(2026, 4, 13),
]


def _aggregate(trades: list[dict], per_game: dict) -> dict:
    n = len(trades)
    net = sum(t.get("pnl") or 0.0 for t in trades)
    return {
        "n": n,
        "net_ev": net,
        "net_per_trade": net / n if n > 0 else 0.0,
        "fill_rate": per_game["fill_rate"],
        "per_game": per_game["per_game"],
        "worst": per_game["worst"],
    }


def eval_cell(all_synced, alpha: float, ratio: float) -> dict:
    single, _ = run_silent(
        all_synced, alpha,
        min_move=BASELINE_MIN_MOVE_CENTS / 100.0,
        min_move_to_fee_ratio=ratio,
    )
    stats = stats_with_per_game(single)
    return {"alpha": alpha, "ratio": ratio, **_aggregate(single, stats)}


def pareto_frontier(rows: list[dict]) -> list[dict]:
    """Cells not dominated on (per_game↑, worst↑). Same convention as
    `alpha_minmove.py` — lets the reader pick based on drawdown
    tolerance, not just headline return."""
    frontier = []
    for r in rows:
        dominated = any(
            other is not r
            and other["per_game"] >= r["per_game"]
            and other["worst"] >= r["worst"]
            and (other["per_game"] > r["per_game"] or other["worst"] > r["worst"])
            for other in rows
        )
        if not dominated:
            frontier.append(r)
    return frontier


def _print_caveats():
    print("=" * 88)
    print("(alpha, min_move_to_fee_ratio) 2D sweep")
    print("=" * 88)
    print(f"Held fixed: min_move_cents={BASELINE_MIN_MOVE_CENTS}")
    print(f"Dates: {', '.join(d.isoformat() for d in SWEEP_DATES)}")
    print()
    print("Question: how do alpha (expected-move scalar) and the")
    print("price-aware ratio gate interact? Higher alpha widens the")
    print("predicted move, which can lift fee-ratios past threshold")
    print("for trades a lower alpha would have filtered out.")
    print()
    print("Caveats:")
    print("  - Fill model is simulated, not real. Directional only.")
    print("  - Report only. Production defaults should not change")
    print("    based on this sweep alone — confirm against live")
    print("    dry-run data first.")
    print()


def _print_heatmap(results: list[dict]):
    """Net EV grid: rows = ratio, cols = alpha. The full-cell table
    below has the complete stats; this view shows the 2D shape at a
    glance so the interaction is legible."""
    by_key = {(r["alpha"], r["ratio"]): r for r in results}

    print("Net EV by cell  (rows = ratio, cols = alpha):")
    hdr = f"{'Ratio':>6}  " + "  ".join(f"α={a:.2f}".rjust(10) for a in ALPHA_GRID)
    print(hdr)
    print("-" * len(hdr))
    peak_net = max(r["net_ev"] for r in results)
    for ratio in RATIO_GRID:
        cells = []
        for alpha in ALPHA_GRID:
            r = by_key[(alpha, ratio)]
            mark = " *" if r["net_ev"] == peak_net else "  "
            cells.append(f"${r['net_ev']:>+7,.0f}{mark}")
        print(f"{ratio:>5.1f}x  " + "  ".join(cells))
    print()
    print("  (* = overall peak)")
    print()


def _print_top5_and_frontier(results: list[dict]):
    sorted_desc = sorted(results, key=lambda r: r["per_game"], reverse=True)
    top5 = sorted_desc[:5]
    frontier = pareto_frontier(results)

    print("Top 5 cells by $/game:")
    hdr = (
        f"{'Rank':>4}  {'α':>4}  {'Ratio':>5}  {'Trades':>6}  "
        f"{'Net EV':>10}  {'$/game':>8}  {'Worst':>8}  {'Fill%':>6}  "
        f"{'Net/Trade':>9}"
    )
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(top5, start=1):
        print(
            f"{i:>4}  {r['alpha']:>4.2f}  {r['ratio']:>4.1f}x  "
            f"{r['n']:>6,}  ${r['net_ev']:>+9,.0f}  "
            f"${r['per_game']:>+7,.0f}  ${r['worst']:>+7,.0f}  "
            f"{r['fill_rate']:>5.1f}%  ${r['net_per_trade']:>+8,.2f}"
        )

    print()
    print("Pareto frontier (best $/game at each worst-game level):")
    for r in sorted(frontier, key=lambda x: x["worst"]):
        print(
            f"  α={r['alpha']:.2f}  ratio={r['ratio']:.1f}x  "
            f"→  $/game=${r['per_game']:+,.0f}  "
            f"worst=${r['worst']:+,.0f}  "
            f"trades={r['n']:,}  fill={r['fill_rate']:.1f}%"
        )


def _summarize_interaction(results: list[dict]):
    """Per-alpha: which ratio was best? Looking across rows highlights
    whether the optimal ratio shifts with alpha (interaction) or stays
    constant (independent effects)."""
    print()
    print("Interaction check — best ratio at each alpha:")
    print(f"  {'α':>5}  {'Best ratio':>10}  {'Best Net EV':>11}  "
          f"{'Best $/game':>11}  {'Trades':>7}")
    print("-" * 56)
    for alpha in ALPHA_GRID:
        cells = [r for r in results if r["alpha"] == alpha]
        best = max(cells, key=lambda r: r["net_ev"])
        print(
            f"  {alpha:>4.2f}  {best['ratio']:>9.1f}x  "
            f"${best['net_ev']:>+10,.0f}  ${best['per_game']:>+10,.0f}  "
            f"{best['n']:>6,}"
        )


def _write_csv(results: list[dict]) -> str:
    path = timestamped_path("alpha_ratio_sweep", "csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "alpha", "ratio", "n_trades",
            "net_ev", "net_per_trade",
            "fill_rate_pct", "per_game", "worst_game",
        ])
        for r in sorted(results, key=lambda x: (x["alpha"], x["ratio"])):
            writer.writerow([
                r["alpha"], r["ratio"], r["n"],
                round(r["net_ev"], 2), round(r["net_per_trade"], 4),
                round(r["fill_rate"], 2), round(r["per_game"], 2),
                round(r["worst"], 2),
            ])
    return str(path)


def main():
    # Every alpha in the grid must be simulated by fill_sim — widen once
    # to cover all of them before load_and_sync runs.
    widen_alphas(ALPHA_GRID)
    all_synced = load_and_sync(SWEEP_DATES)

    _print_caveats()

    results = [
        eval_cell(all_synced, a, r)
        for a in ALPHA_GRID for r in RATIO_GRID
    ]

    _print_heatmap(results)
    _print_top5_and_frontier(results)
    _summarize_interaction(results)

    csv_path = _write_csv(results)
    print()
    print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
