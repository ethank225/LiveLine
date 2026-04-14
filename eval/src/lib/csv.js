import { costBasis } from "./columns.js";

const HEADERS = [
  "entry_time_kalshi_utc",
  "event",
  "fee_adjusted",
  "inning",
  "half",
  "outs",
  "runners",
  "away_abbr",
  "away_score",
  "home_abbr",
  "home_score",
  "market",
  "ticker",
  "side",
  "qty",
  "entry_cents",
  "exit_cents",
  "cost_basis_usd",
  "exit_value_usd",
  "duration_sec",
  "gross_usd",
  "fees_usd",
  "net_usd",
  "pct_of_cost",
  "exit_type",
  "open",
  "alpha",
  "predicted_delta",
  "entry_fee_est",
  "exit_fee_est",
  "net_expected_profit",
  "gross_pick_ticker",
  "status",
];

// Format a Date as ISO-8601 with seconds in UTC — matches the dashboard's
// "Entry Time" column and keeps the CSV unambiguous across timezones. The
// source is `p.startTime`, which is the Kalshi fill timestamp from the
// Recent-Activity-Trade CSV (parse.js → Original_Date), NOT the Supabase
// user_timestamp. Supabase rows are merged in only to attach context.
function toISOSeconds(d) {
  if (!d) return "";
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

// RFC 4180: wrap with quotes and escape embedded quotes if the value contains
// a comma, quote, or newline. Plain values pass through so the file stays
// readable in a plain text editor.
function escapeCell(v) {
  if (v == null) return "";
  const s = String(v);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function rowFor(p) {
  const ctx = p.context || {};
  const gs = ctx.gameState || {};
  const pct = costBasis(p) > 0 ? (p.net / costBasis(p)) * 100 : null;
  return {
    entry_time_kalshi_utc: toISOSeconds(p.startTime),
    event: ctx.eventType || "",
    fee_adjusted: ctx.feeAdjusted ? "true" : "false",
    inning: gs.inning ?? "",
    half: gs.half ?? "",
    outs: gs.outs ?? "",
    runners: gs.runners ?? "",
    away_abbr: p.game?.away || "",
    away_score: gs.away_score ?? "",
    home_abbr: p.game?.home || "",
    home_score: gs.home_score ?? "",
    market: p.market ?? "",
    ticker: p.ticker ?? "",
    side: p.side ?? "",
    qty: p.qty ?? "",
    entry_cents: p.entryAvg != null ? p.entryAvg.toFixed(2) : "",
    exit_cents: p.exitAvg != null ? p.exitAvg.toFixed(2) : "",
    cost_basis_usd: costBasis(p).toFixed(2),
    exit_value_usd: p.exitAvg != null ? ((p.exitAvg * p.qty) / 100).toFixed(2) : "",
    duration_sec: Number.isFinite(p.endTime - p.startTime)
      ? Math.round((p.endTime - p.startTime) / 1000)
      : "",
    gross_usd: p.gross != null ? p.gross.toFixed(2) : "",
    fees_usd: p.fees != null ? p.fees.toFixed(2) : "",
    net_usd: p.net != null ? p.net.toFixed(2) : "",
    pct_of_cost: pct != null ? pct.toFixed(2) : "",
    exit_type: p.exitType ?? "",
    open: p.open ? "true" : "false",
    alpha: ctx.alpha != null ? ctx.alpha : "",
    predicted_delta: ctx.predictedDelta != null ? ctx.predictedDelta : "",
    entry_fee_est: ctx.entryFeeEst != null ? ctx.entryFeeEst.toFixed(4) : "",
    exit_fee_est: ctx.exitFeeEst != null ? ctx.exitFeeEst.toFixed(4) : "",
    net_expected_profit:
      ctx.netExpectedProfit != null ? ctx.netExpectedProfit.toFixed(4) : "",
    gross_pick_ticker: ctx.grossPickTicker || "",
    status: ctx.status || "",
  };
}

export function playsToCSV(plays) {
  const lines = [HEADERS.join(",")];
  for (const p of plays) {
    const row = rowFor(p);
    lines.push(HEADERS.map((h) => escapeCell(row[h])).join(","));
  }
  return lines.join("\n");
}

export function downloadCSV(plays, filename = "trades.csv") {
  const blob = new Blob([playsToCSV(plays)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
