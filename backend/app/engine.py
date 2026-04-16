"""
Win expectancy engine.

Loads empirical WE and run-distribution lookup tables and computes
win-probability, over/under, and spread deltas for each batting event.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.constants import (
    ALL_OU_LINES, ALL_SPREAD_LINES,
    DEFAULT_OU_LINES, DEFAULT_SPREAD_LINES,
)

_DATA = Path(__file__).parent.parent / "data"

# Score-diff range stored in the empirical WE table
_SD_MIN, _SD_MAX = -8, 8

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GameState:
    inning: int       # 1-9 (extras capped at 9 for lookup)
    half: str         # "top" or "bot"
    outs: int         # 0, 1, 2
    runners: str      # "000" through "111" (first, second, third)
    score_diff: int   # home_runs - away_runs


@dataclass
class EventDelta:
    event: str
    win_expectancy_before: float
    win_expectancy_after: float
    delta: float                                # moneyline delta
    over_under: dict = field(default_factory=dict)   # {"7.5": {"before","after","delta"}}
    spread: dict = field(default_factory=dict)       # {"-1.5": {"before","after","delta"}}


# ---------------------------------------------------------------------------
# WE table
# ---------------------------------------------------------------------------

_we_table: Optional[dict[str, float]] = None


def load_win_expectancy_table() -> dict[str, float]:
    global _we_table
    if _we_table is None:
        with open(_DATA / "win_expectancy.json") as f:
            _we_table = json.load(f)
    return _we_table


def _make_key(state: GameState) -> str:
    inning = max(1, min(9, state.inning))
    sd = max(_SD_MIN, min(_SD_MAX, state.score_diff))
    return f"{inning}-{state.half}-{state.outs}-{state.runners}-{sd}"


def get_win_expectancy(state: GameState) -> float:
    """Look up home-team win probability. Falls back to nearest score_diff."""
    # Bottom 9+, home winning → game is already won (bot 9 isn't played or
    # home is batting with a lead and just needs to finish the inning)
    if state.half == "bot" and state.inning >= 9 and state.score_diff > 0:
        return 1.0

    table = load_win_expectancy_table()
    key = _make_key(state)
    if key in table:
        return table[key]

    # Fallback: search outward for nearest score_diff that exists
    inning = max(1, min(9, state.inning))
    sd = max(_SD_MIN, min(_SD_MAX, state.score_diff))
    for offset in range(1, _SD_MAX - _SD_MIN + 1):
        for candidate in (sd + offset, sd - offset):
            if _SD_MIN <= candidate <= _SD_MAX:
                k = f"{inning}-{state.half}-{state.outs}-{state.runners}-{candidate}"
                if k in table:
                    return table[k]
    return 0.5


# ---------------------------------------------------------------------------
# Run-distribution table
# ---------------------------------------------------------------------------

_rd_table: Optional[dict[str, list[float]]] = None


def _load_run_distribution() -> dict[str, list[float]]:
    global _rd_table
    if _rd_table is None:
        with open(_DATA / "run_distribution.json") as f:
            _rd_table = json.load(f)
    return _rd_table


def get_run_distribution(outs: int, runners: str) -> list[float]:
    """P(exactly i runs score in the remainder of this half-inning)."""
    table = _load_run_distribution()
    key = f"{outs}-{runners}"
    if key in table:
        return table[key]
    # Fallback to bases-empty same outs
    fallback = f"{outs}-000"
    return table.get(fallback, [1.0])


# ---------------------------------------------------------------------------
# Convolution helpers  (precomputed on first use)
# ---------------------------------------------------------------------------

def _convolve(a: list[float], b: list[float]) -> list[float]:
    """Convolve two discrete probability distributions."""
    la, lb = len(a), len(b)
    result = [0.0] * (la + lb - 1)
    for i in range(la):
        ai = a[i]
        if ai == 0:
            continue
        for j in range(lb):
            result[i + j] += ai * b[j]
    return result


_cum_conv: Optional[list[list[float]]] = None


def _get_cum_conv() -> list[list[float]]:
    """_cum_conv[n] = n-fold convolution of the 0-000 (full half-inning) dist."""
    global _cum_conv
    if _cum_conv is not None:
        return _cum_conv

    full = get_run_distribution(0, "000")
    maxn = 11  # up to 10 remaining half-innings
    cc: list[list[float]] = [[1.0]]  # 0-fold = degenerate (0 runs certain)
    for _ in range(1, maxn):
        cc.append(_convolve(cc[-1], full))
    _cum_conv = cc
    return _cum_conv


# ---------------------------------------------------------------------------
# Remaining half-innings
# ---------------------------------------------------------------------------

def _remaining_halves(inning: int, half: str):
    """
    Return (batting_full, fielding_full, batting_is_home).

    batting_full  = full half-innings still to play for the *batting* team
                    (not counting the current partial one)
    fielding_full = full half-innings still to play for the *fielding* team
    """
    n = max(1, min(9, inning))
    if half == "top":
        # away is batting
        return 9 - n, 10 - n, False
    else:
        # home is batting
        return 9 - n, 9 - n, True


# ---------------------------------------------------------------------------
# Over/Under probability
# ---------------------------------------------------------------------------

def compute_over_under_probability(
    home_score: int,
    away_score: int,
    inning: int,
    half: str,
    outs: int,
    runners: str,
    total_line: float,
) -> float:
    """P(final total runs > total_line).  E.g. total_line=8.5 → P(total >= 9)."""
    current_total = home_score + away_score
    runs_needed = math.ceil(total_line - current_total + 1e-9)
    # e.g. line 8.5, current 5 → need >= 4 more runs

    if runs_needed <= 0:
        return 1.0

    batting_full, fielding_full, _ = _remaining_halves(inning, half)
    cc = _get_cum_conv()

    partial = get_run_distribution(outs, runners)
    batting_dist = _convolve(partial, cc[batting_full])
    fielding_dist = cc[fielding_full]

    total_dist = _convolve(batting_dist, fielding_dist)

    if runs_needed >= len(total_dist):
        return 0.0
    return min(1.0, sum(total_dist[runs_needed:]))


# ---------------------------------------------------------------------------
# Spread probability
# ---------------------------------------------------------------------------

def compute_spread_probability(
    home_score: int,
    away_score: int,
    inning: int,
    half: str,
    outs: int,
    runners: str,
    spread_line: float,
) -> float:
    """
    P(home_final - away_final > spread_line).

    spread_line = -1.5  →  P(home doesn't lose by 2+)
    spread_line = +1.5  →  P(home wins by 2+)
    """
    score_diff = home_score - away_score
    margin_needed = spread_line - score_diff
    min_diff = math.floor(margin_needed) + 1  # min integer (home_rem - away_rem)

    batting_full, fielding_full, batting_is_home = _remaining_halves(inning, half)
    cc = _get_cum_conv()

    partial = get_run_distribution(outs, runners)
    batting_dist = _convolve(partial, cc[batting_full])
    fielding_dist = cc[fielding_full]

    if batting_is_home:
        home_dist, away_dist = batting_dist, fielding_dist
    else:
        away_dist, home_dist = batting_dist, fielding_dist

    # Prefix sum of away_dist for fast inner loop
    alen = len(away_dist)
    away_prefix = [0.0] * (alen + 1)
    for i in range(alen):
        away_prefix[i + 1] = away_prefix[i] + away_dist[i]

    prob = 0.0
    for h in range(len(home_dist)):
        # Need: h - a >= min_diff  →  a <= h - min_diff
        max_a = h - min_diff
        if max_a < 0:
            continue
        valid_a = min(max_a + 1, alen)
        prob += home_dist[h] * away_prefix[valid_a]

    return min(1.0, max(0.0, prob))


# ---------------------------------------------------------------------------
# Event state transitions  (unchanged from prior version)
# ---------------------------------------------------------------------------

_BB_TABLE = {
    "000": ("100", 0), "100": ("110", 0), "010": ("110", 0), "001": ("101", 0),
    "110": ("111", 0), "101": ("111", 0), "011": ("111", 0), "111": ("111", 1),
}

_SINGLE_TABLE = {
    "000": ("100", 0), "100": ("101", 0), "010": ("100", 1), "001": ("100", 1),
    "110": ("101", 1), "101": ("101", 1), "011": ("100", 2), "111": ("101", 2),
}

# 2B is probabilistic: R1 only scores ~45% of the time on a double.
# Each entry is a list of (probability, new_bases, runs_scored) outcomes.
_DOUBLE_TABLE: dict[str, list[tuple[float, str, int]]] = {
    "000": [(1.0, "010", 0)],
    "100": [(0.45, "010", 1), (0.55, "011", 0)],
    "010": [(1.0, "010", 1)],
    "001": [(1.0, "010", 1)],
    "110": [(0.45, "010", 2), (0.55, "011", 1)],
    "101": [(0.45, "010", 2), (0.55, "011", 1)],
    "011": [(1.0, "010", 2)],
    "111": [(0.45, "010", 3), (0.55, "011", 2)],
}

_HR_TABLE = {
    "000": ("000", 1), "100": ("000", 2), "010": ("000", 2), "001": ("000", 2),
    "110": ("000", 3), "101": ("000", 3), "011": ("000", 3), "111": ("000", 4),
}

# Standard force DP advances trailing runners one base.
_DP_TABLE = {
    "100": ("000", 0),
    "010": ("000", 0),
    "001": ("000", 0),
    "110": ("010", 0),  # R2 holds at 2B
    "101": ("000", 1),  # R3 scores
    "011": ("001", 0),  # R3 holds, R2 out
    "111": ("001", 1),  # R3 scores, R2 to 3B
}


def _advance_runners_ground_out(runners: str) -> tuple[str, int]:
    first, second, third = int(runners[0]), int(runners[1]), int(runners[2])
    runs = third
    return f"0{first}{second}", runs


def _flip_inning(state: GameState) -> GameState:
    if state.half == "top":
        return GameState(state.inning, "bot", 0, "000", state.score_diff)
    else:
        return GameState(min(state.inning + 1, 9), "top", 0, "000", state.score_diff)


def _apply_score(state: GameState, runs: int) -> int:
    if state.half == "top":
        return state.score_diff - runs
    else:
        return state.score_diff + runs


def apply_event(state: GameState, event: str) -> Optional[GameState]:
    """Apply a batting event and return the resulting GameState (or None)."""
    if event == "K":
        new_outs = state.outs + 1
        if new_outs >= 3:
            return _flip_inning(state)
        return GameState(state.inning, state.half, new_outs, state.runners, state.score_diff)

    elif event == "OUT":
        new_outs = state.outs + 1
        # 3rd out of the inning: runs don't score, runners are stranded
        if new_outs >= 3:
            return _flip_inning(GameState(state.inning, state.half, new_outs, state.runners, state.score_diff))
        new_runners, runs = _advance_runners_ground_out(state.runners)
        new_sd = _apply_score(state, runs)
        return GameState(state.inning, state.half, new_outs, new_runners, new_sd)

    elif event == "BB":
        new_runners, runs = _BB_TABLE[state.runners]
        new_sd = _apply_score(state, runs)
        return GameState(state.inning, state.half, state.outs, new_runners, new_sd)

    elif event == "1B":
        new_runners, runs = _SINGLE_TABLE[state.runners]
        new_sd = _apply_score(state, runs)
        return GameState(state.inning, state.half, state.outs, new_runners, new_sd)

    elif event == "2B":
        # Pick the most-likely outcome for the discrete state transition.
        outcomes = _DOUBLE_TABLE[state.runners]
        _, new_runners, runs = max(outcomes, key=lambda o: o[0])
        new_sd = _apply_score(state, runs)
        return GameState(state.inning, state.half, state.outs, new_runners, new_sd)

    elif event == "HR":
        new_runners, runs = _HR_TABLE[state.runners]
        new_sd = _apply_score(state, runs)
        return GameState(state.inning, state.half, state.outs, new_runners, new_sd)

    elif event == "DP":
        if state.runners == "000" or state.outs >= 2:
            return None
        new_runners, runs = _DP_TABLE[state.runners]
        new_sd = _apply_score(state, runs)
        new_outs = state.outs + 2
        if new_outs >= 3:
            return _flip_inning(GameState(state.inning, state.half, new_outs, new_runners, new_sd))
        return GameState(state.inning, state.half, new_outs, new_runners, new_sd)

    return None


def _runs_scored(state: GameState, event: str) -> float:
    """Expected runs this event produces from the current state.

    Integer for deterministic events; can be fractional for probabilistic ones (2B).
    """
    if event == "K":
        return 0
    if event == "OUT":
        # 3rd out ends the inning — no runs score
        if state.outs >= 2:
            return 0
        _, r = _advance_runners_ground_out(state.runners)
        return r
    if event == "DP":
        if state.runners == "000" or state.outs >= 2:
            return 0
        _, r = _DP_TABLE[state.runners]
        return r
    if event == "2B":
        # Expected runs across probabilistic outcomes.
        return sum(prob * runs for prob, _, runs in _DOUBLE_TABLE[state.runners])
    table = {"BB": _BB_TABLE, "1B": _SINGLE_TABLE, "HR": _HR_TABLE}
    _, r = table[event][state.runners]
    return r


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------

ALL_EVENTS = ["K", "OUT", "BB", "1B", "2B", "HR", "DP"]


def compute_deltas(
    state: GameState,
    home_score: int | None = None,
    away_score: int | None = None,
    ou_lines: list[float] | None = None,
    spread_lines: list[float] | None = None,
) -> list[EventDelta]:
    """
    For each batting event compute moneyline, O/U, and spread deltas.

    If home_score / away_score are not provided, only moneyline deltas
    are returned (O/U and spread dicts will be empty).

    ou_lines / spread_lines override the default fixed lines.  Pass
    ALL_OU_LINES / ALL_SPREAD_LINES for full coverage.
    """
    current_we = get_win_expectancy(state)
    include_markets = home_score is not None and away_score is not None

    use_ou = ou_lines if ou_lines is not None else DEFAULT_OU_LINES
    use_sp = spread_lines if spread_lines is not None else DEFAULT_SPREAD_LINES

    # Pre-compute current O/U and spread probabilities once
    cur_ou: dict[str, float] = {}
    cur_sp: dict[str, float] = {}
    if include_markets:
        inn, h, o, r = state.inning, state.half, state.outs, state.runners
        for line in use_ou:
            cur_ou[str(line)] = compute_over_under_probability(
                home_score, away_score, inn, h, o, r, line,
            )
        for line in use_sp:
            cur_sp[str(line)] = compute_spread_probability(
                home_score, away_score, inn, h, o, r, line,
            )

    deltas: list[EventDelta] = []

    for event in ALL_EVENTS:
        new_state = apply_event(state, event)
        if new_state is None:
            continue

        ou_dict: dict = {}
        sp_dict: dict = {}

        if event == "2B":
            # Probabilistic outcomes: weighted-average WE/OU/SP across them.
            outcomes = _DOUBLE_TABLE[state.runners]
            new_we = 0.0
            ou_after_acc: dict[str, float] = {str(line): 0.0 for line in use_ou}
            sp_after_acc: dict[str, float] = {str(line): 0.0 for line in use_sp}

            for prob, new_bases, runs in outcomes:
                new_sd = _apply_score(state, runs)
                ns = GameState(state.inning, state.half, state.outs, new_bases, new_sd)
                # Walk-off can occur per-outcome if this run takes the lead.
                if ns.half == "bot" and ns.inning >= 9 and ns.score_diff > 0:
                    we_o = 1.0
                else:
                    we_o = get_win_expectancy(ns)
                new_we += prob * we_o

                if include_markets:
                    if state.half == "top":
                        nh, na = home_score, away_score + runs
                    else:
                        nh, na = home_score + runs, away_score
                    for line in use_ou:
                        ou_after_acc[str(line)] += prob * compute_over_under_probability(
                            nh, na, ns.inning, ns.half, ns.outs, ns.runners, line,
                        )
                    for line in use_sp:
                        sp_after_acc[str(line)] += prob * compute_spread_probability(
                            nh, na, ns.inning, ns.half, ns.outs, ns.runners, line,
                        )

            if include_markets:
                for line in use_ou:
                    before = cur_ou[str(line)]
                    after = ou_after_acc[str(line)]
                    ou_dict[str(line)] = {
                        "before": round(before, 4),
                        "after": round(after, 4),
                        "delta": round(after - before, 4),
                    }
                for line in use_sp:
                    before = cur_sp[str(line)]
                    after = sp_after_acc[str(line)]
                    sp_dict[str(line)] = {
                        "before": round(before, 4),
                        "after": round(after, 4),
                        "delta": round(after - before, 4),
                    }

            deltas.append(EventDelta(
                event=event,
                win_expectancy_before=round(current_we, 4),
                win_expectancy_after=round(new_we, 4),
                delta=round(new_we - current_we, 4),
                over_under=ou_dict,
                spread=sp_dict,
            ))
            continue

        new_we = get_win_expectancy(new_state)

        # Walk-off: bottom 9+, home takes the lead on THIS play → game over
        if new_state.half == "bot" and new_state.inning >= 9 and new_state.score_diff > 0:
            new_we = 1.0

        # 3rd out in bottom 9+ (detected by inning flipping to top)
        if state.half == "bot" and state.inning >= 9 and new_state.half == "top":
            if new_state.score_diff > 0:
                new_we = 1.0   # home was winning → game over, home wins
            elif new_state.score_diff < 0:
                new_we = 0.0   # home was losing → game over, home loses
            # score_diff == 0 → tied, goes to extras (flip to top 10 is correct)

        if include_markets:
            runs = _runs_scored(state, event)
            if state.half == "top":
                new_away = away_score + runs
                new_home = home_score
            else:
                new_home = home_score + runs
                new_away = away_score

            # Detect game-ending state for O/U and spread
            game_over = (state.half == "bot" and state.inning >= 9
                         and new_state.half == "top" and new_state.score_diff != 0)

            ns = new_state
            for line in use_ou:
                if game_over:
                    after = 1.0 if (new_home + new_away) > line else 0.0
                else:
                    after = compute_over_under_probability(
                        new_home, new_away, ns.inning, ns.half, ns.outs, ns.runners, line,
                    )
                before = cur_ou[str(line)]
                ou_dict[str(line)] = {
                    "before": round(before, 4),
                    "after": round(after, 4),
                    "delta": round(after - before, 4),
                }
            for line in use_sp:
                if game_over:
                    after = 1.0 if (new_home - new_away) > line else 0.0
                else:
                    after = compute_spread_probability(
                        new_home, new_away, ns.inning, ns.half, ns.outs, ns.runners, line,
                    )
                before = cur_sp[str(line)]
                sp_dict[str(line)] = {
                    "before": round(before, 4),
                    "after": round(after, 4),
                    "delta": round(after - before, 4),
                }

        deltas.append(EventDelta(
            event=event,
            win_expectancy_before=round(current_we, 4),
            win_expectancy_after=round(new_we, 4),
            delta=round(new_we - current_we, 4),
            over_under=ou_dict,
            spread=sp_dict,
        ))

    return deltas
