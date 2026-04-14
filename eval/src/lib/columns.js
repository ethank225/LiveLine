import { situationSortKey } from "./merge.js";

// Sort by run margin (home − away) so blowouts cluster.
function scoreSortKey(p) {
  const gs = p.context?.gameState;
  if (!gs || gs.away_score == null) return -Infinity;
  return (gs.home_score ?? 0) - (gs.away_score ?? 0);
}

// Cost basis in dollars: entry price × qty.
export function costBasis(p) {
  return (p.entryAvg * p.qty) / 100;
}

export function pctOfCost(p, dollars) {
  const cost = costBasis(p);
  if (!cost) return null;
  return (dollars / cost) * 100;
}

export const COLUMNS = [
  { key: "startTime", label: "Entry Time", width: 130, getter: (p) => p.startTime.getTime() },
  { key: "event",     label: "Event",      width: 70,  getter: (p) => p.context?.eventType || "" },
  { key: "situation", label: "Situation",  width: 110, getter: situationSortKey },
  { key: "score",     label: "Score",      width: 130, getter: scoreSortKey },
  { key: "market",    label: "Market",     width: 160, getter: (p) => p.market },
  { key: "qty",       label: "Qty",        width: 55,  getter: (p) => p.qty },
  { key: "pricePath", label: "Price",      width: 115, getter: (p) => p.entryAvg },
  { key: "amount",    label: "Amount",     width: 120, getter: (p) => costBasis(p) },
  { key: "duration",  label: "Duration",   width: 85,  getter: (p) => p.endTime - p.startTime },
  { key: "pnl",       label: "P&L",        width: 135, getter: (p) => p.net },
  { key: "exitType",  label: "Exit",       width: 60,  getter: (p) => p.exitType },
];

export function sortPlays(plays, sort) {
  const col = COLUMNS.find((c) => c.key === sort.key);
  if (!col) return plays;
  const dir = sort.dir === "asc" ? 1 : -1;
  return [...plays].sort((a, b) => {
    const av = col.getter(a);
    const bv = col.getter(b);
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return 0;
  });
}
