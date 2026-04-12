// Centralized skeleton loaders for the app.
//
// Skeletons render ONLY the content area — never the header. The header is
// rendered once by the parent page and stays mounted across the skeleton/real
// swap, so nav elements (back arrow, settings gear) don't re-animate.

// ---------------------------------------------------------------------------
// Primitive
// ---------------------------------------------------------------------------

export function SkeletonBox({ className = '' }) {
  return (
    <div className={`bg-slate-800 rounded animate-pulse ${className}`} />
  )
}

// ---------------------------------------------------------------------------
// Composable blocks
// ---------------------------------------------------------------------------

export function GameCardSkeleton() {
  return (
    <div className="rounded-xl p-4 mb-3 bg-slate-800/60">
      <div className="flex items-center justify-between">
        <div className="flex-1 space-y-2">
          <div className="flex items-center justify-between">
            <SkeletonBox className="h-4 w-20" />
            <SkeletonBox className="h-4 w-6" />
          </div>
          <div className="flex items-center justify-between">
            <SkeletonBox className="h-4 w-20" />
            <SkeletonBox className="h-4 w-6" />
          </div>
        </div>
        <div className="ml-4 space-y-1.5">
          <SkeletonBox className="h-3 w-12" />
          <SkeletonBox className="h-3 w-10" />
        </div>
      </div>
    </div>
  )
}

export function EventButtonSkeleton() {
  return (
    <div className="rounded-xl border border-slate-700 bg-slate-800/40 px-2 py-3 min-h-[72px] flex flex-col items-center justify-center gap-1.5">
      <SkeletonBox className="h-4 w-8" />
      <SkeletonBox className="h-3 w-10" />
      <SkeletonBox className="h-2 w-16" />
    </div>
  )
}

export function PositionCardSkeleton() {
  return (
    <div className="rounded-lg border-l-4 border-slate-700 bg-slate-800/60 p-3 mb-2">
      <div className="flex items-start justify-between">
        <div className="space-y-1.5 flex-1">
          <SkeletonBox className="h-4 w-32" />
          <SkeletonBox className="h-3 w-20" />
        </div>
        <SkeletonBox className="h-4 w-14" />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Content-only skeletons (no header — parent page renders the header)
// ---------------------------------------------------------------------------

export function GameSelectorSkeletonContent() {
  return (
    <>
      {/* Search placeholder */}
      <div className="px-4 pb-3">
        <input
          type="text"
          placeholder="Search teams..."
          disabled
          className="w-full bg-slate-800 text-sm text-white placeholder-slate-500 rounded-lg px-3 py-2.5 outline-none opacity-60"
        />
      </div>

      {/* Skeleton game sections */}
      <div className="px-4 pb-8">
        <div className="mb-6">
          <SkeletonBox className="h-3 w-20 mb-2" />
          {[0, 1, 2].map(i => <GameCardSkeleton key={i} />)}
        </div>
        <div className="mb-6">
          <SkeletonBox className="h-3 w-24 mb-2" />
          {[0, 1].map(i => <GameCardSkeleton key={i} />)}
        </div>
      </div>
    </>
  )
}

export function TradingSkeletonContent({ sessionPnl, tradeCount, winRate }) {
  return (
    <>
      {/* Skeleton game state panel */}
      <div className="px-4 pb-3">
        <div className="bg-slate-800/60 rounded-xl p-3">
          <div className="flex items-center justify-between">
            <div className="flex-1 flex flex-col items-center gap-2">
              <SkeletonBox className="h-5 w-10" />
              <SkeletonBox className="h-7 w-8" />
            </div>
            <div className="flex flex-col items-center gap-2 px-2">
              <SkeletonBox className="h-3 w-6" />
              <SkeletonBox className="h-16 w-16 rounded-lg" />
              <SkeletonBox className="h-2 w-12" />
            </div>
            <div className="flex-1 flex flex-col items-center gap-2">
              <SkeletonBox className="h-5 w-10" />
              <SkeletonBox className="h-7 w-8" />
            </div>
          </div>
        </div>
      </div>

      {/* Skeleton status row */}
      <div className="px-4 pb-2">
        <div className="flex items-center gap-2 mb-1">
          <SkeletonBox className="h-1.5 w-1.5 rounded-full" />
          <SkeletonBox className="h-3 w-12" />
        </div>
        <SkeletonBox className="h-1 w-full" />
      </div>

      {/* Skeleton event buttons */}
      <div className="px-4 pb-2">
        <div className="text-xs text-slate-500 mb-2 font-medium">Tap when you see it</div>
        <div className="space-y-2">
          <div className="grid grid-cols-4 gap-2">
            {[0, 1, 2, 3].map(i => <EventButtonSkeleton key={i} />)}
          </div>
          <div className="grid grid-cols-3 gap-2">
            {[0, 1, 2].map(i => <EventButtonSkeleton key={i} />)}
          </div>
        </div>
      </div>

      {/* Real Session footer */}
      <div className="sticky bottom-0 bg-slate-900/95 backdrop-blur border-t border-slate-800 px-4 py-3 mt-auto">
        <div className="flex justify-between text-center">
          <div>
            <div className={`text-sm font-mono font-bold tabular-nums ${(sessionPnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {(sessionPnl ?? 0) >= 0 ? '+' : ''}${(sessionPnl ?? 0).toFixed(2)}
            </div>
            <div className="text-[10px] text-slate-500 mt-0.5">Session P&L</div>
          </div>
          <div>
            <div className="text-sm font-mono font-bold text-white">{tradeCount ?? 0}</div>
            <div className="text-[10px] text-slate-500 mt-0.5">Trades</div>
          </div>
          <div>
            <div className="text-sm font-mono font-bold text-white">{(winRate ?? 0).toFixed(0)}%</div>
            <div className="text-[10px] text-slate-500 mt-0.5">Win rate</div>
          </div>
        </div>
      </div>
    </>
  )
}

export function SettingsSkeletonContent() {
  return (
    <div className="px-4 pb-8 space-y-5">
      {[0, 1, 2, 3, 4, 5].map(i => (
        <div key={i} className="space-y-2">
          <SkeletonBox className="h-4 w-40" />
          <SkeletonBox className="h-3 w-60" />
          <SkeletonBox className="h-10 w-full rounded-lg" />
        </div>
      ))}
    </div>
  )
}
