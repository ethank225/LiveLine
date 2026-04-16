import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api'
import Diamond from '../components/Diamond'
import ButtonGrid from '../components/ButtonGrid'
import UndoOverlay from '../components/UndoOverlay'
import PositionCard from '../components/PositionCard'
import GroupedPositionCard from '../components/GroupedPositionCard'
import CountdownBar from '../components/CountdownBar'
import { TradingSkeletonContent } from '../components/Skeleton'
import { TradingHeader } from '../components/Header'

function parseRunners(runners) {
  if (!runners) return {}
  const s = String(runners).padStart(3, '0')
  return { first: s[0] === '1', second: s[1] === '1', third: s[2] === '1' }
}

function isTopHalf(half) {
  if (!half) return true
  return half.toLowerCase().startsWith('top')
}

export default function Trading() {
  const { gameId } = useParams()
  const navigate = useNavigate()

  const [gameData, setGameData] = useState(null)
  const [positions, setPositions] = useState([])
  const [history, setHistory] = useState([])
  // Session aggregates come from the backend now instead of being re-reduced
  // client-side every render. Shape: {realized, total_trades, wins, resolved,
  // win_rate}. Null until the first /positions response lands.
  const [sessionPnlSnapshot, setSessionPnlSnapshot] = useState(null)
  const [balance, setBalance] = useState(null)
  // Queue of pending undo entries. Rapid taps stack instead of overwriting.
  // Each entry carries its own absolute `expireAt` timestamp so expiry is
  // per-entry regardless of when the overlay re-renders.
  const [undoQueue, setUndoQueue] = useState([])
  const [refreshing, setRefreshing] = useState(false)
  const [flashPanel, setFlashPanel] = useState(false)
  const [authError, setAuthError] = useState(false)
  // When the backend session_loss_limit trips, the backend arms the kill
  // switch and pushes a `session_limit_reached` SSE event. We keep the
  // latest payload (pnl + limit) so the banner can show numbers; any
  // truthy value also disables buy buttons for the rest of the session.
  const [sessionLimit, setSessionLimit] = useState(null)
  // Manual kill switch state. The button lives on the Settings page; here
  // we only render the "trading disabled" banner and block buy buttons
  // whenever the backend flag is armed for this user. Populated on mount
  // from GET /kill so navigating back from Settings picks up the state.
  const [killed, setKilled] = useState(null)

  // Connection status
  const [connStatus, setConnStatus] = useState('reconnecting')
  const sseRetries = useRef(0)
  const sseRef = useRef(null)
  const wakeLock = useRef(null)
  const countdownRef = useRef(null)

  // Flash the game state panel
  const triggerFlash = useCallback(() => {
    setFlashPanel(true)
    setTimeout(() => setFlashPanel(false), 600)
  }, [])

  // ---------------------------------------------------------------------------
  // Data fetching
  // ---------------------------------------------------------------------------

  // Called by countdown onComplete (auto) — don't reset the animation
  const doRefresh = useCallback(async () => {
    console.log('[Countdown] dot hit edge → refreshing MLB state')
    try {
      const data = await api.refresh(gameId)
      console.log('[MLB] refresh complete', data?.state ? `inning=${data.state.inning} outs=${data.state.outs}` : '(no state)')
      setGameData(data)
      setConnStatus('live')
      sseRetries.current = 0
    } catch (e) {
      console.log('[MLB] refresh failed', e)
    }
  }, [gameId])

  // Called by manual refresh button — restart the animation
  const doManualRefresh = useCallback(async () => {
    console.log('[Manual] refresh button tapped')
    try {
      const data = await api.refresh(gameId)
      console.log('[MLB] refresh complete', data?.state ? `inning=${data.state.inning} outs=${data.state.outs}` : '(no state)')
      setGameData(data)
      setConnStatus('live')
      sseRetries.current = 0
      countdownRef.current?.reset()
      triggerFlash()
    } catch (e) {
      console.log('[MLB] refresh failed', e)
    }
  }, [gameId, triggerFlash])

  const fetchPositions = useCallback(async () => {
    try {
      const data = await api.getPositions(gameId)
      // Response is now the session snapshot: {positions, pnl, live_positions}.
      const all = Array.isArray(data) ? data : data.positions || []
      setPositions(all)
      if (data && data.pnl) setSessionPnlSnapshot(data.pnl)
      setAuthError(false)
    } catch (e) {
      // 401 during a mid-game poll shouldn't wipe the display — the user is
      // still watching live positions. Keep existing state, flag the banner,
      // and let the next poll recover.
      if (e instanceof ApiError && e.status === 401) {
        setAuthError(true)
        return
      }
      // Other errors: silently keep existing state (best effort, as before).
    }
  }, [gameId])

  const fetchBalance = useCallback(async () => {
    try {
      const data = await api.getBalance()
      if (data.connected) setBalance(data.total)
    } catch { /* best effort */ }
  }, [])

  // Supabase-backed history for THIS game. Fetched once on mount; live
  // in-memory positions take priority in the merged view.
  const fetchHistory = useCallback(async () => {
    try {
      const rows = await api.getHistory(gameId)
      setHistory(Array.isArray(rows) ? rows : [])
    } catch { /* best effort — live positions still render */ }
  }, [gameId])

  // Wake Lock
  useEffect(() => {
    const requestWakeLock = async () => {
      try {
        if ('wakeLock' in navigator) {
          wakeLock.current = await navigator.wakeLock.request('screen')
        }
      } catch { /* best effort */ }
    }
    requestWakeLock()
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') requestWakeLock()
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      document.removeEventListener('visibilitychange', handleVisibility)
      wakeLock.current?.release()
    }
  }, [])

  // Merge SSE-pushed current_price updates into positions without refetching
  const applyPositionPrices = useCallback((priceMap) => {
    if (!priceMap || typeof priceMap !== 'object') return
    setPositions(prev => prev.map(p => {
      const id = p.position_id || p.id
      const update = priceMap[id]
      if (!update) return p
      return { ...p, current_price: update.current_price }
    }))
  }, [])

  // Initial load
  useEffect(() => {
    doRefresh()
    fetchPositions()        // one-time — gives us the list and initial prices
    fetchHistory()          // one-time — fills in earlier-in-game trades from DB
    fetchBalance()
    // Refresh full positions list once a minute to catch status changes (fills/expiries)
    const statusInterval = setInterval(fetchPositions, 60000)
    const balanceInterval = setInterval(fetchBalance, 30000)
    return () => {
      clearInterval(statusInterval)
      clearInterval(balanceInterval)
    }
  }, [doRefresh, fetchPositions, fetchHistory, fetchBalance])

  // ---------------------------------------------------------------------------
  // SSE — game_state events only (trades handled by ButtonGrid)
  // ---------------------------------------------------------------------------

  useEffect(() => {
    let closed = false
    let pollTimer = null

    const connect = async () => {
      if (closed) return
      setConnStatus('reconnecting')
      const es = await api.streamGame(gameId)
      if (closed) { es.close(); return }
      sseRef.current = es

      es.onopen = () => {
        sseRetries.current = 0
        setConnStatus('live')
      }

      // MLB game state change → score, inning, outs, runners
      es.addEventListener('game_state', (e) => {
        try {
          const data = JSON.parse(e.data)
          if (data.game_state) {
            setGameData(prev => ({ ...prev, ...data.game_state }))
          }
          setConnStatus('live')
          triggerFlash()
        } catch { /* ignore */ }
      })

      // Kalshi price tick → merge current_price updates into open positions
      // (ButtonGrid handles its own trades-dict update)
      es.addEventListener('trades', (e) => {
        try {
          const data = JSON.parse(e.data)
          if (data.position_prices) {
            applyPositionPrices(data.position_prices)
          }
        } catch { /* ignore */ }
      })

      // Backend pushes this whenever a trade reaches a terminal state
      // (filled / expired / stopped / canceled). Without this, the card
      // would stay in its active state until the next 60s /positions poll.
      es.addEventListener('positions_update', () => {
        fetchPositions()
        fetchHistory()
      })

      // Session loss limit breached: backend armed the kill switch and
      // flushed any open positions. Render a persistent banner and keep
      // buy buttons disabled until the user starts a new game.
      es.addEventListener('session_limit_reached', (e) => {
        try {
          const data = JSON.parse(e.data)
          setSessionLimit({
            pnl: data.pnl,
            limit: data.limit,
          })
        } catch {
          setSessionLimit({ pnl: null, limit: null })
        }
      })

      es.onerror = () => {
        es.close()
        sseRetries.current += 1
        if (sseRetries.current >= 5) {
          setConnStatus('polling')
          pollTimer = setInterval(() => { doRefresh() }, 30000)
        } else {
          setConnStatus('reconnecting')
          setTimeout(connect, Math.min(1000 * 2 ** sseRetries.current, 10000))
        }
      }
    }

    connect()

    return () => {
      closed = true
      sseRef.current?.close()
      if (pollTimer) clearInterval(pollTimer)
    }
  }, [gameId, triggerFlash, doRefresh, applyPositionPrices, fetchPositions, fetchHistory])

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  const handleRefresh = async () => {
    if (refreshing) return
    if (navigator.vibrate) navigator.vibrate(10)
    setRefreshing(true)
    await doManualRefresh()
    setRefreshing(false)
  }

  const handleBuy = useCallback(async (event) => {
    const data = await api.buy(gameId, event)
    const positions = data.positions || []
    const anyInUndo = positions.some(p => p.status === 'undo_window')
    if (anyInUndo) {
      // Any position_id in the group works — backend expands cancel by group.
      const firstUndo = positions.find(p => p.status === 'undo_window')
      const qty = positions.reduce((s, p) => s + (p.quantity ?? 0), 0)
      const cost = positions.reduce(
        (s, p) => s + (p.entry_price ?? 0) * (p.quantity ?? 0), 0,
      )
      const windowSec = data.undo_window_seconds ?? 3
      const entry = {
        position_id: firstUndo.id || firstUndo.position_id,
        undo_group_id: data.undo_group_id,
        event,
        positions,
        quantity: qty,
        cost,
        market_ticker: positions[0]?.market_ticker,
        undo_window_seconds: windowSec,
        expireAt: Date.now() + windowSec * 1000,
      }
      setUndoQueue(prev => [...prev, entry])
    } else {
      fetchPositions()
    }
  }, [gameId, fetchPositions])

  // Cancel every pending entry. Parent empties the queue optimistically so
  // the overlay closes immediately; backend calls run in the background.
  const handleUndoAll = useCallback(async (entries) => {
    setUndoQueue([])
    await Promise.all(
      entries.map(e =>
        api.cancel(e.position_id).catch(() => { /* best effort */ }),
      ),
    )
    fetchPositions()
  }, [fetchPositions])

  // A single entry's 3s window ran out — its positions are activating on the
  // backend. Drop just that entry; leave any newer ones in the queue.
  const handleEntryExpire = useCallback((positionId) => {
    setUndoQueue(prev => prev.filter(e => e.position_id !== positionId))
    fetchPositions()
  }, [fetchPositions])

  // Instant-sell on a resting-open position. Calls POST /cancel which
  // (for status=open) cancels the resting limit sell, IOC-flattens at
  // the bid, and returns the realized P&L. The SSE positions_update
  // push that follows swaps the card for its terminal row, so we just
  // refresh the lists defensively here in case SSE is flaky.
  const handleInstantSell = useCallback(async (positionId) => {
    try {
      await api.cancel(positionId)
    } catch (e) {
      console.error('Instant sell failed', e)
    }
    fetchPositions()
    fetchHistory()
  }, [fetchPositions, fetchHistory])

  // User tapped RESET on the banner. Backend disarms the flag; UI clears
  // the banner. Flattened positions stay flattened (not re-opened).
  const handleKillReset = useCallback(async () => {
    try {
      await api.killSwitchReset()
    } catch (e) {
      console.error('Kill reset failed', e)
    }
    setKilled(null)
  }, [])

  // On mount / gameId change, sync kill-switch state from the backend.
  // If the user armed from Settings and then navigated here, the banner
  // should appear without needing a page reload.
  useEffect(() => {
    let cancelled = false
    api.killSwitchStatus()
      .then(s => { if (!cancelled && s?.armed) setKilled({ armed: true }) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [gameId])

  // ---------------------------------------------------------------------------
  // Derived display values
  // ---------------------------------------------------------------------------

  const gd = gameData || {}
  const state = gd.state || {}
  const notStarted = !state.inning && gd.status !== 'Live' && gd.status !== 'In Progress'

  const outs = state.outs ?? 0
  const outDots = [0, 1, 2].map(i => i < outs)
  const runners = parseRunners(state.runners)

  // Merge live positions with Supabase history. In-memory wins: live trades
  // carry current_price + active timers that history rows can't. We dedupe
  // on trade_db_id (the Supabase PK both sides publish).
  const mergedPositions = (() => {
    const liveDbIds = new Set(
      positions.map(p => p.trade_db_id).filter(Boolean),
    )
    const historyOnly = history.filter(
      h => h.trade_db_id && !liveDbIds.has(h.trade_db_id),
    )
    return [...positions, ...historyOnly]
  })()

  // Session aggregates come from the backend Session.snapshot() — no more
  // client-side reduce. First-load fallback to 0 while sessionPnlSnapshot is
  // still null (pre-response paint).
  const sessionPnl = sessionPnlSnapshot?.realized ?? 0
  const tradeCount = sessionPnlSnapshot?.total_trades ?? 0
  const winRate = (sessionPnlSnapshot?.win_rate ?? 0) * 100

  // Cards still render from the merged live + history list. Only the footer
  // stats source changed.
  const positionGroups = (() => {
    const m = new Map()
    for (const p of mergedPositions) {
      const key = p.undo_group_id || p.id || p.position_id
      if (!m.has(key)) m.set(key, [])
      m.get(key).push(p)
    }
    return m
  })()
  const groupsArr = Array.from(positionGroups.values())

  const statusCfg = {
    live:         { dot: 'bg-emerald-400 animate-pulse', text: 'Live',              textColor: 'text-emerald-400' },
    stale:        { dot: 'bg-emerald-400',               text: 'Live',              textColor: 'text-emerald-400' },
    reconnecting: { dot: 'bg-amber-400 animate-pulse',   text: 'Reconnecting...',   textColor: 'text-amber-400' },
    polling:      { dot: 'bg-slate-400',                  text: 'Polling every 30s', textColor: 'text-slate-400' },
  }
  const sc = statusCfg[connStatus] || statusCfg.reconnecting

  const RefreshIcon = ({ spinning }) => (
    <svg
      width="12" height="12" fill="none" viewBox="0 0 24 24"
      stroke="currentColor" strokeWidth="2.5"
      className={spinning ? 'animate-spin' : ''}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h5M20 20v-5h-5" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M20.49 9A9 9 0 005.64 5.64L4 9m16 6l-1.64 3.36A9 9 0 014.51 15" />
    </svg>
  )

  const loading = !gameData

  return (
    <div className="min-h-screen bg-slate-900 flex flex-col page-enter">
      <TradingHeader balance={balance} />

      {loading ? (
        <TradingSkeletonContent
          sessionPnl={sessionPnl}
          tradeCount={tradeCount}
          winRate={winRate}
        />
      ) : (
      <>
      {/* Game state panel */}
      {notStarted ? (
        <div className="text-center py-8 text-slate-400 text-sm">
          Game hasn't started yet
        </div>
      ) : (
        <div className="px-4 pb-3">
          <div
            className={`bg-slate-800 rounded-xl p-3 transition-colors duration-500 ${
              flashPanel ? 'bg-slate-700' : ''
            }`}
          >
            <div className="flex items-center justify-between">
              <div className="text-center flex-1">
                <div className={`text-lg font-bold ${state.half === 'top' ? 'underline underline-offset-4 decoration-amber-400 decoration-2' : ''}`}>{gd.away_abbreviation || gd.away_team || '—'}</div>
                <div className="text-2xl font-mono font-bold tabular-nums">{gd.away_score ?? '—'}</div>
              </div>

              <div className="flex flex-col items-center gap-1 px-2">
                <div className="flex items-center gap-1 text-xs font-semibold text-slate-400">
                  <svg
                    width="10" height="10" viewBox="0 0 10 10" fill="currentColor"
                    className={isTopHalf(state.half) ? '' : 'rotate-180'}
                  >
                    <path d="M5 1.5 L9 7 L1 7 Z" />
                  </svg>
                  <span className="tabular-nums">{state.inning}</span>
                </div>
                <Diamond runners={runners} size={64} />
                <div className="flex gap-1.5">
                  {outDots.map((filled, i) => (
                    <span
                      key={i}
                      className={`w-2 h-2 rounded-full ${filled ? 'bg-amber-400' : 'bg-slate-600'}`}
                    />
                  ))}
                </div>
              </div>

              <div className="text-center flex-1">
                <div className={`text-lg font-bold ${state.half === 'bot' ? 'underline underline-offset-4 decoration-amber-400 decoration-2' : ''}`}>{gd.home_abbreviation || gd.home_team || '—'}</div>
                <div className="text-2xl font-mono font-bold tabular-nums">{gd.home_score ?? '—'}</div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Live status + countdown */}
      {!notStarted && (
        <div className="px-4 pb-2">
          <div className="flex items-center gap-2 mb-1">
            <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${sc.dot}`} />
            <span className={`text-[10px] font-medium ${sc.textColor}`}>{sc.text}</span>
            <div className="flex-1" />
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              className="text-slate-500 active:text-white disabled:opacity-40 transition-colors"
            >
              <RefreshIcon spinning={refreshing} />
            </button>
          </div>
          <CountdownBar ref={countdownRef} seconds={10} onComplete={doRefresh} />
        </div>
      )}

      {/* Transient auth error — preserves existing positions underneath */}
      {authError && (
        <div className="mx-4 mb-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-300 flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
          Auth error — retrying
        </div>
      )}

      {/* Session loss limit tripped — backend has disabled trading. */}
      {sessionLimit && (
        <div className="mx-4 mb-2 rounded-md border border-rose-500/50 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-200 flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-rose-400 animate-pulse" />
          <span>
            Session loss limit reached
            {sessionLimit.pnl != null && sessionLimit.limit != null && (
              <span className="text-rose-300/70">
                {' '}· ${Number(sessionLimit.pnl).toFixed(2)} ≤ ${Number(sessionLimit.limit).toFixed(2)}
              </span>
            )}
            <span className="text-rose-300/70"> · trading disabled</span>
          </span>
        </div>
      )}

      {/* Kill switch armed — banner stays up until the user resets from
          here or from Settings. Flatten counts only appear when the fire
          happened in this tab (direct POST response); a kill from another
          tab or from Settings renders the banner without the count. */}
      {killed && (
        <div className="mx-4 mb-2 rounded-md border border-rose-500/60 bg-rose-500/15 px-3 py-2 text-[12px] text-rose-100 flex items-center gap-3">
          <span className="w-1.5 h-1.5 rounded-full bg-rose-400 animate-pulse" />
          <span className="flex-1">
            Kill switch armed
            {Array.isArray(killed.flattened) && (
              <span className="text-rose-300/80"> · flattened {killed.flattened.length}</span>
            )}
            <span className="text-rose-300/80"> · trading disabled</span>
          </span>
          <button
            onClick={handleKillReset}
            className="text-[11px] font-bold tracking-wider px-2 py-0.5 rounded border border-rose-400/50 bg-rose-500/10 text-rose-200 hover:bg-rose-500/20 transition-colors"
          >
            RESET
          </button>
        </div>
      )}

      {/* Event buttons — isolated from parent re-renders */}
      <div className="px-4 pb-2">
        <div className="text-xs text-slate-500 mb-2 font-medium">Tap when you see it</div>
        <ButtonGrid
          gameId={gameId}
          disabled={notStarted || !!sessionLimit || !!killed}
          onBuy={handleBuy}
        />
      </div>

      {/* Undo overlay — one component, stacked queue underneath */}
      {undoQueue.length > 0 && (
        <div className="px-4 py-2">
          <UndoOverlay
            queue={undoQueue}
            onUndo={handleUndoAll}
            onEntryExpire={handleEntryExpire}
          />
        </div>
      )}

      {/* Active positions (grouped by undo_group_id) */}
      <div className="px-4 pt-2 flex-1">
        {groupsArr.length > 0 && (
          <>
            <div className="text-xs text-slate-500 mb-2 font-medium">Active positions</div>
            {groupsArr
              .slice()
              .sort((a, b) => {
                const ta = Math.max(...a.map(p => new Date(p.created_at || 0).getTime()))
                const tb = Math.max(...b.map(p => new Date(p.created_at || 0).getTime()))
                return tb - ta
              })
              .map(g => {
                const key = g[0].undo_group_id || g[0].id || g[0].position_id
                return g.length > 1
                  ? <GroupedPositionCard key={key} positions={g} onInstantSell={handleInstantSell} />
                  : <PositionCard key={key} position={g[0]} onInstantSell={handleInstantSell} />
              })}
          </>
        )}
      </div>

      {/* Session footer */}
      <div className="sticky bottom-0 bg-slate-900/95 backdrop-blur border-t border-slate-800 px-4 py-3 mt-auto">
        <div className="flex justify-between text-center">
          <div>
            <div className={`text-sm font-mono font-bold tabular-nums ${sessionPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {sessionPnl >= 0 ? '+' : ''}${sessionPnl.toFixed(2)}
            </div>
            <div className="text-[10px] text-slate-500 mt-0.5">Session P&L</div>
          </div>
          <div>
            <div className="text-sm font-mono font-bold text-white">{tradeCount}</div>
            <div className="text-[10px] text-slate-500 mt-0.5">Trades</div>
          </div>
          <div>
            <div className="text-sm font-mono font-bold text-white">{winRate.toFixed(0)}%</div>
            <div className="text-[10px] text-slate-500 mt-0.5">Win rate</div>
          </div>
        </div>
      </div>
      </>
      )}
    </div>
  )
}
