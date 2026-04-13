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

from backtests.constants import EVENT_ORDER

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


def _build_candidates(group: list[dict], alpha: float) -> list[dict]:
    """From a set of synced records sharing the same play, build candidate trades."""
    seen_tickers: set[str] = set()
    out = []
    for rec in group:
        pred = rec.get("predicted_ml_delta") or 0.0
        if pred == 0:
            continue
        entry = rec.get("price_before") or 0.0
        if entry <= 0 or _is_dead(entry, pred, alpha):
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

def _make_trade(cand: dict, budget: float, contracts: int) -> dict:
    return {
        **cand,
        "contracts": contracts,
        "budget": budget,
        "pnl": contracts * cand["pnl_per_contract"],
    }


def _execute_single(candidates: list[dict], max_dollars: float) -> list[dict]:
    if not candidates:
        return []
    best = max(candidates, key=lambda c: c["ev_per_contract"])
    if best["ev_per_contract"] <= 0:
        return []
    contracts = int(max_dollars / best["entry_price"])
    if contracts <= 0:
        return []
    return [_make_trade(best, max_dollars, contracts)]


def _execute_multi(candidates: list[dict], max_dollars: float) -> list[dict]:
    positive = [c for c in candidates if c["ev_per_contract"] > 0]
    if not positive:
        return []
    total_ev = sum(c["ev_per_contract"] for c in positive)
    if total_ev <= 0:
        return []
    trades = []
    for c in positive:
        budget = max_dollars * (c["ev_per_contract"] / total_ev)
        contracts = int(budget / c["entry_price"])
        if contracts <= 0:
            continue
        trades.append(_make_trade(c, budget, contracts))
    return trades


def run_comparison(
    all_synced: list[dict], alpha: float, max_dollars: float,
) -> tuple[list[dict], list[dict]]:
    """Run both modes on the same candidate set. Returns (single, multi)."""
    groups = _group_by_play(all_synced)
    single_trades: list[dict] = []
    multi_trades: list[dict] = []
    for group in groups.values():
        candidates = _build_candidates(group, alpha)
        single_trades.extend(_execute_single(candidates, max_dollars))
        multi_trades.extend(_execute_multi(candidates, max_dollars))
    return single_trades, multi_trades


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "fill_rate": 0.0, "avg_profit_fill": 0.0,
                "avg_loss_miss": 0.0, "ev_per_trade": 0.0,
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
        ("Total P&L", f"${s['total_pnl']:+,.0f}", f"${m['total_pnl']:+,.0f}",
         _fmt_diff_pct(s['total_pnl'], m['total_pnl'])),
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
    "contracts", "budget", "pnl_per_contract", "filled", "pnl",
]


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
                "contracts": t["contracts"],
                "budget": round(t["budget"], 2),
                "pnl_per_contract": round(t["pnl_per_contract"], 4),
                "filled": t["filled"],
                "pnl": round(t["pnl"], 2),
            })
