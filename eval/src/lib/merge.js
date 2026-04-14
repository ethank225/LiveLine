const MATCH_WINDOW_MS = 60_000;

function parseGameState(s) {
  if (!s) return null;
  try { return JSON.parse(s); } catch { return null; }
}

function toNum(v) {
  if (v === "" || v == null) return null;
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : null;
}

export function parseSupabase(rows) {
  return rows
    .filter((r) => r.market_ticker && r.side && r.user_timestamp)
    .map((r) => ({
      id: r.id,
      ticker: r.market_ticker,
      side: r.side,
      eventType: r.event_type || "",
      marketLabel: r.market_label || "",
      userTime: new Date(r.user_timestamp),
      createdAt: r.created_at ? new Date(r.created_at) : null,
      closedAt: r.closed_at ? new Date(r.closed_at) : null,
      entryPrice: toNum(r.entry_price),
      exitPrice: toNum(r.exit_price),
      sellTarget: toNum(r.sell_target),
      quantity: toNum(r.quantity),
      alpha: toNum(r.alpha),
      predictedDelta: toNum(r.predicted_delta),
      status: r.status || "",
      dryRun: r.dry_run === "true",
      gameState: parseGameState(r.game_state_json),
      entryFeeEst: toNum(r.entry_fee_est),
      exitFeeEst: toNum(r.exit_fee_est),
      netExpectedProfit: toNum(r.net_expected_profit),
      feeAdjusted: r.fee_adjusted === "true",
      grossPickTicker: r.gross_pick_ticker || "",
    }));
}

// Match each Kalshi play to a single Supabase row by ticker + side + nearest
// user_timestamp within 60s. Greedy nearest-first so overlapping candidates
// don't steal each other's best match.
export function mergeSupabase(plays, supabaseRows) {
  const supa = supabaseRows.filter((r) => !r.dryRun);
  const candidates = [];
  for (let pi = 0; pi < plays.length; pi++) {
    const p = plays[pi];
    for (let si = 0; si < supa.length; si++) {
      const r = supa[si];
      if (r.ticker !== p.ticker) continue;
      if (r.side !== p.side) continue;
      const delta = Math.abs(r.userTime - p.startTime);
      if (delta > MATCH_WINDOW_MS) continue;
      candidates.push({ pi, si, delta });
    }
  }
  candidates.sort((a, b) => a.delta - b.delta);
  const usedPlay = new Set();
  const usedSupa = new Set();
  for (const c of candidates) {
    if (usedPlay.has(c.pi) || usedSupa.has(c.si)) continue;
    plays[c.pi].context = supa[c.si];
    usedPlay.add(c.pi);
    usedSupa.add(c.si);
  }
  return plays;
}

export function collectEventTypes(plays) {
  const set = new Set();
  for (const p of plays) if (p.context?.eventType) set.add(p.context.eventType);
  return Array.from(set).sort();
}

// Per-event-type rollup over closed plays. netPct is cost-weighted.
export function eventTypeStats(plays) {
  const groups = new Map();
  for (const p of plays) {
    if (p.open) continue;
    const et = p.context?.eventType;
    if (!et) continue;
    if (!groups.has(et)) groups.set(et, []);
    groups.get(et).push(p);
  }
  const out = [];
  for (const [eventType, ps] of groups) {
    const netTotal = ps.reduce((s, p) => s + p.net, 0);
    const cost = ps.reduce((s, p) => s + (p.entryAvg * p.qty) / 100, 0);
    const wins = ps.filter((p) => p.net > 0).length;
    out.push({
      eventType,
      count: ps.length,
      netTotal,
      netPct: cost ? (netTotal / cost) * 100 : 0,
      winRate: ps.length ? (wins / ps.length) * 100 : 0,
    });
  }
  out.sort((a, b) => b.count - a.count);
  return out;
}

export function fmtRunners(r) {
  if (!r || r.length !== 3) return "—";
  const bases = [];
  if (r[0] === "1") bases.push("1B");
  if (r[1] === "1") bases.push("2B");
  if (r[2] === "1") bases.push("3B");
  return bases.length ? bases.join(" ") : "bases empty";
}

export function fmtInning(gs) {
  if (!gs || gs.inning == null) return "—";
  const arrow = gs.half === "top" ? "▲" : gs.half === "bot" ? "▼" : "";
  return `${arrow} ${gs.inning}`;
}

// Sort key: half-innings since game start (top 1 = 0, bot 1 = 1, ...). Null-safe.
export function situationSortKey(p) {
  const gs = p.context?.gameState;
  if (!gs || gs.inning == null) return -Infinity;
  const halfOffset = gs.half === "bot" ? 1 : 0;
  const outs = gs.outs ?? 0;
  return (gs.inning - 1) * 2 + halfOffset + outs / 10;
}
