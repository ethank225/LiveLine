#!/usr/bin/env python3
"""2D sweep over (alpha, min_move_cents) on cached 3-day backtest.

Reuses the same synced trade set across all cells; only `run_comparison`
re-invokes per cell.
"""

from backtests.sweeps._common import (
    load_and_sync,
    run_silent,
    stats_with_per_game,
    widen_alphas,
)

ALPHA_GRID = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
MIN_MOVE_GRID_CENTS = [3, 4, 5, 6, 7, 8]


def pareto_frontier(rows: list[dict]) -> list[dict]:
    """Cells not dominated on (per_game↑, worst↑). A row dominates
    another if it has ≥ per_game AND ≥ worst (less negative), with
    strict inequality on at least one axis."""
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


def eval_cell(all_synced, alpha, min_move_cents):
    single, _ = run_silent(all_synced, alpha, min_move=min_move_cents / 100.0)
    s = stats_with_per_game(single)
    return {"alpha": alpha, "min_move": min_move_cents, **s}


def main():
    widen_alphas()
    all_synced = load_and_sync()

    results = [
        eval_cell(all_synced, a, m)
        for a in ALPHA_GRID for m in MIN_MOVE_GRID_CENTS
    ]
    results.sort(key=lambda r: r["per_game"], reverse=True)
    top5_keys = {(r["alpha"], r["min_move"]) for r in results[:5]}
    frontier = pareto_frontier(results)
    frontier_keys = {(r["alpha"], r["min_move"]) for r in frontier}

    hdr = (
        f"{'Rank':>4}  {'Alpha':>5}  {'MinMove':>7}  {'Trades':>6}  "
        f"{'Fill%':>6}  {'Net P&L':>10}  {'$/game':>8}  {'Worst':>8}  Flags"
    )
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(results, start=1):
        flags = []
        if (r["alpha"], r["min_move"]) in top5_keys:
            flags.append("TOP5")
        if (r["alpha"], r["min_move"]) in frontier_keys:
            flags.append("pareto")
        print(
            f"{i:>4}  {r['alpha']:>5.2f}  {r['min_move']:>5}¢   {r['n']:>6,}  "
            f"{r['fill_rate']:>5.1f}%  ${r['net']:>+9,.0f}  "
            f"${r['per_game']:>+7,.0f}  ${r['worst']:>+7,.0f}  {' '.join(flags)}"
        )

    print("\n" + "=" * 60)
    print("Pareto frontier (best $/game at each worst-game level):")
    print("=" * 60)
    for r in sorted(frontier, key=lambda x: x["worst"]):
        print(
            f"  α={r['alpha']:.2f}  min={r['min_move']}¢  "
            f"→  $/game=${r['per_game']:+,.0f}   worst=${r['worst']:+,.0f}   "
            f"trades={r['n']:,}   fill={r['fill_rate']:.1f}%"
        )


if __name__ == "__main__":
    main()
