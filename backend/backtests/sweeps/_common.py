"""Shared helpers for sweep scripts.

Every sweep does the same four things: widen `ALPHAS` so `fill_sim` has
data for every value it tests, load cached games and run all three sync
paths, run `run_comparison` with stdout silenced for the "Fee filter
killed" noise, and aggregate per-game P&L alongside the standard
summary. Centralized here so a sweep script is just "the question" —
`DATES`, `ALPHA_GRID`, and a `main()` that builds a table.

`widen_alphas` mutates the list in place via `[:] =`. Every consumer of
`from constants import ALPHAS` holds a reference to the same list
object, so in-place mutation propagates. The order of widening vs
importing doesn't matter — `fill_sim` iterates `ALPHAS` at simulation
time, not import time.
"""

import contextlib
import io
from collections import defaultdict
from datetime import date

from app import constants as app_consts
from backtests.core import constants as bt_consts
from backtests.core.cache import list_cached_games, load_game_cache
from backtests.core.enrichment import enrich_with_model
from backtests.core.kalshi_sync import (
    sync_plays_dynamic_ou,
    sync_plays_dynamic_spread,
    sync_plays_with_trades,
)
from backtests.core.multi_market import _summarize, run_comparison

# Standard sweep range: 0.30..0.80 step 0.05. Covers every α sweep.
DEFAULT_ALPHAS_SWEEP = [round(0.30 + 0.05 * i, 2) for i in range(11)]

# 3-day cached window most sweeps target by default.
DEFAULT_DATES: list[date] = [
    date(2026, 4, 8), date(2026, 4, 9), date(2026, 4, 10),
]

# Same as the live picker default; every sweep uses this.
MAX_DOLLARS = 500.0


def widen_alphas(values: list[float] | None = None) -> list[float]:
    """Mutate `ALPHAS` in place on both the app and backtests copies so
    fill simulation produces data for every sweep value. Returns the
    resolved list so callers can iterate it directly."""
    resolved = list(values) if values is not None else list(DEFAULT_ALPHAS_SWEEP)
    app_consts.ALPHAS[:] = resolved
    bt_consts.ALPHAS[:] = resolved
    return resolved


def load_raw_games(dates: list[date] | None = None) -> list[tuple]:
    """Read cached MLB + Kalshi inputs (no sync yet). Returned as a list
    of `(game_id, game_info, records, cached_specs, cached_trades)` tuples
    so callers that re-sync per offset (e.g. offset_fill) don't have to
    re-read the JSON every iteration. `enrich_with_model` has already
    been applied to each `records` list."""
    dates = dates or DEFAULT_DATES
    raw: list[tuple] = []
    for target in dates:
        for gid in list_cached_games(target):
            loaded = load_game_cache(target, gid)
            if loaded is None:
                continue
            game_info, records, cached_specs, cached_trades = loaded
            enrich_with_model(records)
            raw.append((gid, game_info, records, cached_specs, cached_trades))
    return raw


def sync_raw_games(raw_games: list[tuple]) -> list[dict]:
    """Run all three sync paths over preloaded raw games, stamp
    `game_tag`, return the combined synced list. Kept as a standalone
    step so the per-offset sweep can re-sync without re-reading cache."""
    all_synced: list[dict] = []
    for gid, game_info, records, cached_specs, cached_trades in raw_games:
        game_synced: list[dict] = []
        ml_specs = [s for s in cached_specs if s.market_type == "moneyline"]
        ou_specs = [s for s in cached_specs if s.market_type == "over_under"]
        spread_specs = [s for s in cached_specs if s.market_type == "spread"]

        for spec in ml_specs:
            trades = cached_trades.get(spec.ticker)
            if trades:
                game_synced.extend(
                    sync_plays_with_trades(records, trades, spec=spec)
                )
        if ou_specs:
            ou_trades = {
                sp.ticker: cached_trades[sp.ticker]
                for sp in ou_specs if sp.ticker in cached_trades
            }
            if ou_trades:
                game_synced.extend(
                    sync_plays_dynamic_ou(records, ou_specs, ou_trades)
                )
        if spread_specs:
            spread_trades = {
                sp.ticker: cached_trades[sp.ticker]
                for sp in spread_specs if sp.ticker in cached_trades
            }
            if spread_trades:
                game_synced.extend(
                    sync_plays_dynamic_spread(records, spread_specs, spread_trades)
                )

        tag = f"{gid} {game_info['away_abbr']}@{game_info['home_abbr']}"
        for r in game_synced:
            r["game_tag"] = tag
        all_synced.extend(game_synced)
    return all_synced


def load_and_sync(dates: list[date] | None = None, *, verbose: bool = True) -> list[dict]:
    """One-shot: read cache + run sync + stamp game_tag. Convenience for
    sweeps that don't vary entry offset (most of them)."""
    raw = load_raw_games(dates)
    synced = sync_raw_games(raw)
    if verbose:
        print(f"Loaded {len(raw)} cached games, {len(synced)} synced records\n")
    return synced


def run_silent(
    all_synced: list[dict],
    alpha: float,
    *,
    min_move: float,
    max_dollars: float = MAX_DOLLARS,
    fees_on: bool = True,
    blowout_filter: bool = True,
) -> tuple[list[dict], list[dict]]:
    """run_comparison with the "Fee filter killed N" stdout line
    swallowed. Returns (single_trades, multi_trades)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return run_comparison(
            all_synced, alpha, max_dollars,
            min_move=min_move, fees_on=fees_on, blowout_filter=blowout_filter,
        )


def stats_with_per_game(trades: list[dict]) -> dict:
    """Extend _summarize with per-game P&L aggregation. Returns the
    exact shape sweep tables consume: `n`, `fill_rate`, `avg_profit_fill`,
    `avg_loss_miss`, `net`, `per_game`, `worst`."""
    s = _summarize(trades)
    by_game: dict[str, float] = defaultdict(float)
    for t in trades:
        by_game[t["rec"].get("game_tag", "")] += t["pnl"]
    n_games = len(by_game) or 1
    return {
        "n": s["n"],
        "fill_rate": s["fill_rate"],
        "avg_profit_fill": s["avg_profit_fill"],
        "avg_loss_miss": s["avg_loss_miss"],
        "net": s["total_pnl"],
        "per_game": sum(by_game.values()) / n_games,
        "worst": min(by_game.values()) if by_game else 0.0,
    }
