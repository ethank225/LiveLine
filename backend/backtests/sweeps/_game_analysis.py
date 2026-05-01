"""
Shared components for game-level backtest diagnostics.

Pure helpers — no script logic, no argparse, no global state. Every function
takes its inputs explicitly and returns a DataFrame, a dict, or a string.
The runner (`analyze_games.py`) wires them together; future sweeps that need
"how does this subset of games look" can import what they need from here.

Conventions:
  - DataFrames flow through unchanged where possible. Bucket / leaderboard
    / characteristic helpers all return DataFrames whose columns are stable
    across modes so the printer can render them uniformly.
  - Subset-agnostic helpers (`game_characteristics_summary`,
    `compare_subsets`, `filter_audit`, `late_inning_close_audit`) take a
    list of `game_id` ints. Pass any subset — bottom-20, top-20, tied
    games, HR-only — they don't care.
  - Bucket boundaries are constants below so bad and good modes use
    identical bins (counts only make sense across modes if the bins line
    up).
"""

from __future__ import annotations

import glob
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BACKTESTS_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = _BACKTESTS_ROOT / "reports"
CACHE_ROOT = _BACKTESTS_ROOT / "results" / "cache"


# ---------------------------------------------------------------------------
# Filter defaults — mirror app.market_selector.DEFAULT_SETTINGS. Kept as
# module constants rather than imported because the sweep modules
# deliberately don't depend on the live runtime.
# ---------------------------------------------------------------------------

DEFAULT_MIN_MOVE = 0.04          # 4 cents
DEFAULT_MIN_MOVE_TO_FEE_RATIO = 2.0


# ---------------------------------------------------------------------------
# Bucket boundaries — shared across bad and good modes so counts are
# comparable. Bins are inclusive on the right per pd.cut convention.
# ---------------------------------------------------------------------------

PNL_BINS = [-float("inf"), -500, -200, 0, 200, 500, float("inf")]
PNL_LABELS = ["<-$500", "-$500..-$200", "-$200..$0",
              "$0..$200", "$200..$500", ">$500"]

FINAL_MARGIN_BINS = [-1, 1, 3, 5, float("inf")]
FINAL_MARGIN_LABELS = ["0-1 runs", "2-3 runs", "4-5 runs", "6+ runs"]

LEAD_VOL_BINS = [-1, 1.0, 2.0, 3.0, float("inf")]
LEAD_VOL_LABELS = ["<1.0 (wire-to-wire)", "1.0-2.0 (some shifts)",
                   "2.0-3.0 (back-and-forth)", "3.0+ (chaotic)"]

TRADE_COUNT_BINS = [-1, 7, 12, 18, 24, float("inf")]
TRADE_COUNT_LABELS = ["<8", "8-12", "13-18", "19-24", "25+"]

FILL_RATE_BINS = [-1, 30, 45, 60, 75, 101]
FILL_RATE_LABELS = ["<30%", "30-45%", "45-60%", "60-75%", "75%+"]


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class PlayState:
    """One row of the play-by-play cache. Mirrors the fields the backtest
    cache writes per game; we keep it as a dataclass rather than relying
    on the dict shape so the consumer side has one fixed contract."""
    timestamp: str
    inning: int
    top_or_bot: str   # "top" | "bot"
    outs: int
    runners: str      # "000" / "100" / etc. — first/second/third occupied bits
    home_score: int
    away_score: int

    @property
    def margin(self) -> int:
        return self.home_score - self.away_score


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _latest(pattern: str) -> Path:
    paths = sorted(glob.glob(str(REPORTS_DIR / pattern)),
                   key=lambda p: Path(p).stat().st_mtime)
    if not paths:
        raise SystemExit(f"No file matching {pattern} in {REPORTS_DIR}")
    return Path(paths[-1])


def load_latest_reports() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the most recent per-game and per-trade CSVs (by mtime). Prints
    the filenames so it's clear which run is being analyzed."""
    games_csv = _latest("per_game_pnl_single_*.csv")
    trades_csv = _latest("multi_market_single_trades_*.csv")
    print(f"Per-game CSV:  {games_csv.name}")
    print(f"Per-trade CSV: {trades_csv.name}")

    games = pd.read_csv(games_csv)
    trades = pd.read_csv(trades_csv)
    games["game_id"] = games["game_tag"].str.split().str[0].astype(int)
    trades["game_id"] = trades["game_tag"].str.split().str[0].astype(int)
    return games, trades


