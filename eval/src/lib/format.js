export const fmtMoney = (n) => `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}`;

export const fmtCents = (n) => (n == null ? "—" : `${n.toFixed(1)}¢`);

export const fmtTime = (d) =>
  d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

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
