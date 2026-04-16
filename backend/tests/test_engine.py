"""
Tests for the win expectancy engine.

Run:  cd backend && python -m pytest tests/test_engine.py -v
"""

import pytest

from app.engine import (
    ALL_EVENTS,
    GameState,
    apply_event,
    compute_deltas,
    compute_over_under_probability,
    compute_spread_probability,
    get_run_distribution,
    get_win_expectancy,
    load_win_expectancy_table,
    _convolve,
    _DOUBLE_TABLE,
    _remaining_halves,
    _runs_scored,
)

RUNNER_CONFIGS = ["000", "100", "010", "001", "110", "101", "011", "111"]


# ===================================================================
# WE table lookups
# ===================================================================

class TestWELookup:
    def test_table_loads(self):
        table = load_win_expectancy_table()
        assert len(table) > 5000

    def test_start_of_game_home_advantage(self):
        """Top of 1st, tie — home should be slightly favored (~0.54)."""
        we = get_win_expectancy(GameState(1, "top", 0, "000", 0))
        assert 0.52 < we < 0.58

    def test_bot_1_tie_higher(self):
        """Bot of 1st, tie — home just survived top 1, WE higher than top 1."""
        top = get_win_expectancy(GameState(1, "top", 0, "000", 0))
        bot = get_win_expectancy(GameState(1, "bot", 0, "000", 0))
        assert bot > top

    def test_positive_score_diff_favors_home(self):
        for sd in [1, 3, 5, 8]:
            we = get_win_expectancy(GameState(5, "top", 0, "000", sd))
            assert we > 0.5, f"Home up {sd} should be > 0.5, got {we}"

    def test_negative_score_diff_favors_away(self):
        for sd in [-1, -3, -5, -8]:
            we = get_win_expectancy(GameState(5, "top", 0, "000", sd))
            assert we < 0.5, f"Home down {abs(sd)} should be < 0.5, got {we}"

    def test_we_monotonic_in_score_diff(self):
        """WE should increase as home's score advantage grows."""
        vals = []
        for sd in range(-8, 9):
            vals.append(get_win_expectancy(GameState(5, "top", 0, "000", sd)))
        for i in range(len(vals) - 1):
            assert vals[i] <= vals[i + 1] + 0.005, (
                f"WE not monotonic: sd={i - 8} gave {vals[i]}, sd={i - 7} gave {vals[i + 1]}"
            )

    def test_extreme_score_diff_near_boundary(self):
        we_high = get_win_expectancy(GameState(5, "top", 0, "000", 8))
        we_low = get_win_expectancy(GameState(5, "top", 0, "000", -8))
        assert we_high > 0.90
        assert we_low < 0.10

    def test_extras_capped_at_9(self):
        """Inning 12 should use inning-9 values."""
        we_9 = get_win_expectancy(GameState(9, "top", 0, "000", 0))
        we_12 = get_win_expectancy(GameState(12, "top", 0, "000", 0))
        assert we_9 == we_12

    def test_fallback_for_missing_key(self):
        """Even with an unusual state, we should get a number back, not crash."""
        we = get_win_expectancy(GameState(9, "bot", 2, "111", -8))
        assert 0.0 <= we <= 1.0

    def test_all_values_in_range(self):
        table = load_win_expectancy_table()
        for key, val in table.items():
            assert 0.0 <= val <= 1.0, f"{key} has value {val} outside [0,1]"


# ===================================================================
# Event state transitions
# ===================================================================

