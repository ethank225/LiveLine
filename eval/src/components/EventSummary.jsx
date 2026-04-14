import React from "react";
import { fmtMoney, fmtPct, pnlClass } from "../lib/format.js";

export default function EventSummary({ stats, selected, onToggle, onClear }) {
  const allActive = selected.size === 0;
  return (
    <div className="event-summary">
      <button
        className={`event-card event-card-all ${allActive ? "active" : ""}`}
        onClick={onClear}
      >
        <div className="event-card-head">
          <span className="event-card-alltag">All events</span>
        </div>
        <div className="event-card-sub">click a type to filter</div>
      </button>
      {stats.map((s) => {
        const active = selected.has(s.eventType);
        return (
          <button
            key={s.eventType}
            className={`event-card ${active ? "active" : ""}`}
            onClick={() => onToggle(s.eventType)}
          >
            <div className="event-card-head">
              <span className={`event-badge ev-${s.eventType.toLowerCase()}`}>
                {s.eventType}
              </span>
              <span className="event-card-count">{s.count}</span>
            </div>
            <div className={`event-card-net ${pnlClass(s.netPct)}`}>
              {fmtPct(s.netPct)}
            </div>
            <div className="event-card-sub">
              {s.winRate.toFixed(0)}% win · {fmtMoney(s.netTotal)}
            </div>
          </button>
        );
      })}
    </div>
  );
}
