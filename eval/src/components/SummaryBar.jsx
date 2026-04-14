import React from "react";
import { fmtMoney, pnlClass } from "../lib/format.js";

function Card({ label, value, valueClass }) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className={`card-value ${valueClass || ""}`}>{value}</div>
    </div>
  );
}

export default function SummaryBar({ summary }) {
  const cards = [
    { label: "Trades", value: summary.count },
    { label: "Net P&L", value: fmtMoney(summary.net), valueClass: pnlClass(summary.net) },
    { label: "Total Fees", value: fmtMoney(summary.fees) },
    { label: "Win Rate", value: `${summary.winRate.toFixed(0)}%` },
    { label: "Avg / Trade", value: fmtMoney(summary.avg), valueClass: pnlClass(summary.avg) },
  ];
  return (
    <div className="card-grid">
      {cards.map((c) => <Card key={c.label} {...c} />)}
    </div>
  );
}
