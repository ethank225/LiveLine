#!/usr/bin/env python3
"""
Parse raw data files into JSON lookup tables.

Usage:
    python scripts/build_tables.py

Reads:
    data/probs.txt          -> data/win_expectancy.json
    data/runsperinning.xml  -> data/run_distribution.json
"""

import csv
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"

# Runners encoding: numeric -> 3-char string (first, second, third)
RUNNERS_MAP = {
    1: "000",  # bases empty
    2: "100",  # runner on 1st
    3: "010",  # runner on 2nd
    4: "110",  # runners on 1st+2nd
    5: "001",  # runner on 3rd
    6: "101",  # runners on 1st+3rd
    7: "011",  # runners on 2nd+3rd
    8: "111",  # bases loaded
}

HALF_MAP = {"H": "bot", "V": "top"}

MIN_GAMES = 10
SD_MIN, SD_MAX = -8, 8


# ---------------------------------------------------------------------------
# Task 1: probs.txt -> win_expectancy.json
# ---------------------------------------------------------------------------

def parse_probs() -> dict[str, float]:
    path = DATA_DIR / "probs.txt"

    # Accumulate per key so extra-inning rows merge into inning 9
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            half_raw = row[0]
            inning = int(row[1])
            outs = int(row[2])
            runners_enc = int(row[3])
            score_diff = int(row[4])
            total_games = int(row[5])
            wins = int(row[6])

            # score_diff and wins are from the BATTING team's perspective.
            # V rows: batting = visitor, so negate sd to get home perspective.
            if half_raw == "V":
                score_diff = -score_diff

            if score_diff < SD_MIN or score_diff > SD_MAX:
                continue
            if total_games < MIN_GAMES:
                continue

            half = HALF_MAP[half_raw]
            runners = RUNNERS_MAP[runners_enc]
            inning = min(inning, 9)

            key = f"{inning}-{half}-{outs}-{runners}-{score_diff}"
            totals[key][0] += total_games
            totals[key][1] += wins

    table = {}
    for key, (total, wins) in sorted(totals.items()):
        if total >= MIN_GAMES:
            half = key.split("-")[1]
            if half == "top":
                # V rows: wins = visitor wins → home WE = 1 - wins/total
                table[key] = round(1.0 - wins / total, 6)
            else:
                # H rows: wins = home wins → home WE = wins/total
                table[key] = round(wins / total, 6)

    return table


# ---------------------------------------------------------------------------
# Task 2: runsperinning.xml -> run_distribution.json
# ---------------------------------------------------------------------------

def parse_run_distribution() -> dict[str, list[float]]:
    path = DATA_DIR / "runsperinning.xml"
    tree = ET.parse(path)
    root = tree.getroot()

    table = {}

    for situation in root.findall("situation"):
        outs = int(situation.get("outs"))
        runners_enc = int(situation.get("runners"))
        runners = RUNNERS_MAP[runners_enc]
        total = int(situation.find("total").text)

        if total == 0:
            continue

        counts: dict[int, int] = {}
        for count_el in situation.findall("count"):
            runs = int(count_el.get("runs"))
            count = int(count_el.text)
            counts[runs] = count

        max_runs = max(counts.keys()) if counts else 0
        probs = [round(counts.get(i, 0) / total, 6) for i in range(max_runs + 1)]

        # Truncate trailing near-zero probabilities
        while probs and probs[-1] < 0.0001:
            probs.pop()

        key = f"{outs}-{runners}"
        table[key] = probs

    return table


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(we: dict, rd: dict):
    print("=" * 60)
    print("VALIDATION")
    print("=" * 60)

    # --- Win Expectancy ---
    print(f"\nWin Expectancy Table: {len(we)} entries")

    we_checks = [
        ("1-bot-0-000-0", 0.55, 0.05, "Home advantage, start of game"),
        ("9-bot-2-000--1", 0.20, 0.10, "Down 1, 2 outs, bot 9"),
        ("5-top-0-000-0", 0.50, 0.05, "Tie game, mid-game"),
        ("1-top-0-000-0", 0.50, 0.05, "Very start of game"),
        ("9-bot-0-000-0", 0.55, 0.10, "Bot 9, tie, 0 outs"),
        ("5-top-0-000-5", 0.95, 0.05, "Up 5 mid-game"),
        ("5-top-0-000--5", 0.10, 0.10, "Down 5 mid-game"),
    ]

    for key, expected, tol, desc in we_checks:
        actual = we.get(key)
        if actual is None:
            print(f"  MISS: {key} — {desc}")
        else:
            ok = abs(actual - expected) <= tol
            print(f"  {'PASS' if ok else 'FAIL'}: {key} = {actual:.6f} "
                  f"(expected ~{expected:.2f} +/-{tol}) — {desc}")

    # --- Run Distribution ---
    print(f"\nRun Distribution Table: {len(rd)} entries")

    rd_checks = [
        ("0-000", 0, 0.73, 0.05, "P(0 runs), bases empty, 0 outs"),
        ("0-111", 0, 0.13, 0.05, "P(0 runs), bases loaded, 0 outs"),
        ("2-000", 0, 0.93, 0.03, "P(0 runs), bases empty, 2 outs"),
        ("0-000", 1, 0.15, 0.05, "P(1 run), bases empty, 0 outs"),
    ]

    for key, idx, expected, tol, desc in rd_checks:
        dist = rd.get(key)
        if dist is None:
            print(f"  MISS: {key} — {desc}")
        elif idx >= len(dist):
            print(f"  FAIL: {key}[{idx}] out of range — {desc}")
        else:
            actual = dist[idx]
            ok = abs(actual - expected) <= tol
            print(f"  {'PASS' if ok else 'FAIL'}: {key}[{idx}] = {actual:.6f} "
                  f"(expected ~{expected:.2f} +/-{tol}) — {desc}")

    print(f"\n  Sample 0-000: {rd.get('0-000', [])[:10]}")
    print(f"  Sample 0-111: {rd.get('0-111', [])[:10]}")
    print(f"  Lengths: " + ", ".join(
        f"{k}={len(v)}" for k, v in sorted(rd.items())
    ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Parsing probs.txt ...")
    we = parse_probs()

    print("Parsing runsperinning.xml ...")
    rd = parse_run_distribution()

    we_path = DATA_DIR / "win_expectancy.json"
    with open(we_path, "w") as f:
        json.dump(we, f, indent=2)
    print(f"Written {len(we)} entries to {we_path}")

    rd_path = DATA_DIR / "run_distribution.json"
    with open(rd_path, "w") as f:
        json.dump(rd, f, indent=2)
    print(f"Written {len(rd)} entries to {rd_path}")

    validate(we, rd)