def _build_game_id_to_date_index() -> dict[int, str]:
    """Walk cache/<date>/<game_id>/ once to build a flat lookup. Cheaper
    than re-scanning per game when the analysis touches hundreds."""
    idx: dict[int, str] = {}
    for date_dir in sorted(CACHE_ROOT.iterdir()):
        if not date_dir.is_dir():
            continue
        for game_dir in date_dir.iterdir():
            if game_dir.is_dir() and (game_dir / "game.json").exists():
                try:
                    idx[int(game_dir.name)] = date_dir.name
                except ValueError:
                    continue
    return idx


def load_cached_game_state(game_ids: list[int]) -> dict[int, list[PlayState]]:
    """Read play-by-play for each requested game_id from the cache. Returns
    a mapping; games whose cache is missing are simply absent. Prints the
    match rate so a low-cache run is visible immediately."""
    idx = _build_game_id_to_date_index()
    out: dict[int, list[PlayState]] = {}
    for gid in set(game_ids):
        date = idx.get(int(gid))
        if not date:
            continue
        path = CACHE_ROOT / date / str(gid) / "game.json"
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        plays = [
            PlayState(
                timestamp=r["timestamp"],
                inning=int(r["inning"]),
                top_or_bot=r["half"],
                outs=int(r["outs_before"]),
                runners=r["runners_before"],
                home_score=int(r["home_score"]),
                away_score=int(r["away_score"]),
            )
            for r in data.get("records", [])
        ]
        if plays:
            out[int(gid)] = plays
    print(f"Cached game-state loaded: {len(out)}/{len(set(game_ids))} games")
    return out


# ---------------------------------------------------------------------------
# Per-game metrics
# ---------------------------------------------------------------------------

def compute_lead_volatility(plays: list[PlayState]) -> float:
    """Population std of running margin across plays. Wire-to-wire games
    are near 0; back-and-forth games trend higher. ddof=0 so a one-play
    game returns 0 instead of NaN."""
    if not plays:
        return float("nan")
    margins = pd.Series([p.margin for p in plays])
    return float(margins.std(ddof=0))


def attach_per_game_lead_volatility(
    per_game_df: pd.DataFrame,
    cached_states: dict[int, list[PlayState]],
) -> pd.DataFrame:
    """Add a `lead_vol` column to the per-game frame. Games with no cache
    get NaN — handled gracefully downstream."""
    out = per_game_df.copy()
    out["lead_vol"] = out["game_id"].map(
        lambda gid: compute_lead_volatility(cached_states.get(int(gid), []))
    )
    return out


# ---------------------------------------------------------------------------
# Trade enrichment
# ---------------------------------------------------------------------------

def enrich_trades_with_state(
    trades_df: pd.DataFrame,
    cached_states: dict[int, list[PlayState]],
) -> pd.DataFrame:
    """Join each trade to the play state at its timestamp.

    The backtest sync writes `rec.timestamp` verbatim onto every synced
    trade row (kalshi_sync._build_synced_record line 409), so an exact
    string-equality merge keys correctly without needing asof tolerance.
    Trades whose game has no cache, or whose timestamp doesn't match a
    play, get NaN cached fields and remain in the frame.
    """
    pieces = []
    for gid, group in trades_df.groupby("game_id"):
        plays = cached_states.get(int(gid))
        if not plays:
            piece = group.copy()
            for c in ("cached_inning", "cached_half", "cached_outs",
                      "cached_runners", "cached_home_score",
                      "cached_away_score", "cached_margin", "lead_vol_so_far"):
                piece[c] = pd.NA
            pieces.append(piece)
            continue

        plays_df = pd.DataFrame([{
            "timestamp": p.timestamp,
            "cached_inning": p.inning,
            "cached_half": p.top_or_bot,
            "cached_outs": p.outs,
            "cached_runners": p.runners,
            "cached_home_score": p.home_score,
            "cached_away_score": p.away_score,
            "cached_margin": p.margin,
        } for p in plays])

        # Lead volatility computed at each play (population std of all
        # margins up to and including this play). Useful for "would a
        # pre-trade volatility gate have caught this?" audits.
        plays_df["lead_vol_so_far"] = (
            plays_df["cached_margin"].expanding().std(ddof=0).fillna(0)
        )

        merged = group.merge(plays_df, how="left", on="timestamp")
        pieces.append(merged)

    enriched = pd.concat(pieces, ignore_index=True) if pieces else trades_df.copy()

    # Convenience derived columns used by leaderboards and characteristics.
    enriched["state"] = enriched["cached_margin"].apply(_state_label)
    enriched["phase"] = enriched["cached_inning"].apply(_phase_label)

    matched = enriched["cached_inning"].notna().sum()
    print(f"Trades joined to cached state: {matched}/{len(enriched)} "
          f"({100 * matched / max(len(enriched), 1):.1f}%)")
    return enriched


