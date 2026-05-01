"""Regression: SPR predicted-move cap in late-tied games.

Cap is empirical (Kalshi window-max in late-tied SPR-1.5 trades averaged 0.10
where the engine predicted ~0.38). Each invariant below maps to a guard
the cap must hold without altering predictions in any other regime.

Run:
    cd backend && python -m pytest tests/test_regime_caps.py -v
"""

from app.regime_caps import cap_spread_predicted_move


class TestSpreadPredictedMoveCap:
    def test_late_tied_positive_capped(self):
        assert cap_spread_predicted_move(0.59, inning=9, margin=0, market_type="spread") == 0.10

    def test_late_tied_negative_capped(self):
        assert cap_spread_predicted_move(-0.59, inning=9, margin=0, market_type="spread") == -0.10

    def test_late_tied_below_cap_unchanged(self):
        assert cap_spread_predicted_move(0.05, inning=9, margin=0, market_type="spread") == 0.05

    def test_late_tied_at_cap_unchanged(self):
        assert cap_spread_predicted_move(0.10, inning=9, margin=0, market_type="spread") == 0.10

    def test_inning_8_unchanged(self):
        assert cap_spread_predicted_move(0.59, inning=8, margin=0, market_type="spread") == 0.59

    def test_margin_2_unchanged(self):
        assert cap_spread_predicted_move(0.59, inning=9, margin=2, market_type="spread") == 0.59

    def test_margin_neg_2_unchanged(self):
        assert cap_spread_predicted_move(0.59, inning=9, margin=-2, market_type="spread") == 0.59

    def test_moneyline_unchanged(self):
        assert cap_spread_predicted_move(0.59, inning=9, margin=0, market_type="moneyline") == 0.59

    def test_over_under_unchanged(self):
        assert cap_spread_predicted_move(0.59, inning=9, margin=0, market_type="over_under") == 0.59

    def test_extras_still_capped(self):
        # Inning 11 tied → still in regime
        assert cap_spread_predicted_move(0.59, inning=11, margin=0, market_type="spread") == 0.10

    def test_margin_1_capped(self):
        # |margin| == 1 is at the boundary, should still be in regime
        assert cap_spread_predicted_move(0.59, inning=9, margin=1, market_type="spread") == 0.10
        assert cap_spread_predicted_move(0.59, inning=9, margin=-1, market_type="spread") == 0.10

    def test_disabled_returns_raw(self, monkeypatch):
        monkeypatch.setattr("app.regime_caps.SPR_LATE_TIED_CAP_ENABLED", False)
        assert cap_spread_predicted_move(0.59, inning=9, margin=0, market_type="spread") == 0.59

    def test_zero_unchanged(self):
        assert cap_spread_predicted_move(0.0, inning=9, margin=0, market_type="spread") == 0.0
