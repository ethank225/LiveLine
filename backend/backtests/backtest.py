#!/usr/bin/env python3
"""
Backtest: compare model-predicted deltas to actual Kalshi price moves.

Usage:
    python backtests/backtest.py --date 2026-04-10
    python backtests/backtest.py --from 2026-04-07 --to 2026-04-10 --quiet
    python backtests/backtest.py --date 2026-04-10 --game-id 823482 --trace 3
    python backtests/backtest.py --date 2026-04-10 --mlb-only
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtests.mlb import get_games_for_date, pull_play_by_play
from backtests.enrichment import enrich_with_model
from backtests.constants import (
    WINDOW_SECONDS, ENTRY_OFFSET, DEFAULT_ALPHA, DEFAULT_MAX_DOLLARS, ALPHAS,
)
from backtests.kalshi_sync import (
    set_trace_mode, set_stop_loss_analysis,
    pull_kalshi_trades, sync_plays_with_trades,
    sync_plays_dynamic_ou, sync_plays_dynamic_spread,
)
from backtests.discovery import get_kalshi_client, find_kalshi_event_ticker, discover_game_markets
from backtests.reports import (
    print_play_log, print_play_gap_stats, print_stop_loss_analysis,
    write_stop_loss_csv, print_flagged_summary,
    print_accuracy_summary, print_timing_analysis,
)
from backtests.output import save_csv, print_trace, OUTPUT_DIR
from backtests.multi_market import (
    run_comparison, print_comparison, write_trades_csv,
)
from backtests.report_io import capture_report, timestamped_path
from backtests.cache import save_game_cache, load_game_cache, list_cached_games


def main():
    with capture_report("backtest") as report_path:
        _main_inner()
        print(f"\nReport saved: {report_path}")


def _main_inner():
    parser = argparse.ArgumentParser(description="Backtest LiveLine engine against historical data")
    parser.add_argument("--date", type=str, default=None,
                        help="Single date (YYYY-MM-DD). Defaults to yesterday.")
    parser.add_argument("--from", dest="from_date", type=str, default=None,
                        help="Start of date range (YYYY-MM-DD).")
    parser.add_argument("--to", dest="to_date", type=str, default=None,
                        help="End of date range (YYYY-MM-DD). Defaults to --from.")
    parser.add_argument("--game-id", type=int, default=None,
                        help="Specific MLB game ID (single game only).")
    parser.add_argument("--event-ticker", type=str, default=None,
                        help="Kalshi event ticker (e.g. KXMLBGAME-26APR101840AZPHI).")
    parser.add_argument("--market-ticker", type=str, default=None,
                        help="Specific Kalshi market ticker (backward compat).")
    parser.add_argument("--mlb-only", action="store_true",
                        help="Only pull MLB data, skip Kalshi price comparison.")
    parser.add_argument("--quiet", action="store_true",
                        help="Skip per-game play-by-play logs.")
    parser.add_argument("--trace", type=int, default=0, metavar="N",
                        help="Print walkthrough for the N highest-delta trades.")
    parser.add_argument("--stop-loss-analysis", action="store_true",
                        help="Run stop loss simulation across multiple levels.")
    parser.add_argument("--multi-market", action="store_true",
                        help="Run single-vs-multi-market comparison on the synced set.")
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA,
                        help=f"Alpha for single/multi-market comparison (default: {DEFAULT_ALPHA}).")
    parser.add_argument("--max-dollars", type=float, default=DEFAULT_MAX_DOLLARS,
                        help=f"Per-play budget for comparison (default: ${DEFAULT_MAX_DOLLARS:.0f}).")
    parser.add_argument("--min-move-cents", type=int, default=4,
                        help="Skip trades whose expected entry→target move is "
                             "below N cents (mirrors live min_move_cents; default 4).")
    parser.add_argument("--no-fees", action="store_true",
                        help="Disable Kalshi fee deduction (for A/B against the "
                             "old fee-free backtest). Default: fees on.")
    parser.add_argument("--use-cached", action="store_true",
                        help="Skip MLB+Kalshi API calls; reload plays/trades from "
                             "results/cache/ and rerun model + fill simulation.")
    args = parser.parse_args()

    if args.use_cached and args.mlb_only:
        print("Note: --mlb-only is implied by --use-cached when no Kalshi cache is present.")

    if args.multi_market and args.alpha not in ALPHAS:
        print(f"Warning: alpha={args.alpha} is not in simulated ALPHAS={ALPHAS}. "
              f"Fill simulation will not have data for this alpha.")

    if args.trace:
        set_trace_mode(True)
    if args.stop_loss_analysis:
        set_stop_loss_analysis(True)

    # --- Build list of dates ---
    if args.from_date:
        d_start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        d_end = (datetime.strptime(args.to_date, "%Y-%m-%d").date()
                 if args.to_date else d_start)
        dates = []
        d = d_start
        while d <= d_end:
            dates.append(d)
            d += timedelta(days=1)
    elif args.date:
        dates = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        dates = [date.today() - timedelta(days=1)]

    print(f"Backtesting {len(dates)} day(s): "
          f"{dates[0].isoformat()} — {dates[-1].isoformat()}")
    print(f"Entry offset: {ENTRY_OFFSET}s before event | "
          f"Exit window: {WINDOW_SECONDS}s after event")

    # --- Init Kalshi client ---
    kalshi_client = None
    if args.use_cached:
        print("Using cached MLB + Kalshi data (skipping all API calls)")
    elif not args.mlb_only:
        kalshi_client = get_kalshi_client()
        if kalshi_client:
            env = os.getenv("KALSHI_ENV", "demo")
            print(f"Kalshi connected ({env})")
        else:
            print("Kalshi credentials not available — running MLB-only")

    # --- Collect across all games ---
    all_synced: list[dict] = []
    total_games = 0
    total_plays = 0
    games_with_kalshi = 0

    for target in dates:
        if args.use_cached:
            if args.game_id:
                game_ids = [args.game_id]
            else:
                game_ids = list_cached_games(target)
            if not game_ids:
                print(f"\n{target.isoformat()}: no cached games found in "
                      f"{OUTPUT_DIR / 'cache' / target.isoformat()}")
                continue
        else:
            date_str = target.strftime("%m/%d/%Y")
            if args.game_id:
                game_ids = [args.game_id]
            else:
                games = get_games_for_date(date_str)
                if not games:
                    print(f"\n{target.isoformat()}: no completed games")
                    continue
                game_ids = [g["game_id"] for g in games]

        print(f"\n{'=' * 70}")
        print(f"{target.isoformat()} — {len(game_ids)} game(s)")
        print(f"{'=' * 70}")

        for game_id in game_ids:
            total_games += 1

            # --- Load plays + trades + market specs (cached or live) ---
            cached_specs: list = []
            cached_trades: dict[str, list[tuple[int, float]]] = {}

            if args.use_cached:
                loaded = load_game_cache(target, game_id)
                if loaded is None:
                    print(f"  [{game_id}] no cache entry — skipping")
                    continue
                game_info, records, cached_specs, cached_trades = loaded
            else:
                try:
                    records, game_info = pull_play_by_play(game_id)
                except Exception as e:
                    print(f"  [{game_id}] Failed to pull MLB data: {e}")
                    continue

            total_plays += len(records)
            tag = f"{game_info['away_abbr']}@{game_info['home_abbr']}"

            enrich_with_model(records)

            if not args.quiet:
                print_play_log(records, game_info)

            # --- Sync against Kalshi markets ---
            game_synced: list[dict] = []
            home_abbr = game_info["home_abbr"].upper()
            away_abbr = game_info["away_abbr"].upper()

            # Collected during a live run for caching at end-of-game.
            run_specs: list = []
            run_trades: dict[str, list[tuple[int, float]]] = {}

            if args.use_cached:
                ml_specs = [s for s in cached_specs if s.market_type == "moneyline"]
                ou_specs = [s for s in cached_specs if s.market_type == "over_under"]
                spread_specs = [s for s in cached_specs if s.market_type == "spread"]

                for spec in ml_specs:
                    trades = cached_trades.get(spec.ticker)
                    if trades:
                        synced = sync_plays_with_trades(records, trades, spec=spec)
                        game_synced.extend(synced)
                        print(f"  [{tag}] {spec.label}: {len(synced)} plays "
                              f"({len(trades)} trades, cached)")

                if ou_specs:
                    ou_trades = {sp.ticker: cached_trades[sp.ticker]
                                 for sp in ou_specs if sp.ticker in cached_trades}
                    if ou_trades:
                        synced = sync_plays_dynamic_ou(records, ou_specs, ou_trades)
                        game_synced.extend(synced)
                        n_trades = sum(len(t) for t in ou_trades.values())
                        print(f"  [{tag}] O/U dynamic: {len(synced)} plays "
                              f"({len(ou_trades)} markets, {n_trades} trades, cached)")

                if spread_specs:
                    spread_trades = {sp.ticker: cached_trades[sp.ticker]
                                     for sp in spread_specs if sp.ticker in cached_trades}
                    if spread_trades:
                        synced = sync_plays_dynamic_spread(records, spread_specs, spread_trades)
                        game_synced.extend(synced)
                        n_trades = sum(len(t) for t in spread_trades.values())
                        print(f"  [{tag}] SPR dynamic: {len(synced)} plays "
                              f"({len(spread_trades)} markets, {n_trades} trades, cached)")

                if not (ml_specs or ou_specs or spread_specs):
                    print(f"  [{tag}] {len(records)} plays (no cached Kalshi data)")

            elif kalshi_client and not args.mlb_only:
                day_start = int(datetime(target.year, target.month, target.day,
                                         tzinfo=timezone.utc).timestamp())
                day_end = day_start + 36 * 3600

                if args.market_ticker:
                    is_away = args.market_ticker.rsplit("-", 1)[-1].upper() == away_abbr
                    trades = pull_kalshi_trades(kalshi_client, args.market_ticker, day_start, day_end)
                    if trades:
                        run_trades[args.market_ticker] = trades
                        synced = sync_plays_with_trades(records, trades, is_away_contract=is_away)
                        game_synced.extend(synced)
                        side = "away→flipped" if is_away else "home"
                        print(f"  [{tag}] {len(synced)} plays ({len(trades)} trades, {side})")

                else:
                    evt = args.event_ticker
                    if not evt:
                        evt = find_kalshi_event_ticker(kalshi_client, target, home_abbr, away_abbr)

                    if evt:
                        specs = discover_game_markets(kalshi_client, evt, home_abbr, away_abbr)
                        run_specs.extend(specs)
                        ml_specs = [s for s in specs if s.market_type == "moneyline"]
                        ou_specs = [s for s in specs if s.market_type == "over_under"]
                        spread_specs = [s for s in specs if s.market_type == "spread"]

                        for spec in ml_specs:
                            trades = pull_kalshi_trades(kalshi_client, spec.ticker, day_start, day_end)
                            if trades:
                                run_trades[spec.ticker] = trades
                                synced = sync_plays_with_trades(records, trades, spec=spec)
                                game_synced.extend(synced)
                                print(f"  [{tag}] {spec.label}: {len(synced)} plays ({len(trades)} trades)")

                        if ou_specs:
                            ou_trades: dict[str, list] = {}
                            for sp in ou_specs:
                                tr = pull_kalshi_trades(kalshi_client, sp.ticker, day_start, day_end)
                                if tr:
                                    ou_trades[sp.ticker] = tr
                                    run_trades[sp.ticker] = tr
                            if ou_trades:
                                synced = sync_plays_dynamic_ou(records, ou_specs, ou_trades)
                                game_synced.extend(synced)
                                n_trades = sum(len(t) for t in ou_trades.values())
                                print(f"  [{tag}] O/U dynamic: {len(synced)} plays "
                                      f"({len(ou_trades)} markets, {n_trades} trades)")

                        if spread_specs:
                            spread_trades: dict[str, list] = {}
                            for sp in spread_specs:
                                tr = pull_kalshi_trades(kalshi_client, sp.ticker, day_start, day_end)
                                if tr:
                                    spread_trades[sp.ticker] = tr
                                    run_trades[sp.ticker] = tr
                            if spread_trades:
                                synced = sync_plays_dynamic_spread(records, spread_specs, spread_trades)
                                game_synced.extend(synced)
                                n_trades = sum(len(t) for t in spread_trades.values())
                                print(f"  [{tag}] SPR dynamic: {len(synced)} plays "
                                      f"({len(spread_trades)} markets, {n_trades} trades)")
                    else:
                        print(f"  [{tag}] no Kalshi event found")
            else:
                print(f"  [{tag}] {len(records)} plays (MLB only)")

            if game_synced:
                games_with_kalshi += 1
                game_tag = f"{game_id} {tag}"
                for r in game_synced:
                    r["game_tag"] = game_tag
                all_synced.extend(game_synced)

            save_csv(records, game_info, game_synced)

            # Persist raw inputs so future runs can replay with --use-cached.
            if not args.use_cached:
                save_game_cache(target, game_id, game_info, records,
                                run_specs, run_trades)

    # --- Combined summary ---
    print(f"\n{'=' * 70}")
    print(f"COMBINED RESULTS — {total_games} games, {total_plays} plays")
    print(f"{'=' * 70}")

    if all_synced:
        print(f"Games with Kalshi data: {games_with_kalshi}")
        print_play_gap_stats(all_synced)
        print_accuracy_summary(all_synced)
        print_timing_analysis(all_synced)
        if args.stop_loss_analysis:
            print_stop_loss_analysis(all_synced)
            sl_path = timestamped_path("stop_loss_events", "csv")
            write_stop_loss_csv(all_synced, sl_path)
            print(f"Saved stop-loss events: {sl_path}")
            print_flagged_summary(all_synced)
        if args.multi_market:
            single_trades, multi_trades = run_comparison(
                all_synced, args.alpha, args.max_dollars,
                min_move=args.min_move_cents / 100.0,
                fees_on=not args.no_fees,
            )
            print_comparison(single_trades, multi_trades,
                             args.alpha, args.max_dollars)
            single_path = timestamped_path("multi_market_single_trades", "csv")
            multi_path = timestamped_path("multi_market_multi_trades", "csv")
            write_trades_csv(single_trades, single_path)
            write_trades_csv(multi_trades, multi_path)
            print(f"\nSaved single-mode trades: {single_path}")
            print(f"Saved multi-mode trades:  {multi_path}")
        if args.trace:
            print_trace(all_synced, args.trace)
    else:
        print("No Kalshi price data synced.")

    print("\nDone.")


if __name__ == "__main__":
    main()
