# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LiveLine is an MLB win-expectancy engine wired to Kalshi live trading with a React frontend. A user opens a game, the backend streams "best trade per batting event" cards computed from `alpha * |WE delta| - spread`, and a tap fires a real IOC buy + resting limit sell on Kalshi.

Three top-level trees:

- `backend/` — FastAPI app (`app/`), historical backtest + sweep harness (`backtests/`), test suite (`tests/`), pregenerated WE tables (`data/`).
- `frontend/` — React 19 + Vite 8 + Tailwind 4 + Supabase-JS. Lives at `liveline.watch`.
- `eval/` — separate Vite app: offline P&L tracker against exported trade logs. Independent build, its own `package.json`.

## Commands

Backend runs in a local venv (`backend/venv/`). Use it explicitly, not a global `python`.

```bash
# Backend dev server — lifespan connects Kalshi + Supabase on startup
cd backend && venv/bin/uvicorn app.main:app --reload

# Tests (pytest discovery under backend/tests/)
cd backend && venv/bin/python -m pytest
cd backend && venv/bin/python -m pytest tests/test_engine.py -v
cd backend && venv/bin/python -m pytest tests/test_engine.py::test_name -v

# Backtest + sweeps
cd backend && venv/bin/python -m backtests.backtest --from 2026-04-08 --to 2026-04-10 --multi-market
cd backend && venv/bin/python -m backtests.sweeps.alpha    # one question per file

# Frontend
cd frontend && npm run dev     # Vite on 5173/5174/5175 (all allow-listed in CORS)
cd frontend && npm run build
cd frontend && npm run lint

# Eval P&L tracker (separate app)
cd eval && npm run dev
```