class TestEventTransitions:
    def test_k_adds_one_out(self):
        s = apply_event(GameState(3, "top", 0, "100", 0), "K")
        assert s.outs == 1
        assert s.runners == "100"  # runners unchanged

    def test_k_third_out_flips_inning(self):
        s = apply_event(GameState(3, "top", 2, "110", 0), "K")
        assert s.half == "bot"
        assert s.outs == 0
        assert s.runners == "000"
        assert s.inning == 3  # same inning, just flipped half

    def test_out_advances_runners(self):
        # Runner on 3rd scores on ground out
        s = apply_event(GameState(5, "bot", 0, "001", 0), "OUT")
        assert s.outs == 1
        assert s.score_diff == 1  # home scores in bottom half

    def test_out_runner_on_first_advances(self):
        s = apply_event(GameState(5, "top", 0, "100", 0), "OUT")
        assert s.runners == "010"  # 1st to 2nd

    def test_bb_bases_loaded_scores(self):
        s = apply_event(GameState(5, "bot", 0, "111", 0), "BB")
        assert s.runners == "111"
        assert s.score_diff == 1  # forced run scores for home

    def test_bb_empty_bases(self):
        s = apply_event(GameState(3, "top", 0, "000", 0), "BB")
        assert s.runners == "100"
        assert s.score_diff == 0

    @pytest.mark.parametrize("runners,expected_runners,expected_runs", [
        ("000", "100", 0),
        ("100", "101", 0),
        ("010", "100", 1),
        ("001", "100", 1),
        ("110", "101", 1),
        ("011", "100", 2),
        ("111", "101", 2),
    ])
    def test_single_all_configs(self, runners, expected_runners, expected_runs):
        s = apply_event(GameState(5, "bot", 0, runners, 0), "1B")
        assert s.runners == expected_runners
        assert s.score_diff == expected_runs  # bot half, home scores

    @pytest.mark.parametrize("runners,expected_branches,expected_runs", [
        # Deterministic configs (no R1 on base): one outcome.
        ("000", 1, 0.0),
        ("010", 1, 1.0),
        ("001", 1, 1.0),
        ("011", 1, 2.0),
        # Probabilistic configs (R1 on base): two outcomes split 45/55
        # between "R1 scores" and "R1 stops at 3B". Expected-runs values
        # are 0.45·(runs if R1 scores) + 0.55·(runs if R1 doesn't).
        ("100", 2, 0.45),
        ("110", 2, 1.45),
        ("101", 2, 1.45),
        ("111", 2, 2.45),
    ])
    def test_double_all_configs(self, runners, expected_branches, expected_runs):
        """Pin _DOUBLE_TABLE's probabilistic shape. R1-on-base configs
        have two branches (45% scores / 55% stops at 3B); the rest are
        deterministic. Branch probabilities must sum to 1.0 and the
        expected-runs weighted average must match the parametrized value.

        Replaces an older deterministic test that asserted
        `s.runners == "010"` and integer `expected_runs` — that model
        doesn't hold since 2B became probabilistic, and `apply_event` no
        longer returns a single canonical post-state for 2B."""
        branches = _DOUBLE_TABLE[runners]
        assert len(branches) == expected_branches
        assert sum(p for p, _, _ in branches) == pytest.approx(1.0)
        weighted_runs = sum(p * r for p, _, r in branches)
        assert weighted_runs == pytest.approx(expected_runs)

    @pytest.mark.parametrize("runners,expected_runs", [
        ("000", 1), ("100", 2), ("010", 2), ("001", 2),
        ("110", 3), ("101", 3), ("011", 3), ("111", 4),
    ])
    def test_hr_all_configs(self, runners, expected_runs):
        s = apply_event(GameState(5, "bot", 0, runners, 0), "HR")
        assert s.runners == "000"  # bases cleared
        assert s.score_diff == expected_runs

    def test_dp_not_possible_bases_empty(self):
        assert apply_event(GameState(5, "top", 0, "000", 0), "DP") is None

    def test_dp_not_possible_two_outs(self):
        assert apply_event(GameState(5, "top", 2, "100", 0), "DP") is None

    def test_dp_runner_on_first(self):
        s = apply_event(GameState(5, "top", 0, "100", 0), "DP")
        assert s.outs == 2
        assert s.runners == "000"

    def test_dp_bases_loaded_run_scores(self):
        s = apply_event(GameState(5, "bot", 0, "111", 0), "DP")
        assert s.outs == 2
        assert s.runners == "001"  # 2nd to 3rd, 3rd scores
        assert s.score_diff == 1

    def test_dp_r1_r3_force_scores(self):
        """Force DP from "101": R1 forced at 2B, batter out at 1B, R3
        scores on the play. Nobody remains on base — the old
        `_DP_TABLE["101"] = ("010", 1)` was physically impossible (there
        is no runner at 2B once R1 is the force-out)."""
        s = apply_event(GameState(5, "bot", 0, "101", 0), "DP")
        assert s.outs == 2
        assert s.runners == "000"
        assert s.score_diff == 1

    def test_dp_third_out_flips(self):
        s = apply_event(GameState(5, "top", 1, "100", 0), "DP")
        assert s.half == "bot"
        assert s.outs == 0
        assert s.runners == "000"


