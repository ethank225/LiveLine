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

function spreadAbbr(ticker) {
  // Last segment is like "COL4" or "SD5" — letters followed by digits
  const parts = ticker?.split('-')
  if (!parts) return null
  const last = parts[parts.length - 1] || ''
  const match = last.match(/^([A-Z]{2,4})\d+$/)
  return match ? match[1] : null
}

export function betLabel(obj) {
  if (!obj) return null
  const ticker = obj.market_ticker || ''
  const title = obj.market_title || ''
  const side = obj.side
  const mt = obj.market_type || inferMarketType(ticker)

  if (!side) return null

  if (mt === 'over_under') {
    // Last segment of ticker is the line: KXMLBTOTAL-...-7 → "7"
    const linePart = ticker?.split('-').pop()
    const line = linePart?.match(/^\d+(?:\.\d+)?$/)?.[0]
    if (line) return side === 'YES' ? `Over ${line}` : `Under ${line}`
    return side === 'YES' ? 'Over' : 'Under'
  }

  if (mt === 'spread') {
    const abbr = spreadAbbr(ticker)
    const line = title.match(/\d+(?:\.\d+)?/)?.[0]
    if (abbr && line) {
      return side === 'YES' ? `${abbr} -${line}` : `${abbr} +${line}`
    }
    return `${side} Spread`
  }

  if (mt === 'moneyline') {
    const abbr = moneylineAbbr(ticker)
    if (abbr) {
      return side === 'YES' ? `${abbr} wins` : `${abbr} loses`
    }
    return side === 'YES' ? 'Home wins' : 'Home loses'
  }

  return side
}
