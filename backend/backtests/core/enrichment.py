"""Enrich MLB play records with model predictions."""

from app.engine import (
    GameState,
    apply_event,
    get_win_expectancy,
    compute_over_under_probability,
    compute_spread_probability,
    _runs_scored,
)
from app.regime_caps import cap_spread_predicted_move
from backtests.core.mlb import PlayRecord


def enrich_with_model(records: list[PlayRecord]):
    """Add model WE and delta predictions to each play record."""
    for rec in records:
        state = GameState(
            inning=min(rec.inning, 9),
            half=rec.half,
            outs=rec.outs_before,
            runners=rec.runners_before,
            score_diff=rec.home_score - rec.away_score,
        )

        rec.we_before = get_win_expectancy(state)

        new_state = apply_event(state, rec.mapped_event)
        if new_state:
            rec.we_after = get_win_expectancy(new_state)
            # Walk-off: bottom 9+, home takes lead on this play
            if new_state.half == "bot" and new_state.inning >= 9 and new_state.score_diff > 0:
                rec.we_after = 1.0
            # 3rd out in bottom 9+ (inning flipped to top)
            if state.half == "bot" and state.inning >= 9 and new_state.half == "top":
                if new_state.score_diff > 0:
                    rec.we_after = 1.0   # home wins
                elif new_state.score_diff < 0:
                    rec.we_after = 0.0   # home loses
            rec.ml_delta = rec.we_after - rec.we_before
        else:
            rec.we_after = rec.we_before
            rec.ml_delta = 0.0

        new_home, new_away = rec.home_score, rec.away_score
        if new_state:
            runs = _runs_scored(state, rec.mapped_event)
            if rec.half == "top":
                new_away = rec.away_score + runs
            else:
                new_home = rec.home_score + runs

        # Detect game-ending state: 3rd out in bot 9+ (inning flipped to top)
        # with a decisive score (not tied). No more innings will be played —
        # total runs and margin are FINAL.
        game_over = (new_state and state.half == "bot" and state.inning >= 9
                     and new_state.half == "top" and new_state.score_diff != 0)

        inn = min(rec.inning, 9)
        before_args = (rec.home_score, rec.away_score, inn, rec.half,
                       rec.outs_before, rec.runners_before)
        after_args = (new_home, new_away, new_state.inning, new_state.half,
                      new_state.outs, new_state.runners) if new_state else before_args

        for line in [2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5, 12.5, 13.5]:
            before = compute_over_under_probability(*before_args, line)
            if game_over:
                # Game ended — total is final. Over if total > line.
                final_total = new_home + new_away
                after = 1.0 if final_total > line else 0.0
            elif new_state:
                after = compute_over_under_probability(*after_args, line)
            else:
                after = before
            delta = after - before
            rec.model_deltas[f"ou_{line}"] = round(delta, 6)
            if line == 8.5:
                rec.ou_85_before = before
                rec.ou_85_after = after
                rec.ou_85_delta = delta

        for line in [-1.5, -2.5, -3.5, -4.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5]:
            before = compute_spread_probability(*before_args, line)
            if game_over:
                # Game ended — margin is final.
                final_margin = new_home - new_away
                after = 1.0 if final_margin > line else 0.0
            elif new_state:
                after = compute_spread_probability(*after_args, line)
            else:
                after = before
            raw_delta = after - before
            capped = cap_spread_predicted_move(
                raw_delta,
                inning=rec.inning,
                margin=rec.home_score - rec.away_score,
                market_type="spread",
            )
            rec.model_deltas[f"sp_{line}"] = round(capped, 6)

        rec.model_deltas["ml"] = round(rec.ml_delta, 6)
