import { useEffect, useState } from 'react'
import { ageLabel } from '../utils/time'

// Self-ticking age component. Each instance owns its own setInterval + state,
// so a parent re-render (e.g. SSE price tick flushing new `position`
// objects downstream) never resets the visible countup. The card that
// contains it stays memoized on meaningful-data fields; this leaf handles
// the tick-forward animation independently.
//
// Freezes when `closedAt` is provided — terminal trades should show the
// absolute completion time, which the caller formats via formatClock.
export function TradeTimer({ createdAt, closedAt }) {
  const startMs = createdAt ? new Date(createdAt).getTime() : null
  const [label, setLabel] = useState(() =>
    startMs ? ageLabel(startMs, Date.now()) : '',
  )
  useEffect(() => {
    if (!startMs || closedAt) return
    const iv = setInterval(() => {
      setLabel(ageLabel(startMs, Date.now()))
    }, 1000)
    return () => clearInterval(iv)
  }, [startMs, closedAt])
  if (!startMs) return null
  return <span>{label}</span>
}

// Countdown hook lives in ../utils/time (kept out of this JSX file so
// fast-refresh stays happy — it requires pure-component exports).
