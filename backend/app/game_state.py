"""
MLB game state fetcher using the MLB-StatsAPI package.

Retrieves today's schedule and parses live game feeds into clean GameState objects.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import statsapi

from app.engine import GameState

# MLB's business day boundary is roughly US Pacific — the latest West
# Coast games start around 7 PM PT. Running on UTC (Railway, most cloud
# hosts) otherwise drops those games from "today" as soon as midnight
# UTC crosses ~5 PM PT. Using zoneinfo (Python 3.9+) avoids the pytz
# dependency.
_MLB_TZ = ZoneInfo("US/Pacific")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GameInfo:
    game_id: int
    away_team: str
    home_team: str
    away_abbreviation: str
    home_abbreviation: str
    away_score: int
    home_score: int
    status: str           # "Preview", "Live", "Final", "Delayed", "Suspended"
    state: GameState | None  # None for games not in progress


# ---------------------------------------------------------------------------
# Today's schedule
# ---------------------------------------------------------------------------

def get_todays_games() -> list[dict]:
    """
    Fetch today's MLB schedule. Returns a list of game summary dicts.

    "Today" is evaluated in US Pacific time, not the server's local
    timezone — a 7 PM PT game (2 AM UTC the next day) would otherwise
    drop off the schedule on UTC hosts like Railway.
    """
    today = datetime.now(_MLB_TZ).strftime("%m/%d/%Y")
    games = statsapi.schedule(date=today)

    return [
        {
            "game_id": g["game_id"],
            "away_team": g["away_name"],
            "home_team": g["home_name"],
            "away_score": g.get("away_score", 0),
            "home_score": g.get("home_score", 0),
            "status": g["status"],
            "inning": g.get("current_inning", 0),
            "inning_state": g.get("inning_state", ""),
            "start_time": g.get("game_datetime", ""),
        }
        for g in games
    ]


# ---------------------------------------------------------------------------
# Live game state
# ---------------------------------------------------------------------------

def _parse_runners(offense: dict) -> str:
    """
    Parse runner configuration from the linescore offense object.
    Keys 'first', 'second', 'third' are present ONLY when occupied.
    """
    first = "1" if "first" in offense else "0"
    second = "1" if "second" in offense else "0"
    third = "1" if "third" in offense else "0"
    return f"{first}{second}{third}"


def get_game_state(game_id: int) -> GameInfo:
    """
    Fetch and parse the live feed for a specific game.
    Returns a GameInfo object with the current state.
    """
    data = statsapi.get("game", {"gamePk": game_id})

    game_data = data["gameData"]
    live_data = data["liveData"]

    # Team info
    home = game_data["teams"]["home"]
    away = game_data["teams"]["away"]
    home_team = home["name"]
    away_team = away["name"]
    home_abbr = home.get("abbreviation", home["teamName"][:3].upper())
    away_abbr = away.get("abbreviation", away["teamName"][:3].upper())

    # Game status
    abstract_state = game_data["status"]["abstractGameState"]  # Preview, Live, Final
    detailed_state = game_data["status"].get("detailedState", abstract_state)

    # Map detailed states
    if detailed_state in ("Delayed", "Delayed Start"):
        status = "Delayed"
    elif detailed_state == "Suspended":
        status = "Suspended"
    else:
        status = abstract_state  # Preview, Live, Final

    linescore = live_data.get("linescore", {})

    # Scores
    teams_score = linescore.get("teams", {})
    home_score = teams_score.get("home", {}).get("runs", 0)
    away_score = teams_score.get("away", {}).get("runs", 0)

    # Parse game state for live games
    state = None

    if status in ("Live", "Delayed", "Suspended"):
        inning = linescore.get("currentInning", 1)
        is_top = linescore.get("isTopInning", True)
        outs = linescore.get("outs", 0)

        # Handle mid-inning transition (3 outs showing briefly)
        if outs >= 3:
            outs = 0
            if is_top:
                is_top = False
            else:
                is_top = True
                inning = min(inning + 1, 9)

        half = "top" if is_top else "bot"
        inning = max(1, min(inning, 9))

        offense = linescore.get("offense", {})
        runners = _parse_runners(offense)

        score_diff = max(-10, min(10, home_score - away_score))

        state = GameState(
            inning=inning,
            half=half,
            outs=outs,
            runners=runners,
            score_diff=score_diff,
        )

    elif status == "Preview":
        # Game hasn't started — return default pre-game state
        state = GameState(inning=1, half="top", outs=0, runners="000", score_diff=0)

    elif status == "Final":
        # Game is over
        score_diff = max(-10, min(10, home_score - away_score))
        state = GameState(inning=9, half="bot", outs=3, runners="000", score_diff=score_diff)

    return GameInfo(
        game_id=game_id,
        away_team=away_team,
        home_team=home_team,
        away_abbreviation=away_abbr,
        home_abbreviation=home_abbr,
        away_score=away_score or 0,
        home_score=home_score or 0,
        status=status,
        state=state,
    )
