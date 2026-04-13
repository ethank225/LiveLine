import { memo } from 'react'
import { betLabel } from '../utils/betLabel'
import { formatClock, useTradeCountdown } from '../utils/time'
import { TradeTimer } from './TradeTimer'

// Mirrors PositionCard.STATUS_CONFIG — kept in sync by hand (see that
// file for reasoning). Grouped subs don't render a progress bar, so the
// `bar` key is unused here but preserved so the two configs stay
// structurally identical.
const STATUS_CONFIG = {
  undo_window:      { border: 'border-blue-500',    label: 'Waiting for fill',     text: 'text-blue-400',    value: 'text-blue-400' },
  open:             { border: 'border-blue-500',    label: 'Waiting for fill',     text: 'text-blue-400',    value: 'text-blue-400' },
  pending_exit:     { border: 'border-amber-500',   label: 'Exiting at market...', text: 'text-amber-400',   value: 'text-amber-400' },
  filled:           { border: 'border-emerald-500', label: 'Filled',               text: 'text-emerald-400', value: 'text-emerald-400' },
  expired:          { border: 'border-red-500',     label: 'Expired',              text: 'text-red-400',     value: 'text-red-400' },
  stopped:          { border: 'border-red-500',     label: 'Stopped out',          text: 'text-red-400',     value: 'text-red-400' },
  canceled:         { border: 'border-slate-600',   label: 'Canceled',             text: 'text-slate-400',   value: 'text-slate-400' },
  canceled_by_user: { border: 'border-slate-600',   label: 'Canceled',             text: 'text-slate-400',   value: 'text-slate-400' },
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

function subFields(p) {
  // Tuple used by the sub-row memo — only these affect the DOM.
  return [p.status, p.realized_pnl, p.current_price, p.exit_price, p.completed_at]
}

// ---------------------------------------------------------------------------
// Multi-position card
//
// Layout (mockup):
//     HR  [3 markets]                      8s ago
//   ┃ Over 8.5                             Filled
//   ┃ 45¢ → 57¢                         +$4.80
//   ┃ ATL -2.5                           22s left
//   ┃ 40¢ → 52¢                          Now 44¢
//   ┃ CLE wins                            Expired
//   ┃ 38¢ → 42¢                         -$1.20
//     Combined                          +$3.60
//
// Outer card has no left border (subs own their own). The "Combined"
// footer is plain (no inner background), just a baseline-aligned row.
// ---------------------------------------------------------------------------

function GroupedPositionCard({ positions }) {
  if (!positions?.length) return null
  // A length-1 basket shouldn't reach this component anymore (PositionCard
  // handles singles directly), but render defensively just in case.
  const event = positions[0].event
  const terminalCount = positions.filter(p => TERMINAL.has(p.status)).length
  const allTerminal = terminalCount === positions.length

  const realizedSum = positions.reduce((s, p) => s + (p.realized_pnl ?? 0), 0)
  // Potential: sum of (target - entry) * qty for still-open legs + the
  // realized amount for anything that already settled. Gives a single
  // "where this basket is headed" number while it's in flight.
  const potentialSum = positions.reduce((s, p) => {
    if (TERMINAL.has(p.status)) return s + (p.realized_pnl ?? 0)
    const qty = p.quantity ?? 0
    const entry = p.entry_price ?? 0
    const target = p.sell_target ?? 0
    return s + (target - entry) * qty
  }, 0)

  // Group age uses the earliest created_at — all members fire off one tap
  // so timestamps are within ms of each other.
  const groupCreatedAt = positions
    .map(p => p.created_at)
    .filter(Boolean)
    .sort()[0]

  return (
    <div className="rounded-lg bg-slate-800 p-3 mb-2">
      {/* Header: event + count pill · timestamp */}
      <div className="flex items-baseline justify-between gap-2 mb-2">
        <div className="text-sm truncate min-w-0 flex-1">
          <span className="font-bold text-white">{event}</span>
          <span className="ml-2 text-[11px] px-2 py-0.5 rounded bg-slate-700/60 text-slate-300 whitespace-nowrap">
            {positions.length} markets
          </span>
        </div>
        <span className="text-[11px] text-slate-400 font-mono tabular-nums flex-shrink-0">
          {allTerminal
            ? formatClock(groupCreatedAt)
            : <TradeTimer createdAt={groupCreatedAt} />}
        </span>
      </div>

      {/* Sub-positions */}
      <div className="space-y-1">
        {positions.map((p) => (
          <SubRow key={p.id || p.position_id} position={p} />
        ))}
      </div>

      {/* Combined footer — plain row, no inner background */}
      <div className="flex items-baseline justify-between mt-2 px-0.5">
        <span className="text-sm text-slate-400">
          {allTerminal ? 'Combined' : 'Combined potential'}
        </span>
        {allTerminal ? (
          <span className={`font-mono font-bold tabular-nums text-base ${realizedSum >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {formatPnl(realizedSum)}
          </span>
        ) : (
          <span className={`font-mono font-bold tabular-nums text-base ${potentialSum >= 0 ? 'text-blue-400' : 'text-red-400'}`}>
            ~{formatPnl(potentialSum)}
          </span>
        )}
      </div>
    </div>
  )
}

// Skip parent-driven re-renders when nothing material changed.
export default memo(GroupedPositionCard, (prev, next) => {
  const a = prev.positions || []
  const b = next.positions || []
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const af = subFields(a[i])
    const bf = subFields(b[i])
    for (let j = 0; j < af.length; j++) {
      if (af[j] !== bf[j]) return false
    }
  }
  return true
})


// ---------------------------------------------------------------------------
// Sub-row — one leg of a basket. Memoized so a tick / SSE flush touching
// one leg doesn't re-render its siblings.
//
//   ┃ Over 8.5                             Filled
//   ┃ 45¢ → 57¢                         +$4.80
// ---------------------------------------------------------------------------

function SubRowInner({ position }) {
  const status = position.status || 'open'
  const isOpen = status === 'open' || status === 'undo_window'
  const isTerminal = TERMINAL.has(status)

  const { isPending, remaining } = useTradeCountdown(
    isOpen ? position.created_at : null,
    position.clean_window_seconds ?? 45,
  )
  const effective = (isPending && isOpen) ? 'pending_exit' : status
  const cfg = STATUS_CONFIG[effective] || STATUS_CONFIG.open

  const title = position.display_label || betLabel(position) || position.market_ticker
  const entryC = formatCents(position.entry_price)
  const targetC = formatCents(position.sell_target)

  // Right-top: status label (+ clock for terminal legs).
  let statusLine
  if (isTerminal) {
    statusLine = cfg.label
  } else if (isPending) {
    statusLine = cfg.label
  } else {
    statusLine = `${remaining}s left`
  }

  // Right-bottom: value. Same rules as PositionCard — PnL for terminal,
  // live "Now NN¢" for active.
  let valueEl = null
  const pnl = position.realized_pnl
  if (isTerminal && pnl != null) {
    const color = pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
    valueEl = (
      <span className={`font-mono font-bold tabular-nums text-lg ${color}`}>
        {formatPnl(pnl)}
      </span>
    )
  } else if (!isTerminal) {
    const current = position.current_price
    valueEl = (
      <span className={`font-mono font-bold tabular-nums text-lg ${cfg.value}`}>
        Now {formatCents(current)}
      </span>
    )
  }

  return (
    <div className={`bg-slate-800/60 border-l-[3px] ${cfg.border} rounded pl-3 pr-3 py-2 transition-[border-color] duration-700 ease-out`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-semibold text-white truncate min-w-0 flex-1">
          {title}
        </span>
        <span className={`text-xs whitespace-nowrap flex-shrink-0 ${cfg.text} ${isPending ? 'animate-pulse' : ''}`}>
          {statusLine}
        </span>
      </div>
      <div className="flex items-baseline justify-between gap-2 mt-0.5">
        <span className="text-[11px] text-slate-500 font-mono tabular-nums">
          {entryC} → {targetC}
        </span>
        {valueEl}
      </div>
    </div>
  )
}

const SubRow = memo(SubRowInner, (prev, next) => {
  const af = subFields(prev.position)
  const bf = subFields(next.position)
  for (let i = 0; i < af.length; i++) {
    if (af[i] !== bf[i]) return false
  }
  return true
})
