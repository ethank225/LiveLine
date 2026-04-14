import React, { useState } from "react";
import PlayRow from "./PlayRow.jsx";
import GameHeaderRow from "./GameHeaderRow.jsx";
import { COLUMNS } from "../lib/columns.js";

function SortArrow({ active, dir }) {
  if (!active) return <span className="sort-arrow inactive">↕</span>;
  return <span className="sort-arrow">{dir === "asc" ? "▲" : "▼"}</span>;
}

const playKey = (p) => `${p.ticker}|${p.startTime.getTime()}|${p.side}`;

export default function TradesTable({ plays, groupBy, gameGroups, sort, onSort }) {
  const [expanded, setExpanded] = useState(null);
  const toggle = (key) => setExpanded((cur) => (cur === key ? null : key));

  const flatEmpty = groupBy === "none" && plays.length === 0;
  const groupedEmpty = groupBy === "game" && gameGroups.length === 0;

  const renderRow = (p) => {
    const key = playKey(p);
    return (
      <PlayRow
        key={key}
        p={p}
        expanded={expanded === key}
        onToggle={() => toggle(key)}
      />
    );
  };

  return (
    <div className="table-wrap">
      <table className="trades-table">
        <colgroup>
          {COLUMNS.map((c) => <col key={c.key} style={{ width: c.width }} />)}
        </colgroup>
        <thead>
          <tr>
            {COLUMNS.map((c) => {
              const active = sort.key === c.key;
              return (
                <th
                  key={c.key}
                  className={`sortable ${active ? "active" : ""}`}
                  onClick={() => onSort(c.key)}
                >
                  {c.label}
                  <SortArrow active={active} dir={sort.dir} />
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {groupBy === "none" && plays.map(renderRow)}
          {groupBy === "game" && gameGroups.map((g) => (
            <React.Fragment key={g.game.key}>
              <GameHeaderRow group={g} colSpan={COLUMNS.length} />
              {g.plays.map(renderRow)}
            </React.Fragment>
          ))}
          {(flatEmpty || groupedEmpty) && (
            <tr>
              <td colSpan={COLUMNS.length} className="empty-row">
                No trades match your search.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
