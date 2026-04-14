export const fmtMoney = (n) => `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}`;

export const fmtCents = (n) => (n == null ? "—" : `${n.toFixed(1)}¢`);

export const fmtTime = (d) =>
  d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "UTC",
  }) + " UTC";

export const fmtDuration = (ms) => {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60), rm = m % 60;
  return `${h}h ${rm}m`;
};

export const pnlClass = (n) => (n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "pnl-zero");

export const fmtPct = (n) =>
  n == null || !Number.isFinite(n) ? "—" : `${n >= 0 ? "" : "-"}${Math.abs(n).toFixed(1)}%`;
