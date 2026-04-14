import React, { useState, useMemo } from "react";
import csvText from "../data/Kalshi-Recent-Activity-Trade.csv?raw";
import supabaseText from "../data/trades_rows.csv?raw";
import {
  parseCSV,
  buildPlays,
  summarize,
  groupByGame,
  matchesQuery,
} from "./lib/parse.js";
import {
  parseSupabase,
  mergeSupabase,
  eventTypeStats,
} from "./lib/merge.js";
import { sortPlays } from "./lib/columns.js";
import Header from "./components/Header.jsx";
import SummaryBar from "./components/SummaryBar.jsx";
import GroupToggle from "./components/GroupToggle.jsx";
import SearchBar from "./components/SearchBar.jsx";
import EventSummary from "./components/EventSummary.jsx";
import TradesTable from "./components/TradesTable.jsx";
import StakeCalculator from "./components/StakeCalculator.jsx";
import EmptyState from "./components/EmptyState.jsx";

const CSV_PATH = "data/Kalshi-Recent-Activity-Trade.csv + data/trades_rows.csv";
const INITIAL_PLAYS = mergeSupabase(
  buildPlays(parseCSV(csvText)),
  parseSupabase(parseCSV(supabaseText)),
);
const EVENT_STATS = eventTypeStats(INITIAL_PLAYS);

export default function PnLTracker() {
  const [plays] = useState(INITIAL_PLAYS);
  const [groupBy, setGroupBy] = useState("none");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ key: "startTime", dir: "desc" });
  const [eventFilter, setEventFilter] = useState(new Set());

  const matchedCount = plays.filter((p) => p.context).length;
  const status = `${plays.length} plays · ${matchedCount} matched to Supabase`;

  const onSort = (key) =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "startTime" ? "desc" : "asc" }
    );

  const toggleEvent = (et) =>
    setEventFilter((prev) => {
      const next = new Set(prev);
      if (next.has(et)) next.delete(et);
      else next.add(et);
      return next;
    });

  const filtered = useMemo(
    () =>
      plays.filter((p) => {
        if (!matchesQuery(p, query)) return false;
        if (eventFilter.size) {
          const et = p.context?.eventType || "";
          if (!eventFilter.has(et)) return false;
        }
        return true;
      }),
    [plays, query, eventFilter],
  );
  const sorted = useMemo(() => sortPlays(filtered, sort), [filtered, sort]);
  const summary = useMemo(() => summarize(sorted), [sorted]);
  const gameGroups = useMemo(() => {
    const groups = groupByGame(sorted);
    return groups.map((g) => ({ ...g, plays: sortPlays(g.plays, sort) }));
  }, [sorted, sort]);

  return (
    <div className="app">
      <div className="container">
        <Header source={CSV_PATH} status={status} />
        {plays.length > 0 ? (
          <>
            <SummaryBar summary={summary} />
            <div className="toolbar">
              <GroupToggle value={groupBy} onChange={setGroupBy} />
              <SearchBar
                value={query}
                onChange={setQuery}
                count={filtered.length}
                total={plays.length}
              />
            </div>
            {EVENT_STATS.length > 0 && (
              <EventSummary
                stats={EVENT_STATS}
                selected={eventFilter}
                onToggle={toggleEvent}
                onClear={() => setEventFilter(new Set())}
              />
            )}
            <TradesTable
              plays={sorted}
              groupBy={groupBy}
              gameGroups={gameGroups}
              sort={sort}
              onSort={onSort}
            />
            <StakeCalculator plays={sorted} />
          </>
        ) : (
          <EmptyState message="No trades found in CSV." />
        )}
      </div>
    </div>
  );
}
