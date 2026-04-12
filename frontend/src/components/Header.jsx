import { Link, useNavigate } from 'react-router-dom'

function SettingsGearIcon({ size = 20 }) {
  return (
    <svg width={size} height={size} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
      <path strokeLinecap="round" strokeLinejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  )
}

// GameSelector header: logo, balance, settings gear
export function GameSelectorHeader({ balance }) {
  return (
    <div className="flex items-center justify-between px-4 pt-3 pb-2">
      <span className="text-lg font-bold">LiveLine</span>
      <div className="flex items-center gap-3">
        {balance !== null && balance !== undefined && (
          <span className="text-sm font-mono text-slate-300">${balance.toFixed(2)}</span>
        )}
        <Link to="/settings" className="text-slate-400 active:text-white transition-colors" aria-label="Settings">
          <SettingsGearIcon size={20} />
        </Link>
      </div>
    </div>
  )
}

// Trading header: back arrow, logo, balance, settings gear
export function TradingHeader({ balance }) {
  return (
    <div className="flex items-center justify-between px-4 pt-3 pb-2">
      <div className="flex items-center gap-3">
        <Link to="/games" className="text-slate-400 active:text-white text-xl leading-none arrow-enter">
          &#8592;
        </Link>
        <span className="text-lg font-bold">LiveLine</span>
      </div>
      <div className="flex items-center gap-3">
        {balance !== null && balance !== undefined && (
          <span className="text-sm font-mono text-slate-300">${balance.toFixed(2)}</span>
        )}
        <Link to="/settings" className="text-slate-400 active:text-white transition-colors" aria-label="Settings">
          <SettingsGearIcon size={20} />
        </Link>
      </div>
    </div>
  )
}

// Settings header: back arrow (browser back), title
export function SettingsHeader() {
  const navigate = useNavigate()

  const handleBack = () => {
    // Go back to previous page (game page or game selector).
    // If there's no history (user landed on /settings directly), fall back to /games.
    if (window.history.state && window.history.state.idx > 0) {
      navigate(-1)
    } else {
      navigate('/games')
    }
  }

  return (
    <div className="flex items-center gap-3 px-4 pt-4 pb-3">
      <button
        onClick={handleBack}
        className="text-slate-400 active:text-white text-xl leading-none arrow-enter"
      >
        &#8592;
      </button>
      <h1 className="text-lg font-bold">Settings</h1>
    </div>
  )
}