def _state_label(margin) -> str:
    if pd.isna(margin):
        return "?"
    margin = int(margin)
    if margin > 0:
        return f"home+{margin}"
    if margin < 0:
        return f"away+{-margin}"
    return "tied"


def _phase_label(inning) -> str:
    if pd.isna(inning):
        return "?"
    inning = int(inning)
    if inning <= 3:
        return "early"
    if inning <= 6:
        return "mid"
    return "late"


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

def _bucket_summary(
    df: pd.DataFrame,
    bucket_col: str,
    labels: list[str],
    *,
    include_lead_vol: bool = False,
) -> pd.DataFrame:
    """Shared aggregator for every bucket function. Returns one row per
    label (in label order, even if empty) with: n_games, total_net,
    avg_net, worst_game, worst_net, avg_trades, avg_fill%."""
    rows = []
    for lbl in labels:
        sub = df[df[bucket_col] == lbl]
        if len(sub):
            worst_idx = sub["net_pnl"].idxmin()
            worst_tag = sub.loc[worst_idx, "game_tag"]
            worst_net = sub.loc[worst_idx, "net_pnl"]
        else:
            worst_tag = "—"
            worst_net = float("nan")
        row = {
            "bucket": lbl,
            "n_games": len(sub),
            "total_net": round(sub["net_pnl"].sum(), 2) if len(sub) else 0,
            "avg_net": round(sub["net_pnl"].mean(), 2) if len(sub) else float("nan"),
            "worst_game": worst_tag,
            "worst_net": round(worst_net, 2) if len(sub) else float("nan"),
            "avg_trades": round(sub["trades"].mean(), 1) if len(sub) else float("nan"),
            "avg_fill%": round((sub["fills"] / sub["trades"] * 100).mean(), 1)
                          if len(sub) else float("nan"),
        }
        if include_lead_vol:
            row["avg_lead_σ"] = (round(sub["lead_vol"].mean(), 2)
                                 if len(sub) and "lead_vol" in sub else float("nan"))
        rows.append(row)
    return pd.DataFrame(rows)


def pnl_distribution_buckets(per_game_df: pd.DataFrame) -> pd.DataFrame:
    g = per_game_df.copy()
    g["bucket"] = pd.cut(g["net_pnl"], bins=PNL_BINS, labels=PNL_LABELS)
    return _bucket_summary(g, "bucket", PNL_LABELS, include_lead_vol=True)


def score_margin_buckets(per_game_df: pd.DataFrame) -> pd.DataFrame:
    g = per_game_df.copy()
    g["final_margin"] = (g["home_final"] - g["away_final"]).abs()
    g["bucket"] = pd.cut(g["final_margin"], bins=FINAL_MARGIN_BINS,
                         labels=FINAL_MARGIN_LABELS)
    return _bucket_summary(g, "bucket", FINAL_MARGIN_LABELS)


def lead_volatility_buckets(per_game_df: pd.DataFrame) -> pd.DataFrame:
    g = per_game_df.copy()
    g["bucket"] = pd.cut(g["lead_vol"], bins=LEAD_VOL_BINS,
                         labels=LEAD_VOL_LABELS)
    return _bucket_summary(g, "bucket", LEAD_VOL_LABELS)


def trade_count_buckets(per_game_df: pd.DataFrame) -> pd.DataFrame:
    g = per_game_df.copy()
    g["bucket"] = pd.cut(g["trades"], bins=TRADE_COUNT_BINS,
                         labels=TRADE_COUNT_LABELS)
    return _bucket_summary(g, "bucket", TRADE_COUNT_LABELS)


def fill_rate_buckets(per_game_df: pd.DataFrame) -> pd.DataFrame:
    g = per_game_df.copy()
    g["fill_rate"] = g["fills"] / g["trades"] * 100
    g["bucket"] = pd.cut(g["fill_rate"], bins=FILL_RATE_BINS,
                         labels=FILL_RATE_LABELS)
    return _bucket_summary(g, "bucket", FILL_RATE_LABELS)


# ---------------------------------------------------------------------------
# Leaderboards
# ---------------------------------------------------------------------------

def top_n_games(
    per_game_df: pd.DataFrame, n: int = 15, ascending: bool = True
) -> pd.DataFrame:
    """Worst (ascending=True) or best (ascending=False) n games by net P&L."""
    g = per_game_df.copy()
    g["fill_rate"] = (g["fills"] / g["trades"] * 100).round(1)
    g["dollar_per_trade"] = (g["net_pnl"] / g["trades"]).round(2)
    g["score"] = g["away_final"].astype(str) + "-" + g["home_final"].astype(str)
    g["fmar"] = (g["home_final"] - g["away_final"]).abs()
    sorted_g = g.nsmallest(n, "net_pnl") if ascending else g.nlargest(n, "net_pnl")
    cols = ["game_tag", "score", "fmar", "lead_vol", "trades", "fills",
            "fill_rate", "gross_pnl", "fees", "net_pnl", "dollar_per_trade"]
    out = sorted_g[cols].rename(columns={
        "game_tag": "matchup", "lead_vol": "lead_σ", "fill_rate": "fill%",
        "gross_pnl": "gross", "net_pnl": "net", "dollar_per_trade": "$/trade",
    })
    out["lead_σ"] = out["lead_σ"].round(2)
    for c in ("gross", "fees", "net"):
        out[c] = out[c].round(2)
    return out


