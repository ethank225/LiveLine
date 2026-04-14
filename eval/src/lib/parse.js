export function parseCSV(text) {
  const rows = [];
  let i = 0, field = "", row = [], inQuotes = false;
  const t = text.replace(/^\uFEFF/, "");
  while (i < t.length) {
    const c = t[i];
    if (inQuotes) {
      if (c === '"' && t[i + 1] === '"') { field += '"'; i += 2; continue; }
      if (c === '"') { inQuotes = false; i++; continue; }
      field += c; i++;
    } else {
      if (c === '"') { inQuotes = true; i++; continue; }
      if (c === ",") { row.push(field); field = ""; i++; continue; }
      if (c === "\n" || c === "\r") {
        if (field !== "" || row.length) { row.push(field); rows.push(row); }
        field = ""; row = [];
        if (c === "\r" && t[i + 1] === "\n") i++;
        i++;
        continue;
      }
      field += c; i++;
    }
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];
  const header = rows[0].map((h) => h.trim());
  return rows.slice(1).filter((r) => r.length > 1).map((r) => {
    const o = {};
    header.forEach((h, idx) => { o[h] = r[idx] ?? ""; });
    return o;
  });
}

// Mirror of backend/app/bet_label.py — keep these in sync if the backend changes.

function inferMarketType(ticker) {
  if (!ticker) return null;
  if (ticker.startsWith("KXMLBGAME")) return "moneyline";
  if (ticker.startsWith("KXMLBTOTAL")) return "over_under";
  if (ticker.startsWith("KXMLBSPREAD")) return "spread";
  return null;
}

// `KXMLBSPREAD-...-{ABBR}{N}` → { abbr, line: N - 0.5 }
function spreadInfo(ticker) {
  if (!ticker) return null;
  const last = (ticker.split("-").pop() || "").toUpperCase();
  let i = 0;
  while (i < last.length && /[A-Z]/.test(last[i])) i++;
  const abbr = last.slice(0, i);
  const nStr = last.slice(i);
  if (!abbr || !nStr || !/^\d+$/.test(nStr)) return null;
  return { abbr, line: parseInt(nStr, 10) - 0.5 };
}

// `KXMLBTOTAL-...-{N}` → "{N}"
function ouLine(ticker) {
  if (!ticker) return null;
  const last = ticker.split("-").pop() || "";
  return /^\d+$/.test(last) ? last : null;
}

// `KXMLBGAME-...-{HOME_ABBR}` → home abbr
function mlAbbr(ticker) {
  if (!ticker) return null;
  const last = (ticker.split("-").pop() || "").toUpperCase();
  return /^[A-Z]{2,4}$/.test(last) ? last : null;
}

// Build the bet-meaning label, folding side into the market text.
// YES: chosen team -line / Over / team wins
// NO:  flipped team +line / Under / other team wins
export function computeDisplayLabel(ticker, side, away, home) {
  const type = inferMarketType(ticker);
  if (type === "over_under") {
    const n = ouLine(ticker);
    if (n) return side === "YES" ? `Over ${n}` : `Under ${n}`;
    return side === "YES" ? "Over" : "Under";
  }
  if (type === "spread") {
    const info = spreadInfo(ticker);
    if (info) {
      const lineStr = info.line.toString();
      if (side === "YES") return `${info.abbr} -${lineStr}`;
      const other = info.abbr === away ? home : info.abbr === home ? away : info.abbr;
      return `${other} +${lineStr}`;
    }
  }
  if (type === "moneyline") {
    const abbr = mlAbbr(ticker);
    if (abbr) {
      if (side === "YES") return `${abbr} wins`;
      const other = abbr === home ? away : abbr === away ? home : null;
      return other ? `${other} wins` : `${abbr} loses`;
    }
  }
  return ticker;
}

const MONTHS = { JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5, JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11 };

// e.g. "KXMLBGAME-26APR131610HOUSEA-SEA" → { label: "HOU @ SEA", sub: "Apr 13" }
export function parseGame(ticker) {
  if (!ticker) return { key: "unknown", label: "Unknown" };
  const mid = ticker.split("-")[1] || "";
  const m = mid.match(/^(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})([A-Z]{3})([A-Z]{3})$/);
  if (!m) return { key: mid || "unknown", label: mid || "Unknown" };
  const [, yy, mon, dd, hh, mm, away, home] = m;
  const date = new Date(
    2000 + parseInt(yy, 10),
    MONTHS[mon] ?? 0,
    parseInt(dd, 10),
    parseInt(hh, 10),
    parseInt(mm, 10),
  );
  return {
    key: mid,
    away,
    home,
    label: `${away} @ ${home}`,
    sub: date.toLocaleString(undefined, { month: "short", day: "numeric" }),
    sortTime: date.getTime(),
  };
}

