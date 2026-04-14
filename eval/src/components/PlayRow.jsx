import React from "react";
import { fmtMoney, fmtCents, fmtTime, fmtDuration, fmtPct, pnlClass } from "../lib/format.js";
import { fmtRunners, fmtInning } from "../lib/merge.js";
import { COLUMNS, pctOfCost, costBasis } from "../lib/columns.js";

function Situation({ gs }) {
  if (!gs || gs.inning == null) {
    return <span className="situation empty">—</span>;
  }
  const arrow = gs.half === "top" ? "▲" : gs.half === "bot" ? "▼" : "";
  const r = gs.runners || "000";
  const outs = gs.outs ?? 0;
  return (
    <span className="situation">
      <span className="sit-inning">{arrow}{gs.inning}</span>
      <span className="sit-outs">
        <span className={`out-dot ${outs >= 1 ? "on" : ""}`} />
        <span className={`out-dot ${outs >= 2 ? "on" : ""}`} />
      </span>
      <span className="sit-bases">
        <span className={`base b2 ${r[1] === "1" ? "on" : ""}`} />
        <span className={`base b3 ${r[2] === "1" ? "on" : ""}`} />
        <span className={`base b1 ${r[0] === "1" ? "on" : ""}`} />
      </span>
    </span>
  );
}

function ScoreCell({ p }) {
  const gs = p.context?.gameState;
  if (!gs || gs.away_score == null) return <span className="cell-muted">—</span>;
  return (
    <span className="score-cell">
      <span className="score-team">
        <span className="score-abbr">{p.game.away || "AWY"}</span>
        <span className="score-num">{gs.away_score}</span>
      </span>
      <span className="score-sep">·</span>
      <span className="score-team">
        <span className="score-abbr">{p.game.home || "HOM"}</span>
        <span className="score-num">{gs.home_score}</span>
      </span>
    </span>
  );
}

export default function PlayRow({ p, expanded, onToggle }) {
  const ctx = p.context;
  const eventCls = ctx?.eventType ? `event-badge ev-${ctx.eventType.toLowerCase()}` : "";

  return (
    <>
      <tr
        className={`play-row ${expanded ? "expanded" : ""} ${ctx ? "" : "unmatched"}`}
        onClick={onToggle}
      >
        <td className="cell-time">{fmtTime(p.startTime)}</td>
        <td className="cell-event">
          {ctx?.eventType ? (
            <span className={eventCls}>{ctx.eventType}</span>
          ) : (
            <span className="event-badge ev-none">—</span>
          )}
          {ctx?.feeAdjusted && (
            <span
              className="event-badge ev-feeadj"
              title={
                ctx.grossPickTicker
                  ? `Fee-aware pick differs from gross pick (${ctx.grossPickTicker})`
                  : "Fee-aware pick differs from gross pick"
              }
            >
              fee-adj
            </span>
          )}
        </td>
        <td className="cell-situation"><Situation gs={ctx?.gameState} /></td>
        <td className="cell-score"><ScoreCell p={p} /></td>
        <td className="cell-market">{p.market}</td>
        <td className="num">{p.qty}</td>
        <td className="cell-price-path">
          <span className="price-entry">{fmtCents(p.entryAvg)}</span>
          <span className="price-arrow">→</span>
          <span className="price-exit">
            {p.exitAvg != null ? fmtCents(p.exitAvg) : "—"}
          </span>
        </td>
        <td className="cell-amount">
          <span className="amt-in">{fmtMoney(costBasis(p))}</span>
          <span className="price-arrow">→</span>
          <span className="amt-out">
            {p.exitAvg != null
              ? fmtMoney((p.exitAvg * p.qty) / 100)
              : "—"}
          </span>
        </td>
        <td className="cell-muted">{fmtDuration(p.endTime - p.startTime)}</td>
        <td className="cell-pnl">
          {p.open ? (
            <span className="pnl-open">open</span>
          ) : (
            <>
              <div className={`pnl-main ${pnlClass(p.net)}`}>
                <span className="pnl-dollar">{fmtMoney(p.net)}</span>
                <span className="pnl-pct">{fmtPct(pctOfCost(p, p.net))}</span>
              </div>
              <div className="pnl-sub">
                <span>gross {fmtMoney(p.gross)}</span>
                <span className="pnl-sep">·</span>
                <span>fees {fmtMoney(p.fees)}</span>
              </div>
            </>
          )}
        </td>
        <td className="cell-exit-type">{p.exitType}</td>
      </tr>
      {expanded && (
        <tr className="detail-row">
          <td colSpan={COLUMNS.length}>
            <ExpandedDetail p={p} ctx={ctx} />
          </td>
        </tr>
      )}
    </>
  );
}

function ExpandedDetail({ p, ctx }) {
  if (!ctx) {
    return (
      <div className="detail-empty">
        No matching Supabase trade within the 60s window for{" "}
        <code>{p.ticker}</code> / {p.side}.
      </div>
    );
  }
  const gs = ctx.gameState || {};
  return (
    <div className="detail-grid">
      <Section title="Signal">
        <Field label="Event" value={ctx.eventType || "—"} />
        <Field
          label="Alpha"
          value={ctx.alpha != null ? ctx.alpha.toFixed(3) : "—"}
        />
        <Field
          label="Predicted Δ"
          value={
            ctx.predictedDelta != null
              ? `${(ctx.predictedDelta * 100).toFixed(2)}¢`
              : "—"
          }
        />
        <Field label="Status" value={ctx.status || "—"} />
      </Section>
      <Section title="Game State">
        <Field label="Inning" value={fmtInning(gs)} />
        <Field label="Outs" value={gs.outs ?? "—"} />
        <Field label="Runners" value={fmtRunners(gs.runners)} />
        <Field
          label="Score"
          value={
            gs.away_score != null
              ? `${gs.away_score} – ${gs.home_score}`
              : "—"
          }
        />
      </Section>
      <Section title="Supabase Plan">
        <Field
          label="Entry"
          value={ctx.entryPrice != null ? `${(ctx.entryPrice * 100).toFixed(0)}¢` : "—"}
        />
        <Field
          label="Target"
          value={ctx.sellTarget != null ? `${(ctx.sellTarget * 100).toFixed(0)}¢` : "—"}
        />
        <Field label="Qty" value={ctx.quantity ?? "—"} />
        <Field label="Label" value={ctx.marketLabel || "—"} />
      </Section>
      <Section title="Picker">
        <Field
          label="Entry fee est"
          value={ctx.entryFeeEst != null ? fmtMoney(ctx.entryFeeEst) : "—"}
        />
        <Field
          label="Exit fee est"
          value={ctx.exitFeeEst != null ? fmtMoney(ctx.exitFeeEst) : "—"}
        />
        <Field
          label="Net EV"
          value={ctx.netExpectedProfit != null ? fmtMoney(ctx.netExpectedProfit) : "—"}
          className={ctx.netExpectedProfit != null ? pnlClass(ctx.netExpectedProfit) : ""}
        />
        <Field
          label={ctx.feeAdjusted ? "Gross pick (rejected)" : "Fee adjusted"}
          value={
            ctx.feeAdjusted
              ? (ctx.grossPickTicker || "yes")
              : "no"
          }
        />
      </Section>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="detail-section">
      <div className="detail-section-title">{title}</div>
      <div className="detail-fields">{children}</div>
    </div>
  );
}

function Field({ label, value, className = "" }) {
  return (
    <div className="detail-field">
      <div className="detail-label">{label}</div>
      <div className={`detail-value ${className}`}>{value}</div>
    </div>
  );
}