Env: `backend/.env` holds `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `KALSHI_ENV` (`prod` or demo), and Kalshi key material (either `KALSHI_{DEMO,PROD}_API_KEY_ID` + `KALSHI_{DEMO,PROD}_KEY_B64` or PEM files at `backend/kalshi-{demo,prod}-key.pem`). Frontend uses `frontend/.env.local` with `VITE_API_URL` and Supabase anon keys. `.env`, `.env.*`, and `*.pem` are gitignored globally — do not loosen.

## Architecture

### Backend (`backend/app/`)

`main.py` is the HTTP surface. Everything else is pure modules.

- **`engine.py`** — win-expectancy math. `GameState → get_win_expectancy()` via the JSON lookup in `backend/data/`. `compute_deltas()` returns a per-event `EventDelta` carrying ML, O/U, and spread shifts.
- **`market_selector.py`** — Kalshi market discovery (`discover_markets`), poll-driven ticker refresh (`refresh_markets`), per-tick best-trade recompute (`on_price_update`), SSE fan-out to connected frontends. Owns `DEFAULT_SETTINGS` (the frozen fallback; `Session.settings` is runtime authority). Filters: blowout, `min_move_cents` (flat absolute floor), `min_move_to_fee_ratio` (price-aware floor — target move ≥ N× per-contract round-trip fee), `stale_market_seconds`, `exit_slippage_cents`. Both min-move gates apply; a candidate must clear each. Picks O/U and spread lines dynamically from score state. `_pick_spread_markets_both_sides` pairs the home-side pick with the away-side ticker at the matching mirror line — do not re-run `pick_spread_line` on the away bucket (it's stored negated, which breaks the "closest above margin" rule and silently substitutes a non-mirror line). `_evaluate_market` always returns a dict: either a valid-candidate dict or a `{"active": False, "rejection_reason": ...}` dict consumed by the decision-log builder. `format_decision_log` is the per-tap rendering function `main.buy_event` calls after the Kalshi order fires.
- **`trader.py`** — `Trade`, `TradeGroup`, `Session` classes. One `Session` per `(user_id, game_id)`, lazily created on first `/buy` or `/stream` and flushed to Supabase when the game goes Final. `TradeGroup` is the atomic unit for one button tap — single-market mode is a group of 1, multi-market mode is a basket. State machine lives in the docstring at the top of `trader.py`. Per-user kill switch (`arm_kill_switch` / `/kill`) flattens every live position and blocks new buys. Sell-placement retries 3× with 500ms spacing (`_activate`) before panic-flattening via IOC — stable `_coid("s")` keeps retries idempotent on Kalshi's side.
- **`kalshi_client.py`** — wrapper around `pykalshi`. Hooks: `on_tick` drives `market_selector.on_price_update` + `trader.check_stop_losses`; `on_fill` dispatches websocket fill events directly to the owning `Trade` via `dispatch_websocket_fill`. Includes schema-drift patches for pykalshi 1.0.4.
- **`database.py`** — Supabase logging. Every public function is best-effort (try/except, returns None, never raises). A Supabase outage must not block a trade. Uses the SERVICE role key; end-user reads go through the anon-key client in the frontend.
- **`auth.py`** — verifies Supabase JWTs via `supabase.auth.get_user(token)`. Two-tier cache: 1h fresh TTL + stale-while-error so a Supabase outage doesn't knock authed sessions offline. SSE passes the JWT as `?token=` since EventSource can't set headers.
- **`pnl.py`**, **`fees.py`**, **`line_selection.py`** — shared math. `pnl.compute_net` is the single source of truth for P&L (gross − entry fee − exit fee); both live trading and backtests call it.
- **`constants.py`** — shared with backtests via re-export. Change here, not in duplicates.

Live data flow: Kalshi WS tick → `on_price_update` recomputes best trade per event → pushes to SSE queues in `market_selector` → frontend `EventSource(/stream/{game_id})`. MLB state is polled every 10s per game by `main._poll_mlb_state`, started on first SSE subscriber, stopped on last disconnect or on game Final. The same poll tick also calls `market_selector.refresh_markets` to diff-in any newly-listed Kalshi spread / O/U ladder rungs — without this the picker freezes on the initial ticker set and `pick_spread_line` pins on the tightest known line as the margin widens.

### Backtests (`backend/backtests/`)

Mirror of the live picker running against cached MLB + Kalshi data. Layout and sweep conventions are documented in `backend/backtests/README.md` — read it before adding analysis. In short:

- `core/` is infrastructure (don't edit unless fixing a cross-analysis bug).
- `reporting/` is output formatting (tables, CSVs, trade traces).
- `sweeps/` is "one file per research question," all importing from `sweeps/_common.py`.
- Candidate-building is duplicated between `app/market_selector.py` and `backtests/core/multi_market.py`. Keep them in sync when live filters change.
- `ENTRY_OFFSET` convention in `app/constants.py` is load-bearing for interpreting backtest results; don't flip its sign without checking with the user.

### Frontend (`frontend/src/`)

React Router in `main.jsx` — `App.jsx` is unused. `AuthContext.jsx` wraps everything in a Supabase session. `api.js` exposes `api.getTrades`, `api.buy`, etc., all JWT-authed through `authedFetch`. `pages/Trading.jsx` is the live trading surface; it opens an SSE connection to `/stream/{game_id}` and renders `EventButton` / `PositionCard` / `GroupedPositionCard`. `pages/Settings.jsx` edits per-user trading params via `GET|PUT /settings` (server whitelists keys — see `USER_SETTINGS_KEYS` in `main.py`).

### Supabase schema

`backend/supabase_schema.sql` is idempotent — re-running it is safe. Tables: `users`, `sessions`, `trades`, `orderbook_snapshots`. RLS is enabled; the backend writes via the service role (bypasses RLS). Only the whitelisted keys in `USER_SETTINGS_KEYS` ever enter `users.settings` — never put secrets there.

## Conventions

- **Shared constants go in `app/constants.py`.** Backtests re-export; live code imports directly. A change in one place lands everywhere.
- **`dry_run=True` is the default.** Trades simulate against live Kalshi prices but don't place real orders. Flipping requires an explicit settings update.
- **Never let Supabase failures raise.** `database.py` functions return None/default on error; callers assume that. Adding new DB helpers? Follow the same pattern.
- **MLB-day math goes through `game_state.mlb_today()`.** Kalshi ticker prefixes and MLB schedule queries must agree on the Pacific day boundary.
- **Kill-switch-armed users get a 503 on `/buy`.** Don't add trade-placing paths that bypass the check in `main.buy_event`.
- **Kalshi order fires before any observability write on `/buy`.** The decision log emission and the initial `db.log_trade` insert both happen AFTER `await asyncio.to_thread(_fire)` returns — stdout + Supabase RTT are not on the critical path between decision and order. Guarded by `tests/test_buy_ordering.py`; don't reorder them back.
- **`trade_info` carries values at two different notionals.** `entry_fee_est` / `exit_fee_est` / `net_expected_profit` are computed at `bet_size` (100) for ranking fairness across candidates; `total_ev` is at `est_qty` (the sizing-limited actual fill). `Trade._recompute_expected_pnl` rewrites the matching keys at real-fill quantity before the DB row is written. Downstream consumers that need fee numbers aligned with `total_ev` must recompute via `taker_fee` / `maker_fee` at `est_qty` (see `format_decision_log` for the pattern) — don't mix-and-match notionals.
- **NO-side sizing reads `book.yes`, not `book.no`.** `OrderbookManager.best_ask` is derived from `book.no` alone and returns None when only NO-side bids are empty. The local-book fast path in `_get_ask_levels` / `calculate_position_size` / `estimate_fill_qty` / `get_orderbook_snapshot` gates on the side-specific dict (YES needs `book.no` for asks; NO needs `book.yes` for asks) — a gate on `best_ask is not None` falsely skips the local read for NO-side queries against a fully-populated YES-side book.
- **`db.save_user_settings` is an UPSERT, not an UPDATE.** Legacy users who pre-date the `handle_new_auth_user` trigger don't have a `public.users` row; a plain UPDATE would silently affect 0 rows and every subsequent Session would seed from `DEFAULT_SETTINGS`. Keep the upsert.
- **Comments about *why*, not *what*.** The codebase leans heavily on docstring-level explanations where a design decision isn't obvious; mirror that style. Skip comments when the code is self-explanatory.
