import { useEffect, useRef, useState } from 'react'

// Stacked undo overlay. The parent owns an array of pending entries; this
// component renders the most recent one, badges how many more are stacked,
// and drives per-entry expiry off absolute `expireAt` timestamps.
//
// One internal setInterval:
//   (1) re-renders for the visible entry's countdown / progress bar
//   (2) emits onEntryExpire for any entry past its expireAt (once each,
//       guarded by a ref-tracked set)
export default function UndoOverlay({ queue, onUndo, onEntryExpire }) {
  // Re-render tick for the visible entry's progress bar.
  const [, setNow] = useState(Date.now())

  // Keep the latest queue + callback available inside the interval without
  // recreating it every render.
  const queueRef = useRef(queue)
  queueRef.current = queue
  const onEntryExpireRef = useRef(onEntryExpire)
  onEntryExpireRef.current = onEntryExpire

  // Prevent re-emitting onEntryExpire for an entry that's already been flagged
  // but hasn't yet disappeared from the incoming `queue` prop.
  const notifiedRef = useRef(new Set())

  useEffect(() => {
    const iv = setInterval(() => {
      const now = Date.now()
      for (const e of queueRef.current) {
        if (e.expireAt <= now && !notifiedRef.current.has(e.position_id)) {
          notifiedRef.current.add(e.position_id)
          onEntryExpireRef.current?.(e.position_id)
        }
      }
      setNow(now)
    }, 50)
    return () => clearInterval(iv)
  }, [])

  if (!queue.length) return null

  // Most recent entry drives the visible label + progress bar.
  const visible = queue[queue.length - 1]
  const extraCount = queue.length - 1

  const now = Date.now()
  const windowMs = (visible.undo_window_seconds ?? 3) * 1000
  const remainingMs = Math.max(0, visible.expireAt - now)
  const progress = Math.max(0, Math.min(1, remainingMs / windowMs))
  const remainingSec = Math.ceil(remainingMs / 1000)

  const isBasket = (visible.positions || []).length > 1
  const handleUndo = () => {
    if (navigator.vibrate) navigator.vibrate(10)
    onUndo(queue)
  }

  return (
    <div className="bg-slate-800 rounded-xl p-4 border border-slate-700">
      {/* Info */}
      <div className="text-center mb-3">
        <div className="text-sm font-semibold text-white mb-1">
          {extraCount > 0 ? (
            <>
              Undo {visible.event}
              <span className="text-slate-400 font-normal ml-1">(+ {extraCount} more)</span>
            </>
          ) : isBasket ? (
            `Undo ${visible.event} (${visible.positions.length} positions)`
          ) : (
            `${visible.event} — ${visible.market_ticker ?? ''}`
          )}
        </div>
        <div className="text-xs text-slate-400">
          {visible.quantity ? `Qty ${visible.quantity} · ` : ''}
          Cost ${(visible.cost ?? visible.estimated_cost ?? 0).toFixed(2)}
        </div>
      </div>

      {/* Progress bar */}
      <div className="h-1.5 bg-slate-700 rounded-full mb-3 overflow-hidden">
        <div
          className="h-full bg-red-500 rounded-full transition-none"
          style={{ width: `${progress * 100}%` }}
        />
      </div>

      {/* Undo button + countdown */}
      <div className="flex items-center gap-3">
        <button
          onClick={handleUndo}
          className="flex-1 bg-red-600 active:bg-red-700 text-white font-bold text-sm py-3 rounded-lg active:scale-95 transition-transform"
        >
          UNDO{extraCount > 0 ? ` ALL (${queue.length})` : ''}
        </button>
        <span className="text-2xl font-bold text-white font-mono w-8 text-center tabular-nums">
          {remainingSec}
        </span>
      </div>
    </div>
  )
}
