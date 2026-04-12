import { useNavigate } from 'react-router-dom'

function formatCountdown(startTime) {
  const diff = new Date(startTime) - Date.now()
  if (diff <= 0) return 'Starting...'
  const mins = Math.floor(diff / 60000)
  const hrs = Math.floor(mins / 60)
  if (hrs > 0) return `${hrs}h ${mins % 60}m`
  return `${mins}m`
}

function formatTime(startTime) {
  return new Date(startTime).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

export default function GameCard({ game, highlighted }) {
  const navigate = useNavigate()
  const isLive = game.status === 'In Progress'
  const isFinal = game.status === 'Final'
  const isPreGame = !isLive && !isFinal

  const handleTap = () => {
    if (isFinal) return
    if (navigator.vibrate) navigator.vibrate(10)
    navigate(`/game/${game.game_id}`)
  }

  return (
    <div
      onClick={handleTap}
      className={`
        rounded-xl p-4 mb-3 transition-all
        ${isFinal ? 'bg-slate-800/40 opacity-50' : 'bg-slate-800 active:scale-[0.98]'}
        ${highlighted ? 'ring-2 ring-blue-500' : ''}
        ${isFinal ? '' : 'cursor-pointer'}
      `}
    >
      <div className="flex items-center justify-between">
        {/* Teams & Score */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between mb-1">
            <span className="font-semibold text-sm">{game.away_team}</span>
            <span className="font-mono text-sm tabular-nums">
              {isLive || isFinal ? game.away_score : ''}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="font-semibold text-sm">{game.home_team}</span>
            <span className="font-mono text-sm tabular-nums">
              {isLive || isFinal ? game.home_score : ''}
            </span>
          </div>
        </div>

        {/* Status badge */}
        <div className="ml-4 flex-shrink-0 text-right">
          {isLive && (
            <div>
              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-400">
                <span className="w-2 h-2 bg-emerald-400 rounded-full animate-pulse" />
                Live
              </span>
              <div className="flex items-center justify-end gap-1 text-xs text-slate-400 mt-1">
                <svg
                  width="9" height="9" viewBox="0 0 10 10" fill="currentColor"
                  className={(game.inning_state || '').startsWith('Top') ? '' : 'rotate-180'}
                >
                  <path d="M5 1.5 L9 7 L1 7 Z" />
                </svg>
                <span className="tabular-nums">{game.inning}</span>
              </div>
            </div>
          )}
          {isPreGame && (
            <div>
              <div className="text-xs text-slate-400">{formatTime(game.start_time)}</div>
              <div className="text-xs text-blue-400 mt-0.5">{formatCountdown(game.start_time)}</div>
            </div>
          )}
          {isFinal && (
            <span className="text-xs text-slate-500 font-medium">Final</span>
          )}
        </div>
      </div>
    </div>
  )
}
