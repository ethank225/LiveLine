// Kalshi fee schedule:
//   taker fee = ceil(0.07   * C * P * (1 - P))  in cents
//   maker fee = ceil(0.0175 * C * P * (1 - P))  in cents
// where C = contracts, P = price in dollars (0..1). Rounded UP to the next cent.
const TAKER = 0.07;
const MAKER = 0.0175;

function feeForFill(qty, priceCents, orderType) {
  if (qty <= 0) return 0;
  const p = priceCents / 100;
  const coef = orderType === "Maker" ? MAKER : TAKER;
  const raw = coef * qty * p * (1 - p);
  // Kalshi rounds UP to the next whole cent. Guard against FP dust.
  const cents = Math.ceil(raw * 100 - 1e-9);
  return cents / 100;
}

// Recompute total fees for a play if every fill's qty were scaled by `scale`.
// Rounding is applied per-fill, matching Kalshi's per-order-book behavior.
export function scaledFees(play, scale) {
  let total = 0;
  for (const f of play.entryFills || []) {
    total += feeForFill(f.qty * scale, f.price, f.orderType);
  }
  for (const f of play.exitFills || []) {
    total += feeForFill(f.qty * scale, f.price, f.orderType);
  }
  return total;
}

// Gross scales exactly linearly (price × qty), so no formula adjustment needed.
export function scaledGross(play, scale) {
  return play.gross * scale;
}
