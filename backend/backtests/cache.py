"""On-disk cache of MLB plays + Kalshi trades + market specs per game.

Populated automatically during normal backtest runs and consumed by
``backtest.py --use-cached`` to re-run model + fill simulation without
re-fetching MLB or Kalshi APIs.
"""

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from backtests.mlb import PlayRecord, MarketSpec
from backtests.output import OUTPUT_DIR

CACHE_DIR = OUTPUT_DIR / "cache"


def _date_dir(target: date) -> Path:
    return CACHE_DIR / target.isoformat()


def _game_dir(target: date, game_id: int) -> Path:
    return _date_dir(target) / str(game_id)


def list_cached_games(target: date) -> list[int]:
    d = _date_dir(target)
    if not d.exists():
        return []
    return sorted(
        int(p.name) for p in d.iterdir()
        if p.is_dir() and p.name.isdigit() and (p / "game.json").exists()
    )


def _record_to_dict(rec: PlayRecord) -> dict:
    return {
        "timestamp": rec.timestamp,
        "inning": rec.inning,
        "half": rec.half,
        "outs_before": rec.outs_before,
        "runners_before": rec.runners_before,
        "home_score": rec.home_score,
        "away_score": rec.away_score,
        "mlb_event": rec.mlb_event,
        "mapped_event": rec.mapped_event,
        "description": rec.description,
        "next_pitch_ts": rec.next_pitch_ts,
    }


def _dict_to_record(d: dict) -> PlayRecord:
    return PlayRecord(
        timestamp=d["timestamp"],
        inning=d["inning"],
        half=d["half"],
        outs_before=d["outs_before"],
        runners_before=d["runners_before"],
        home_score=d["home_score"],
        away_score=d["away_score"],
        mlb_event=d["mlb_event"],
        mapped_event=d["mapped_event"],
        description=d["description"],
        next_pitch_ts=d.get("next_pitch_ts"),
    )


def _safe_filename(ticker: str) -> str:
    return ticker.replace("/", "_").replace("\\", "_")


def save_game_cache(
    target: date,
    game_id: int,
    game_info: dict,
    records: list[PlayRecord],
    market_specs: list[MarketSpec],
    trades_by_ticker: dict[str, list[tuple[int, float]]],
):
    """Write game data + trades to cache. Overwrites any existing cache."""
    g = _game_dir(target, game_id)
    g.mkdir(parents=True, exist_ok=True)

    payload = {
        "game_info": game_info,
        "records": [_record_to_dict(r) for r in records],
        "markets": [asdict(s) for s in market_specs],
    }
    with open(g / "game.json", "w") as f:
        json.dump(payload, f)

    trades_dir = g / "trades"
    trades_dir.mkdir(exist_ok=True)
    for ticker, trades in trades_by_ticker.items():
        with open(trades_dir / f"{_safe_filename(ticker)}.json", "w") as f:
            json.dump(trades, f)


def load_game_cache(target: date, game_id: int):
    """Load game_info, records, market_specs, and trades for a cached game.

    Returns ``(game_info, records, market_specs, trades_by_ticker)`` or
    ``None`` if the game has no cache entry.
    """
    g = _game_dir(target, game_id)
    if not (g / "game.json").exists():
        return None
    with open(g / "game.json") as f:
        payload = json.load(f)

    game_info = payload["game_info"]
    records = [_dict_to_record(d) for d in payload["records"]]
    specs = [MarketSpec(**s) for s in payload["markets"]]

    trades_by_ticker: dict[str, list[tuple[int, float]]] = {}
    trades_dir = g / "trades"
    if trades_dir.exists():
        for p in sorted(trades_dir.iterdir()):
            if p.suffix != ".json":
                continue
            with open(p) as f:
                raw = json.load(f)
            trades_by_ticker[p.stem] = [(int(ts), float(price)) for ts, price in raw]

    return game_info, records, specs, trades_by_ticker