class TestInningFlip:
    def test_top_to_bottom(self):
        s = apply_event(GameState(4, "top", 2, "000", 0), "K")
        assert s.half == "bot"
        assert s.inning == 4

    def test_bottom_to_next_top(self):
        s = apply_event(GameState(4, "bot", 2, "000", 0), "K")
        assert s.half == "top"
        assert s.inning == 5

    def test_bottom_9_to_top_caps_at_9(self):
        s = apply_event(GameState(9, "bot", 2, "000", 0), "K")
        assert s.half == "top"
        assert s.inning == 9  # capped

    def test_flip_clears_runners(self):
        s = apply_event(GameState(3, "top", 2, "111", 0), "K")
        assert s.runners == "000"

    def test_flip_preserves_score_diff(self):
        s = apply_event(GameState(3, "top", 2, "000", 3), "K")
        assert s.score_diff == 3


class TestScoreDirection:
    def test_top_half_runs_decrease_diff(self):
        """Away team batting in top — runs scored decrease home's score_diff."""
        s = apply_event(GameState(3, "top", 0, "000", 2), "HR")
        assert s.score_diff == 1  # was +2, away scored 1 → +1

    def test_bot_half_runs_increase_diff(self):
        """Home team batting in bottom — runs scored increase score_diff."""
        s = apply_event(GameState(3, "bot", 0, "000", 0), "HR")
        assert s.score_diff == 1


# ===================================================================
# Run distribution
# ===================================================================

class TestRunDistribution:
    def test_loads_all_24_states(self):
        for outs in range(3):
            for runners in RUNNER_CONFIGS:
                dist = get_run_distribution(outs, runners)
                assert len(dist) > 0

    def test_probabilities_sum_to_approximately_one(self):
        for outs in range(3):
            for runners in RUNNER_CONFIGS:
                dist = get_run_distribution(outs, runners)
                total = sum(dist)
                # Truncated tail means sum < 1.0 but very close
                assert 0.999 < total <= 1.001, (
                    f"{outs}-{runners}: sum={total}"
                )

    def test_empty_bases_zero_runs_most_likely(self):
        dist = get_run_distribution(0, "000")
        assert dist[0] > 0.70  # ~73% of the time 0 runs score

    def test_bases_loaded_zero_runs_rare(self):
        dist = get_run_distribution(0, "111")
        assert dist[0] < 0.20  # only ~13% of the time

    def test_more_outs_means_fewer_runs(self):
        """P(0 runs) should increase with more outs."""
        p0_0out = get_run_distribution(0, "000")[0]
        p0_1out = get_run_distribution(1, "000")[0]
        p0_2out = get_run_distribution(2, "000")[0]
        assert p0_0out < p0_1out < p0_2out

    def test_runners_on_base_increase_expected_runs(self):
        dist_empty = get_run_distribution(0, "000")
        dist_loaded = get_run_distribution(0, "111")
        er_empty = sum(i * p for i, p in enumerate(dist_empty))
        er_loaded = sum(i * p for i, p in enumerate(dist_loaded))
        assert er_loaded > er_empty

    def test_fallback_for_missing_key(self):
        """Should not crash on unexpected input."""
        dist = get_run_distribution(0, "000")
        assert isinstance(dist, list)


# ===================================================================
# Convolution
# ===================================================================

