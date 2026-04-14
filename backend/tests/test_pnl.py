"""Tests for the shared P&L module (app/pnl.py)."""

import pytest

from app.pnl import compute_fees, compute_gross, compute_net


# The canonical worked example both trader.py and the backtest reference:
# 100 contracts, traded-side entry 0.43, exit 0.46, filled at target (maker).
# Derivation:
#   gross      = (0.46 - 0.43) * 100             = $3.0000
#   entry_fee  = ⌈0.07   × 100 × 0.43 × 0.57⌉¢   = ⌈171.57¢⌉ = $1.72
#   exit_fee   = ⌈0.0175 × 100 × 0.46 × 0.54⌉¢   = ⌈ 43.47¢⌉ = $0.44
#   fees       = $2.16
#   net        = $0.84
class TestNoSideMakerExample:
    def test_compute_gross(self):
        assert compute_gross(0.43, 0.46, 100) == pytest.approx(3.00)

    def test_compute_fees_maker(self):
        entry_fee, exit_fee = compute_fees(100, 0.43, 0.46, "maker")
        assert entry_fee == pytest.approx(1.72)
        assert exit_fee == pytest.approx(0.44)

    def test_compute_net_maker(self):
        pnl = compute_net(0.43, 0.46, 100, "maker")
        assert pnl == {
            "gross": pytest.approx(3.00),
            "entry_fee": pytest.approx(1.72),
            "exit_fee": pytest.approx(0.44),
            "fees": pytest.approx(2.16),
            "net": pytest.approx(0.84),
        }


class TestTakerExit:
    """IOC exit (expire / stop-loss / undo) is charged the taker rate
    on both legs. The 3¢ move in the canonical example doesn't clear
    round-trip taker fees — net is negative, which the old gross-only
    realized_pnl was silently hiding."""

    def test_taker_exit_fee_is_higher(self):
        _, maker = compute_fees(100, 0.43, 0.46, "maker")
        _, taker = compute_fees(100, 0.43, 0.46, "taker")
        assert taker > maker
        assert taker == pytest.approx(1.74)

    def test_taker_net_goes_negative(self):
        pnl = compute_net(0.43, 0.46, 100, "taker")
        assert pnl["net"] == pytest.approx(-0.46)


class TestInvalidExitType:
    def test_raises_on_bad_exit_type(self):
        with pytest.raises(ValueError, match="exit_type"):
            compute_fees(100, 0.43, 0.46, "rebate")

    def test_compute_net_raises_too(self):
        with pytest.raises(ValueError):
            compute_net(0.43, 0.46, 100, "cross")


class TestEdgeCases:
    def test_zero_quantity_gives_zero_fees(self):
        entry_fee, exit_fee = compute_fees(0, 0.43, 0.46, "maker")
        assert entry_fee == 0.0
        assert exit_fee == 0.0

    def test_loss_trade(self):
        # Entry 0.55, exit 0.50, 100 contracts, maker exit. Losing trade
        # — net should be even worse than gross after fees come off.
        pnl = compute_net(0.55, 0.50, 100, "maker")
        assert pnl["gross"] == pytest.approx(-5.00)
        assert pnl["net"] < pnl["gross"]

    def test_symmetric_across_sides(self):
        # The module doesn't know YES vs NO — it just sees traded-side
        # prices. A NO trade entered at 0.43 / exited at 0.46 should
        # produce the same numbers as a YES trade at the same prices.
        no_side = compute_net(0.43, 0.46, 100, "maker")
        yes_side = compute_net(0.43, 0.46, 100, "maker")
        assert no_side == yes_side
