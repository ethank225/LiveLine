import { useState, useEffect, useRef } from 'react'
import EventButton from './EventButton'
import { api } from '../api'

const EVENT_LAYOUT = [
  ['HR', '2B', '1B', 'BB'],
  ['K', 'OUT', 'DP'],
]

export default function ButtonGrid({ gameId, disabled, onBuy }) {
  const [trades, setTrades] = useState({})
  const [multiMarket, setMultiMarket] = useState(false)
  const esRef = useRef(null)
  // Tracks whether SSE has already delivered a frame for this game.
  // The REST fetch must not clobber newer SSE data with whatever was in
  // the backend's trade cache at the moment the fetch resolved.
  const sseSeenRef = useRef(false)

  // Condense a trades dict into something that prints as one short line.
  // Lets you eyeball whether HR/2B/1B actually carry distinct values.
  const summarize = (t) => Object.fromEntries(
    Object.entries(t || {}).map(([ev, v]) => [
      ev,
      v ? `${v.market_type || '?'} ${v.side || '-'} ${v.entry_price}→${v.sell_target} (ev=${v.ev_per_contract})` : null,
    ]),
  )

  // Own SSE listener for trades events only
  useEffect(() => {
    let cancelled = false
    let es = null
    sseSeenRef.current = false

    api.streamGame(gameId).then((source) => {
      if (cancelled) {
        source.close()
        return
      }
      es = source
      esRef.current = es

      es.addEventListener('trades', (e) => {
        try {
          const data = JSON.parse(e.data)
          if (data.trades && typeof data.trades === 'object' && !Array.isArray(data.trades)) {
            sseSeenRef.current = true
            console.log('ButtonGrid received trades: [SSE]', summarize(data.trades))
            setTrades({ ...data.trades })
          }
        } catch { /* ignore */ }
      })
    })

    return () => {
      cancelled = true
      if (es) es.close()
    }
  }, [gameId])

  // One-shot REST fetch on mount — fills the grid before the first SSE
  // frame arrives. Skipped if SSE already delivered, because the backend's
  // /trades cache lags a real-time recompute and would overwrite fresher
  // SSE data with a stale snapshot.
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const data = await api.getTrades(gameId)
        if (cancelled) return
        if (data.settings) {
          setMultiMarket(!!data.settings.multi_market)
        }
        if (data.trades && typeof data.trades === 'object') {
          if (sseSeenRef.current) {
            console.log(
              'ButtonGrid received trades: [REST stale — skipping]',
              summarize(data.trades),
            )
            return
          }
          console.log('ButtonGrid received trades: [REST]', summarize(data.trades))
          setTrades({ ...data.trades })
        }
      } catch { /* best effort */ }
    }
    load()
    return () => { cancelled = true }
  }, [gameId])

  return (
    <div className="space-y-2">
      {EVENT_LAYOUT.map((row, ri) => (
        <div key={ri} className="grid gap-2" style={{ gridTemplateColumns: `repeat(${row.length}, 1fr)` }}>
          {row.map(event => (
            <EventButton
              key={event}
              event={event}
              trade={trades[event]}
              disabled={disabled}
              onBuy={onBuy}
              multiMarket={multiMarket}
            />
          ))}
        </div>
      ))}
    </div>
  )
}
