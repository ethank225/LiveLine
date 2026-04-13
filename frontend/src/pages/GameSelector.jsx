import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api'
import GameCard from '../components/GameCard'
import { GameSelectorSkeletonContent } from '../components/Skeleton'
import { GameSelectorHeader } from '../components/Header'

export default function GameSelector() {
  const [games, setGames] = useState([])
  const [balance, setBalance] = useState(null)
  const [search, setSearch] = useState('')
  const [error, setError] = useState(null)
  const [offline, setOffline] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [toast, setToast] = useState(null)
  const cachedGames = useRef([])
  const toastTimer = useRef(null)

  // Shown when the user taps a game that hasn't started yet. Auto-hides
  // after 3s; subsequent taps reset the timer rather than stacking.
  const handlePreGameTap = useCallback((game) => {
    const when = game.start_time
      ? new Date(game.start_time).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : null
    setToast(when ? `Game starts at ${when}. Come back then!` : 'Game hasn\u2019t started yet.')
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3000)
  }, [])

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current)
  }, [])

  const fetchGames = useCallback(async () => {
    try {
      const data = await api.getGames()
      const list = Array.isArray(data) ? data : data.games || []
      setGames(list)
      cachedGames.current = list
      setOffline(false)
      setError(null)
    } catch {
      setOffline(true)
      if (cachedGames.current.length > 0) {
        setGames(cachedGames.current)
      }
    } finally {
      setLoaded(true)
    }
  }, [])

  const fetchBalance = useCallback(async () => {
    try {
      const data = await api.getBalance()
      if (data.connected) {
        setBalance(data.total)
      }
    } catch {
      // balance fetch is best-effort
    }
  }, [])

  useEffect(() => {
    fetchGames()
    fetchBalance()
    const gamesInterval = setInterval(fetchGames, 60000)
    const balanceInterval = setInterval(fetchBalance, 30000)
    return () => {
      clearInterval(gamesInterval)
      clearInterval(balanceInterval)
    }
  }, [fetchGames, fetchBalance])

  // Filter & group
  const query = search.toLowerCase().trim()
  const filtered = query
    ? games.filter(g => {
        const haystack = `${g.away_team} ${g.home_team} ${g.away_city || ''} ${g.home_city || ''}`.toLowerCase()
        return haystack.includes(query)
      })
    : games

  const live = filtered
    .filter(g => g.status === 'In Progress')
    .sort((a, b) => new Date(a.start_time) - new Date(b.start_time))

  const upcoming = filtered
    .filter(g => g.status === 'Pre-Game' || g.status === 'Scheduled' || g.status === 'Preview')
    .sort((a, b) => new Date(a.start_time) - new Date(b.start_time))

  const final = filtered
    .filter(g => g.status === 'Final')
    .sort((a, b) => new Date(b.start_time) - new Date(a.start_time))

  return (
    <div className="min-h-screen bg-slate-900 page-enter">
      {/* Offline banner */}
      {offline && (
        <div className="bg-amber-600 text-white text-xs text-center py-1.5 font-medium">
          Connection lost — showing cached data
        </div>
      )}

      <GameSelectorHeader balance={balance} />

      {!loaded ? (
        <GameSelectorSkeletonContent />
      ) : (
      <>
      {/* Search */}
      <div className="px-4 pb-3">
        <input
          type="text"
          placeholder="Search teams..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full bg-slate-800 text-sm text-white placeholder-slate-500 rounded-lg px-3 py-2.5 outline-none focus:ring-2 focus:ring-blue-500 transition-shadow"
        />
      </div>

      {/* Game sections */}
      <div className="px-4 pb-8">
        {/* Live now */}
        {live.length > 0 && (
          <Section title="Live now" count={live.length}>
            {live.map(g => (
              <GameCard key={g.game_id} game={g} highlighted={query && true} onPreGameTap={handlePreGameTap} />
            ))}
          </Section>
        )}

        {/* Starting soon */}
        {upcoming.length > 0 && (
          <Section title="Starting soon" count={upcoming.length}>
            {upcoming.map(g => (
              <GameCard key={g.game_id} game={g} highlighted={query && true} onPreGameTap={handlePreGameTap} />
            ))}
          </Section>
        )}

        {/* Final */}
        {final.length > 0 && (
          <Section title="Final" count={final.length}>
            {final.map(g => (
              <GameCard key={g.game_id} game={g} highlighted={false} onPreGameTap={handlePreGameTap} />
            ))}
          </Section>
        )}

        {/* Empty state */}
        {filtered.length === 0 && !error && (
          <div className="text-center text-slate-500 text-sm pt-16">
            {games.length === 0
              ? 'No games available'
              : `No games matching "${search}"`}
          </div>
        )}
      </div>
      </>
      )}

      {/* Toast — fixed above the tab bar, auto-dismisses. One slot,
          no stacking: a second tap resets the same message. */}
      {toast && (
        <div
          className="fixed bottom-20 left-1/2 -translate-x-1/2 z-50
                     bg-slate-700 text-white text-sm px-6 py-3 rounded-full
                     shadow-lg pointer-events-none animate-fade-in"
          role="status"
          aria-live="polite"
        >
          {toast}
        </div>
      )}
    </div>
  )
}

function Section({ title, count, children }) {
  return (
    <div className="mb-6">
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">{title}</h2>
        <span className="text-xs text-slate-500">{count}</span>
      </div>
      {children}
    </div>
  )
}
