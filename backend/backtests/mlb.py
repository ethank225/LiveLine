"""MLB play-by-play data: PlayRecord, event mapping, game fetching."""

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import statsapi

sys.path.insert(0, str(Path(__file__).parent.parent))

# Maps MLB Stats API result['event'] strings to our engine event codes
EVENT_MAP = {
    "Strikeout": "K",
    "Strikeout - DP": "K",
    "Strikeout Double Play": "K",
    "Called Third Strike": "K",
    "Single": "1B",
    "Double": "2B",
    "Home Run": "HR",
    "Walk": "BB",
    "Intent Walk": "BB",
    "Hit By Pitch": "BB",
    "Double Play": "DP",
    "Grounded Into DP": "DP",
    "Lined Into DP": "DP",
    "Groundout": "OUT",
    "Flyout": "OUT",
    "Lineout": "OUT",
    "Pop Out": "OUT",
    "Force Out": "OUT",
    "Forceout": "OUT",
    "Field Out": "OUT",
    "Bunt Groundout": "OUT",
    "Bunt Pop Out": "OUT",
    "Sac Fly": "OUT",
    "Sac Bunt": "OUT",
    "Sacrifice Bunt DP": "DP",
    "Triple Play": "OUT",
    "Fielders Choice": "OUT",
    "Fielders Choice Out": "OUT",
    "Field Error": "1B",
    "Triple": "2B",
}

SKIP_EVENTS = {
    "Stolen Base 2B", "Stolen Base 3B", "Stolen Base Home",
    "Caught Stealing 2B", "Caught Stealing 3B", "Caught Stealing Home",
    "Pickoff 1B", "Pickoff 2B", "Pickoff 3B",
    "Wild Pitch", "Passed Ball", "Balk",
    "Runner Out", "Pickoff Caught Stealing 2B",
    "Pickoff Caught Stealing 3B",
    "Catcher Interference", "Game Advisory",
    "Pitching Substitution", "Offensive Substitution",
    "Defensive Sub", "Defensive Switch", "Defensive Indiff",
    "Ejection", "Injury",
}


@dataclass
class PlayRecord:
    """One play from MLB play-by-play, enriched with model predictions."""
    timestamp: str
    inning: int
    half: str
    outs_before: int
    runners_before: str
    home_score: int
    away_score: int
    mlb_event: str
    mapped_event: str
    description: str

    we_before: float = 0.0
    we_after: float = 0.0
    ml_delta: float = 0.0
    ou_85_before: float = 0.0
    ou_85_after: float = 0.0
    ou_85_delta: float = 0.0
    model_deltas: dict = field(default_factory=dict)
    next_pitch_ts: int | None = None


@dataclass
class MarketSpec:
    """Describes one Kalshi market and how it maps to a model delta."""
    ticker: str
    market_type: str
    line: float | None
    delta_key: str
    flip: bool
    label: str


def get_games_for_date(target_date: str) -> list[dict]:
    """Get all MLB games for a date. Date format: MM/DD/YYYY."""
    games = statsapi.schedule(date=target_date)
    return [g for g in games if g["status"] == "Final"]


def _runners_from_play(play: dict) -> str:
    bases = set()
    for runner in play.get("runners", []):
        origin = runner.get("movement", {}).get("originBase")
        if origin in ("1B", "2B", "3B"):
            bases.add(origin)
    first = "1" if "1B" in bases else "0"
    second = "1" if "2B" in bases else "0"
    third = "1" if "3B" in bases else "0"
    return f"{first}{second}{third}"


def pull_play_by_play(game_id: int) -> tuple[list[PlayRecord], dict]:
    """Pull full play-by-play for a game."""
    data = statsapi.get("game", {"gamePk": game_id})
    game_data = data["gameData"]
    live_data = data["liveData"]

    home_team = game_data["teams"]["home"]["name"]
    away_team = game_data["teams"]["away"]["name"]
    home_abbr = game_data["teams"]["home"].get("abbreviation", "???")
    away_abbr = game_data["teams"]["away"].get("abbreviation", "???")

    game_info = {
        "game_id": game_id,
        "home_team": home_team, "away_team": away_team,
        "home_abbr": home_abbr, "away_abbr": away_abbr,
    }

    all_plays = live_data["plays"]["allPlays"]
    records = []

    cur_inning = 1
    cur_half = "top"
    cur_outs = 0
    home_score = 0
    away_score = 0

    for play in all_plays:
        about = play.get("about", {})
        result = play.get("result", {})

        event_type = result.get("event", "")
        if not event_type:
            continue
        if event_type in SKIP_EVENTS:
            continue

        mapped = EVENT_MAP.get(event_type)
        if mapped is None:
            print(f"  [SKIP] Unknown event: '{event_type}' — {result.get('description', '')[:80]}")
            continue

        inning = about.get("inning", 1)
        half_raw = about.get("halfInning", "top")
        half = "top" if half_raw == "top" else "bot"
        timestamp = about.get("startTime", "")

        if inning != cur_inning or half != cur_half:
            cur_inning = inning
            cur_half = half
            cur_outs = 0

        runners_before = _runners_from_play(play)

        rec = PlayRecord(
            timestamp=timestamp, inning=inning, half=half,
            outs_before=cur_outs, runners_before=runners_before,
            home_score=home_score, away_score=away_score,
            mlb_event=event_type, mapped_event=mapped,
            description=result.get("description", "")[:120],
        )
        records.append(rec)

        outs_this_play = sum(
            1 for r in play.get("runners", [])
            if r.get("movement", {}).get("isOut")
        )
        cur_outs += outs_this_play

        home_score = result.get("homeScore", home_score)
        away_score = result.get("awayScore", away_score)

    linescore = live_data.get("linescore", {})
    teams = linescore.get("teams", {})
    game_info["home_final"] = teams.get("home", {}).get("runs", home_score)
    game_info["away_final"] = teams.get("away", {}).get("runs", away_score)

    # Extract first-pitch timestamp for each at-bat
    first_pitch_ts: list[int | None] = []
    for play in all_plays:
        ts = None
        for ev in play.get("playEvents", []):
            if ev.get("isPitch"):
                raw = ev.get("startTime")
                if raw:
                    try:
                        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        ts = int(dt.timestamp())
                    except Exception:
                        pass
                break
        first_pitch_ts.append(ts)

    rec_idx = 0
    for play_idx, play in enumerate(all_plays):
        if rec_idx >= len(records):
            break
        about_ts = play.get("about", {}).get("startTime", "")
        if about_ts == records[rec_idx].timestamp:
            for future in range(play_idx + 1, len(all_plays)):
                if first_pitch_ts[future] is not None:
                    records[rec_idx].next_pitch_ts = first_pitch_ts[future]
                    break
            rec_idx += 1

    return records, game_info
