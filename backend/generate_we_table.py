"""
Generate win expectancy lookup table for baseball.

Parametric model calibrated to Tom Tango's empirical win expectancy data.
Produces ~9,072 entries keyed by "{inning}-{half}-{outs}-{runners}-{score_diff}".

Reference: https://www.tangotiger.net/we.html
"""

import json
import math

# Logistic steepness per inning — how much each run of score differential matters.
# Increases as the game progresses (late-game runs are more decisive).
K_BY_INNING = {
    1: 0.4096,
    2: 0.4389,
    3: 0.4768,
    4: 0.5279,
    5: 0.5928,
    6: 0.6946,
    7: 0.8473,
    8: 1.1147,
    9: 1.6732,
}

# Bottom-of-inning logit offset (last-at-bat / home advantage).
# At tie score, bases empty, 0 outs, the home team has > 0.500 WE in the bottom half.
BOTTOM_OFFSET = {
    1: 0.1886,
    2: 0.2007,
    3: 0.2168,
    4: 0.2371,
    5: 0.2615,
    6: 0.2982,
    7: 0.3475,
    8: 0.4263,
    9: 0.5494,
}

# Run expectancy from the 24 base-out states (Tango/Lichtman RE matrix).
# Used to derive runner adjustments for WE.
# Format: RE[runners][outs]
RUN_EXPECTANCY = {
    "000": {0: 0.481, 1: 0.254, 2: 0.098},
    "100": {0: 0.859, 1: 0.509, 2: 0.224},
    "010": {0: 1.100, 1: 0.664, 2: 0.319},
    "001": {0: 1.350, 1: 0.950, 2: 0.353},
    "110": {0: 1.437, 1: 0.884, 2: 0.429},
    "101": {0: 1.784, 1: 1.130, 2: 0.478},
    "011": {0: 1.964, 1: 1.376, 2: 0.580},
    "111": {0: 2.292, 1: 1.541, 2: 0.752},
}

RUNNER_CONFIGS = ["000", "100", "010", "001", "110", "101", "011", "111"]


def logistic(x: float) -> float:
    """Standard logistic function, clamped to avoid overflow."""
    x = max(-20.0, min(20.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def compute_we(inning: int, half: str, outs: int, runners: str, score_diff: int) -> float:
    """
    Compute home team win expectancy for a given game state.

    Args:
        inning: 1-9 (extras use 9)
        half: "top" or "bot"
        outs: 0, 1, or 2
        runners: 3-char string like "000", "110", etc.
        score_diff: home_runs - away_runs, clamped to [-10, 10]
    """
    inn = min(max(inning, 1), 9)
    score_diff = max(-10, min(10, score_diff))

    k = K_BY_INNING[inn]

    # Base logit value from score differential
    logit_val = k * score_diff

    # Bottom-half offset (home team advantage when batting)
    if half == "bot":
        logit_val += BOTTOM_OFFSET[inn]

    # Out adjustment: in the top half, more outs favor the home team (visiting team
    # wasting outs). In the bottom half, more outs hurt the home team.
    # The magnitude scales with inning (late-game outs matter more).
    out_factor = 0.06 * (1.0 + 0.25 * (inn - 1))
    if half == "top":
        logit_val += outs * out_factor
    else:
        logit_val -= outs * out_factor

    # Runner adjustment: runners on base benefit the batting team.
    # Derived from run expectancy delta relative to bases empty.
    re_empty = RUN_EXPECTANCY["000"][outs]
    re_current = RUN_EXPECTANCY[runners][outs]
    re_delta = re_current - re_empty

    # Scale the RE delta into logit space. The scaling factor represents
    # how much one expected run shifts win probability, which increases
    # as the game progresses.
    runner_scale = 0.22 * (1.0 + 0.15 * (inn - 1))

    if half == "top":
        # Away team batting — runners hurt the home team
        logit_val -= re_delta * runner_scale
    else:
        # Home team batting — runners help the home team
        logit_val += re_delta * runner_scale

    we = logistic(logit_val)

    # Special cases for bottom of 9th (and extras)
    if half == "bot" and inn == 9:
        if score_diff > 0:
            # Home team already ahead entering bottom of 9th — they win
            we = 1.0
        elif score_diff == 0:
            # Tie game bottom 9 — boost significantly (walk-off territory)
            # Runners amplify this further
            we = max(we, 0.50 + 0.15 + re_delta * 0.08)

    # Clamp to avoid exact 0 or 1 (except bot-9 with lead)
    if not (half == "bot" and inn == 9 and score_diff > 0):
        we = max(0.001, min(0.999, we))

    return round(we, 4)


def generate_table() -> dict:
    """Generate the full win expectancy lookup table."""
    table = {}

    for inning in range(1, 10):
        for half in ["top", "bot"]:
            for outs in range(3):
                for runners in RUNNER_CONFIGS:
                    for sd in range(-10, 11):
                        key = f"{inning}-{half}-{outs}-{runners}-{sd}"
                        table[key] = compute_we(inning, half, outs, runners, sd)

    return table


def validate_table(table: dict) -> None:
    """Spot-check key values against known benchmarks."""
    checks = [
        # (key, expected_approx, tolerance, description)
        ("1-top-0-000-0", 0.500, 0.02, "Start of game, neutral"),
        ("1-bot-0-000-0", 0.547, 0.02, "Bottom 1st, home advantage"),
        ("5-top-0-000-0", 0.500, 0.03, "Mid-game tie, top 5th"),
        ("9-bot-0-000-1", 1.000, 0.001, "Bot 9, home ahead by 1"),
        ("9-bot-0-000--1", 0.35, 0.15, "Bot 9, home down 1, bases empty"),
        ("9-bot-2-000--1", 0.10, 0.10, "Bot 9, 2 outs, down 1, empty"),
        ("5-top-0-000-5", 0.95, 0.05, "Up 5 mid-game"),
        ("5-top-0-000--5", 0.05, 0.05, "Down 5 mid-game"),
        ("1-top-0-000-10", 0.99, 0.01, "Up 10"),
        ("1-top-0-000--10", 0.01, 0.01, "Down 10"),
    ]

    print("Validation checks:")
    all_pass = True
    for key, expected, tol, desc in checks:
        actual = table[key]
        passed = abs(actual - expected) <= tol
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  {status}: {desc}: {key} = {actual:.4f} (expected ~{expected:.3f} +/- {tol})")

    print(f"\nTotal entries: {len(table)}")
    values = list(table.values())
    print(f"Value range: [{min(values):.4f}, {max(values):.4f}]")
    print(f"Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")


if __name__ == "__main__":
    import pathlib

    table = generate_table()
    validate_table(table)

    out_path = pathlib.Path(__file__).parent / "data" / "win_expectancy.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w") as f:
        json.dump(table, f, indent=2)

    print(f"\nWritten to {out_path}")
