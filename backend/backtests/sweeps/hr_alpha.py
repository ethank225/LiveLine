#!/usr/bin/env python3
"""Sweep HR-specific alpha while keeping all other events at α=0.65.

Each play in the synced set carries exactly one event type, so
splitting `all_synced` by event and running `run_comparison` on each
half gives a per-event alpha without touching the candidate builder.
Combining results from both passes reconstructs a single logical
strategy run.
"""

from collections import defaultdict

from backtests.sweeps._common import (
    load_and_sync,
    run_silent,
    stats_with_per_game,
    widen_alphas,
)

MIN_MOVE = 0.03
BASE_ALPHA = 0.65  # everything except HR
HR_ALPHA_GRID = [round(0.30 + 0.05 * i, 2) for i in range(8)]  # 0.30..0.65


def _net_and_worst(trades: list[dict]) -> tuple[float, float, int]:
    """Combined-mode per_game + worst from two trade lists."""
    by_game: dict[str, float] = defaultdict(float)
    for t in trades:
        by_game[t["rec"].get("game_tag", "")] += t["pnl"]
    n_games = len(by_game) or 1
    return (
        sum(by_game.values()) / n_games,
        min(by_game.values()) if by_game else 0.0,
        n_games,
    )


def main():
    widen_alphas()
    all_synced = load_and_sync()
    hr_synced = [r for r in all_synced if r.get("event") == "HR"]
    other_synced = [r for r in all_synced if r.get("event") != "HR"]

    # The non-HR arm is fixed across every row — compute once.
    other_single, _ = run_silent(other_synced, BASE_ALPHA, min_move=MIN_MOVE)
    other_stats = stats_with_per_game(other_single)

    # Baseline: HRs also at α=0.65. Anchor for the Δ column.
    baseline_hr, _ = run_silent(hr_synced, BASE_ALPHA, min_move=MIN_MOVE)
    baseline_total = stats_with_per_game(baseline_hr)["net"] + other_stats["net"]

    print(
        f"Non-HR arm (fixed at α={BASE_ALPHA}): "
        f"n={other_stats['n']}, fill={other_stats['fill_rate']:.1f}%, "
        f"net=${other_stats['net']:+,.0f}"
    )
    print(f"Baseline (HR α=0.65 too): total net = ${baseline_total:+,.0f}\n")

    hdr = (
        f"{'HR α':>5}  {'HR n':>5}  {'HR Fill%':>8}  {'HR Net':>9}  "
        f"{'Total Net':>10}  {'Δ vs base':>10}  {'$/game':>8}  {'Worst':>8}"
    )
    print(hdr)
    print("-" * len(hdr))

    for hr_alpha in HR_ALPHA_GRID:
        hr_single, _ = run_silent(hr_synced, hr_alpha, min_move=MIN_MOVE)
        hr_st = stats_with_per_game(hr_single)
        combined_net = hr_st["net"] + other_stats["net"]
        pg, worst, _ = _net_and_worst(hr_single + other_single)
        print(
            f"{hr_alpha:>5.2f}  {hr_st['n']:>5,}  {hr_st['fill_rate']:>7.1f}%  "
            f"${hr_st['net']:>+8,.0f}  ${combined_net:>+9,.0f}  "
            f"${combined_net - baseline_total:>+9,.0f}  "
            f"${pg:>+7,.0f}  ${worst:>+7,.0f}"
        )


if __name__ == "__main__":
    main()