class TestConvolution:
    def test_identity(self):
        """Convolving with [1.0] returns the same distribution."""
        a = [0.5, 0.3, 0.2]
        result = _convolve(a, [1.0])
        assert len(result) == len(a)
        for x, y in zip(result, a):
            assert abs(x - y) < 1e-10

    def test_two_coins(self):
        """Convolving two fair coin flips: P(0)=0.25, P(1)=0.5, P(2)=0.25."""
        coin = [0.5, 0.5]
        result = _convolve(coin, coin)
        assert abs(result[0] - 0.25) < 1e-10
        assert abs(result[1] - 0.50) < 1e-10
        assert abs(result[2] - 0.25) < 1e-10

    def test_output_sums_to_one(self):
        a = [0.6, 0.3, 0.1]
        b = [0.7, 0.2, 0.1]
        result = _convolve(a, b)
        assert abs(sum(result) - 1.0) < 1e-10


# ===================================================================
# Remaining half-innings
# ===================================================================

class TestRemainingHalves:
    def test_top_of_1(self):
        bat, field, is_home = _remaining_halves(1, "top")
        assert bat == 8     # away: T2..T9
        assert field == 9   # home: B1..B9
        assert is_home is False

    def test_bot_of_1(self):
        bat, field, is_home = _remaining_halves(1, "bot")
        assert bat == 8     # home: B2..B9
        assert field == 8   # away: T2..T9
        assert is_home is True

    def test_top_of_9(self):
        bat, field, is_home = _remaining_halves(9, "top")
        assert bat == 0     # away: no more
        assert field == 1   # home: B9
        assert is_home is False

    def test_bot_of_9(self):
        bat, field, is_home = _remaining_halves(9, "bot")
        assert bat == 0
        assert field == 0
        assert is_home is True

    def test_extras_capped(self):
        """Inning 12 treated as inning 9."""
        bat, field, _ = _remaining_halves(12, "top")
        assert bat == 0
        assert field == 1


# ===================================================================
# Over/under probability
# ===================================================================

class TestOverUnder:
    def test_already_over(self):
        """If current total already exceeds line, P = 1."""
        p = compute_over_under_probability(5, 5, 7, "top", 0, "000", 8.5)
        assert p == 1.0

    def test_start_of_game_near_half(self):
        """O/U 8.5 at game start should be roughly 0.4–0.6."""
        p = compute_over_under_probability(0, 0, 1, "top", 0, "000", 8.5)
        assert 0.3 < p < 0.7

    def test_late_game_low_score_low_probability(self):
        """Bot 9, 2 outs, total = 3, O/U 8.5 — nearly impossible."""
        p = compute_over_under_probability(2, 1, 9, "bot", 2, "000", 8.5)
        assert p < 0.01

    def test_higher_line_lower_probability(self):
        """P(over 7.5) > P(over 8.5) > P(over 9.5) for same state."""
        p75 = compute_over_under_probability(0, 0, 1, "top", 0, "000", 7.5)
        p85 = compute_over_under_probability(0, 0, 1, "top", 0, "000", 8.5)
        p95 = compute_over_under_probability(0, 0, 1, "top", 0, "000", 9.5)
        assert p75 > p85 > p95

    def test_more_runs_scored_increases_over_probability(self):
        """Higher current total → higher P(over) for same line."""
        p_low = compute_over_under_probability(1, 1, 5, "top", 0, "000", 8.5)
        p_high = compute_over_under_probability(3, 3, 5, "top", 0, "000", 8.5)
        assert p_high > p_low

    def test_earlier_inning_more_uncertainty(self):
        """Same score, earlier inning → closer to 0.5 (more innings left)."""
        p_early = compute_over_under_probability(2, 2, 3, "top", 0, "000", 8.5)
        p_late = compute_over_under_probability(2, 2, 8, "top", 0, "000", 8.5)
        # Early should be closer to the start-of-game level; late should be more extreme
        assert abs(p_early - 0.5) < abs(p_late - 0.5) or p_late < p_early

    def test_returns_valid_probability(self):
        """Output always in [0, 1]."""
        for total_line in [5.5, 7.5, 8.5, 9.5, 12.5]:
            p = compute_over_under_probability(0, 0, 1, "top", 0, "000", total_line)
            assert 0.0 <= p <= 1.0