function newPlay(f) {
  return {
    ticker: f.ticker,
    side: f.direction === "Yes" ? "YES" : "NO",
    entryDir: f.direction,
    entryFills: [f],
    exitFills: [],
    entryQty: f.qty,
    exitQty: 0,
    startTime: f.time,
    endTime: f.time,
  };
}

function finalize(p) {
  const isYes = p.side === "YES";
  const onSide = (price) => (isYes ? price : 100 - price);
  const entryQty = p.entryFills.reduce((s, f) => s + f.qty, 0);
  const exitQty = p.exitFills.reduce((s, f) => s + f.qty, 0);
  const entryAvg = entryQty
    ? p.entryFills.reduce((s, f) => s + onSide(f.price) * f.qty, 0) / entryQty
    : 0;
  const exitAvg = exitQty
    ? p.exitFills.reduce((s, f) => s + onSide(f.price) * f.qty, 0) / exitQty
    : 0;
  const qtyMatched = Math.min(entryQty, exitQty);
  const gross = ((exitAvg - entryAvg) * qtyMatched) / 100;
  const fees =
    p.entryFills.reduce((s, f) => s + f.fee, 0) +
    p.exitFills.reduce((s, f) => s + f.fee, 0);
  const exitTypes = p.exitFills.map((f) => f.orderType);
  const makers = exitTypes.filter((x) => x === "Maker").length;
  const exitType = !exitTypes.length
    ? "—"
    : makers >= exitTypes.length - makers
      ? "Limit"
      : "IOC";
  const game = parseGame(p.ticker);
  return {
    ticker: p.ticker,
    market: computeDisplayLabel(p.ticker, p.side, game.away, game.home),
    game,
    side: p.side,
    qty: qtyMatched || entryQty,
    entryAvg,
    exitAvg: exitQty ? exitAvg : null,
    gross,
    fees,
    net: gross - fees,
    exitType,
    startTime: p.entryFills[0].time,
    endTime: p.exitFills.length
      ? p.exitFills[p.exitFills.length - 1].time
      : p.endTime,
    open: exitQty < entryQty,
    entryFills: p.entryFills,
    exitFills: p.exitFills,
  };
}

export function buildPlays(rows) {
  const trades = rows
    .filter((r) => r.type === "Trade")
    .map((r) => ({
      time: new Date(r.Original_Date),
      qty: parseInt(r.Amount_In_Dollars, 10) || 0,
      fee: parseFloat(r.Fee_In_Dollars) || 0,
      direction: r.Direction,
      price: parseInt(r.Price_In_Cents, 10) || 0,
      orderType: r.Order_Type,
      ticker: r.Market_Ticker,
      marketId: r.Market_Id,
    }))
    .filter((t) => t.qty > 0 && (t.direction === "Yes" || t.direction === "No"));

  const groups = new Map();
  for (const t of trades) {
    if (!groups.has(t.marketId)) groups.set(t.marketId, []);
    groups.get(t.marketId).push(t);
  }

  const plays = [];
  for (const [, fills] of groups) {
    fills.sort((a, b) => a.time - b.time);
    let cur = null;
    for (const f of fills) {
      if (!cur) { cur = newPlay(f); continue; }
      cur.endTime = f.time;
      if (f.direction === cur.entryDir) {
        if (cur.exitQty === 0) {
          cur.entryFills.push(f);
          cur.entryQty += f.qty;
        } else {
          plays.push(cur);
          cur = newPlay(f);
        }
      } else {
        cur.exitFills.push(f);
        cur.exitQty += f.qty;
        if (cur.exitQty >= cur.entryQty) {
          plays.push(cur);
          cur = null;
        }
      }
    }
    if (cur) plays.push(cur);
  }
  return plays.map(finalize);
}

export function summarize(plays) {
  const closed = plays.filter((p) => !p.open);
  const net = closed.reduce((s, p) => s + p.net, 0);
  const fees = closed.reduce((s, p) => s + p.fees, 0);
  const wins = closed.filter((p) => p.net > 0).length;
  return {
    count: closed.length,
    net,
    fees,
    wins,
    winRate: closed.length ? (wins / closed.length) * 100 : 0,
    avg: closed.length ? net / closed.length : 0,
  };
}

export function groupByGame(plays) {
  const m = new Map();
  for (const p of plays) {
    const k = p.game.key;
    if (!m.has(k)) m.set(k, { game: p.game, plays: [] });
    m.get(k).plays.push(p);
  }
  const arr = Array.from(m.values()).map((g) => ({ ...g, ...summarize(g.plays) }));
  arr.sort((a, b) => (b.game.sortTime || 0) - (a.game.sortTime || 0));
  return arr;
}

export function matchesQuery(p, q) {
  if (!q) return true;
  const hay = `${p.market} ${p.side} ${p.game.label} ${p.game.sub} ${p.exitType} ${p.ticker}`.toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .every((t) => !t || hay.includes(t));
}
