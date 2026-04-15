# LiveLine backtesting

```
backtests/
├── backtest.py               ← main CLI
├── core/                     ← infrastructure
├── reporting/                ← output formatting
├── sweeps/                   ← research scripts (one question each)
├── results/                  ← generated: cached games, synced CSVs
└── reports/                  ← generated: timestamped analysis outputs
```

If you're adding a new research question, add a file to `sweeps/`. Don't modify anything under `core/` or `reporting/` unless you're fixing a bug that affects every analysis.

---

## `core/` — infrastructure

| File | Purpose |
|---|---|
| `mlb.py` | MLB Stats API fetch. `PlayRecord` dataclass. Timestamp = `about.endTime` (scorer log time — *not* at-bat start). |
| `discovery.py` | Enumerate all Kalshi markets (moneyline / O/U / spread) for a game with line, flip flag, delta key. |
| `kalshi_sync.py` | Fill simulator. For each play, finds the Kalshi trade closest to `entry_ts = play_ts - ENTRY_OFFSET`, runs `_simulate_limit_order` over the clean window. Owns `set_entry_offset()` and `_pick_spread_specs_both_sides` (mirrors the live picker). |
| `enrichment.py` | Runs the WE engine on `PlayRecord`s to produce predicted deltas per market type. |
| `multi_market.py` | Candidate builder + execution. `_build_candidates` applies the same filters as the live picker (blowout, min-move, dead-market). `_execute_single` / `_execute_multi` rank by net-expected-profit. Uses `compute_gross` / `compute_fees` from `app/pnl.py`. |
| `cache.py` | Persists raw MLB + Kalshi trades under `results/cache/YYYY-MM-DD/<game_id>/` so sweeps can rerun without API calls. |
| `constants.py` | Re-exports from `app.constants`. |

## `reporting/` — output formatting

| File | Purpose |
|---|---|
| `reports.py` | Per-play tables, timing analysis, accuracy summary, stop-loss sweep, flagged-trade breakdowns. |
| `output.py` | Per-game CSV writers + trade-trace printer. `OUTPUT_DIR` is `backtests/results/`. |
| `report_io.py` | Timestamped-path helper + `capture_report` context manager. Tees stdout to `backtests/reports/backtest_*.txt`. |

## `sweeps/` — one question each

Every sweep imports from `sweeps._common` (see below). Run with `python -m backtests.sweeps.<name>`.

| File | Question | Output |
|---|---|---|
| `alpha.py` | Which alpha maximizes net P&L? | Table of α ∈ [0.30, 0.80] step 0.05: trades, fill%, avg-profit/fill, avg-loss/miss, net, $/game, worst. |
| `alpha_minmove.py` | 2D: which (α, min_move_cents) combo wins? What's the Pareto frontier? | Sorted 36-cell table + top-5 highlight + Pareto frontier summary. |
| `hr_alpha.py` | Does a lower HR-specific alpha raise total P&L? | HR n, HR fill%, HR net, total net, Δ vs α=0.65 baseline, worst day. |
| `offset_fill.py` | How does fill rate change with entry timing, by event type? | Matrix of offset (t−10s → t+10s) × event type (HR/2B/1B/BB/DP/OUT/K). CSV to `reports/`. |
| `analyze_losers.py` | Why do the worst-P&L games lose money? | Per-trade classification (never_reached / touched_but_missed / no_market_data / extreme_entry) across 3 losers vs 1 winner. Reads the latest `multi_market_single_trades_*.csv` from `reports/`. |

## `sweeps/_common.py` — shared sweep helpers

Pulled out because every sweep used to re-implement the same four things. Import from this module instead of duplicating:

