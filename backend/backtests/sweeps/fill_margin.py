#!/usr/bin/env python3
"""Fill-rate margin analysis against the current production baseline.

Four numbers:
  - avg P&L per filled trade (target hit)
  - avg P&L per unfilled trade (clean-window timeout / stopped / IOC exit)
  - breakeven fill rate implied by those two
  - margin = current fill rate − breakeven fill rate

The breakeven identity: E[pnl] = f · W + (1 − f) · L. Setting E[pnl] = 0
and solving for f gives f_be = −L / (W − L). Positive margin → room to
breathe; shrinking margin → strategy is approaching the point where a
modest drop in fill rate flips the sign of EV.

Primary numbers reported are per-trade totals (dollars, net of fees).
Per-trade is the production-meaningful unit: "if fill rate drops, at
what point does total P&L flip sign?"

Per-contract breakeven is also printed for reference but is misleading
on its own — unfilled trades cluster on cheap contracts (budget buys
many more contracts at $0.10 entry than at $0.80), so per-contract
losses on misses look tiny even when the dollar hit per trade is real.

Baseline parameters:
  α = 0.65, min_move_cents = 3, min_move_to_fee_ratio = 2.0
  — current tuned values per backtests/README.md §Current honest baseline
  and the ratio sweep's flat-plateau peak at 2.0×.

Run:  cd backend && venv/bin/python -m backtests.sweeps.fill_margin
"""

from datetime import date

from backtests.sweeps._common import load_and_sync, run_silent, widen_alphas


BASELINE_ALPHA = 0.65
BASELINE_MIN_MOVE_CENTS = 3
BASELINE_RATIO = 2.0

SWEEP_DATES = [
    date(2026, 4, 8),
    date(2026, 4, 9),
    date(2026, 4, 10),
    date(2026, 4, 13),
]


def _compute_margin(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "n_fill": 0, "n_miss": 0,
            "fill_rate": 0.0,
            "avg_fill_pc": 0.0, "avg_miss_pc": 0.0,
            "avg_fill_dollar": 0.0, "avg_miss_dollar": 0.0,
            "breakeven_fill_rate": None, "margin_pct": None,
            "total_net": 0.0, "total_gross": 0.0, "total_fees": 0.0,
        }

    fills = [t for t in trades if t["filled"]]
    misses = [t for t in trades if not t["filled"]]
    fill_rate = len(fills) / n

    # Per-contract (sizing-independent). This is what the breakeven
    # identity operates on — two trades with the same pnl_per_contract
    # should count equally regardless of how many contracts filled.
    avg_fill_pc = (
        sum(t["pnl_per_contract"] for t in fills) / len(fills)
        if fills else 0.0
    )
    avg_miss_pc = (
        sum(t["pnl_per_contract"] for t in misses) / len(misses)
        if misses else 0.0
    )

    # Per-trade total-dollar averages — sanity-check context, not used
    # for the breakeven math (sizing variation would dominate).
    avg_fill_dollar = (
        sum(t["pnl"] for t in fills) / len(fills) if fills else 0.0
    )
    avg_miss_dollar = (
        sum(t["pnl"] for t in misses) / len(misses) if misses else 0.0
    )

    # Breakeven: f_be = -L / (W - L). Only defined when W > 0 and L < 0
    # — otherwise the strategy isn't on a breakeven curve at all (either
    # every outcome loses or every outcome wins, both degenerate).
    def _breakeven(W: float, L: float) -> float | None:
        if W > 0 and L < 0:
            return -L / (W - L)
        return None

    be_dollar = _breakeven(avg_fill_dollar, avg_miss_dollar)
    be_pc = _breakeven(avg_fill_pc, avg_miss_pc)

    return {
        "n": n, "n_fill": len(fills), "n_miss": len(misses),
        "fill_rate": fill_rate,
        "avg_fill_pc": avg_fill_pc, "avg_miss_pc": avg_miss_pc,
        "avg_fill_dollar": avg_fill_dollar, "avg_miss_dollar": avg_miss_dollar,
        "breakeven_dollar": be_dollar, "breakeven_pc": be_pc,
        "margin_dollar_pts": (
            (fill_rate - be_dollar) * 100 if be_dollar is not None else None
        ),
        "margin_pc_pts": (
            (fill_rate - be_pc) * 100 if be_pc is not None else None
        ),
        "total_net": sum(t["pnl"] for t in trades),
        "total_gross": sum(t.get("gross_pnl", 0.0) for t in trades),
        "total_fees": sum(t.get("fees", 0.0) for t in trades),
    }


def main():
    widen_alphas([BASELINE_ALPHA])
    all_synced = load_and_sync(SWEEP_DATES)

    single, _ = run_silent(
        all_synced, BASELINE_ALPHA,
        min_move=BASELINE_MIN_MOVE_CENTS / 100.0,
        min_move_to_fee_ratio=BASELINE_RATIO,
    )
    m = _compute_margin(single)

    print("=" * 72)
    print("Fill-rate margin vs breakeven")
    print("=" * 72)
    print(
        f"Baseline: α={BASELINE_ALPHA}  min_move={BASELINE_MIN_MOVE_CENTS}¢  "
        f"ratio={BASELINE_RATIO:.1f}x"
    )
    print(f"Window:   {SWEEP_DATES[0]} → {SWEEP_DATES[-1]}  "
          f"({m['n']} trades across cached games)")
    print()
    print(f"Trades:        {m['n']:,}   filled={m['n_fill']:,}  "
          f"unfilled={m['n_miss']:,}")
    print(f"Fill rate:     {m['fill_rate'] * 100:.2f}%")
    print()
    print("Per-trade totals (primary — net of fees, what lands in the DB):")
    print(f"  avg P&L when filled (target hit):      "
          f"${m['avg_fill_dollar']:+,.2f}")
    print(f"  avg P&L when unfilled (forced exit):   "
          f"${m['avg_miss_dollar']:+,.2f}")
    print()

    if m["breakeven_dollar"] is None:
        print("Per-trade breakeven undefined — filled/unfilled averages")
        print("aren't on a breakeven curve (one side never loses or wins).")
    else:
        be = m["breakeven_dollar"] * 100
        margin = m["margin_dollar_pts"]
        cur = m["fill_rate"] * 100
        print("Breakeven analysis (per-trade dollars):")
        print(f"  breakeven fill rate:   {be:.2f}%")
        print(f"  current fill rate:     {cur:.2f}%")
        print(f"  margin:                {margin:+.2f} pts")
        if margin > 0:
            print(
                f"  → {margin:.1f} pts of room above breakeven. "
                f"A drop to ~{be:.1f}% fill rate flips total EV to 0."
            )
        else:
            print(
                f"  → {-margin:.1f} pts BELOW breakeven. "
                f"Total EV is negative."
            )

    print()
    print("Per-contract P&L (reference — asymmetric because misses cluster")
    print("on cheap contracts where $500 budget buys many more units):")
    print(f"  avg P&L when filled:    ${m['avg_fill_pc']:+.4f}/contract")
    print(f"  avg P&L when unfilled:  ${m['avg_miss_pc']:+.4f}/contract")
    if m["breakeven_pc"] is not None:
        print(
            f"  per-contract breakeven fill rate: "
            f"{m['breakeven_pc'] * 100:.2f}%  (margin "
            f"{m['margin_pc_pts']:+.1f} pts) — see docstring"
        )

    print()
    print(f"Aggregate context: net=${m['total_net']:+,.0f}  "
          f"gross=${m['total_gross']:+,.0f}  fees=${m['total_fees']:+,.0f}")


if __name__ == "__main__":
    main()