def top_n_individual_trades(
    trades_enriched_df: pd.DataFrame, n: int = 30, ascending: bool = True
) -> pd.DataFrame:
    """Worst (ascending) or best (descending) n trades by per-trade net P&L."""
    t = trades_enriched_df.copy()
    t = t.nsmallest(n, "pnl") if ascending else t.nlargest(n, "pnl")
    t["mt"] = t["market_type"].map({
        "moneyline": "ML", "over_under": "O/U", "spread": "SPR"
    }).fillna(t["market_type"])
    t["actual_move"] = t["pnl_per_contract"].round(4)
    t["pred_move"] = t["predicted_delta"].round(4)
    cols = [
        "game_tag", "cached_inning", "cached_outs", "cached_runners",
        "cached_home_score", "cached_away_score", "state", "phase", "event",
        "mt", "market_label", "pred_move", "actual_move", "entry_price",
        "sell_target", "contracts", "filled", "gross_pnl", "fees", "pnl",
    ]
    out = t[cols].rename(columns={
        "game_tag": "matchup", "cached_inning": "inn", "cached_outs": "out",
        "cached_runners": "runs", "cached_home_score": "h",
        "cached_away_score": "a", "market_label": "label",
        "entry_price": "entry", "sell_target": "tgt", "contracts": "qty",
        "gross_pnl": "gross",
    })
    for c in ("gross", "fees", "pnl"):
        out[c] = out[c].round(2)
    return out


# ---------------------------------------------------------------------------
# Specialty filters
# ---------------------------------------------------------------------------

def fee_trap_games(
    per_game_df: pd.DataFrame,
    gross_threshold: float = 0.0,
    net_threshold: float = -300.0,
) -> pd.DataFrame:
    """Games where gross was positive but fees pulled net under the floor.
    Used in bad mode to surface "the system fired into a break-even
    environment and got eaten by round-trip fees" cases."""
    g = per_game_df.copy()
    g["fill_rate"] = (g["fills"] / g["trades"] * 100).round(1)
    g["score"] = g["away_final"].astype(str) + "-" + g["home_final"].astype(str)
    traps = g[(g["gross_pnl"] > gross_threshold) & (g["net_pnl"] < net_threshold)].copy()
    traps["fee_to_gross"] = (traps["fees"] / traps["gross_pnl"]).round(2)
    traps = traps.sort_values("net_pnl")
    cols = ["game_tag", "score", "gross_pnl", "fees", "net_pnl",
            "fee_to_gross", "trades", "fill_rate", "lead_vol"]
    out = traps[cols].rename(columns={
        "game_tag": "matchup", "gross_pnl": "gross", "net_pnl": "net",
        "fee_to_gross": "fee/gross", "fill_rate": "fill%",
        "lead_vol": "lead_σ",
    })
    for c in ("gross", "fees", "net"):
        out[c] = out[c].round(2)
    out["lead_σ"] = out["lead_σ"].round(2)
    return out


def profit_concentration_games(
    per_game_df: pd.DataFrame, net_threshold: float = 1000.0
) -> pd.DataFrame:
    """Counterpart to fee_trap_games for good mode: which games carried
    more than `net_threshold` of net P&L? These are the heavy lifters
    whose absence would gut the strategy's total."""
    g = per_game_df.copy()
    g["fill_rate"] = (g["fills"] / g["trades"] * 100).round(1)
    g["score"] = g["away_final"].astype(str) + "-" + g["home_final"].astype(str)
    big = g[g["net_pnl"] > net_threshold].copy()
    big["fee_to_gross"] = (big["fees"] / big["gross_pnl"].replace(0, pd.NA)).round(2)
    big = big.sort_values("net_pnl", ascending=False)
    cols = ["game_tag", "score", "gross_pnl", "fees", "net_pnl",
            "fee_to_gross", "trades", "fill_rate", "lead_vol"]
    out = big[cols].rename(columns={
        "game_tag": "matchup", "gross_pnl": "gross", "net_pnl": "net",
        "fee_to_gross": "fee/gross", "fill_rate": "fill%",
        "lead_vol": "lead_σ",
    })
    for c in ("gross", "fees", "net"):
        out[c] = out[c].round(2)
    out["lead_σ"] = out["lead_σ"].round(2)
    return out


