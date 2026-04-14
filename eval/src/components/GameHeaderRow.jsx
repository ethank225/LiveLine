import React from "react";
import { fmtMoney, pnlClass } from "../lib/format.js";

export default function GameHeaderRow({ group, colSpan }) {
  const winPct = group.count ? Math.round((group.wins / group.count) * 100) : 0;
  return (
    <tr className="game-header">
      <td colSpan={colSpan}>
        <div className="game-header-inner">
          <div className="game-left">
            <span className="game-label">{group.game.label}</span>
            <span className="game-sub">{group.game.sub}</span>
            <span className="game-count">· {group.count} plays</span>
          </div>
          <div className="game-stats">
            <span>Fees {fmtMoney(group.fees)}</span>
            <span>Win {winPct}%</span>
            <span className={`net ${pnlClass(group.net)}`}>Net {fmtMoney(group.net)}</span>
          </div>
        </div>
      </td>
    </tr>
  );
}
