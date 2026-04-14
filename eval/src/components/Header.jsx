import React from "react";

export default function Header({ source, status }) {
  return (
    <>
      <div className="header-bar">
        <div>
          <h1 className="title">LiveLine P&amp;L Tracker</h1>
          <div className="subtitle">
            Kalshi activity CSV → trade-by-trade reconciliation
          </div>
        </div>
      </div>
      {source && <div className="filename">Source: {source} — {status}</div>}
    </>
  );
}
