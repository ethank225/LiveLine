import { memo } from 'react'
import { betLabel } from '../utils/betLabel'
import { formatClock, useTradeCountdown } from '../utils/time'
import { TradeTimer } from './TradeTimer'

// Mirrors the status palette from PositionCard so the visual language stays
// consistent between single and grouped cards.
const STATUS_CONFIG = {
  undo_window:      { border: 'border-blue-500',    bg: 'bg-blue-500/10',    label: 'Waiting for fill',     text: 'text-blue-400',    bar: 'bg-blue-500/70' },
  open:             { border: 'border-blue-500',    bg: 'bg-blue-500/10',    label: 'Waiting for fill',     text: 'text-blue-400',    bar: 'bg-blue-500/70' },
  pending_exit:     { border: 'border-amber-500',   bg: 'bg-amber-500/10',   label: 'Exiting at market...', text: 'text-amber-400',   bar: 'bg-amber-500/70' },
  filled:           { border: 'border-emerald-500', bg: 'bg-emerald-500/10', label: 'Filled at target',     text: 'text-emerald-400', bar: 'bg-emerald-500/70' },
  expired:          { border: 'border-red-500',     bg: 'bg-red-500/10',     label: 'Expired at market',    text: 'text-red-400',     bar: 'bg-red-500/70' },
  stopped:          { border: 'border-red-500',     bg: 'bg-red-500/10',     label: 'Stopped out',          text: 'text-red-400',     bar: 'bg-red-500/70' },
  canceled:         { border: 'border-slate-600',   bg: 'bg-slate-800/60',   label: 'Canceled',             text: 'text-slate-400',   bar: 'bg-slate-600' },
  canceled_by_user: { border: 'border-slate-600',   bg: 'bg-slate-800/60',   label: 'Canceled',             text: 'text-slate-400',   bar: 'bg-slate-600' },
}

const TERMINAL = new Set(['filled', 'expired', 'stopped', 'canceled', 'canceled_by_user'])

function formatCents(p) {
  if (p === null || p === undefined) return '—'
  return `${Math.round(p * 100)}¢`
}

function subFields(p) {
  // Tuple used by the memo comparator — only these affect the sub's DOM.
  return [p.status, p.realized_pnl, p.current_price, p.exit_price]
}

