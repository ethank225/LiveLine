"""Single-market vs multi-market backtest comparison.

Takes the per-(play, market) records built by the sync pipeline and runs two
allocation strategies against the same candidate set:

  single: for each play, pick the highest-EV market and commit the full
          per-play budget to it.
  multi:  for each play, take every positive-EV candidate and split the
          budget proportional to EV.

EV approximation (no bid/ask available in historical trade data):
    ev_per_contract = alpha * |predicted_delta|

A candidate is dead (skipped) if:
    entry_price >= 0.95 or <= 0.05, or alpha * |predicted_delta| < 0.01.

Fill simulation is reused verbatim from the sync pipeline — same methodology
for both modes guarantees the comparison is apples-to-apples.
"""

import csv
from collections import defaultdict
from statistics import pstdev

from backtests.constants import EVENT_ORDER, BLOWOUT_THRESHOLD
from app.fees import taker_fee, maker_fee
from app.pnl import compute_fees, compute_gross

MARKET_TYPES = ["moneyline", "over_under", "spread"]
MARKET_LABELS = {"moneyline": "Moneyline", "over_under": "O/U", "spread": "Spread"}


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------

def _is_dead(entry_price: float, predicted_delta: float, alpha: float) -> bool:
    if entry_price >= 0.95 or entry_price <= 0.05:
        return True
    if alpha * abs(predicted_delta) < 0.01:
        return True
    return False


def _sell_target(entry_price: float, predicted_delta: float, alpha: float) -> float:
    move = alpha * abs(predicted_delta)
    return entry_price + move if predicted_delta > 0 else entry_price - move


def _build_candidates(
    group: list[dict], alpha: float,
    min_move: float = 0.0, blowout_filter: bool = True,
) -> list[dict]:
    """From a set of synced records sharing the same play, build candidate trades.

    `min_move` (in dollars, e.g. 0.04) filters candidates whose expected
    entry→target move won't clear round-trip fees.

    `blowout_filter` (default True) skips MONEYLINE candidates when the
    score margin at this play is >= BLOWOUT_THRESHOLD — mirrors the live
    picker's `compute_best_trades` gate at market_selector.py:724. O/U
    and spread always trade because their lines still move in blowouts.
    """
    # Plays in `group` share the same game state (they're the same play
    # against different markets), so reading the score off the first record
    # is sufficient — and matches what the live picker does in
    # compute_best_trades, which gates moneyline candidates per-event using
    # the current score.
    is_blowout = False
    if group:
        first = group[0]
        hs = first.get("home_score") or 0
        as_ = first.get("away_score") or 0
        is_blowout = abs(hs - as_) >= BLOWOUT_THRESHOLD

    seen_tickers: set[str] = set()
    out = []
    for rec in group:
        pred = rec.get("predicted_ml_delta") or 0.0
        if pred == 0:
            continue
        # Live picker's blowout gate is moneyline-only; totals and spreads
        # still have edge because the line is still moving even in a 10-2.
        if blowout_filter and is_blowout and rec.get("market_type") == "moneyline":
            continue
        entry = rec.get("price_before") or 0.0
        if entry <= 0 or _is_dead(entry, pred, alpha):
            continue
        # Min-move filter mirrors live _evaluate_market gate. Skip trades
        # whose expected move is too small to cover round-trip fees.
        if alpha * abs(pred) < min_move:
            continue
        ticker = rec.get("market_ticker", "")
        if ticker and ticker in seen_tickers:
            continue
        if ticker:
            seen_tickers.add(ticker)
        fill = rec.get("fill_sim", {}).get(alpha)
        if not fill:
            continue
        out.append({
            "rec": rec,
            "entry_price": entry,
            "sell_target": _sell_target(entry, pred, alpha),
            "predicted_delta": pred,
            "ev_per_contract": alpha * abs(pred),
            "pnl_per_contract": fill["pnl"],
            "filled": bool(fill["filled"]),
            "market_type": rec.get("market_type", "moneyline"),
            "event": rec.get("event", ""),
            "market_ticker": ticker,
            "market_label": rec.get("market_label", ""),
        })
    return out


def _group_by_play(all_synced: list[dict]) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for rec in all_synced:
        key = (rec.get("game_tag", ""), rec.get("timestamp", ""))
        groups[key].append(rec)
    return groups


# ---------------------------------------------------------------------------
# Execution strategies
# ---------------------------------------------------------------------------

