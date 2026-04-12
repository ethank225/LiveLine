import { memo } from 'react'
import { betLabel } from '../utils/betLabel'
import { useTradeCountdown } from '../utils/time'
import { TradeTimer } from './TradeTimer'
import GroupedPositionCard from './GroupedPositionCard'

// ---------------------------------------------------------------------------
// Shared config
// ---------------------------------------------------------------------------

const STATUS_CONFIG = {
  undo_window:      { border: 'border-blue-500',    bg: 'bg-blue-500/10',    label: 'Undo available',           text: 'text-blue-400',    bar: 'bg-blue-500/70' },
  open:             { border: 'border-blue-500',    bg: 'bg-blue-500/10',    label: 'Waiting for fill',         text: 'text-blue-400',    bar: 'bg-blue-500/70' },
  pending_exit:     { border: 'border-amber-500',   bg: 'bg-amber-500/10',   label: 'Exiting at market...',     text: 'text-amber-400',   bar: 'bg-amber-500/70' },
  filled:           { border: 'border-emerald-500', bg: 'bg-emerald-500/10', label: 'Filled at target',         text: 'text-emerald-400', bar: 'bg-emerald-500/70' },
  expired:          { border: 'border-red-500',     bg: 'bg-red-500/10',     label: 'Expired at market',        text: 'text-red-400',     bar: 'bg-red-500/70' },
  stopped:          { border: 'border-red-500',     bg: 'bg-red-500/10',     label: 'Stopped out',              text: 'text-red-400',     bar: 'bg-red-500/70' },
  canceled:         { border: 'border-slate-600',   bg: 'bg-slate-800/60',   label: 'Canceled',                 text: 'text-slate-400',   bar: 'bg-slate-600' },
  canceled_by_user: { border: 'border-slate-600',   bg: 'bg-slate-800/60',   label: 'Canceled',                 text: 'text-slate-400',   bar: 'bg-slate-600' },
}

const TERMINAL = new Set(['filled', 'expired', 'stopped', 'canceled', 'canceled_by_user'])

function formatCents(p) {
  if (p === null || p === undefined) return '—'
  return `${Math.round(p * 100)}¢`
}


// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

function PositionCard({ position }) {
  const status = position.status || 'open'
  const isTerminal = TERMINAL.has(status)

  // Terminal single positions delegate to GroupedPositionCard with a 1-item
  // array so they render pixel-identical to a grouped card's terminal
  // sub-row (no duplicate layout to keep in sync). Active single positions
  // keep the richer OpenBody with its countdown bar and Entry/Now row.
  if (isTerminal) return <GroupedPositionCard positions={[position]} />

  return (
    <PositionShell position={position}>
      <OpenBody position={position} />
    </PositionShell>
  )
}

// Re-render only when values that actually affect the rendered DOM change.
// Parent SSE ticks produce new `position` object identities on every frame
// but usually with the same status/pnl/prices — those renders are skipped.
export default memo(PositionCard, (prev, next) => {
  const a = prev.position || {}
  const b = next.position || {}
  return (
    a.status === b.status &&
    a.realized_pnl === b.realized_pnl &&
    a.current_price === b.current_price &&
    a.exit_price === b.exit_price
  )
})


// ---------------------------------------------------------------------------
// Shared shell
// ---------------------------------------------------------------------------

function PositionShell({ position, children }) {
  const rawStatus = position.status || 'open'
  const title = betLabel(position)
  // Override color/border with the soon-to-be-expired tint while waiting
  // on the backend — the countdown component reports isPending locally.
  const { isPending } = useTradeCountdown(
    position.created_at,
    position.clean_window_seconds ?? 45,
  )
  const effectiveStatus = (isPending && rawStatus === 'open')
    ? 'pending_exit'
    : rawStatus
  const shownConfig = STATUS_CONFIG[effectiveStatus] || STATUS_CONFIG.open

  return (
    <div
      className={`
        rounded-lg border-l-[3px] p-3 mb-2 bg-slate-800
        min-h-[92px] flex flex-col
        transition-[border-color] duration-700 ease-out
        ${shownConfig.border}
      `}
    >
      {/* Header row — age ticker self-updates. */}
      <div className="mb-1 flex items-baseline gap-2">
        <div className="text-sm font-semibold text-white truncate flex-1 min-w-0">
          {title}
          <span className="text-slate-500 font-normal text-xs"> · on {position.event}</span>
        </div>
        <span className="text-[10px] text-slate-500 font-mono tabular-nums flex-shrink-0">
          <TradeTimer createdAt={position.created_at} />
        </span>
      </div>

      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Open body — live tracking with countdown bar
// ---------------------------------------------------------------------------

function OpenBody({ position }) {
  const entry = position.entry_price
  const target = position.sell_target
  const current = position.current_price

  return (
    <>
      <CountdownStatus
        createdAt={position.created_at}
        cleanWindowSec={position.clean_window_seconds ?? 45}
        openLabel={STATUS_CONFIG.open.label}
        pendingLabel={STATUS_CONFIG.pending_exit.label}
      />

      <div className="flex items-center justify-between text-[10px] mt-auto pt-2">
        <span className="text-slate-500">
          Entry <span className="text-slate-300 font-mono">{formatCents(entry)}</span>
        </span>
        <span className="text-slate-400 font-medium">
          Sell aim: <span className="text-white font-mono">{formatCents(target)}</span>
        </span>
        <span className="text-slate-500">
          Now <span className={`font-mono ${current !== null && current !== undefined && current >= entry ? 'text-emerald-400' : 'text-red-400'}`}>
            {formatCents(current)}
          </span>
        </span>
      </div>

      <CountdownBar
        createdAt={position.created_at}
        cleanWindowSec={position.clean_window_seconds ?? 45}
      />
    </>
  )
}

// Status line + "Ns left". Owns its own tick so parent re-renders don't
// reset the countdown.
function CountdownStatus({ createdAt, cleanWindowSec, openLabel, pendingLabel }) {
  const { remaining, isPending } = useTradeCountdown(createdAt, cleanWindowSec)
  const cfg = isPending ? STATUS_CONFIG.pending_exit : STATUS_CONFIG.open
  return (
    <div className={`text-xs transition-colors duration-700 ease-out ${cfg.text} ${isPending ? 'animate-pulse' : ''}`}>
      {isPending ? pendingLabel : openLabel}
      {!isPending && (
        <span className="text-slate-500"> · {remaining}s left</span>
      )}
    </div>
  )
}

// Progress bar — same tick, independent component so it stays smooth even
// when status color flips to pending_exit.
function CountdownBar({ createdAt, cleanWindowSec }) {
  const { pct, isPending } = useTradeCountdown(createdAt, cleanWindowSec)
  const cfg = isPending ? STATUS_CONFIG.pending_exit : STATUS_CONFIG.open
  return (
    <div className="relative h-1 bg-slate-800 rounded-full mt-1.5 overflow-hidden">
      <div
        className={`absolute inset-y-0 left-0 transition-all duration-1000 ease-linear ${cfg.bar} ${isPending ? 'animate-pulse' : ''}`}
        style={{ width: isPending ? '100%' : `${pct}%` }}
      />
    </div>
  )
}

