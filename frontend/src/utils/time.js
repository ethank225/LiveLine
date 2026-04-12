import { useSyncExternalStore } from 'react'

// -----------------------------------------------------------------------
// Shared "now" clock — a single setInterval across the whole app rather
// than one per card. Updates every 10s (per spec). Any component that
// needs a re-render on tick subscribes via useSharedNow().
// -----------------------------------------------------------------------

const TICK_MS = 10_000

let _now = Date.now()
const _subs = new Set()

if (typeof window !== 'undefined') {
  setInterval(() => {
    _now = Date.now()
    _subs.forEach((cb) => cb())
  }, TICK_MS)
}

function subscribe(cb) {
  _subs.add(cb)
  return () => _subs.delete(cb)
}
function getSnapshot() {
  return _now
}

export function useSharedNow() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

// -----------------------------------------------------------------------
// Formatters
// -----------------------------------------------------------------------

/**
 * Relative age for fresh timestamps, absolute clock time for anything
 * over an hour old.
 *   <  60s   → "45s ago"
 *   <  60m   → "12m ago"
 *   >= 60m   → "3:42 PM"
 */
export function formatAge(iso, now = Date.now()) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diffMs = now - t
  if (diffMs < 0) return 'just now'
  const s = Math.floor(diffMs / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return formatClock(iso)
}

/** 3:42 PM */
export function formatClock(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/**
 * Countdown / count-up label helper:
 *   <60s → "Ns ago",  <60m → "Nm ago",  ≥60m → clock time.
 * Used by TradeTimer (1 s tick) so the conversion logic stays here.
 */
export function ageLabel(startMs, nowMs) {
  const diff = nowMs - startMs
  if (diff < 0) return 'just now'
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return new Date(startMs).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

// Countdown state for an open trade's clean window. Returns integer
// `remaining` seconds, a `pct` (0–100) of window left, and `isPending`
// once the window has fully elapsed. Each caller gets its own 1 s
// interval by design — one card's countdown is never interrupted by
// another card's state changes, and parent SSE ticks don't reset it.
import { useEffect, useState } from 'react'
export function useTradeCountdown(createdAt, totalSec) {
  const startMs = createdAt ? new Date(createdAt).getTime() : null

  const [state, setState] = useState(() => {
    if (!startMs) return { remaining: totalSec, pct: 100, isPending: false }
    const elapsedSec = (Date.now() - startMs) / 1000
    const remaining = Math.max(0, Math.ceil(totalSec - elapsedSec))
    const pct = Math.max(0, 100 - (elapsedSec / totalSec) * 100)
    return { remaining, pct, isPending: remaining <= 0 }
  })

  useEffect(() => {
    if (!startMs) return
    // Captures the current startMs + totalSec. When either changes, useEffect
    // clears and restarts with fresh values — no ref gymnastics needed.
    const iv = setInterval(() => {
      const elapsedSec = (Date.now() - startMs) / 1000
      const remaining = Math.max(0, Math.ceil(totalSec - elapsedSec))
      const pct = Math.max(0, 100 - (elapsedSec / totalSec) * 100)
      setState({ remaining, pct, isPending: remaining <= 0 })
    }, 1000)
    return () => clearInterval(iv)
  }, [startMs, totalSec])

  return state
}