# ---------------------------------------------------------------------------
# Subset characterization
# ---------------------------------------------------------------------------

def _trade_dist_by(trades: pd.DataFrame, col: str) -> dict[Any, float]:
    if len(trades) == 0:
        return {}
    counts = Counter(trades[col].dropna())
    total = sum(counts.values())
    return {k: round(100 * v / total, 1) for k, v in counts.most_common()}


def game_characteristics_summary(
    per_game_df: pd.DataFrame,
    trades_enriched_df: pd.DataFrame,
    game_ids: list[int],
) -> dict[str, Any]:
    """Compute the full characteristics dict for an arbitrary subset.

    Filled-only metrics (pred move, actual move, gross/trade, fees/trade,
    net/trade) are restricted to filled rows so unfilled rejections don't
    drag averages toward zero. Everything else uses the full trade set.
    """
    games_subset = per_game_df[per_game_df["game_id"].isin(game_ids)]
    trades_subset = trades_enriched_df[trades_enriched_df["game_id"].isin(game_ids)]
    filled = trades_subset[trades_subset["filled"] == True]  # noqa: E712

    avg_trades = trades_subset.groupby("game_id").size().mean() \
        if len(trades_subset) else float("nan")
    avg_fills = (filled.groupby("game_id").size()
                 .reindex(games_subset["game_id"]).fillna(0).mean()) \
        if len(games_subset) else float("nan")
    avg_fill_rate = (games_subset["fills"] / games_subset["trades"] * 100).mean() \
        if len(games_subset) else float("nan")

    pred_avg = filled["predicted_delta"].mean() if len(filled) else float("nan")
    actual_avg = filled["pnl_per_contract"].mean() if len(filled) else float("nan")
    pred_actual_ratio = (pred_avg / actual_avg) \
        if (actual_avg and abs(actual_avg) > 1e-9) else float("nan")

    avg_gross = (filled["gross_pnl"] / filled["contracts"]).mean() if len(filled) else float("nan")
    avg_fees = (filled["fees"] / filled["contracts"]).mean() if len(filled) else float("nan")
    avg_net = (filled["pnl"] / filled["contracts"]).mean() if len(filled) else float("nan")

    mt_dist = _trade_dist_by(trades_subset, "market_type")
    ev_dist = _trade_dist_by(trades_subset, "event")

    cached_margin = trades_subset["cached_margin"].dropna()
    avg_margin = cached_margin.abs().mean() if len(cached_margin) else float("nan")
    avg_inning = trades_subset["cached_inning"].dropna().mean()

    games_with_vol = games_subset["lead_vol"].dropna() if "lead_vol" in games_subset else pd.Series(dtype=float)
    avg_lead_vol = games_with_vol.mean() if len(games_with_vol) else float("nan")

    pct_close = 100 * (cached_margin.abs() <= 1).sum() / max(len(cached_margin), 1)

    def _r(v, p=4):
        return round(v, p) if (v is not None and not pd.isna(v)) else None

    return {
        "n_games": len(games_subset),
        "avg_trades": _r(avg_trades, 2),
        "avg_fills": _r(avg_fills, 2),
        "avg_fill%": _r(avg_fill_rate, 1),
        "avg_pred_move": _r(pred_avg, 4),
        "avg_actual_move": _r(actual_avg, 4),
        "pred/actual": _r(pred_actual_ratio, 2),
        "avg_gross/trade": _r(avg_gross, 3),
        "avg_fees/trade": _r(avg_fees, 3),
        "avg_net/trade": _r(avg_net, 3),
        "%ML": mt_dist.get("moneyline", 0),
        "%OU": mt_dist.get("over_under", 0),
        "%SPR": mt_dist.get("spread", 0),
        "%HR": ev_dist.get("HR", 0), "%2B": ev_dist.get("2B", 0),
        "%1B": ev_dist.get("1B", 0), "%BB": ev_dist.get("BB", 0),
        "%K": ev_dist.get("K", 0), "%OUT": ev_dist.get("OUT", 0),
        "%DP": ev_dist.get("DP", 0),
        "avg_|margin|@trade": _r(avg_margin, 2),
        "avg_inning@trade": _r(avg_inning, 2),
        "avg_lead_σ": _r(avg_lead_vol, 2),
        "%trades_close": _r(pct_close, 1),
    }


