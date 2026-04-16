# LiveLine

Live trading against Kalshi MLB prediction markets, driven by a baseball win-expectancy engine.

LiveLine watches every pitch of an MLB game, scores each possible at-bat outcome (single, double, HR, K, etc.) against its effect on win probability + over/under + spread contracts on Kalshi, and surfaces the one trade per batting event that currently has positive expected value. When you tap a button, the backend places a real IOC buy + resting limit sell on Kalshi and tracks the position to exit.

Live at [liveline.watch](https://liveline.watch).

---

## How it works

```
       MLB Stats API                        Kalshi
            │                                  │
            │ poll every 10s                   │ websocket ticks + fills
            ▼                                  ▼
     ┌──────────────┐    deltas     ┌────────────────────┐
     │  engine.py   │──────────────▶│ market_selector.py │
     │ (WE tables)  │               │  (EV = α·|Δ| − spread)
     └──────────────┘               └──────────┬─────────┘
                                               │ best trade per event
                                               ▼
                                        ┌──────────────┐
                                        │  trader.py   │──▶ Kalshi orders
                                        │ Trade/Group/ │    (IOC buy +
                                        │  Session FSM │     resting sell)
                                        └──────┬───────┘
                                               │ SSE
                                               ▼
                                     React frontend (Trading page)
```

1. **Win-expectancy engine** (`backend/app/engine.py`) looks up home-team win probability for the current `(inning, half, outs, runners, score_diff)` state from a precomputed Tango-style table (`backend/data/win_expectancy.json`). It computes the *delta* — how much WE shifts for each possible batting outcome (HR, 2B, 1B, BB, DP, OUT, K) — plus parallel deltas for over/under and spread contracts, using a run-distribution convolution.

2. **Market selector** (`backend/app/market_selector.py`) discovers all three Kalshi contract types for the game (moneyline `KXMLBGAME-*`, over/under `KXMLBTOTAL-*`, spread `KXMLBSPREAD-*`), dynamically picks the O/U and spread *lines* closest to current game state, then for each event scores every candidate market by `EV = α · |Δ| − spread` and returns the best. Filters drop markets that are stale, dead, blown-out, or have insufficient exit liquidity.

3. **Trader** (`backend/app/trader.py`) executes trades as a state machine — `pending → undo_window → open → {filled, stopped, expired, canceled, …}`. A `Trade` owns its full lifecycle: IOC buy at ask, resting limit sell at target, stop loss armed against websocket ticks, 45-second clean-window timeout that force-exits if the target isn't hit. One user tap = one `TradeGroup` (1 trade in single-market mode, N in multi-market basket mode).

4. **Frontend** (`frontend/`) connects via Server-Sent Events to `/stream/{game_id}` and renders a button per batting event, recolored every tick with current EV + entry + target. Tapping fires `/buy`. Positions show live P&L against Kalshi's current bid.

Live Kalshi data pushes the whole loop: every websocket tick triggers a recompute for every subscribed game and fans out to connected clients. MLB game state is polled every 10 seconds per active game. The same poll step also runs `market_selector.refresh_markets` to pick up any new Kalshi spread / O/U lines listed mid-game, so the dynamic line picker isn't frozen on the opening ticker set.

---

## Repo layout

```
backend/
  app/                     FastAPI application
    main.py                HTTP + SSE endpoints
    engine.py              Win-expectancy math
    market_selector.py     Market discovery, EV scoring, SSE fan-out
    trader.py              Trade/TradeGroup/Session FSM, kill switch
    kalshi_client.py       pykalshi wrapper (WS + orders + orderbook)
    database.py            Supabase logging (best-effort, never raises)
    auth.py                Supabase JWT verification + cache
    game_state.py          MLB Stats API fetch
    line_selection.py      Dynamic O/U + spread line picking
    pnl.py, fees.py        Shared P&L math — live + backtest call these
    constants.py           Single source of truth; backtests re-export
    bet_label.py           Display labels for Kalshi contracts
  backtests/               Historical replay + parameter sweeps
    backtest.py            CLI entry point
    core/                  Fill sim, MLB / Kalshi pull, enrichment
    reporting/             CSVs + printed tables
    sweeps/                One research question per file
  tests/                   pytest suite
  data/                    win_expectancy.json, run_distribution.json
  generate_we_table.py     Rebuilds win_expectancy.json from Tango constants
  scripts/                 Ad-hoc probes (find a ticker, list markets, …)
  supabase_schema.sql      Idempotent schema — paste into Supabase SQL editor
  requirements.txt
frontend/                  React 19 + Vite 8 + Tailwind 4 (production app)
  src/pages/               Landing, GameSelector, Trading, Settings
  src/components/          ButtonGrid, EventButton, PositionCard, Diamond, …
  src/AuthContext.jsx      Supabase session wrapper
  src/api.js               JWT-authed fetch wrapper
eval/                      Independent Vite app: offline P&L tracker
```

---

## Prerequisites

- **Python 3.11+** with a virtualenv at `backend/venv/`.
- **Node 20+** and `npm` for `frontend/` and `eval/`.
- A **Supabase project** with the schema in `backend/supabase_schema.sql` applied, and Google OAuth configured as a sign-in provider.
- **Kalshi API credentials** — API key ID + RSA private key PEM for demo and/or prod. Request from Kalshi.
- **MLB Stats API** — no auth needed, used via the `MLB-StatsAPI` Python package.

---

## Setup

### Backend

```bash
cd backend
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Drop Kalshi key PEMs at `backend/kalshi-demo-key.pem` and/or `backend/kalshi-prod-key.pem`, then create `backend/.env`:

```ini
# Supabase (service role — bypasses RLS, backend only)
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service-role-key>

# Kalshi
KALSHI_ENV=demo                  # or "prod" for real money
KALSHI_DEMO_API_KEY_ID=<uuid>
KALSHI_PROD_API_KEY_ID=<uuid>

# Optional — base64-encoded PEMs for hosts without disk (Railway, Fly)
# KALSHI_DEMO_KEY_B64=<base64 of demo PEM>
# KALSHI_PROD_KEY_B64=<base64 of prod PEM>

# Optional — extra CORS origins (comma-separated) appended to the baseline list
# CORS_ORIGINS=https://staging.example.com
```

Run the server:

```bash
cd backend
venv/bin/uvicorn app.main:app --reload
```

On startup you should see `Kalshi integration active` and `Supabase logging active`. If either fails, the app still runs — Kalshi failure disables trading, Supabase failure just stops persistent logging; neither crashes the process.

### Frontend

```bash
cd frontend
npm install
```

`frontend/.env.local`:

```ini
VITE_SUPABASE_URL=https://<project>.supabase.co
VITE_SUPABASE_ANON_KEY=<anon-key>
VITE_API_URL=http://localhost:8000
```

Run:

```bash
npm run dev         # http://localhost:5173
npm run build       # production build → dist/
npm run lint
```

The backend's CORS allow-list includes `localhost:5173-5175` plus `liveline.watch`. Add new origins via `CORS_ORIGINS` rather than editing code.

### Supabase

Paste `backend/supabase_schema.sql` into the Supabase SQL editor. It's idempotent — `create table if not exists`, `add column if not exists`, `drop policy if exists` throughout, so re-running on an existing DB upgrades in place.

The schema creates:

- `public.users` — mirrors `auth.users`, auto-populated via trigger on Google sign-in. Holds per-user `settings` JSON.
- `public.sessions` — one row per `(user_id, game_id)` trading session with aggregate P&L + trade count.
- `public.trades` — every trade, real or dry-run, with full lifecycle status, entry/exit prices, fees, and picker annotations (model delta, expected profit, fee-adjusted flag).
- `public.orderbook_snapshots` — top-5 bid/ask depth captured at every trade, for every contract on the game (cross-market snapshot), so post-hoc analysis can reconstruct the full book state that produced a trade.

RLS is enabled on all four. The backend writes via `SUPABASE_SERVICE_KEY` (bypasses RLS). End-user reads go through the anon-key client in the frontend and are scoped to `auth.uid() = user_id` by policy.

---

## Trading model

**Expected value** per contract:

```
EV = α · |Δ_WE| − spread
```

- `Δ_WE` — shift in win probability (or O/U / spread contract probability) produced by the modeled batting event, in dollars per contract.
- `α` (alpha) — *target capture*: what fraction of the engine's predicted move the market is expected to actually realize before exit. Calibrated from backtests; default 0.6.
- `spread` — current bid/ask spread on the traded side of the chosen contract, in dollars.

Only trades where `EV > 0` and the entry → target move clears **both** filters land on the button grid:

- `min_move_cents` (default 4¢) — flat floor; a sanity bound against execution-noise trades.
- `min_move_to_fee_ratio` (default 2.0×) — price-aware floor; target move must be ≥ N× per-contract round-trip fee (Kalshi fees scale with `p·(1−p)`, so a flat cents threshold over-filters cheap contracts and under-filters expensive ones). Sweep data is in `backend/backtests/reports/ratio_*_sweep_*.csv`.

Candidates that fail either filter are rejected with a machine-readable `rejection_reason` (`min_move` or `fee_ratio`) that surfaces in the per-tap decision log (see [Observability](#observability)).

### Execution lifecycle

```
pending ─▶ undo_window ─▶ open ─▶ filled      (sell hit target — profit)
                          │   └─▶ stopped     (stop-loss tick — cut loss)
                          │   └─▶ expired     (clean-window timeout — forced IOC exit)
                          │
                          └─▶ canceled_by_user (tap undo inside 3s)
                          └─▶ canceled_by_kill (kill switch flattened the book)
                          └─▶ canceled         (buy didn't fill or no liquidity)
                          └─▶ error            (sell placement failed)
```

- **Undo window (3s)** — after the IOC buy fills, the limit sell isn't placed for 3 seconds so a misclick can be bailed out at zero slippage.
- **Clean window (45s)** — if the limit sell hasn't filled by then, it's canceled and the position is IOC-exited at market.
- **Stop loss (10¢)** — armed on the open position via websocket price ticks.
- **Session loss limit** — per-session floor (default −$1500). Hitting it arms the kill switch, which flattens every live position for the user and returns 503 on further `/buy` calls until reset.
- **Sell-placement retry (4 attempts, ~1.5s)** — if the resting-sell POST fails on the first try (transient Kalshi 5xx / network blip), retry up to 3 more times with 500ms spacing before falling through to the panic IOC flatten. Idempotent via stable `client_order_id` so a server-side landing under a network error doesn't rest twice.

### Multi-market mode

With `multi_market=true`, one tap fires the entire basket of positive-EV markets for that event (moneyline + O/U + spread, both sides where applicable). Budget is split across the basket proportional to per-market EV. Undo cancels the whole basket atomically.

### Dry run

`dry_run=true` is the default. Trades are priced against live Kalshi data but no real orders hit the exchange — the P&L in Supabase is what you *would* have made. Flip to `false` from the Settings page once you trust the picker.

### Loss-prevention blockers

Every layer has guards whose only job is to stop a losing trade. Ordered from earliest-fired (before a trade is even offered) to last-resort (once capital is already committed).

**Picker filters — a bad trade never reaches the grid.**

| Blocker | Why | Helpful when |
|---|---|---|
| **`EV > 0`** | Net expected value must be positive after the α discount and bid/ask spread. The whole ranker is built on this — non-positive EV means we're expected to lose on average. | Thin books where the spread alone eats the predicted move. |
| **`min_move_cents` (default 3¢)** | Flat absolute floor on the entry→target distance. A sanity bound below which one contract of execution noise (a tick against us on entry) swallows the whole edge. | A 1–2¢ "edge" on a mid-priced ML contract that would be pure noise after a realistic taker fill. |
| **`min_move_to_fee_ratio` (default 2.0×)** | Price-aware floor — target move must be ≥ N× per-contract round-trip fee. Kalshi fees scale with `p·(1−p)` (peak near 50¢), so a flat cents threshold over-filters cheap contracts and under-filters expensive ones. | An 80¢ O/U contract with a 4¢ target move — flat `min_move_cents` passes it, but fees on that price band make it a net loser. |
| **Blowout filter** | Drops moneyline trades on games where win probability has already saturated (e.g. 97%/3%). The model's deltas are still "real" but the market barely moves in response, so realized capture collapses. | 8th inning, home team up 7 with 2 outs — the ML contracts are basically pinned at $0.97, and a model-predicted 2¢ move on a K won't actually show up. |
| **Stale-market guard (`stale_market_seconds`, default 60s)** | Skips any ticker whose websocket hasn't seen a tick in N seconds. A dead book means our entry at ask is theoretical; the resting sell probably won't fill either. | A niche spread line (e.g. `-3.5`) that's technically listed but nobody's quoting — we'd sit filled and bleed out on the 45s clean-window IOC exit. |
| **Exit-liquidity guard (`exit_slippage_cents`, default 5¢)** | Requires enough resting bids within N cents of entry to absorb our `bet_size` on the way out. The filter simulates the exit before committing the entry. | Thin O/U book that quotes 82¢/85¢ but with only 3 contracts at 82¢ — we can get filled on entry but the exit would slip through 75¢ and turn a winner into a loser. |

**Tap-time guards — prevent the handler from firing on stale state.**

| Blocker | Why | Helpful when |
|---|---|---|
| **Cached-trade freshness (5s)** | `/buy` rejects taps against a cached trade snapshot older than 5 seconds. The button colors what you clicked; if the book has moved since that snapshot, the entry price on record is fiction. | You tap right as a tick revises the ask up 3¢ — without this guard you'd buy at the new worse price while thinking you got the old one. |
| **Kill-switch check** | Any `/buy` hits a 503 when the user's kill switch is armed. Flushes out a race where a tap is in flight as the session loss limit breaches. | Session just crossed −$1500, flatten-all just fired, and a tap was already on the wire — this blocks it from opening a new position post-kill. |

**In-trade guards — cut losses once capital is committed.**

| Blocker | Why | Helpful when |
|---|---|---|
| **Undo window (3s)** | After the IOC buy fills, the resting sell isn't placed for 3s. During that window the user can tap undo and the position is IOC-flattened at ~zero slippage. | Fat-fingered the wrong event button — bail inside 3s instead of eating the full spread round-trip. |
| **Clean-window timeout (45s)** | If the resting limit sell hasn't filled in 45s, it's canceled and the position is IOC-exited at market. Stops "hoping" on a target that isn't coming. | Target price printed once and walked away — without the forced exit the position just bleeds as the edge decays. |
| **Stop loss (10¢)** | Armed against websocket ticks. If the market moves 10¢ against the entry, the resting sell is canceled and the position is IOC-flattened. | Blowout inning-change that flips WE 15¢ in one play — cap the loss at 10¢ instead of riding it to the clean-window forced exit (which could be much worse). |
| **Sell-placement retry (4 attempts, ~1.5s)** | If the resting-sell POST fails (transient Kalshi 5xx, network blip), retries 3× at 500ms spacing with a stable `client_order_id` before giving up and panic-IOC-flattening. Prevents a "bought but no exit order" hung position. | Kalshi HTTP briefly 502s right after your buy fills — retry lands cleanly instead of leaving an unhedged position that only the clean-window can close. |
| **Panic IOC flatten (last resort)** | If all sell retries fail, the position is immediately IOC-sold at market rather than left naked. | Kalshi sell endpoint is fully down but tick feed still works — we eat the spread but don't hold a bag waiting for the 45s timeout. |

**Session-level guards — stop the bleeding across many trades.**

| Blocker | Why | Helpful when |
|---|---|---|
| **Session loss limit (default −$1500)** | Per-(user, game) floor. Breaching it auto-arms the kill switch. Bounds how bad a single game can get. | A game where the model is mis-calibrated and every trade loses — stops compounding after a defined cap instead of chasing losses play by play. |
| **Per-user kill switch** | Manual or auto-armed; flattens every live position for the user and returns 503 on further `/buy` calls until reset. Idempotent. | Something's clearly wrong (a bug, a suspicious streak) and you want to halt trading instantly without closing 10 browser tabs. |

**Operational guards — protect across restarts and outages.**

| Blocker | Why | Helpful when |
|---|---|---|
| **Dry-run default** | `dry_run=true` on every new user. No real capital moves until the user explicitly flips it on the Settings page. | New deploys, new users, regression tests — no way to take real losses by accident. |
| **Startup reconciliation** | On boot, any LiveLine-tagged resting orders from a previous run are canceled; open MLB positions are flagged for manual review. | Backend crashed with a resting sell out on Kalshi — reconciliation cancels it rather than letting an unowned-by-any-Trade order sit. |
| **Supabase-failure tolerance** | All `database.py` helpers wrap in `try/except` and return safe defaults. Supabase outages disable logging but do not block trading or cause a misread of session state. | Supabase maintenance window — trades continue to place and exit correctly; logging catches up when it recovers. |

---

## Running the backtest

Historical replay against cached MLB + Kalshi data. Lives in `backend/backtests/`.

```bash
cd backend

# One date range (API calls — caches per-game under backtests/results/cache/)
venv/bin/python -m backtests.backtest --from 2026-04-08 --to 2026-04-10 --multi-market

# Re-run from cache with no API calls
venv/bin/python -m backtests.backtest --from 2026-04-08 --to 2026-04-10 --use-cached --multi-market

# Single game + trade-level trace for the 3 highest-delta plays
venv/bin/python -m backtests.backtest --date 2026-04-10 --game-id 823482 --trace 3
```

**Sweeps** — one research question per file in `backtests/sweeps/`:

```bash
venv/bin/python -m backtests.sweeps.alpha            # optimal α
venv/bin/python -m backtests.sweeps.alpha_minmove    # (α, min_move_cents) grid
venv/bin/python -m backtests.sweeps.ratio            # min_move_to_fee_ratio bend-finder
venv/bin/python -m backtests.sweeps.ratio_2d         # (min_move_cents, ratio) grid — is the flat floor still load-bearing?
venv/bin/python -m backtests.sweeps.alpha_ratio      # (α, ratio) interaction grid
venv/bin/python -m backtests.sweeps.offset_fill      # fill rate by entry-timing offset
venv/bin/python -m backtests.sweeps.analyze_losers   # per-trade classification of worst games
```

See `backend/backtests/README.md` for the sweep-writing conventions (use `sweeps/_common.py`), the known findings, and the ENTRY_OFFSET discussion. Candidate filters are duplicated between `app/market_selector.py` and `backtests/core/multi_market.py` — any change to live filter logic must be mirrored in the backtest, or the two diverge.

---

## Testing

```bash
cd backend
venv/bin/python -m pytest                                    # all
venv/bin/python -m pytest tests/test_engine.py -v            # one file
venv/bin/python -m pytest tests/test_engine.py::test_x -v    # one test
```

Notable test files:

- `test_engine.py` — WE lookup + convolution correctness.
- `test_pnl.py` — gross / fee / net math against hand-computed examples.
- `test_instant_sell.py`, `test_sell_fail_flattens.py`, `test_exit_qty_persisted.py` — Trade state-machine edges.
- `test_kill_switch.py`, `test_session_loss_limit.py` — safety gates.
- `test_buy_freshness_guard.py` — 5s stale-trade-cache rejection on `/buy`.
- `test_websocket_fill.py`, `test_orderbook_snapshot.py` — push-based fill wiring + cross-market snapshots.
- `test_settings_update.py` — per-user settings persistence + whitelist.

---

## API surface (summary)

All endpoints require a Supabase Bearer JWT except `/health`. SSE passes the JWT as `?token=` because `EventSource` can't set headers.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Unauthenticated liveness |
| GET | `/auth/me` | Verify current user |
| GET | `/games` | Today's MLB games |
| GET | `/game-state/:id` | Inning, runners, WE, deltas |
| POST | `/refresh/:id` | Re-fetch state + recompute trades |
| GET | `/stream/:id` | SSE: per-tick trades + game-state pushes |
| GET | `/trades/:id` | Cached best-trade-per-event (falls back to fresh compute) |
| POST | `/buy/:id/:event` | Execute best trade for a batting event |
| POST | `/cancel/:position_id` | Undo-window bail or open-position instant-sell |
| GET | `/positions?game_id=` | Live snapshot for the user + game |
| GET | `/history/:id` | Every trade this user logged on this game |
| GET | `/balance` | Real Kalshi account balance |
| GET\|PUT | `/settings` | Per-user trading parameters (whitelisted keys only) |
| POST | `/kill`, `/kill/reset`, GET `/kill` | User kill switch |
| GET | `/debug/evaluate/:id/:event` | Why a given market was picked |

---

## Safety

Trade-level loss-prevention guards are enumerated in [Loss-prevention blockers](#loss-prevention-blockers) above. This section covers only the availability-adjacent guarantees that aren't about stopping a losing trade:

- **Auth cache is stale-while-error** — a previously-verified JWT continues to work for up to an hour during a Supabase auth outage, so active sessions don't drop.
- **Kill-switch + freshness guards are enforced server-side.** The frontend can't bypass them by spoofing a direct `/buy` — every trade-placing path goes through `main.buy_event`, which checks both unconditionally.

---

## Observability

Three log streams that together reconstruct what produced any specific trade. All emit after the Kalshi buy returns — never on the critical path between decision and order.

- **`[LATENCY]`** — per-tap timing split printed from `main.buy_event`. `prep` is handler-entry → just-before-Kalshi, `fire` is Kalshi RTT + in-thread post-buy work (initial DB insert, SELL kickoff), `total_backend` is the sum. Frontend network hop is separate — measure as TTFB in browser devtools.

- **`[DECISION <EVENT>]`** — multi-line block emitted per tap listing every candidate the picker evaluated: the PICKED winner, every REJECTED loser (with per-contract fees, net-after-fees, fee-pct, and gross/net EV ranks), and every SKIPPED ticker (with the guard that tripped: `stale_market`, `no_viable_side`, `min_move`, `fee_ratio`, `exit_liquidity`, `blowout_filter`). Labels use the ticker-perspective convention (`WSH YES`, `PIT NO`, `SEA -1.5 YES`, `Over 7.5 YES`) so two same-outcome candidates on different tickers stay visually distinct. A `fee_adjusted: YES/NO` line flags when gross vs net ranking would disagree on the winner. Format source: `market_selector.format_decision_log`.

- **`[TRADE <EVENT> <trade_id>] SETTINGS`** — one-line snapshot of every `session.settings` key that produced the trade (alpha, min_move_cents, min_move_to_fee_ratio, dry_run, stop_loss_cents, etc.). Emitted right after the BUY fill line so a specific trade can be correlated with its exact config at that instant, independent of whatever the user has toggled since.

`grep "DECISION HR"` surfaces the full evaluation block for one tap; `grep "\[LATENCY\]"` gives you the per-tap backend timing distribution.

---

## Deployment notes

- **Backend** — Railway. Private keys are injected as `KALSHI_{DEMO,PROD}_KEY_B64` (base64 PEM) since the filesystem is ephemeral. `kalshi_client._decode_pem` writes them to a temp file at import time.
- **Frontend** — Vercel (`frontend/vercel.json` rewrites all paths to `index.html` for client-side routing).
- **CORS** — the baseline allow-list in `main.py` covers local dev ports + the two known deployed hostnames. `CORS_ORIGINS` env var appends without a code change.
- **Logging** — uvicorn access logs are filtered to drop 2xx/3xx on polling endpoints (`/positions`, `/refresh`, `/balance`, `/history`, `/stream`) so trade-lifecycle log lines aren't swamped. `pykalshi`, `httpx`, `httpcore` are pinned at WARNING.

---

## Development conventions

- **`app/constants.py` is the single source of truth** for any value shared between live trading and backtests. Backtests re-export from there; live code imports directly. Never fork a constant.
- **P&L math lives in `app/pnl.py`**. Live trader and backtest both call `compute_gross` / `compute_fees` / `compute_net`. If the math needs to change, it changes there once.
- **`mlb_today()` in `game_state.py` owns the MLB-day boundary.** Kalshi ticker prefixes (`YYMONDD`) and MLB schedule queries must agree on Pacific-day dates or a 7 PM PT game gets a next-day prefix and never matches.
- **Supabase writes never raise.** Every `database.py` public helper catches, logs, and returns None/default.
- **Comments explain *why*, not *what*.** The codebase leans on docstring-level design notes where a decision isn't obvious from the code; match that style.