def _side_buy_price(cand: dict) -> float:
    """Per-contract entry price on the side actually bought.

    Backtest stores `entry_price` as the YES ask. For negative-delta trades
    the live system buys NO, which costs `(1 - yes_ask)` per contract, so
    the fee base flips.
    """
    entry = cand["entry_price"]
    return entry if cand["predicted_delta"] > 0 else round(1.0 - entry, 4)


def _side_sell_target(cand: dict) -> float:
    """Target sell price on the side actually traded.

    Same identity used by the live picker: sell_target = entry + alpha*|delta|.
    The fee base for the maker exit is this price on the traded side, not
    the YES-ask-derived target stored in the candidate.
    """
    buy = _side_buy_price(cand)
    return max(0.0, min(1.0, buy + cand["ev_per_contract"]))


def _expected_net_profit(cand: dict, contracts: int) -> float:
    """Net expected profit at `contracts` size, mirroring market_selector's
    (sell_target - entry) * bet_size - taker_entry - maker_exit.

    Used at selection time, before fills are known — so the exit leg is
    assumed to fill as a maker limit at the target.
    """
    if contracts <= 0:
        return 0.0
    buy = _side_buy_price(cand)
    sell = _side_sell_target(cand)
    gross = (sell - buy) * contracts
    return gross - taker_fee(contracts, buy) - maker_fee(contracts, sell)


def _make_trade(cand: dict, budget: float, contracts: int, fees_on: bool) -> dict:
    # Gross comes straight off the fill sim (round-trip with Kalshi's own
    # prices, no clamping). Fees use the clamped sell price because an
    # out-of-range price would break the fee formula — but gross has no
    # such concern, and clamping it would silently change P&L numbers
    # that should track the simulation verbatim.
    buy_price = _side_buy_price(cand)
    sell_price_raw = buy_price + cand["pnl_per_contract"]
    gross_pnl = compute_gross(buy_price, sell_price_raw, contracts)
    if fees_on and contracts > 0:
        sell_price_clamped = max(0.0, min(1.0, sell_price_raw))
        exit_type = "maker" if cand["filled"] else "taker"
        buy_fee, sell_fee = compute_fees(
            contracts, buy_price, sell_price_clamped, exit_type,
        )
        fees = round(buy_fee + sell_fee, 4)
    else:
        buy_fee = sell_fee = fees = 0.0
    return {
        **cand,
        "contracts": contracts,
        "budget": budget,
        "gross_pnl": gross_pnl,
        "buy_fee": buy_fee,
        "sell_fee": sell_fee,
        "fees": fees,
        "pnl": round(gross_pnl - fees, 4),
    }


def _annotate_net_ev(candidates: list[dict], max_dollars: float, fees_on: bool) -> None:
    """Attach contract count + net expected profit (pre-fill) to each candidate.

    When fees_on=False we fall back to the old gross-EV ranking so the
    --no-fees A/B comparison behaves like the pre-fee backtest.
    """
    for c in candidates:
        contracts = int(max_dollars / _side_buy_price(c)) if _side_buy_price(c) > 0 else 0
        c["_contracts_est"] = contracts
        if fees_on:
            c["net_expected_profit"] = _expected_net_profit(c, contracts)
        else:
            c["net_expected_profit"] = c["ev_per_contract"] * contracts


def _execute_single(candidates: list[dict], max_dollars: float, fees_on: bool) -> list[dict]:
    if not candidates:
        return []
    _annotate_net_ev(candidates, max_dollars, fees_on)
    # Rank by net expected profit (entry taker + exit maker accounted for),
    # mirroring market_selector._evaluate_market / compute_best_trades. Kill
    # any candidate that is gross-positive but net-negative.
    viable = [c for c in candidates if c["net_expected_profit"] > 0]
    if not viable:
        return []
    best = max(viable, key=lambda c: c["net_expected_profit"])
    contracts = best["_contracts_est"]
    if contracts <= 0:
        return []
    return [_make_trade(best, max_dollars, contracts, fees_on)]


def _execute_multi(candidates: list[dict], max_dollars: float, fees_on: bool) -> list[dict]:
    _annotate_net_ev(candidates, max_dollars, fees_on)
    # Same gate as single-mode: every basket member must clear fees on its own.
    positive = [c for c in candidates if c["net_expected_profit"] > 0]
    if not positive:
        return []
    # Split budget proportional to net expected profit so better trades get
    # a bigger slice (matches the spirit of the live multi-market split,
    # which sizes by EV rather than a flat contracts-per-market).
    total_weight = sum(c["net_expected_profit"] for c in positive)
    if total_weight <= 0:
        return []
    trades = []
    for c in positive:
        budget = max_dollars * (c["net_expected_profit"] / total_weight)
        contracts = int(budget / _side_buy_price(c))
        if contracts <= 0:
            continue
        trades.append(_make_trade(c, budget, contracts, fees_on))
    return trades