// A grouped card consumes an array of positions that all share an
// undo_group_id and renders them as sub-rows inside one outer card.
function GroupedPositionCard({ positions }) {
  if (!positions?.length) return null
  const event = positions[0].event
  const terminalCount = positions.filter(p => TERMINAL.has(p.status)).length
  const allTerminal = terminalCount === positions.length
  const realizedSum = positions.reduce((s, p) => s + (p.realized_pnl ?? 0), 0)

  // Potential: sum of (target - entry) * qty for open positions +
  // realized for terminal ones. Gives a meaningful "combined" line.
  const potentialSum = positions.reduce((s, p) => {
    if (TERMINAL.has(p.status)) return s + (p.realized_pnl ?? 0)
    const qty = p.quantity ?? 0
    const entry = p.entry_price ?? 0
    const target = p.sell_target ?? 0
    return s + (target - entry) * qty
  }, 0)

  // Group age uses the earliest sub-position's created_at — all members of a
  // group fire from a single tap, so timestamps match within ms.
  const groupCreatedAt = positions
    .map(p => p.created_at)
    .filter(Boolean)
    .sort()[0]

  return (
    <div className="rounded-lg bg-slate-800 p-3 mb-2">
      {/* Header: event + count · self-ticking timestamp */}
      <div className="flex items-baseline justify-between mb-2 gap-2">
        <div className="text-sm font-semibold text-white truncate">
          {event}
          {positions.length > 1 && (
            <span className="text-slate-400 font-normal text-xs ml-2 px-1.5 py-0.5 rounded bg-slate-700/60">
              {positions.length} positions
            </span>
          )}
        </div>
        <span className="text-[10px] text-slate-400 font-mono tabular-nums flex-shrink-0">
          {allTerminal
            ? formatClock(groupCreatedAt)
            : <TradeTimer createdAt={groupCreatedAt} />}
        </span>
      </div>

      {/* Sub-positions */}
      <div className="space-y-1">
        {positions.map((p) => (
          <SubPosition key={p.id || p.position_id} position={p} />
        ))}
      </div>

      {/* Footer: combined */}
      <div className="flex items-center justify-between mt-2 text-xs bg-slate-700/50 rounded px-3 py-2">
        <span className="text-slate-300">
          {allTerminal ? 'Combined' : 'Combined potential'}
        </span>
        {allTerminal ? (
          <span className={`font-mono font-bold tabular-nums text-sm ${realizedSum >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {realizedSum >= 0 ? '+' : ''}${realizedSum.toFixed(2)}
          </span>
        ) : (
          <span className={`font-mono font-bold tabular-nums text-sm ${potentialSum >= 0 ? 'text-blue-400' : 'text-red-400'}`}>
            ~{potentialSum >= 0 ? '+' : ''}${potentialSum.toFixed(2)}
          </span>
        )}
      </div>
    </div>
  )
}

// Skip re-render when parent SSE ticks produce new array identity but the
// status/pnl/price of each sub is unchanged.
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
// Sub-position — memoized so a tick in one sub doesn't re-render its peers
// ---------------------------------------------------------------------------

function SubPositionInner({ position }) {
  const status = position.status || 'open'
  const isOpen = status === 'open' || status === 'undo_window'
  const isTerminal = TERMINAL.has(status)
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.open

  const title = betLabel(position) || position.market_ticker
  const entryC = formatCents(position.entry_price)
  const targetC = formatCents(position.sell_target)
  const pnl = position.realized_pnl ?? 0

  const completedClock = position.completed_at ? formatClock(position.completed_at) : ''

  const rightSide = isTerminal ? (
    <span className="flex flex-col items-end flex-shrink-0">
      <span className={`text-[11px] ${config.text}`}>
        {config.label}
        {completedClock && (
          <span className="font-mono ml-1">{completedClock}</span>
        )}
      </span>
      <span className={`font-mono font-bold tabular-nums text-lg leading-tight ${pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
        {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
      </span>
    </span>
  ) : (
    <span className="text-[10px] text-slate-400 font-mono tabular-nums flex-shrink-0">
      <TradeTimer createdAt={position.created_at} />
    </span>
  )

  return (
    <div className={`bg-slate-800/50 border-l-[3px] ${config.border} rounded pl-3 pr-3 py-2`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm text-white font-semibold truncate min-w-0">
          {title}
          <span className="text-slate-300 font-normal text-xs"> · {entryC} → {targetC}</span>
        </span>
        {rightSide}
      </div>
      {/* Second row only for open subs — terminal subs already show their
          status + completion time on the right of the first row. */}
      {!isTerminal && (
        <SubCountdownLine
          createdAt={position.created_at}
          cleanWindowSec={position.clean_window_seconds ?? 45}
          openLabel={config.label}
          isOpen={isOpen}
        />
      )}
    </div>
  )
}

const SubPosition = memo(SubPositionInner, (prev, next) => {
  const af = subFields(prev.position)
  const bf = subFields(next.position)
  for (let i = 0; i < af.length; i++) {
    if (af[i] !== bf[i]) return false
  }
  return true
})

// Second-row status + "Ns left" for an open sub. Self-ticks on its own 1 s
// interval via useTradeCountdown — peers' ticks and parent re-renders can't
// reset it.
function SubCountdownLine({ createdAt, cleanWindowSec, openLabel, isOpen }) {
  const { remaining, isPending } = useTradeCountdown(
    isOpen ? createdAt : null,
    cleanWindowSec,
  )
  const cfg = isPending ? STATUS_CONFIG.pending_exit : STATUS_CONFIG.open
  return (
    <div className={`text-[10px] mt-0.5 ${cfg.text}`}>
      {isPending ? cfg.label : openLabel}
      {isOpen && !isPending && (
        <span className="text-slate-500"> · {remaining}s left</span>
      )}
    </div>
  )
}
