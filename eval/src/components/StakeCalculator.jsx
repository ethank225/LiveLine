import React, { useMemo, useState } from "react";
import { fmtMoney, fmtPct, pnlClass } from "../lib/format.js";
import { costBasis } from "../lib/columns.js";
import { scaledFees, scaledGross } from "../lib/fees.js";

export default function StakeCalculator({ plays }) {
  const [stakeInput, setStakeInput] = useState("50");
  const stake = Math.max(0, parseFloat(stakeInput) || 0);

  const stats = useMemo(() => {
    const closed = plays.filter((p) => !p.open);
    let net = 0;
    let gross = 0;
    let fees = 0;
    let wins = 0;
    for (const p of closed) {
      const cost = costBasis(p);
      if (!cost) continue;
      const scale = stake / cost;
      const pGross = scaledGross(p, scale);
      const pFees = scaledFees(p, scale);
      const pNet = pGross - pFees;
      net += pNet;
      gross += pGross;
      fees += pFees;
      if (pNet > 0) wins++;
    }
    const count = closed.length;
    return {
      count,
      net,
      gross,
      fees,
      wins,
      winRate: count ? (wins / count) * 100 : 0,
      avg: count ? net / count : 0,
      roiPct: stake && count ? (net / (stake * count)) * 100 : 0,
    };
  }, [plays, stake]);

  return (
    <div className="calc">
      <div className="calc-header">
        <div className="calc-title">Standardized Stake Calculator</div>
        <div className="calc-sub">
          If every one of the <strong>{stats.count}</strong> filtered trades had
          been sized to the same stake, what would the book look like?
        </div>
      </div>
      <div className="calc-body">
        <div className="calc-input-wrap">
          <label className="calc-label">Stake per trade</label>
          <div className="calc-input-row">
            <span className="calc-prefix">$</span>
            <input
              type="number"
              min="0"
              step="5"
              className="calc-input"
              value={stakeInput}
              onChange={(e) => setStakeInput(e.target.value)}
            />
          </div>
          <div className="calc-presets">
            {[25, 50, 100, 250, 500].map((v) => (
              <button
                key={v}
                className={`calc-preset ${stake === v ? "active" : ""}`}
                onClick={() => setStakeInput(String(v))}
              >
                ${v}
              </button>
            ))}
          </div>
        </div>
        <div className="calc-stats">
          <Stat label="Total P&L" value={fmtMoney(stats.net)} cls={pnlClass(stats.net)} big />
          <Stat label="ROI / trade" value={fmtPct(stats.roiPct)} cls={pnlClass(stats.roiPct)} />
          <Stat label="Avg / trade" value={fmtMoney(stats.avg)} cls={pnlClass(stats.avg)} />
          <Stat label="Gross" value={fmtMoney(stats.gross)} muted />
          <Stat label="Fees" value={fmtMoney(stats.fees)} muted />
          <Stat label="Win rate" value={`${stats.winRate.toFixed(0)}%`} sub={`${stats.wins}/${stats.count}`} />
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, cls = "", sub, big, muted }) {
  return (
    <div className={`calc-stat ${big ? "big" : ""} ${muted ? "muted" : ""}`}>
      <div className="calc-stat-label">{label}</div>
      <div className={`calc-stat-value ${cls}`}>{value}</div>
      {sub && <div className="calc-stat-sub">{sub}</div>}
    </div>
  );
}