def compare_subsets(
    per_game_df: pd.DataFrame,
    trades_enriched_df: pd.DataFrame,
    subset_a_ids: list[int],
    subset_b_ids: list[int],
    label_a: str = "A",
    label_b: str = "B",
) -> pd.DataFrame:
    """Side-by-side characteristics. Returns a long-format DataFrame
    with one row per metric — easier to read in a terminal than wide."""
    a = game_characteristics_summary(per_game_df, trades_enriched_df, subset_a_ids)
    b = game_characteristics_summary(per_game_df, trades_enriched_df, subset_b_ids)
    rows = [{"metric": k, label_a: a[k], label_b: b[k]} for k in a.keys()]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Filter audits
# ---------------------------------------------------------------------------

def filter_audit(
    trades_enriched_df: pd.DataFrame,
    game_ids: list[int],
    min_move_threshold: float = DEFAULT_MIN_MOVE,
    ratio_threshold: float = DEFAULT_MIN_MOVE_TO_FEE_RATIO,
) -> dict[str, Any]:
    """How many of this subset's trades cleared the existing filters and
    still went net-negative? `min_move_threshold` is the predicted delta
    floor in dollars (0.04 = 4¢). `ratio_threshold` is the
    min_move_to_fee_ratio gate. Both gates are inclusive at threshold."""
    sub = trades_enriched_df[trades_enriched_df["game_id"].isin(game_ids)].copy()
    if sub.empty:
        return {"n_trades": 0}

    sub["pred_abs"] = sub["predicted_delta"].abs()

    # Per-contract round-trip fee. Filled rows have both legs in the CSV;
    # unfilled rows only paid the buy fee (no sell ever placed), so
    # double the buy fee as the would-be round-trip — matches the live
    # filter's notional cost model.
    rt_fee = sub["buy_fee"].fillna(0) + sub["sell_fee"].fillna(0)
    unfilled = sub["filled"] != True   # noqa: E712
    rt_fee = rt_fee.where(~unfilled, sub["buy_fee"].fillna(0) * 2)
    per_contract_fee = rt_fee / sub["contracts"].replace(0, pd.NA)
    sub["fee_ratio"] = (sub["pred_abs"] * 100) / per_contract_fee

    passed_min = sub[sub["pred_abs"] >= min_move_threshold]
    passed_ratio = sub[sub["fee_ratio"] >= ratio_threshold]
    pmm_neg = passed_min[passed_min["pnl"] < 0]
    pfr_neg = passed_ratio[passed_ratio["pnl"] < 0]

    losers = sub[(sub["pnl"] < 0) & (sub["pred_abs"] >= min_move_threshold)
                 & (sub["fee_ratio"] >= ratio_threshold)].copy()
    avg_gap = (losers["predicted_delta"] - losers["pnl_per_contract"]).abs().mean() \
        if len(losers) else float("nan")

    return {
        "n_trades": len(sub),
        "passed_min_move": len(passed_min),
        "passed_min_move_neg": len(pmm_neg),
        "passed_fee_ratio": len(passed_ratio),
        "passed_fee_ratio_neg": len(pfr_neg),
        "n_losers_passing_both": len(losers),
        "avg_pred_actual_gap": round(avg_gap, 4) if not pd.isna(avg_gap) else None,
    }