# ===================================================================
# Spread probability
# ===================================================================

class TestSpread:
    def test_big_lead_high_cover_probability(self):
        """Home up 5 in the 7th → should cover -1.5 easily."""
        p = compute_spread_probability(7, 2, 7, "top", 0, "000", 1.5)
        assert p > 0.70

    def test_down_big_low_cover_probability(self):
        """Home down 5 → very unlikely to cover -1.5."""
        p = compute_spread_probability(2, 7, 7, "top", 0, "000", 1.5)
        assert p < 0.10

    def test_negative_spread_easier_to_cover(self):
        """P(margin > -1.5) > P(margin > +1.5) at tie."""
        p_neg = compute_spread_probability(0, 0, 1, "top", 0, "000", -1.5)
        p_pos = compute_spread_probability(0, 0, 1, "top", 0, "000", 1.5)
        assert p_neg > p_pos

    def test_tie_game_home_slight_edge(self):
        """At tie, P(home margin > -1.5) should be > 0.5 (home advantage)."""
        p = compute_spread_probability(0, 0, 1, "top", 0, "000", -1.5)
        assert p > 0.5

    def test_returns_valid_probability(self):
        for line in [-3.5, -1.5, 0.5, 1.5, 3.5]:
            p = compute_spread_probability(3, 3, 5, "top", 0, "000", line)
            assert 0.0 <= p <= 1.0


# ===================================================================
# Runs scored helper
# ===================================================================

class TestRunsScored:
    def test_k_scores_zero(self):
        assert _runs_scored(GameState(1, "top", 0, "111", 0), "K") == 0

    def test_hr_bases_loaded_scores_four(self):
        assert _runs_scored(GameState(1, "bot", 0, "111", 0), "HR") == 4

    def test_hr_empty_scores_one(self):
        assert _runs_scored(GameState(1, "bot", 0, "000", 0), "HR") == 1

    def test_single_runners_on_second_and_third_scores_two(self):
        assert _runs_scored(GameState(1, "bot", 0, "011", 0), "1B") == 2

    def test_bb_bases_loaded_scores_one(self):
        assert _runs_scored(GameState(1, "bot", 0, "111", 0), "BB") == 1

    def test_bb_not_loaded_scores_zero(self):
        assert _runs_scored(GameState(1, "bot", 0, "100", 0), "BB") == 0

    def test_dp_bases_loaded_scores_one(self):
        assert _runs_scored(GameState(1, "bot", 0, "111", 0), "DP") == 1

    def test_dp_not_applicable_scores_zero(self):
        assert _runs_scored(GameState(1, "bot", 0, "000", 0), "DP") == 0
        assert _runs_scored(GameState(1, "bot", 2, "100", 0), "DP") == 0


# ===================================================================
# Full delta computation
# ===================================================================

