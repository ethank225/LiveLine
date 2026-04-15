#!/usr/bin/env python3
"""Single-alpha sweep on cached 3-day backtest.

Runs single-market mode at every alpha in [0.30, 0.80] / 0.05 steps,
reusing the same synced trade set.
"""

from backtests.sweeps._common import (
    DEFAULT_ALPHAS_SWEEP,
    load_and_sync,
    run_silent,
    stats_with_per_game,
    widen_alphas,
)

MIN_MOVE = 0.04


def main():
    alphas = widen_alphas()  # default 0.30..0.80
    all_synced = load_and_sync()

    hdr = (
        f"{'Alpha':>5}  {'Trades':>6}  {'Fill%':>6}  {'AvgProf/Fill':>13}  "
        f"{'AvgLoss/Miss':>13}  {'Net P&L':>10}  {'$/game':>9}  {'Worst':>9}"
    )
    print(hdr)
    print("-" * len(hdr))

    for alpha in alphas:
        single, _ = run_silent(all_synced, alpha, min_move=MIN_MOVE)
        s = stats_with_per_game(single)
        print(
            f"{alpha:>5.2f}  {s['n']:>6,}  {s['fill_rate']:>5.1f}%  "
            f"{s['avg_profit_fill']:>+13.4f}  {s['avg_loss_miss']:>+13.4f}  "
            f"${s['net']:>+9,.0f}  ${s['per_game']:>+8,.0f}  ${s['worst']:>+8,.0f}"
        )


if __name__ == "__main__":
    main()