def run_comparison(
    all_synced: list[dict], alpha: float, max_dollars: float,
    min_move: float = 0.04, fees_on: bool = True,
    blowout_filter: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Run both modes on the same candidate set. Returns (single, multi).

    `min_move` (default 4¢) skips trades whose expected entry→target move
    is too small to clear round-trip fees — matches the live `min_move_cents`
    setting. `fees_on` (default True) applies Kalshi taker/maker fees to
    both legs so the simulated P&L matches what a real Kalshi balance sees.
    """
    groups = _group_by_play(all_synced)
    single_trades: list[dict] = []
    multi_trades: list[dict] = []
    # Diagnostic: count candidates the fee filter kills (gross-positive but
    # net-negative once entry taker + exit maker fees come off).
    fee_killed_single = 0
    fee_killed_multi = 0
    for group in groups.values():
        candidates = _build_candidates(
            group, alpha, min_move=min_move, blowout_filter=blowout_filter,
        )
        if candidates and fees_on:
            _annotate_net_ev(candidates, max_dollars, fees_on)
            killed = sum(
                1 for c in candidates
                if c["ev_per_contract"] > 0 and c["net_expected_profit"] <= 0
            )
            fee_killed_single += killed
            fee_killed_multi += killed
        single_trades.extend(_execute_single(candidates, max_dollars, fees_on))
        multi_trades.extend(_execute_multi(candidates, max_dollars, fees_on))
    if fees_on:
        print(f"\nFee filter killed {fee_killed_single} gross-positive, "
              f"net-negative candidates (per mode, same candidate set).")
    return single_trades, multi_trades


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "fill_rate": 0.0, "avg_profit_fill": 0.0,
                "avg_loss_miss": 0.0, "ev_per_trade": 0.0,
                "gross_pnl": 0.0, "fees": 0.0,
                "buy_fees": 0.0, "sell_fees": 0.0,
                "total_pnl": 0.0, "win_rate": 0.0}
    n = len(trades)
    fills = [t for t in trades if t["filled"]]
    misses = [t for t in trades if not t["filled"]]
    fill_pnls = [t["pnl_per_contract"] for t in fills]
    miss_pnls = [t["pnl_per_contract"] for t in misses]
    all_pnls = [t["pnl_per_contract"] for t in trades]
    winners = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "n": n,
        "fill_rate": 100 * len(fills) / n,
        "avg_profit_fill": sum(fill_pnls) / len(fill_pnls) if fill_pnls else 0.0,
        "avg_loss_miss": sum(miss_pnls) / len(miss_pnls) if miss_pnls else 0.0,
        "ev_per_trade": sum(all_pnls) / n,
        "gross_pnl": sum(t.get("gross_pnl", t["pnl"]) for t in trades),
        "fees": sum(t.get("fees", 0.0) for t in trades),
        "buy_fees": sum(t.get("buy_fee", 0.0) for t in trades),
        "sell_fees": sum(t.get("sell_fee", 0.0) for t in trades),
        "total_pnl": sum(t["pnl"] for t in trades),
        "win_rate": 100 * winners / n,
    }


def _by_market(trades: list[dict]) -> dict[str, float]:
    out = {mt: 0.0 for mt in MARKET_TYPES}
    for t in trades:
        mt = t["market_type"]
        if mt in out:
            out[mt] += t["pnl"]
    return out


def _by_event(trades: list[dict]) -> dict[str, dict]:
    out = {ev: {"n": 0, "pnl": 0.0} for ev in EVENT_ORDER}
    for t in trades:
        ev = t["event"]
        if ev in out:
            out[ev]["n"] += 1
            out[ev]["pnl"] += t["pnl"]
    return out


def _per_game(trades: list[dict]) -> dict[str, float]:
    by_game: dict[str, float] = defaultdict(float)
    for t in trades:
        g = t["rec"].get("game_tag", "")
        by_game[g] += t["pnl"]
    return dict(by_game)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_diff_pct(a: float, b: float) -> str:
    if a == 0:
        return "n/a"
    return f"{100 * (b - a) / a:+.1f}%"


def print_comparison(
    single_trades: list[dict], multi_trades: list[dict],
    alpha: float, max_dollars: float,
) -> None:
    s = _summarize(single_trades)
    m = _summarize(multi_trades)

    print(f"\n{'=' * 78}")
    print(f"SINGLE-MARKET vs MULTI-MARKET  (alpha={alpha}, "
          f"${max_dollars:.0f}/play)")
    print(f"{'=' * 78}")
    print(f"\n{'':<20} {'Single Market':>15} {'Multi Market':>15} {'Difference':>15}")
    print("-" * 70)

    def _fee_pct(fees: float, gross: float) -> str:
        if not gross:
            return "n/a"
        return f"{100 * fees / abs(gross):.1f}%"

    rows = [
        ("Total trades", f"{s['n']:,}", f"{m['n']:,}",
         _fmt_diff_pct(s['n'], m['n'])),
        ("Fill rate", f"{s['fill_rate']:.1f}%", f"{m['fill_rate']:.1f}%",
         f"{m['fill_rate'] - s['fill_rate']:+.1f}pp"),
        ("Avg profit/fill", f"{s['avg_profit_fill']:+.4f}",
         f"{m['avg_profit_fill']:+.4f}",
         f"{m['avg_profit_fill'] - s['avg_profit_fill']:+.4f}"),
        ("Avg loss/miss", f"{s['avg_loss_miss']:+.4f}",
         f"{m['avg_loss_miss']:+.4f}",
         f"{m['avg_loss_miss'] - s['avg_loss_miss']:+.4f}"),
        ("EV per trade", f"{s['ev_per_trade']:+.4f}",
         f"{m['ev_per_trade']:+.4f}",
         f"{m['ev_per_trade'] - s['ev_per_trade']:+.4f}"),
        ("Gross P&L", f"${s['gross_pnl']:+,.0f}", f"${m['gross_pnl']:+,.0f}",
         _fmt_diff_pct(s['gross_pnl'], m['gross_pnl'])),
        ("Total fees", f"${s['fees']:,.0f}", f"${m['fees']:,.0f}",
         f"${m['fees'] - s['fees']:+,.0f}"),
        ("  entry (taker)", f"${s['buy_fees']:,.0f}", f"${m['buy_fees']:,.0f}",
         f"${m['buy_fees'] - s['buy_fees']:+,.0f}"),
        ("  exit (maker/taker)", f"${s['sell_fees']:,.0f}", f"${m['sell_fees']:,.0f}",
         f"${m['sell_fees'] - s['sell_fees']:+,.0f}"),
        ("Net P&L", f"${s['total_pnl']:+,.0f}", f"${m['total_pnl']:+,.0f}",
         _fmt_diff_pct(s['total_pnl'], m['total_pnl'])),
        ("Fee % of gross", _fee_pct(s['fees'], s['gross_pnl']),
         _fee_pct(m['fees'], m['gross_pnl']), ""),
        ("Win rate", f"{s['win_rate']:.1f}%", f"{m['win_rate']:.1f}%",
         f"{m['win_rate'] - s['win_rate']:+.1f}pp"),
    ]

    s_games = _per_game(single_trades)
    m_games = _per_game(multi_trades)
    if s_games or m_games:
        all_keys = set(s_games) | set(m_games)
        s_vals = [s_games.get(k, 0.0) for k in all_keys]
        m_vals = [m_games.get(k, 0.0) for k in all_keys]
        n_games = len(all_keys)
        s_avg = sum(s_vals) / n_games if n_games else 0.0
        m_avg = sum(m_vals) / n_games if n_games else 0.0
        s_std = pstdev(s_vals) if len(s_vals) > 1 else 0.0
        m_std = pstdev(m_vals) if len(m_vals) > 1 else 0.0
        s_worst = min(s_vals) if s_vals else 0.0
        m_worst = min(m_vals) if m_vals else 0.0
        rows.append(("Per game", f"${s_avg:+,.0f}", f"${m_avg:+,.0f}",
                     _fmt_diff_pct(s_avg, m_avg)))
        rows.append(("Std dev / game", f"${s_std:,.0f}", f"${m_std:,.0f}",
                     f"${m_std - s_std:+,.0f}"))
        rows.append(("Worst game", f"${s_worst:+,.0f}", f"${m_worst:+,.0f}",
                     f"${m_worst - s_worst:+,.0f}"))

    for label, a, b, d in rows:
        print(f"{label:<20} {a:>15} {b:>15} {d:>15}")

    # Per-market-type P&L
    s_mt = _by_market(single_trades)
    m_mt = _by_market(multi_trades)
    print(f"\nBy market type (total P&L):")
    print(f"  {'Market':<12} {'Single':>12} {'Multi':>12} {'Difference':>12}")
    print(f"  {'-' * 52}")
    for mt in MARKET_TYPES:
        print(f"  {MARKET_LABELS[mt]:<12} "
              f"${s_mt[mt]:>+11,.0f} ${m_mt[mt]:>+11,.0f} "
              f"{_fmt_diff_pct(s_mt[mt], m_mt[mt]):>12}")

    # Per-event breakdown
    s_ev = _by_event(single_trades)
    m_ev = _by_event(multi_trades)
    print(f"\nPer-event breakdown (trades / P&L):")
    print(f"  {'Event':<6} {'Single n':>8} {'Single P&L':>12} "
          f"{'Multi n':>8} {'Multi P&L':>12}")
    print(f"  {'-' * 54}")
    for ev in EVENT_ORDER:
        se = s_ev[ev]
        me = m_ev[ev]
        print(f"  {ev:<6} {se['n']:>8,} ${se['pnl']:>+11,.0f} "
              f"{me['n']:>8,} ${me['pnl']:>+11,.0f}")


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "game_tag", "timestamp", "inning", "half", "event", "mlb_event",
    "market_type", "market_label", "market_ticker",
    "entry_price", "sell_target", "predicted_delta", "ev_per_contract",
    "net_expected_profit",
    "contracts", "budget", "pnl_per_contract", "filled",
    "gross_pnl", "buy_fee", "sell_fee", "fees", "pnl",
]


# ---------------------------------------------------------------------------
# Per-game summary
# ---------------------------------------------------------------------------

# Lead margin at any point in the game that classifies it as a blowout.
# Live trader has BLOWOUT_THRESHOLD = 5 — keep this in sync with that gate.
_BLOWOUT_MARGIN = 5

_PER_GAME_CSV_FIELDS = [
    "game_tag", "away", "home", "away_final", "home_final",
    "max_lead", "blowout", "trades", "fills", "win_rate",
    "gross_pnl", "fees", "net_pnl",
]


def _aggregate_by_game(trades: list[dict], games_meta: dict[str, dict]) -> list[dict]:
    """Group trades by game_tag and join with per-game metadata.

    `games_meta[game_tag]` carries the matchup, final score, and max-lead
    margin computed from play-by-play. Games with zero trades are still
    emitted (so a "no trades" game shows up in the report rather than
    silently disappearing) — but only when meta exists for them, since
    `all_synced` may be empty for games with no Kalshi data.
    """
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        tag = t["rec"].get("game_tag", "")
        if tag:
            by_tag[tag].append(t)

    rows: list[dict] = []
    for tag, meta in games_meta.items():
        ts = by_tag.get(tag, [])
        gross = sum(t.get("gross_pnl", 0.0) for t in ts)
        fees = sum(t.get("fees", 0.0) for t in ts)
        net = sum(t["pnl"] for t in ts)
        fills = sum(1 for t in ts if t.get("filled"))
        wins = sum(1 for t in ts if t["pnl"] > 0)
        win_rate = (100.0 * wins / len(ts)) if ts else 0.0
        rows.append({
            "game_tag": tag,
            "away": meta.get("away", ""),
            "home": meta.get("home", ""),
            "away_final": meta.get("away_final"),
            "home_final": meta.get("home_final"),
            "max_lead": meta.get("max_lead", 0),
            "blowout": meta.get("max_lead", 0) >= _BLOWOUT_MARGIN,
            "trades": len(ts),
            "fills": fills,
            "win_rate": win_rate,
            "gross_pnl": round(gross, 2),
            "fees": round(fees, 2),
            "net_pnl": round(net, 2),
        })
    rows.sort(key=lambda r: r["net_pnl"])
    return rows


def print_per_game_summary(
    trades: list[dict], games_meta: dict[str, dict], mode_label: str = "single-market",
) -> None:
    rows = _aggregate_by_game(trades, games_meta)
    if not rows:
        return
    print(f"\n{'=' * 78}")
    print(f"GAME-BY-GAME P&L ({mode_label} mode) — sorted by net P&L (worst first)")
    print(f"{'=' * 78}")
    print(f"  {'Matchup':<14} {'Score':<8} {'Trades':>6} {'Fills':>5} "
          f"{'Gross':>9} {'Fees':>8} {'Net P&L':>10}  Notes")
    print(f"  {'-' * 76}")
    for r in rows:
        if r["away_final"] is not None and r["home_final"] is not None:
            score = f"{r['away_final']}-{r['home_final']}"
        else:
            score = "—"
        matchup = f"{r['away']}@{r['home']}"
        notes = "blowout" if r["blowout"] else ""
        print(
            f"  {matchup:<14} {score:<8} {r['trades']:>6} {r['fills']:>5} "
            f"${r['gross_pnl']:>+8,.0f} ${r['fees']:>7,.0f} "
            f"${r['net_pnl']:>+9,.0f}  {notes}"
        )
    # Totals
    tot_trades = sum(r["trades"] for r in rows)
    tot_fills = sum(r["fills"] for r in rows)
    tot_gross = sum(r["gross_pnl"] for r in rows)
    tot_fees = sum(r["fees"] for r in rows)
    tot_net = sum(r["net_pnl"] for r in rows)
    print(f"  {'-' * 76}")
    print(
        f"  {'TOTAL':<14} {'':<8} {tot_trades:>6} {tot_fills:>5} "
        f"${tot_gross:>+8,.0f} ${tot_fees:>7,.0f} "
        f"${tot_net:>+9,.0f}"
    )


def write_per_game_csv(
    trades: list[dict], games_meta: dict[str, dict], filepath,
) -> None:
    rows = _aggregate_by_game(trades, games_meta)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_PER_GAME_CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "game_tag": r["game_tag"],
                "away": r["away"],
                "home": r["home"],
                "away_final": r["away_final"] if r["away_final"] is not None else "",
                "home_final": r["home_final"] if r["home_final"] is not None else "",
                "max_lead": r["max_lead"],
                "blowout": "true" if r["blowout"] else "false",
                "trades": r["trades"],
                "fills": r["fills"],
                "win_rate": round(r["win_rate"], 1),
                "gross_pnl": r["gross_pnl"],
                "fees": r["fees"],
                "net_pnl": r["net_pnl"],
            })


def build_game_meta(
    game_tag: str, game_info: dict, records: list,
) -> dict:
    """Extract per-game summary fields from raw MLB pull. `records` is the
    PlayRecord list before sync; we read home_score/away_score off each one
    to compute max lead — that's the score *before* the play, so we also
    fold in the final to catch a last-play margin spike.
    """
    max_lead = 0
    for r in records:
        hs = getattr(r, "home_score", 0) or 0
        as_ = getattr(r, "away_score", 0) or 0
        margin = abs(hs - as_)
        if margin > max_lead:
            max_lead = margin
    home_final = game_info.get("home_final")
    away_final = game_info.get("away_final")
    if home_final is not None and away_final is not None:
        margin = abs(home_final - away_final)
        if margin > max_lead:
            max_lead = margin
    away = game_info.get("away_abbr", "")
    home = game_info.get("home_abbr", "")
    return {
        "away": away,
        "home": home,
        "away_final": away_final,
        "home_final": home_final,
        "max_lead": max_lead,
    }


def write_trades_csv(trades: list[dict], filepath) -> None:
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for t in trades:
            rec = t["rec"]
            writer.writerow({
                "game_tag": rec.get("game_tag", ""),
                "timestamp": rec.get("timestamp", ""),
                "inning": rec.get("inning", ""),
                "half": rec.get("half", ""),
                "event": t["event"],
                "mlb_event": rec.get("mlb_event", ""),
                "market_type": t["market_type"],
                "market_label": t["market_label"],
                "market_ticker": t["market_ticker"],
                "entry_price": round(t["entry_price"], 4),
                "sell_target": round(t["sell_target"], 4),
                "predicted_delta": round(t["predicted_delta"], 4),
                "ev_per_contract": round(t["ev_per_contract"], 4),
                "net_expected_profit": round(t.get("net_expected_profit", 0.0), 4),
                "contracts": t["contracts"],
                "budget": round(t["budget"], 2),
                "pnl_per_contract": round(t["pnl_per_contract"], 4),
                "filled": t["filled"],
                "gross_pnl": round(t.get("gross_pnl", t["pnl"]), 2),
                "buy_fee": round(t.get("buy_fee", 0.0), 2),
                "sell_fee": round(t.get("sell_fee", 0.0), 2),
                "fees": round(t.get("fees", 0.0), 2),
                "pnl": round(t["pnl"], 2),
            })
