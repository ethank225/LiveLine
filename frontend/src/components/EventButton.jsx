import { useState, useEffect, useRef, memo } from 'react'
import { betLabel as buildBetLabel } from '../utils/betLabel'

function priceInfo(trade) {
  const label = trade.display_label || buildBetLabel(trade)
  if (!label) return null
  const entry = trade.entry_price ?? 0
  const target = trade.sell_target ?? 0
  return {
    label,
    entryCents: Math.round(entry * 100),
    targetCents: Math.round(target * 100),
  }
}

const accentStyles = {
  emerald: 'border-emerald-500/40 bg-emerald-500/10 hover:border-emerald-400 hover:bg-emerald-500/20',
  teal:    'border-teal-500/40 bg-teal-500/10 hover:border-teal-400 hover:bg-teal-500/20',
  blue:    'border-blue-500/30 bg-blue-500/8 hover:border-blue-400 hover:bg-blue-500/20',
}

const profitColor = {
  emerald: 'text-emerald-400',
  teal:    'text-teal-400',
  blue:    'text-blue-400',
}

function EventButton({ event, trade, disabled, onBuy, multiMarket = false }) {
  const [loading, setLoading] = useState(false)
  const [flash, setFlash] = useState(false)

  // Positive-EV basket — used only in multi-market mode to aggregate.
  const basket = (trade?.all_trades || []).filter(t => (t?.ev_per_contract ?? 0) > 0)
  const isBasket = multiMarket && basket.length > 1

  // In multi mode with a basket, show the combined profit across every
  // market that will fire. Otherwise fall back to the single best trade.
  const estimatedProfit = isBasket
    ? basket.reduce((s, t) => s + (t.estimated_profit ?? 0), 0)
    : trade?.estimated_profit
  const profit = estimatedProfit ?? 0
  const prevProfit = useRef(estimatedProfit)

  useEffect(() => {
    const prev = prevProfit.current ?? 0
    prevProfit.current = estimatedProfit
    if (Math.abs(profit - prev) > 1) {
      setFlash(true)
      const t = setTimeout(() => setFlash(false), 600)
      return () => clearTimeout(t)
    }
  }, [estimatedProfit, profit])

  const active = trade?.active
  const info = active ? priceInfo(trade) : null
  const inactive = profit <= 0 || !active

  // Percentage always reflects the single best market's entry → target,
  // per spec. The aggregated $ figure is in `profit` above.
  const profitPct = info && info.entryCents > 0
    ? Math.round(((info.targetCents - info.entryCents) / info.entryCents) * 100)
    : null

  let accent = 'blue'
  if (!inactive) {
    if (profit > 15) accent = 'emerald'
    else if (profit >= 5) accent = 'teal'
    else accent = 'blue'
  }

  const handleTap = async () => {
    if (disabled || loading || inactive) return
    if (navigator.vibrate) navigator.vibrate(10)
    setLoading(true)
    try {
      await onBuy(event)
    } finally {
      setLoading(false)
    }
  }

  const baseClasses = 'rounded-xl border w-full min-h-[88px] flex items-center justify-center transition-all duration-150 ease-out'
  const stackClasses = 'flex flex-col items-center justify-center gap-[4px] px-2 py-3'

  if (inactive) {
    return (
      <button
        disabled
        className={`${baseClasses} border-transparent bg-slate-800/30 opacity-30 pointer-events-none`}
      >
        <div className={stackClasses}>
          <span className="text-lg font-black text-white leading-none">{event}</span>
          <span className="text-[10px] text-slate-500 leading-none">no edge</span>
        </div>
      </button>
    )
  }

  return (
    <button
      onClick={handleTap}
      disabled={disabled || loading}
      className={`
        ${baseClasses} cursor-pointer
        ${accentStyles[accent]}
        hover:-translate-y-0.5 hover:shadow-lg
        active:scale-95 active:translate-y-0
        disabled:opacity-40 disabled:pointer-events-none disabled:hover:translate-y-0 disabled:hover:shadow-none
        ${flash ? 'brightness-125 scale-[1.03]' : ''}
      `}
    >
      {loading ? (
        <svg className="animate-spin h-5 w-5 text-white" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" className="opacity-25" />
          <path d="M4 12a8 8 0 018-8" stroke="currentColor" strokeWidth="3" strokeLinecap="round" className="opacity-75" />
        </svg>
      ) : (
        <div className={stackClasses}>
          <span className="text-lg font-black text-white leading-none">{event}</span>
          <span className={`text-sm font-bold font-mono leading-none ${profitColor[accent]}`}>
            {profitPct != null ? `+${profitPct}%` : '—'}
          </span>
          {info && (
            <span className="text-[11px] text-slate-400 font-mono leading-none truncate max-w-full">
              {info.entryCents}¢ → {info.targetCents}¢
            </span>
          )}
          {info && (
            <span className="text-[10px] text-slate-500 leading-none truncate max-w-full">
              {isBasket ? `${basket.length} markets` : info.label}
            </span>
          )}
        </div>
      )}
    </button>
  )
}

export default memo(EventButton, (prev, next) => {
  const pt = prev.trade
  const nt = next.trade
  if (prev.disabled !== next.disabled) return false
  if (prev.multiMarket !== next.multiMarket) return false
  if (!pt && !nt) return true
  if (!pt || !nt) return false
  // Summarize all_trades as "count|sumProfit" so basket changes break the memo.
  const sig = (t) => {
    const a = t.all_trades || []
    const n = a.length
    const s = a.reduce((x, y) => x + (y.estimated_profit ?? 0), 0)
    return `${n}|${s.toFixed(2)}`
  }
  return (
    pt.estimated_profit === nt.estimated_profit &&
    pt.active === nt.active &&
    pt.market_ticker === nt.market_ticker &&
    pt.side === nt.side &&
    pt.entry_price === nt.entry_price &&
    pt.sell_target === nt.sell_target &&
    sig(pt) === sig(nt)
  )
})