class TestComputeDeltas:
    def test_returns_all_applicable_events(self):
        state = GameState(5, "top", 0, "100", 0)
        deltas = compute_deltas(state, 3, 3)
        events = {d.event for d in deltas}
        assert events == {"K", "OUT", "BB", "1B", "2B", "HR", "DP"}

    def test_dp_excluded_when_not_applicable(self):
        # Bases empty
        deltas = compute_deltas(GameState(5, "top", 0, "000", 0), 3, 3)
        events = {d.event for d in deltas}
        assert "DP" not in events

        # Two outs
        deltas = compute_deltas(GameState(5, "top", 2, "100", 0), 3, 3)
        events = {d.event for d in deltas}
        assert "DP" not in events

    def test_delta_signs_top_half(self):
        """Away is batting: K/OUT/DP help home (positive), hits help away (negative)."""
        state = GameState(5, "top", 1, "100", 1)
        deltas = compute_deltas(state, 4, 3)
        by_event = {d.event: d for d in deltas}

        assert by_event["K"].delta > 0, "K should help home"
        assert by_event["OUT"].delta > 0, "OUT should help home"
        assert by_event["DP"].delta > 0, "DP should help home"
        assert by_event["BB"].delta < 0, "BB should help away"
        assert by_event["1B"].delta < 0, "1B should help away"
        assert by_event["2B"].delta < 0, "2B should help away"
        assert by_event["HR"].delta < 0, "HR should help away"

    def test_delta_signs_bottom_half(self):
        """Home is batting: hits help home (positive), K/OUT hurt home (negative)."""
        state = GameState(5, "bot", 1, "100", -1)
        deltas = compute_deltas(state, 2, 3)
        by_event = {d.event: d for d in deltas}

        assert by_event["K"].delta < 0, "K should hurt home"
        assert by_event["HR"].delta > 0, "HR should help home"
        assert by_event["1B"].delta > 0, "1B should help home"

    def test_delta_magnitude_order(self):
        """HR > 2B > 1B > BB in absolute moneyline impact."""
        state = GameState(5, "top", 0, "100", 0)
        deltas = compute_deltas(state, 3, 3)
        ml = {d.event: abs(d.delta) for d in deltas}
        assert ml["HR"] > ml["2B"] > ml["1B"] > ml["BB"]

    def test_ou_deltas_present_when_scores_given(self):
        state = GameState(5, "top", 0, "000", 0)
        deltas = compute_deltas(state, 3, 3)
        for d in deltas:
            assert "8.5" in d.over_under
            assert "7.5" in d.over_under
            assert "9.5" in d.over_under

    def test_spread_deltas_present_when_scores_given(self):
        state = GameState(5, "top", 0, "000", 0)
        deltas = compute_deltas(state, 3, 3)
        for d in deltas:
            assert "-1.5" in d.spread
            assert "1.5" in d.spread

    def test_ou_spread_empty_without_scores(self):
        state = GameState(5, "top", 0, "000", 0)
        deltas = compute_deltas(state)
        for d in deltas:
            assert d.over_under == {}
            assert d.spread == {}

    def test_ou_hr_pushes_over(self):
        """HR scores runs → O/U delta should be positive (more likely to go over)."""
        state = GameState(5, "top", 0, "000", 0)
        deltas = compute_deltas(state, 2, 2)
        hr = next(d for d in deltas if d.event == "HR")
        assert hr.over_under["8.5"]["delta"] > 0

    def test_ou_k_pushes_under(self):
        """K scores nothing, wastes an out → O/U delta should be negative."""
        state = GameState(5, "top", 0, "000", 0)
        deltas = compute_deltas(state, 2, 2)
        k = next(d for d in deltas if d.event == "K")
        assert k.over_under["8.5"]["delta"] < 0

    def test_walkoff_hr_sets_we_to_one(self):
        """Bottom 9, tie, HR → home takes the lead → WE = 1.0."""
        state = GameState(9, "bot", 0, "000", 0)
        deltas = compute_deltas(state, 3, 3)
        hr = next(d for d in deltas if d.event == "HR")
        assert hr.win_expectancy_after == 1.0

    def test_all_deltas_in_valid_range(self):
        """Every before/after WE should be in [0, 1], delta in [-1, 1]."""
        for inning in [1, 5, 9]:
            for half in ["top", "bot"]:
                state = GameState(inning, half, 0, "100", 0)
                deltas = compute_deltas(state, 3, 3)
                for d in deltas:
                    assert 0 <= d.win_expectancy_before <= 1
                    assert 0 <= d.win_expectancy_after <= 1
                    assert -1 <= d.delta <= 1
                    for line, ou in d.over_under.items():
                        assert 0 <= ou["before"] <= 1
                        assert 0 <= ou["after"] <= 1
                    for line, sp in d.spread.items():
                        assert 0 <= sp["before"] <= 1
                        assert 0 <= sp["after"] <= 1