def late_inning_close_audit(
    trades_enriched_df: pd.DataFrame, game_ids: list[int]
) -> dict[str, Any]:
    """What fraction of net-negative trades fired in `inning ≥ 9 AND
    |margin| ≤ 1`? This isolates the late-game-tied tail that Section 8
    keeps surfacing in worst-trade leaderboards."""
    sub = trades_enriched_df[trades_enriched_df["game_id"].isin(game_ids)]
    losers = sub[sub["pnl"] < 0]
    if losers.empty:
        return {"n_losers": 0}
    has_state = losers.dropna(subset=["cached_inning", "cached_margin"])
    late_close = has_state[(has_state["cached_inning"] >= 9)
                           & (has_state["cached_margin"].abs() <= 1)]
    return {
        "n_losers": len(losers),
        "n_with_state": len(has_state),
        "n_late_close": len(late_close),
        "pct_late_close": round(100 * len(late_close) / max(len(has_state), 1), 1),
        "total_late_close_pnl": round(late_close["pnl"].sum(), 2),
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_section_header(num: int, title: str):
    print(f"\n{'=' * 100}")
    print(f"Section {num} — {title}")
    print(f"{'=' * 100}")


def print_table(df: pd.DataFrame, title: str | None = None,
                max_rows: int | None = None, max_colwidth: int = 60):
    if title:
        print(f"\n  {title}")
    if df is None or len(df) == 0:
        print("  (empty)")
        return
    if max_rows:
        df = df.head(max_rows)
    print(df.to_string(index=False, max_colwidth=max_colwidth))


def format_dollars(x) -> str:
    if pd.isna(x):
        return "    —"
    return f"{x:+8.2f}"


def format_pct(x) -> str:
    if pd.isna(x):
        return "  —"
    return f"{x:5.1f}%"


def format_net(x) -> str:
    return format_dollars(x)


# ---------------------------------------------------------------------------
# Findings synthesis
# ---------------------------------------------------------------------------

def synthesize_bad_game_findings(
    per_game_df: pd.DataFrame, trades_enriched_df: pd.DataFrame
) -> str:
    """Build the bad-mode findings paragraph. Pulls bottom-20 vs top-20
    deltas, the worst-tail fee/gross breakdown, and the close-game
    recommendation in one block."""
    bottom = per_game_df.nsmallest(20, "net_pnl")
    top = per_game_df.nlargest(20, "net_pnl")

    bot_vol = bottom["lead_vol"].mean()
    top_vol = top["lead_vol"].mean()
    bot_tc = bottom["trades"].mean()
    top_tc = top["trades"].mean()
    bot_fr = (bottom["fills"] / bottom["trades"] * 100).mean()
    top_fr = (top["fills"] / top["trades"] * 100).mean()
    bot_fm = (bottom["home_final"] - bottom["away_final"]).abs().mean()
    top_fm = (top["home_final"] - top["away_final"]).abs().mean()

    bot10 = per_game_df.nsmallest(10, "net_pnl")
    fees = bot10["fees"].sum()
    gross = bot10["gross_pnl"].sum()
    net = bot10["net_pnl"].sum()
    fee_pct_of_loss = fees / max(abs(net), 1) * 100

    audit = late_inning_close_audit(trades_enriched_df, list(bot10["game_id"]))
    fa = filter_audit(trades_enriched_df, list(bot10["game_id"]))

    grossp_games = per_game_df[per_game_df["gross_pnl"] > 0]
    median_fee_gross = (grossp_games["fees"] / grossp_games["gross_pnl"]).median() \
        if len(grossp_games) else float("nan")

    lines = []
    lines.append("\n  Top patterns distinguishing bad games from good games:")
    lines.append(
        f"  • Lead volatility (margin σ): bad={bot_vol:.2f} vs good={top_vol:.2f} "
        f"(Δ={bot_vol - top_vol:+.2f}). Bad games have more back-and-forth scoring."
    )
    direction = "fewer" if bot_tc < top_tc else "more"
    lines.append(
        f"  • Trade count: bad={bot_tc:.1f} vs good={top_tc:.1f} "
        f"(Δ={bot_tc - top_tc:+.1f}). Bad games take {direction} trades — "
        f"consistent with low fill rates: orders fire but can't get out of "
        f"the way, so the system enters fewer net positions per game."
    )
    fr_dir = "Lower" if bot_fr < top_fr else "Higher"
    lines.append(
        f"  • Fill rate: bad={bot_fr:.1f}% vs good={top_fr:.1f}% "
        f"(Δ={bot_fr - top_fr:+.1f}pp). {fr_dir} fill rate in bad games — "
        f"buy fees are sunk on every order, but unfilled trades collect a "
        f"forced-exit IOC at the end of the clean window, paying both legs."
    )
    lines.append(
        f"  • Final margin: bad-20 avg={bot_fm:.1f} vs good-20 avg={top_fm:.1f}. "
        f"Bottom tail is mixed — close-game disasters and a few blowouts both "
        f"contribute. The close-game tail is sharper per the lead-volatility "
        f"section."
    )

    lines.append("\n  Single biggest contributor to the worst-game tail:")
    lines.append(f"  Worst 10 games: gross={gross:.0f}, fees={fees:.0f}, net={net:.0f}.")
    lines.append(
        f"  Fees are {fee_pct_of_loss:.0f}% of |net loss|. Fee bleed dominates — "
        f"gross is {'positive' if gross > 0 else 'roughly flat or negative'}, the "
        f"system fires too many low-edge orders that round-trip into fee drag."
    )

    lines.append("\n  Filter recommendation:")
    if fa.get("n_trades", 0):
        n_trades = fa["n_trades"]
        n_passing_losers = fa["n_losers_passing_both"]
        gap = fa["avg_pred_actual_gap"]
        lines.append(
            f"  Of {n_trades} trades in the worst 10 games, "
            f"{fa['passed_min_move']} passed min_move and {fa['passed_fee_ratio']} "
            f"passed the fee-ratio gate. {n_passing_losers} of those went net-negative "
            f"with an average |predicted − actual| gap of {gap}."
        )
    if audit.get("n_with_state"):
        lines.append(
            f"  Late-inning + close-margin (inning ≥ 9 AND |margin| ≤ 1) accounts for "
            f"{audit['n_late_close']}/{audit['n_with_state']} losing trades "
            f"({audit['pct_late_close']}%) in the worst 10 games, totaling "
            f"{audit['total_late_close_pnl']:+.2f}. A `late_close` gate that "
            f"suppresses spread/ML candidates when inning ≥ 9 AND |margin| ≤ 1 "
            f"would be a targeted intervention."
        )

    lines.append("\n  Are existing filters sufficient?")
    if not pd.isna(median_fee_gross):
        lines.append(
            f"  Median fee/gross ratio (gross-positive games): {median_fee_gross:.2f}."
        )
        if median_fee_gross > 1:
            lines.append(
                "  Fees > gross on the median gross-positive game. Existing min_move "
                "and fee-ratio gates pass too many marginal trades — tighten "
                "min_move_cents or raise min_move_to_fee_ratio."
            )
        else:
            lines.append(
                "  Fee/gross is acceptable on the median gross-positive game; the "
                "leak is concentrated in the volatile-late-close tail. Tightening "
                "filters helps that tail, not the median."
            )
    return "\n".join(lines)


def synthesize_good_game_findings(
    per_game_df: pd.DataFrame, trades_enriched_df: pd.DataFrame
) -> str:
    """Good-mode counterpart: what's actually working, and how concentrated
    is the profit?"""
    top = per_game_df.nlargest(20, "net_pnl")
    bottom = per_game_df.nsmallest(20, "net_pnl")

    top_vol = top["lead_vol"].mean()
    bot_vol = bottom["lead_vol"].mean()
    top_tc = top["trades"].mean()
    top_fr = (top["fills"] / top["trades"] * 100).mean()

    big_games = per_game_df[per_game_df["net_pnl"] > 1000]
    big_total = big_games["net_pnl"].sum()
    all_total = per_game_df["net_pnl"].sum()
    pct_concentration = 100 * big_total / max(all_total, 1) if all_total else 0

    # Top 10 leaderboard fee mechanics
    top10 = per_game_df.nlargest(10, "net_pnl")
    fees = top10["fees"].sum()
    gross = top10["gross_pnl"].sum()
    net = top10["net_pnl"].sum()
    fee_pct_of_gross = fees / max(gross, 1) * 100 if gross > 0 else float("nan")

    chars = game_characteristics_summary(
        per_game_df, trades_enriched_df, list(top["game_id"])
    )

    lines = []
    lines.append("\n  Top patterns distinguishing good games:")
    lines.append(
        f"  • Lead volatility: top-20 avg σ={top_vol:.2f} vs bottom-20 σ={bot_vol:.2f}. "
        f"Good games tend to have {'less' if top_vol < bot_vol else 'more'} "
        f"in-game margin volatility, supporting the live picker's "
        f"expected-direction bets between scoring shifts."
    )
    lines.append(
        f"  • Trade count: top-20 avg={top_tc:.1f} trades, "
        f"avg fill rate {top_fr:.1f}%. High activity + high fill rate is the "
        f"profile that lets edge accumulate across many small bets."
    )
    lines.append(
        f"  • Trade-side mix: %ML={chars['%ML']}, %O/U={chars['%OU']}, "
        f"%SPR={chars['%SPR']}. Pred/actual ratio on filled trades: "
        f"{chars['pred/actual']}. A ratio below 1 means actual exceeds "
        f"predicted — the model is conservative on the winners."
    )

    lines.append("\n  Profit concentration:")
    if all_total:
        lines.append(
            f"  Games with net > $1000: {len(big_games)}/{len(per_game_df)} "
            f"({100 * len(big_games) / len(per_game_df):.1f}%) carrying "
            f"{big_total:+.0f} of {all_total:+.0f} total ({pct_concentration:.0f}%). "
            f"The tail of big winners is doing most of the lifting."
        )
    if not pd.isna(fee_pct_of_gross):
        lines.append(
            f"  Top 10 games: gross={gross:.0f}, fees={fees:.0f}, net={net:.0f}. "
            f"Fees are {fee_pct_of_gross:.0f}% of gross even on the best games — "
            f"tightening filters would compress this further but might also "
            f"cut into the gross."
        )

    lines.append("\n  What to preserve:")
    lines.append(
        f"  • The trade frequency on top-20 games ({top_tc:.0f}/game) is the "
        f"engine of the strategy. Filter changes that drop average trade "
        f"count below ~15 risk killing the good tail along with the bad."
    )
    lines.append(
        f"  • {chars['%trades_close']}% of top-20 trades fire in close-margin "
        f"states (|margin|≤1). Close states are not uniformly bad — they're "
        f"split between good and bad outcomes. A naive close-state ban would "
        f"lose real edge here."
    )
    return "\n".join(lines)
