# LiveLine — Interview Prep

## 1. 30-Second Pitch

LiveLine is a live MLB win-expectancy engine wired into Kalshi prediction markets. As a game plays, it computes per-batting-event WE deltas across moneyline, total, and spread, ranks the best trade per market by `alpha * |WE delta| - spread`, and on a single tap fires a real IOC buy plus a resting limit sell on Kalshi. The non-obvious bit: the edge isn't a better model — it's the plumbing that gets a decision from a Kalshi WS tick to a placed order before the market re-prices, with a kill switch that flattens everything if anything misbehaves.

## 2. Architecture in One Breath

Kalshi WebSocket tick → `on_price_update` recomputes the best trade per event using cached MLB state and pregenerated WE tables → pushed onto per-user SSE queues in `market_selector` → React frontend renders event cards via `EventSource(/stream/{game_id})`. MLB state is polled every 10s per game (started on first SSE subscriber, stopped on last disconnect or Final). On `/buy`, FastAPI fires the Kalshi IOC + resting limit synchronously, *then* writes the decision log and Supabase row — observability is deliberately off the critical path.

## 3. The Core Problems (lead with these)

**1. Decision-to-order latency on a moving market.** Naive approach: log the decision, persist it, then place the order. Problem: stdout + Supabase RTT compound between the tick and the fill, and the price you decided on is stale. Solution: `main.buy_event` calls `await asyncio.to_thread(_fire)` *before* the decision-log emit and the initial `db.log_trade` insert. Guarded by `tests/test_buy_ordering.py` so nobody reorders it back. **Framing line:** *"Observability is critical, but it doesn't belong between the decision and the order — it belongs after the fill."*

**2. Supabase outages must not kill live trading.** Naive approach: let DB writes raise. Problem: a Supabase blip during a game halts trading on a system whose whole point is acting fast. Solution: every function in `database.py` is best-effort — try/except, returns None, never raises. Auth has a two-tier cache (1h fresh TTL + stale-while-error) so an auth-service outage doesn't knock active sessions offline. **Framing line:** *"Treat your own database as an unreliable downstream — the trade is the source of truth, the row is a side effect."*

**3. Picking the right Kalshi line as the game state shifts.** Naive approach: pick once at game start. Problem: as the margin widens past the tightest listed spread, the picker freezes on stale tickers and `pick_spread_line` pins on the wrong rung. Solution: each MLB poll tick also calls `market_selector.refresh_markets` to diff in newly-listed ladder rungs. Spread pairing is done by mirror line — home-side pick determines the away ticker, never re-running `pick_spread_line` on the away bucket (it's stored negated and would silently substitute a non-mirror line).

**4. Idempotent sell placement against a flaky exchange.** Naive approach: place sell, retry on failure. Problem: retries create duplicate resting orders. Solution: `_activate` retries 3× with 500ms spacing using a stable `_coid("s")` client-order-id so Kalshi dedups the retry; if all three fail, panic-flatten via IOC.

## 4. The Clever Bit

The trade ranker scores each candidate market by

$$\text{score} = \alpha \cdot |\Delta\text{WE}| - \text{spread}$$

then gates with two min-move filters that *both* must pass: `min_move_cents` (a flat absolute floor) and `min_move_to_fee_ratio` (a price-aware floor — the expected move must be at least N× the per-contract round-trip fee). The price-aware gate is the interesting one: a 2¢ move on a 95¢ contract is a worse trade than a 2¢ move on a 50¢ contract because Kalshi's fee schedule is `p(1-p)`-shaped, so flat-cent floors lie at the tails. **Phrase to use:** *"Two filters because the dollar floor and the fee-relative floor disagree near the tails — and that's where most of the bad fills come from."*

## 5. Design Tradeoffs I Chose

- **SSE over WebSocket for the frontend stream.** Chose SSE because the data flow is strictly server→client and EventSource auto-reconnects. Tradeoff: can't pass headers, so the JWT rides on `?token=` — which means it lands in access logs.
- **Two parallel candidate-builders (`app/market_selector.py` and `backtests/core/multi_market.py`).** Chose duplication over shared abstraction because the live picker has to be allocation-light and the backtest harness needs to vectorize across days. Tradeoff: any live-filter change has to be mirrored manually; there's an explicit CLAUDE.md note about it.
- **`dry_run=True` as the default.** Chose safety-by-default; flipping requires an explicit settings update. Tradeoff: a fresh user sees fake fills until they realize there's a flag.

## 6. Honest Critiques (volunteer these)

- **`alpha` is a single hand-tuned scalar.** The whole ranker pivots on it; there's no per-market or per-game-state calibration, and no learned weighting.
- **MLB state is polled every 10s, but Kalshi ticks are sub-second.** That's a real reaction-time floor that no amount of order-path optimization fixes.
- **Backtest/live drift is structurally guaranteed.** Two implementations of candidate-building means one will eventually diverge silently; the right fix is a shared pure module that both call.
- **Single-process FastAPI.** Sessions are in-memory keyed by `(user_id, game_id)` — there's no horizontal scaling story; restarting the process drops live state until trades are re-hydrated from Supabase.
- **Min-move thresholds and the blowout filter are hand-picked.** Tuned against historical eyeballing, not a held-out evaluation set.
- **No replay harness for the SSE stream.** Bugs in `on_price_update` are caught only by unit tests on synthetic inputs, not by replaying real tick logs.

## 7. Anticipated Questions

- **Why FastAPI + SSE instead of a Node WS service?** The math is in Python (WE tables, pykalshi), so co-locating the decision and the order avoids a cross-language hop on the critical path. SSE is good enough for one-way fan-out.
- **How do you evaluate the picker?** Backtests under `backtests/sweeps/` — one file per research question, all sharing `sweeps/_common.py`. P&L uses `pnl.compute_net` (the same function live trading calls) so backtest and live numbers are directly comparable. No formal labeled eval set.
- **What about a Kalshi outage mid-position?** Sells retry 3× with idempotent `_coid`; on full failure, panic-flatten via IOC. The kill switch (`/kill`) flattens every live position for a user and 503s subsequent `/buy` calls.
- **Auth on the SSE endpoint?** Supabase JWT, verified with a 1h-fresh + stale-while-error cache so a Supabase auth blip doesn't disconnect everyone. Token rides on `?token=` because EventSource can't set headers — known tradeoff.
- **How would you scale it?** Move sessions into Redis (today they're per-process), shard by `game_id` since cross-game state is zero, and move the MLB poller into a single leader process that fans out to subscribers via pub/sub instead of N processes polling the same game.
- **What would you do differently?** Collapse the duplicated candidate-builder into one module both live and backtests import. Replace `alpha` with a learned per-state weighting. Add a tick-replay harness so `on_price_update` is testable against real game days, not just synthetic fixtures.
