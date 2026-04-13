// Build a human-readable bet label from a trade or position.
//
// Uses the Kalshi ticker to extract team abbreviations (SEA, HOU, SD, COL, ...)
// instead of the full team name, so labels match the compact style of the
// scoreboard.
//
// Ticker patterns:
//   Moneyline:  KXMLBGAME-<date><away><home>-<HOME_ABBR>
//               e.g. KXMLBGAME-26APR112140HOUSEA-SEA  → home = SEA
//   Spread:     KXMLBSPREAD-<suffix>-<TEAM><N>
//               e.g. KXMLBSPREAD-26APR112040COLSD-COL4  → team = COL, N = 4
//   Over/Under: KXMLBTOTAL-<suffix>-<N>  (no team abbr needed)

export function inferMarketType(ticker) {
  if (!ticker) return null
  if (ticker.startsWith('KXMLBGAME')) return 'moneyline'
  if (ticker.startsWith('KXMLBTOTAL')) return 'over_under'
  if (ticker.startsWith('KXMLBSPREAD')) return 'spread'
  return null
}

function moneylineAbbr(ticker) {
  // Last segment after the final '-' is the home team abbr
  const parts = ticker?.split('-')
  if (!parts) return null
  const last = parts[parts.length - 1]
  return /^[A-Z]{2,4}$/.test(last) ? last : null
}

function spreadInfo(ticker) {
  // Last segment is like "COL4" or "SD5" — letters then integer N.
  // Kalshi convention: line = N - 0.5 (e.g. SEA4 → SEA -3.5)
  const parts = ticker?.split('-')
  if (!parts) return null
  const last = parts[parts.length - 1] || ''
  const match = last.match(/^([A-Z]{2,4})(\d+)$/)
  if (!match) return null
  return { abbr: match[1], line: parseInt(match[2], 10) - 0.5 }
}

function otherTeam(abbr, home, away) {
  if (!abbr) return null
  if (home && abbr === home) return away || null
  if (away && abbr === away) return home || null
  return null
}

export function betLabel(obj) {
  if (!obj) return null
  const ticker = obj.market_ticker || ''
  const side = obj.side
  const mt = obj.market_type || inferMarketType(ticker)
  const home = obj.home_abbr || ''
  const away = obj.away_abbr || ''

  if (!side) return null

  if (mt === 'over_under') {
    const linePart = ticker?.split('-').pop()
    const line = linePart?.match(/^\d+(?:\.\d+)?$/)?.[0]
    if (line) return side === 'YES' ? `Over ${line}` : `Under ${line}`
    return side === 'YES' ? 'Over' : 'Under'
  }

  if (mt === 'spread') {
    const info = spreadInfo(ticker)
    if (info) {
      if (side === 'YES') return `${info.abbr} -${info.line}`
      // NO on "SEA -4.5" = bet against SEA covering = "HOU +4.5"
      const flipped = otherTeam(info.abbr, home, away) || info.abbr
      return `${flipped} +${info.line}`
    }
    return `${side} Spread`
  }

  if (mt === 'moneyline') {
    const abbr = moneylineAbbr(ticker)
    if (abbr) {
      if (side === 'YES') return `${abbr} wins`
      // NO on "SEA wins" = bet SEA loses = "HOU wins"
      const flipped = otherTeam(abbr, home, away)
      return flipped ? `${flipped} wins` : `${abbr} loses`
    }
    return side === 'YES' ? 'Home wins' : 'Home loses'
  }

  return side
}
