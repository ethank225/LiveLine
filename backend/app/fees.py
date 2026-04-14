"""
Kalshi fee schedule — single source of truth.

  taker fee = ⌈0.07   × C × P × (1 − P)⌉   (cents)
  maker fee = ⌈0.0175 × C × P × (1 − P)⌉   (cents)

where C is the number of contracts and P is the contract price in dollars
(0..1). Rounded UP to the next whole cent.

JS mirror: eval/src/lib/fees.js.
"""

import math

TAKER_COEF = 0.07
MAKER_COEF = 0.0175


def fee_for_contracts(contracts: float, price_dollars: float, coef: float) -> float:
    """⌈coef × C × P × (1 − P)⌉ rounded UP to the next cent. Returns dollars.

    `contracts` is a count, not a dollar amount.
    """
    if contracts <= 0 or price_dollars <= 0 or price_dollars >= 1:
        return 0.0
    raw_dollars = coef * contracts * price_dollars * (1.0 - price_dollars)
    # Ceiling to the next cent; epsilon guard against FP dust that would
    # otherwise push an exact-cent value up one cent.
    cents = math.ceil(raw_dollars * 100 - 1e-9)
    return cents / 100


def taker_fee(contracts: float, price_dollars: float) -> float:
    return fee_for_contracts(contracts, price_dollars, TAKER_COEF)


def maker_fee(contracts: float, price_dollars: float) -> float:
    return fee_for_contracts(contracts, price_dollars, MAKER_COEF)