- `widen_alphas(values=None)` — mutates `ALPHAS` in place on both `app.constants` and `backtests.core.constants` so `fill_sim` has data for every value tested. Order of widening vs imports doesn't matter (the mutation is in-place, `fill_sim` iterates at runtime).
- `load_raw_games(dates=None)` — reads cached MLB + Kalshi trades, runs `enrichment`, returns raw tuples.
- `sync_raw_games(raw)` — runs all three sync paths, stamps `game_tag` on every record, returns the combined synced list.
- `load_and_sync(dates=None)` — one-shot: `load_raw_games` + `sync_raw_games`. Use this unless you need to re-sync per iteration (only `offset_fill` does).
- `run_silent(all_synced, alpha, *, min_move, …)` — `run_comparison` with its "Fee filter killed N" stdout line swallowed.
- `stats_with_per_game(trades)` — returns `{n, fill_rate, avg_profit_fill, avg_loss_miss, net, per_game, worst}`.
- `DEFAULT_ALPHAS_SWEEP`, `DEFAULT_DATES`, `MAX_DOLLARS` — standard constants used across sweeps.

---

## Key findings (current session)

### Corrections that materially changed the baseline

1. **`about.startTime` → `about.endTime`** (`core/mlb.py`). Old default made `play_ts` median ~55s before the actual play; positive offsets were massive look-ahead cheats. Fix dropped 3-day net P&L from **$43,781 → $16,696 (−62%)**.

2. **`net_expected_profit` and `*_fee_est` at actual qty, not `bet_size=100`** (`app/trader.py::_recompute_expected_pnl`). DB was logging notional-100 values (e.g. $10.09 expected on an 18-contract trade that actually nets ~$1.81).

3. **Exit-window cap of `CLEAN_WINDOW_SECONDS` (45s)** — previously unbounded per-play cutoff let slow-AB plays use 55s+ exit windows; live never does. Cost ~$77/game.

4. **Both-sides spread evaluation** (`kalshi_sync._pick_spread_specs_both_sides`). Mirrors the live picker. Doubled spread candidate pool.

### Current honest baseline

**+$469/game** at α=0.65, min=3¢, t−5s entry, 45s exit, 36 games over 2026-04-08 to 2026-04-10. Worst game: −$247. Fill rate: 59.1%. Win rate: 65%.

### Tuning

- **α sweep** → peak at α=0.70 ($562/game); best risk-adjusted at α=0.65.
- **(α, min_move)** → α=0.65 / min=3¢ dominates previous default (α=0.60 / min=4¢) on every axis.
- **HR-specific alpha** → *does not help.* Noise across 44 HR trades.
- **Entry offset t−10 → t+10** → fill rate 64.5% → 16.1%. HRs structurally unwinnable at any latency.

### Loser diagnosis

In 3 net-losing games: 24–50% of trades hit markets with zero exit-window activity (vs 0% in the best winner). Drove the `is_market_stale` + `has_exit_liquidity` gates in the live picker.

---

## How to run a new sweep

1. Populate cache: `python -m backtests.backtest --from YYYY-MM-DD --to YYYY-MM-DD --multi-market` (once). Idempotent — already-cached games auto-skip.
2. Write `backtests/sweeps/<name>.py` using the `_common` helpers:
   ```python
   from backtests.sweeps._common import (
       widen_alphas, load_and_sync, run_silent, stats_with_per_game,
   )

   def main():
       widen_alphas()
       all_synced = load_and_sync()
       for cfg in CONFIGS:
           single, _ = run_silent(all_synced, cfg.alpha, min_move=cfg.min_move)
           s = stats_with_per_game(single)
           print(...)

   if __name__ == "__main__":
       main()
   ```
3. Run with `python -m backtests.sweeps.<name>`.

## Shared with the live picker

- `app/fees.py` — Kalshi fee schedule.
- `app/pnl.py` — `compute_gross`, `compute_fees`, `compute_net`. Single source of truth for P&L math.
- `app/line_selection.py` — `pick_ou_line`, `pick_spread_line`.

Candidate-building logic is still duplicated between `app/market_selector.py` and `backtests/core/multi_market.py`. A refactor into `app/picker.py` would eliminate drift between live and backtest; not done yet.
