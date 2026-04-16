import { memo, useState } from 'react'
import { betLabel } from '../utils/betLabel'
import { formatClock, useTradeCountdown } from '../utils/time'

// ---------------------------------------------------------------------------
// Status palette — shared with GroupedPositionCard. Kept in sync by hand
// because the two files render slightly different chrome; extracting it
// into a shared module for two consumers isn't pulling its weight yet.
// ---------------------------------------------------------------------------

const STATUS_CONFIG = {
  undo_window:      { border: 'border-blue-500',    label: 'Undo available',       text: 'text-blue-400',    bar: 'bg-blue-500',    value: 'text-blue-400' },
  open:             { border: 'border-blue-500',    label: 'Waiting for fill',     text: 'text-blue-400',    bar: 'bg-blue-500',    value: 'text-blue-400' },
  pending_exit:     { border: 'border-amber-500',   label: 'Exiting at market...', text: 'text-amber-400',   bar: 'bg-amber-500',   value: 'text-amber-400' },
  filled:           { border: 'border-emerald-500', label: 'Filled',               text: 'text-emerald-400', bar: 'bg-emerald-500', value: 'text-emerald-400' },
  expired:          { border: 'border-red-500',     label: 'Expired',              text: 'text-red-400',     bar: 'bg-red-500',     value: 'text-red-400' },
  stopped:          { border: 'border-red-500',     label: 'Stopped out',          text: 'text-red-400',     bar: 'bg-red-500',     value: 'text-red-400' },
  canceled:         { border: 'border-slate-600',   label: 'Canceled',             text: 'text-slate-400',   bar: 'bg-slate-600',   value: 'text-slate-400' },
  canceled_by_user: { border: 'border-slate-600',   label: 'Canceled',             text: 'text-slate-400',   bar: 'bg-slate-600',   value: 'text-slate-400' },
}

const TERMINAL = new Set(['filled', 'expired', 'stopped', 'canceled', 'canceled_by_user'])

function formatCents(p) {
  if (p === null || p === undefined) return '—'
  return `${Math.round(p * 100)}¢`
}

function formatPnl(x) {
  if (x >= 0) return `+$${x.toFixed(2)}`
  return `-$${Math.abs(x).toFixed(2)}`
}

// ---------------------------------------------------------------------------
// Single-position card
//
// Layout (mockup):
//   ┃ 1B  ATL -3.5                    20s left
//   ┃ 50¢ → 55¢                       Now 52¢
//   ┃ [======>                        ]   ← active only
//
// Left border on the whole card (colored by status). Event label bold,
// market label gray on the same line. Status + time top right, prices
// and value bottom. Thin 2px progress bar under the prices while active.
// ---------------------------------------------------------------------------

