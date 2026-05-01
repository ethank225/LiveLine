#!/usr/bin/env python3
"""
Game-level diagnostic runner.

    python -m backtests.sweeps.analyze_games --mode bad
    python -m backtests.sweeps.analyze_games --mode good
    python -m backtests.sweeps.analyze_games --mode both    # default

All analytical logic lives in `backtests.sweeps._game_analysis`. This file
just wires helpers into a section sequence per mode and prints. Adding a
new mode (e.g. `--mode tied`) means importing more from `_game_analysis`,
not duplicating bucket math.
"""

from __future__ import annotations

import argparse

from backtests.sweeps import _game_analysis as ga


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------

def run_bad(games, trades_enriched):
    s = ga.print_section_header

    s(1, "Worst 15 games (lowest net P&L)")
    ga.print_table(ga.top_n_games(games, n=15, ascending=True))

    s(2, "P&L distribution buckets")
    df = ga.pnl_distribution_buckets(games)
    ga.print_table(df)
    print(f"\n  Total games: {len(games)} (sanity: bucket sum = {df['n_games'].sum()})")

    s(3, "Final-score-margin analysis")
    df = ga.score_margin_buckets(games)
    ga.print_table(df)
    print(f"\n  Total games: {len(games)} (sanity: bucket sum = {df['n_games'].sum()})")

    s(4, "Lead-volatility analysis (margin σ across plays)")
    df = ga.lead_volatility_buckets(games)
    ga.print_table(df)
    n_with_vol = games["lead_vol"].notna().sum()
    print(f"\n  Games with lead-vol data: {n_with_vol}/{len(games)} "
          f"(sanity: bucket sum = {df['n_games'].sum()})")

    s(5, "Trade-count analysis")
    df = ga.trade_count_buckets(games)
    ga.print_table(df)
    print(f"\n  Total games: {len(games)} (sanity: bucket sum = {df['n_games'].sum()})")

    s(6, "Fill-rate analysis")
    df = ga.fill_rate_buckets(games)
    ga.print_table(df)
    print(f"\n  Total games: {len(games)} (sanity: bucket sum = {df['n_games'].sum()})")

    s(7, "Fee traps (gross > $0 but net < -$300)")
    df = ga.fee_trap_games(games)
    ga.print_table(df)
    print(f"\n  Fee-trap games: {len(df)}")

    s(8, "Worst 30 individual trades")
    ga.print_table(ga.top_n_individual_trades(trades_enriched, n=30, ascending=True),
                   max_colwidth=30)

    s(9, "Bottom-20 game characteristics")
    bottom_ids = list(games.nsmallest(20, "net_pnl")["game_id"])
    chars = ga.game_characteristics_summary(games, trades_enriched, bottom_ids)
    for k, v in chars.items():
        print(f"  {k:>22}  {v}")

    s(10, "Filter audit on worst 10 games")
    worst10_ids = list(games.nsmallest(10, "net_pnl")["game_id"])
    audit = ga.filter_audit(trades_enriched, worst10_ids)
    print(f"  Trades in worst 10 games: {audit['n_trades']}")
    print(f"  Passed min_move ({int(ga.DEFAULT_MIN_MOVE * 100)}¢): "
          f"{audit['passed_min_move']} "
          f"({100 * audit['passed_min_move'] / max(audit['n_trades'], 1):.1f}%)")
    print(f"    of those, net-negative:  {audit['passed_min_move_neg']}")
    print(f"  Passed min_move/fee ratio (≥{ga.DEFAULT_MIN_MOVE_TO_FEE_RATIO}): "
          f"{audit['passed_fee_ratio']} "
          f"({100 * audit['passed_fee_ratio'] / max(audit['n_trades'], 1):.1f}%)")
    print(f"    of those, net-negative:  {audit['passed_fee_ratio_neg']}")
    if audit["n_losers_passing_both"]:
        print(f"\n  Losing trades that passed both filters: "
              f"{audit['n_losers_passing_both']}")
        print(f"  Avg |predicted - actual| gap: {audit['avg_pred_actual_gap']}")
    late = ga.late_inning_close_audit(trades_enriched, worst10_ids)
    if late.get("n_with_state"):
        print(f"  Of losing trades with cached state, fired late-inning "
              f"+ close-margin (inning ≥ 9, |margin| ≤ 1): "
              f"{late['n_late_close']}/{late['n_with_state']} "
              f"({late['pct_late_close']}%) totaling "
              f"{late['total_late_close_pnl']:+.2f}")

    s(11, "Findings summary (bad)")
    print(ga.synthesize_bad_game_findings(games, trades_enriched))


