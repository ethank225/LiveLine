-- LiveLine schema — paste into Supabase SQL editor.
-- Idempotent: safe to re-run.

-- ============================================================
-- Extensions
-- ============================================================
create extension if not exists "uuid-ossp";

-- ============================================================
-- users
-- Mirrors auth.users (id matches 1:1) so we can join FKs.
-- Populated by a trigger on auth.users so rows appear
-- automatically on first Google sign-in.
-- ============================================================
create table if not exists public.users (
  id            uuid primary key references auth.users(id) on delete cascade,
  email         text,
  display_name  text,
  created_at    timestamptz not null default now(),
  -- User-saved trading preferences (alpha, bet_size, multi_market, etc.).
  -- Never store secrets here — see USER_SETTINGS_KEYS whitelist in main.py.
  settings      jsonb not null default '{}'::jsonb
);

-- Additive for existing projects.
alter table public.users
  add column if not exists settings jsonb not null default '{}'::jsonb;

create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.users (id, email, display_name)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data->>'full_name', new.raw_user_meta_data->>'name', new.email)
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_auth_user();

-- ============================================================
-- sessions
-- One row per (user, game) trading session. Lazily created
-- on first /buy or /stream connect and closed on inactivity.
-- ============================================================
create table if not exists public.sessions (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null references public.users(id) on delete cascade,
  game_id            integer not null,
  home_team          text,
  away_team          text,
  started_at         timestamptz not null default now(),
  ended_at           timestamptz,
  total_trades       integer not null default 0,
  total_pnl          numeric not null default 0,
  settings_snapshot  jsonb
);

create index if not exists sessions_user_started_idx
  on public.sessions (user_id, started_at desc);

-- Find the currently-open session for a (user, game) quickly.
create index if not exists sessions_open_idx
  on public.sessions (user_id, game_id)
  where ended_at is null;

-- ============================================================
-- trades
-- Every buy attempt (real or dry_run). Lifecycle status updated
-- in place by trader.py hooks.
-- ============================================================
create table if not exists public.trades (
  id                  uuid primary key default gen_random_uuid(),
  session_id          uuid not null references public.sessions(id) on delete cascade,
  user_id             uuid not null references public.users(id) on delete cascade,
  game_id             integer not null,
  created_at          timestamptz not null default now(),

  event_type          text,                    -- HR, K, OUT, BB, 1B, 2B, DP
  market_ticker       text,
  market_type         text,                    -- moneyline, over_under, spread
  market_label        text,                    -- "AZ wins", "Over 7.5", "PHI -1.5"
  side                text,                    -- YES / NO

  entry_price         numeric,
  sell_target         numeric,
  quantity            integer,
  requested_quantity  integer,
  total_cost          numeric,
  predicted_delta     numeric,
  alpha               numeric,

  status              text not null,           -- undo_window, open, filled, expired,
                                               -- stopped, canceled, canceled_by_user,
                                               -- canceled_by_kill, error, no_fill
  exit_price          numeric,
  realized_pnl        numeric,
  closed_at           timestamptz,

  game_state_json     jsonb,                   -- {inning, half, outs, runners, home_score, away_score}
  user_timestamp      timestamptz,             -- client Date.now() at tap time
  dry_run             boolean not null default true,

  -- Groups every position that came from the same user tap. In single-market
  -- mode this is a group of one. In multi-market mode it's the whole basket
  -- fired simultaneously, so undo cancels all of them as a unit and post-hoc
  -- analysis can evaluate the group as a single trading decision.
  undo_group_id       uuid
);

-- Existing projects need this column added manually (re-running the CREATE is a
-- no-op because of IF NOT EXISTS on the table). Safe to run repeatedly.
alter table public.trades
  add column if not exists undo_group_id uuid;

-- P&L breakdown. `realized_pnl` is now the NET number (gross − entry_fee −
-- exit_fee); the three new columns let analytics recover the gross trade
-- and per-leg Kalshi fees without having to recompute. Entry is always a
-- taker fee (IOC buy); exit is maker if the resting limit sell filled at
-- target, taker if we IOC'd out (expire / stop-loss / undo).
alter table public.trades
  add column if not exists gross_pnl  numeric,
  add column if not exists entry_fee  numeric,
  add column if not exists exit_fee   numeric;

-- Contracts that actually exited the position. May differ from
-- `quantity` on partial IOC fills (Kalshi fills against available bids
-- only). Without this, reconciliation via (exit_price - entry_price) *
-- quantity disagrees with `realized_pnl` whenever an IOC partially
-- filled — which at $500/play into thin books happens on every force-
-- exit.
alter table public.trades
  add column if not exists exit_qty integer;

-- Picker annotations. market_selector stamps these on every trade at
-- creation time so post-hoc analysis can reconstruct WHY the picker
-- chose this trade (fee-aware vs gross-EV ranking) and what it expected
-- the P&L to be. `*_est` values are now qty-adjusted in Trade._log_to_db
-- (prior versions wrote bet_size-notional numbers that were 5-10× too
-- high; see backend/app/trader.py::_recompute_expected_pnl).
alter table public.trades
  add column if not exists entry_fee_est       numeric,
  add column if not exists exit_fee_est        numeric,
  add column if not exists net_expected_profit numeric,
  add column if not exists fee_adjusted        boolean,
  add column if not exists gross_pick_ticker   text;

create index if not exists trades_session_created_idx
  on public.trades (session_id, created_at);

create index if not exists trades_user_created_idx
  on public.trades (user_id, created_at desc);