function PositionCard({ position, onInstantSell }) {
  const status = position.status || 'open'
  const isOpen = status === 'open' || status === 'undo_window'
  const isTerminal = TERMINAL.has(status)
  const [selling, setSelling] = useState(false)

  const handleInstantSell = async () => {
    if (selling || !onInstantSell) return
    setSelling(true)
    try {
      await onInstantSell(position.position_id || position.id)
    } finally {
      // Leave selling=true if the request succeeded — the SSE
      // positions_update will swap this card for the terminal row.
      // On error, clear so the user can retry.
      setSelling(false)
    }
  }

  // Clean-window awareness — flips the whole card (border + label + bar)
  // to amber when the force-exit timer is about to fire.
  const { isPending, remaining, pct } = useTradeCountdown(
    isOpen ? position.created_at : null,
    position.clean_window_seconds ?? 45,
  )
  const effective = (isPending && isOpen) ? 'pending_exit' : status
  const cfg = STATUS_CONFIG[effective] || STATUS_CONFIG.open

  const eventLabel = position.event || ''
  const marketLabel = position.display_label || betLabel(position) || position.market_ticker || ''
  const entryC = formatCents(position.entry_price)
  const targetC = formatCents(position.sell_target)

  // Top-right: status + time.
  //   Active (not pending)  → "20s left"
  //   Pending force exit    → "Exiting at market..."
  //   Terminal              → "Filled · 5:32 PM"
  let statusLine
  if (isTerminal) {
    const clk = position.completed_at ? formatClock(position.completed_at) : ''
    statusLine = (
      <>
        {cfg.label}
        {clk && <span className="font-mono"> · {clk}</span>}
      </>
    )
  } else if (isPending) {
    statusLine = cfg.label
  } else {
    statusLine = `${remaining}s left`
  }

  // Right-bottom value — "+$0.10" when terminal, "Now 52¢" when active.
  let valueEl = null
  const pnl = position.realized_pnl
  if (isTerminal && pnl != null) {
    const color = pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
    valueEl = (
      <span className={`font-mono font-bold tabular-nums text-xl ${color}`}>
        {formatPnl(pnl)}
      </span>
    )
  } else if (!isTerminal) {
    const current = position.current_price
    valueEl = (
      <span className={`font-mono font-bold tabular-nums text-xl ${cfg.value}`}>
        Now {formatCents(current)}
      </span>
    )
  }

  return (
    <div className={`rounded-lg bg-slate-800 border-l-[3px] ${cfg.border} p-3 mb-2 transition-[border-color] duration-700 ease-out`}>
      {/* Top row: event · market  —  status · time */}
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-sm truncate min-w-0 flex-1">
          <span className="font-bold text-white">{eventLabel}</span>
          {marketLabel && (
            <span className="text-slate-400 font-normal ml-2">{marketLabel}</span>
          )}
        </div>
        <div
          className={`text-xs whitespace-nowrap flex-shrink-0 ${cfg.text} ${isPending ? 'animate-pulse' : ''}`}
        >
          {statusLine}
        </div>
      </div>

      {/* Middle row: prices — value */}
      <div className="flex items-baseline justify-between gap-2 mt-1.5">
        <span className="text-xs text-slate-500 font-mono tabular-nums">
          {entryC} → {targetC}
        </span>
        {valueEl}
      </div>

      {/* Thin progress bar — active only */}
      {!isTerminal && (
        <div className="h-0.5 bg-slate-700/50 rounded-full mt-2 overflow-hidden">
          <div
            className={`h-full ${cfg.bar} ${isPending ? 'animate-pulse' : ''} transition-all duration-1000 ease-linear`}
            style={{ width: isPending ? '100%' : `${pct}%` }}
          />
        </div>
      )}

      {/* Instant-sell: resting-open only. Distinct from the 3s undo
          overlay (which handles undo_window); this button flattens a
          position that's already live on the book. No confirm dialog
          per spec — one tap is intentional. */}
      {status === 'open' && onInstantSell && (
        <button
          onClick={handleInstantSell}
          disabled={selling}
          className={`mt-2 w-full py-1.5 rounded-md text-xs font-bold tracking-wider
                      border transition-colors
                      ${selling
                        ? 'border-rose-500 bg-rose-500/30 text-rose-100 animate-pulse cursor-wait'
                        : 'border-rose-500/50 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20 hover:border-rose-400 active:bg-rose-500/30'
                      }`}
        >
          {selling ? 'SELLING…' : 'INSTANT SELL'}
        </button>
      )}
    </div>
  )
}

// Re-render only when the fields that actually reach the DOM change.
// The internal useTradeCountdown tick still drives per-second updates;
// the memo guards against parent SSE ticks re-creating identical DOM.
export default memo(PositionCard, (prev, next) => {
  const a = prev.position || {}
  const b = next.position || {}
  return (
    a.status === b.status &&
    a.realized_pnl === b.realized_pnl &&
    a.current_price === b.current_price &&
    a.exit_price === b.exit_price &&
    a.completed_at === b.completed_at
  )
})