def run_good(games, trades_enriched):
    s = ga.print_section_header

    s(1, "Best 15 games (highest net P&L)")
    ga.print_table(ga.top_n_games(games, n=15, ascending=False))

    s(2, "P&L distribution buckets (full distribution for context)")
    df = ga.pnl_distribution_buckets(games)
    ga.print_table(df)
    print(f"\n  Total games: {len(games)} (sanity: bucket sum = {df['n_games'].sum()})")

    s(3, "Final-score-margin analysis (which margins yield the best games?)")
    df = ga.score_margin_buckets(games)
    ga.print_table(df)
    # For good mode it's helpful to also see best-game-per-bucket; the
    # table already has worst_game/worst_net, but adding best is a one-shot
    # amend without bloating the helper signature.
    g = games.copy()
    g["final_margin"] = (g["home_final"] - g["away_final"]).abs()
    g["bucket"] = __import__("pandas").cut(
        g["final_margin"], bins=ga.FINAL_MARGIN_BINS, labels=ga.FINAL_MARGIN_LABELS,
    )
    print("\n  Best game per margin bucket:")
    for lbl in ga.FINAL_MARGIN_LABELS:
        sub = g[g["bucket"] == lbl]
        if len(sub):
            best = sub.nlargest(1, "net_pnl").iloc[0]
            print(f"    {lbl:<10}  {best['game_tag']:<20}  net={best['net_pnl']:+.2f}")

    s(4, "Lead-volatility analysis")
    df = ga.lead_volatility_buckets(games)
    ga.print_table(df)
    n_with_vol = games["lead_vol"].notna().sum()
    print(f"\n  Games with lead-vol data: {n_with_vol}/{len(games)} "
          f"(sanity: bucket sum = {df['n_games'].sum()})")

    s(5, "Trade-count analysis")
    df = ga.trade_count_buckets(games)
    ga.print_table(df)
    print(f"\n  Total games: {len(games)} (sanity: bucket sum = {df['n_games'].sum()})")

    s(6, "Fill-rate analysis")
    df = ga.fill_rate_buckets(games)
    ga.print_table(df)
    print(f"\n  Total games: {len(games)} (sanity: bucket sum = {df['n_games'].sum()})")

    s(7, "Profit concentration (games with net > $1000)")
    df = ga.profit_concentration_games(games, net_threshold=1000.0)
    ga.print_table(df)
    print(f"\n  Big-win games: {len(df)} carrying "
          f"{df['net'].sum() if len(df) else 0:+.2f} of net P&L")

    s(8, "Best 30 individual trades")
    ga.print_table(ga.top_n_individual_trades(trades_enriched, n=30, ascending=False),
                   max_colwidth=30)

    s(9, "Top-20 game characteristics")
    top_ids = list(games.nlargest(20, "net_pnl")["game_id"])
    chars = ga.game_characteristics_summary(games, trades_enriched, top_ids)
    for k, v in chars.items():
        print(f"  {k:>22}  {v}")

    s(10, "Trade-pattern audit on best 10 games")
    best10_ids = list(games.nlargest(10, "net_pnl")["game_id"])
    audit = ga.filter_audit(trades_enriched, best10_ids)
    print(f"  Trades in best 10 games: {audit['n_trades']}")
    print(f"  Passed min_move: {audit['passed_min_move']} "
          f"of which net-negative: {audit['passed_min_move_neg']}")
    print(f"  Passed fee-ratio: {audit['passed_fee_ratio']} "
          f"of which net-negative: {audit['passed_fee_ratio_neg']}")
    late = ga.late_inning_close_audit(trades_enriched, best10_ids)
    if late.get("n_with_state"):
        print(f"  Late-inning + close-margin losers in best 10 games: "
              f"{late['n_late_close']}/{late['n_with_state']} "
              f"({late['pct_late_close']}%) totaling "
              f"{late['total_late_close_pnl']:+.2f}. Even the best games carry "
              f"some volatile-tail noise — gating these states would lose some "
              f"signal too.")

    s(11, "Findings summary (good)")
    print(ga.synthesize_good_game_findings(games, trades_enriched))


def run_comparative(games, trades_enriched):
    """Bottom-20 vs top-20 side by side. Only emitted in `both` mode to
    avoid duplicating it under each individual report."""
    ga.print_section_header(99, "Comparative summary — bottom-20 vs top-20 characteristics")
    bottom_ids = list(games.nsmallest(20, "net_pnl")["game_id"])
    top_ids = list(games.nlargest(20, "net_pnl")["game_id"])
    df = ga.compare_subsets(
        games, trades_enriched, bottom_ids, top_ids,
        label_a="bottom_20", label_b="top_20",
    )
    ga.print_table(df)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("bad", "good", "both"), default="both")
    args = ap.parse_args()

    games, trades = ga.load_latest_reports()
    print(f"Games: {len(games)}  |  Trades: {len(trades)}")

    cached_states = ga.load_cached_game_state(list(games["game_id"]))
    games = ga.attach_per_game_lead_volatility(games, cached_states)
    trades_enriched = ga.enrich_trades_with_state(trades, cached_states)

    if args.mode in ("bad", "both"):
        if args.mode == "both":
            print(f"\n{'#' * 100}")
            print("# BAD-GAME ANALYSIS")
            print(f"{'#' * 100}")
        run_bad(games, trades_enriched)

    if args.mode in ("good", "both"):
        if args.mode == "both":
            print(f"\n\n{'#' * 100}")
            print("# GOOD-GAME ANALYSIS")
            print(f"{'#' * 100}")
        run_good(games, trades_enriched)

    if args.mode == "both":
        print(f"\n\n{'#' * 100}")
        print("# COMPARATIVE SUMMARY")
        print(f"{'#' * 100}")
        run_comparative(games, trades_enriched)


if __name__ == "__main__":
    main()