-- Real-money P&L reporting filters WHERE dry_run = false.
create index if not exists trades_user_real_idx
  on public.trades (user_id, created_at desc)
  where dry_run = false;

-- Group expansion: cancel-by-group on the backend, and post-hoc queries
-- that collapse a basket into one row need a fast lookup by group.
create index if not exists trades_undo_group_idx
  on public.trades (undo_group_id)
  where undo_group_id is not null;

-- ============================================================
-- orderbook_snapshots
-- Top-5 bid/ask depth captured once per trade at execute time.
-- Links to trades(id) so post-hoc analysis can join outcomes to the
-- exact book state that produced them. Answers "how deep was the bid
-- book when I entered?" and "what did I pay to walk the ask stack?".
-- ============================================================
create table if not exists public.orderbook_snapshots (
  id              uuid primary key default gen_random_uuid(),
  trade_id        uuid references public.trades(id) on delete cascade,
  game_id         integer not null,
  market_ticker   text not null,
  side            text not null,           -- YES / NO, matches trade.side
  timestamp       timestamptz not null default now(),

  -- Top-5 bid / ask levels in the trade's side-units. Level N is NULL
  -- if the book is thinner than N levels deep.
  bid_1_price numeric, bid_1_qty integer,
  bid_2_price numeric, bid_2_qty integer,
  bid_3_price numeric, bid_3_qty integer,
  bid_4_price numeric, bid_4_qty integer,
  bid_5_price numeric, bid_5_qty integer,
  ask_1_price numeric, ask_1_qty integer,
  ask_2_price numeric, ask_2_qty integer,
  ask_3_price numeric, ask_3_qty integer,
  ask_4_price numeric, ask_4_qty integer,
  ask_5_price numeric, ask_5_qty integer,

  -- Totals across the WHOLE book (not just top 5) so depth summaries
  -- don't hide deep liquidity beyond the logged levels.
  total_bid_depth integer,
  total_ask_depth integer
);

-- Cross-market snapshot fields. On every trade we now snapshot the
-- traded market plus every other active contract for the game across
-- all three market types (moneyline / over_under / spread, both sides).
-- All rows from the same trade event share trade_id; market_type
-- distinguishes the contract kind; spread + depth_within_Nc are
-- pre-aggregated liquidity summaries on the exit (bid) side.
alter table public.orderbook_snapshots
  add column if not exists market_type     text,
  add column if not exists spread          numeric,
  add column if not exists depth_within_1c integer,
  add column if not exists depth_within_2c integer,
  add column if not exists depth_within_3c integer;

-- phase: 'pre' captured immediately before the IOC buy goes over the
-- wire (reflects the book the sizer used); 'post' captured after fill
-- (reflects the book depleted by our take + any market reaction).
-- Default 'post' backfills historical rows correctly — every existing
-- row was captured at the post-fill point, so 'post' is what they are.
alter table public.orderbook_snapshots
  add column if not exists phase text not null default 'post';

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'orderbook_snapshots_phase_check'
  ) then
    alter table public.orderbook_snapshots
      add constraint orderbook_snapshots_phase_check
      check (phase in ('pre', 'post'));
  end if;
end $$;

create index if not exists ob_snap_trade_idx
  on public.orderbook_snapshots (trade_id)
  where trade_id is not null;

-- Per-trade phase queries ("did we capture both pre and post for this
-- trade?") run on every diagnostic; cheap composite index keeps them
-- index-only.
create index if not exists ob_snap_trade_phase_idx
  on public.orderbook_snapshots (trade_id, phase)
  where trade_id is not null;

-- ============================================================
-- Row Level Security
-- Backend writes use the service role key, which bypasses RLS.
-- These policies only control what an authenticated end user
-- can read via the client SDK / PostgREST with their JWT.
-- ============================================================
alter table public.users               enable row level security;
alter table public.sessions            enable row level security;
alter table public.trades              enable row level security;
alter table public.orderbook_snapshots enable row level security;

-- users: read your own row
drop policy if exists users_select_own on public.users;
create policy users_select_own on public.users
  for select using (id = auth.uid());

-- sessions: read your own rows
drop policy if exists sessions_select_own on public.sessions;
create policy sessions_select_own on public.sessions
  for select using (user_id = auth.uid());

-- trades: read your own rows
drop policy if exists trades_select_own on public.trades;
create policy trades_select_own on public.trades
  for select using (user_id = auth.uid());

-- orderbook_snapshots: scoped to the owning trade's user. Orphan rows
-- (trade_id IS NULL) are never returned because the EXISTS fails — which
-- is the intended fail-closed behavior.
drop policy if exists orderbook_snapshots_select_own on public.orderbook_snapshots;
create policy orderbook_snapshots_select_own on public.orderbook_snapshots
  for select using (
    trade_id is not null
    and exists (
      select 1 from public.trades t
      where t.id = orderbook_snapshots.trade_id
        and t.user_id = auth.uid()
    )
  );

-- No insert/update/delete policies for the anon or authenticated
-- roles on any of these tables. All writes MUST go through the
-- backend using SUPABASE_SERVICE_KEY.

-- ============================================================
-- Cleanup: drop legacy market_snapshots table.
-- Earlier versions wrote top-of-book YES prices every 30s and at trade
-- time; superseded by orderbook_snapshots (per-trade top-5 depth).
-- Kept as an idempotent DROP so re-running this schema on a deployed
-- DB sheds the old table + its indexes in one pass.
-- ============================================================
drop table if exists public.market_snapshots cascade;
