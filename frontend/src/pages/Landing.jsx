import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../AuthContext'
import { supabase } from '../supabase'
import Diamond from '../components/Diamond'

// Scroll-reveal: every element with .reveal flips data-revealed once its
// top enters the viewport.
function useScrollReveal() {
  useEffect(() => {
    const els = document.querySelectorAll('.reveal')
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.setAttribute('data-revealed', 'true')
            io.unobserve(e.target)
          }
        }
      },
      { rootMargin: '0px 0px -10% 0px', threshold: 0.1 },
    )
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [])
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true" className="mr-2">
      <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.9 32.4 29.4 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3L37.7 9.3C34 6 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.2-.1-2.3-.4-3.5z"/>
      <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 15.3 18.9 12 24 12c3.1 0 5.8 1.2 7.9 3L37.7 9.3C34 6 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.3 34.8 26.7 36 24 36c-5.4 0-9.8-3.4-11.4-8.2l-6.5 5C9.6 39.6 16.2 44 24 44z"/>
      <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4-4.1 5.3l6.2 5.2c-.4.4 6.6-4.8 6.6-14.5 0-1.2-.1-2.3-.4-3.5z"/>
    </svg>
  )
}

function CTA({ className = '' }) {
  const { session } = useAuth()
  const navigate = useNavigate()
  const handleClick = () => {
    if (session) { navigate('/games'); return }
    supabase.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo: `${window.location.origin}/games` },
    })
  }
  return (
    <button
      onClick={handleClick}
      className={`inline-flex items-center justify-center rounded-xl
                  bg-white text-slate-950 font-bold px-6 py-3.5
                  shadow-[0_0_40px_-10px_rgba(255,255,255,0.4)]
                  active:scale-[0.98] hover:bg-slate-200 transition ${className}`}
    >
      {session ? (
        <>
          Go to games
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" className="ml-2">
            <path d="M5 12h14M13 5l7 7-7 7" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </>
      ) : (
        <>
          <GoogleIcon />
          Sign in with Google
        </>
      )}
    </button>
  )
}

function Preview() {
  const rows = [
    [
      { ev: 'HR', pct: '+22%', tone: 'emerald', prices: '45¢ → 55¢', label: 'AZ wins' },
      { ev: '2B', pct: '+9%',  tone: 'teal',    prices: '41¢ → 45¢', label: 'Over 7.5' },
      { ev: '1B', pct: '+4%',  tone: 'blue',    prices: '52¢ → 54¢', label: 'AZ wins' },
      { ev: 'BB', tone: 'mute' },
    ],
    [
      { ev: 'K',   pct: '+12%', tone: 'teal', prices: '38¢ → 43¢', label: 'Under 7.5' },
      { ev: 'OUT', pct: '+6%',  tone: 'blue', prices: '49¢ → 52¢', label: 'PHI -1.5' },
      { ev: 'DP',  tone: 'mute' },
    ],
  ]
  const tones = {
    emerald: { border: 'border-emerald-500/40 bg-emerald-500/10', pct: 'text-emerald-400' },
    teal:    { border: 'border-teal-500/40    bg-teal-500/10',    pct: 'text-teal-400' },
    blue:    { border: 'border-blue-500/30    bg-blue-500/10',    pct: 'text-blue-400' },
    mute:    { border: 'border-transparent    bg-slate-800/30',   pct: 'text-slate-500' },
  }
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/70 backdrop-blur p-4
                    shadow-[0_30px_80px_-30px_rgba(16,185,129,0.25)]">
      <div className="flex items-center gap-3 mb-4">
        <Diamond runners={{ first: true, third: true }} size={44} />
        <div className="flex-1">
          <div className="text-xs text-slate-400 font-mono">PHI @ AZ · Top 6 · 1 out</div>
          <div className="text-sm font-bold text-white">3 – 2</div>
        </div>
        <div className="flex items-center gap-1.5 text-[10px] font-mono text-emerald-400
                        bg-emerald-500/10 border border-emerald-500/30 rounded px-2 py-1">
          <span className="w-1.5 h-1.5 bg-emerald-400 rounded-full animate-pulse" />
          LIVE
        </div>
      </div>
      <div className="space-y-2">
        {rows.map((row, ri) => (
          <div key={ri} className="grid gap-2" style={{ gridTemplateColumns: `repeat(${row.length}, 1fr)` }}>
            {row.map((b) => {
              const t = tones[b.tone]
              const dim = b.tone === 'mute' ? 'opacity-30' : ''
              return (
                <div key={b.ev} className={`rounded-xl border ${t.border} ${dim} min-h-[88px] flex items-center justify-center`}>
                  <div className="flex flex-col items-center justify-center gap-[4px] px-2 py-3">
                    <span className="text-lg font-black text-white leading-none">{b.ev}</span>
                    {b.pct ? (
                      <>
                        <span className={`text-sm font-bold font-mono leading-none ${t.pct}`}>{b.pct}</span>
                        <span className="text-[11px] text-slate-400 font-mono leading-none">{b.prices}</span>
                        <span className="text-[10px] text-slate-500 leading-none">{b.label}</span>
                      </>
                    ) : (
                      <span className="text-[10px] text-slate-500 leading-none mt-1">no edge</span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

function SectionLabel({ children }) {
  return (
    <div className="flex items-center gap-3 mb-6">
      <div className="h-px flex-1 bg-slate-800" />
      <h2 className="text-[10px] uppercase tracking-[0.2em] text-slate-500 font-bold">{children}</h2>
      <div className="h-px flex-1 bg-slate-800" />
    </div>
  )
}

export default function Landing() {
  useScrollReveal()

  return (
    <div className="min-h-screen bg-slate-950 text-white page-enter overflow-x-hidden">
      {/* Hero — tight, with a single emerald glow anchoring the page */}
      <section className="relative px-5 pt-20 pb-12 max-w-xl mx-auto text-center">
        <div
          aria-hidden
          className="pointer-events-none absolute left-1/2 top-0 -translate-x-1/2 w-[520px] h-[520px] -z-0"
          style={{
            background: 'radial-gradient(circle, rgba(16,185,129,0.18) 0%, rgba(16,185,129,0) 60%)',
          }}
        />
        <div className="relative">
          <div className="inline-flex items-center gap-2 text-[10px] uppercase tracking-[0.2em]
                          text-emerald-400 bg-emerald-500/10 border border-emerald-500/30
                          rounded-full px-3 py-1 font-bold mb-6">
            <span className="w-1.5 h-1.5 bg-emerald-400 rounded-full animate-pulse" />
            Live MLB · Kalshi
          </div>
          <h1 className="text-6xl font-black tracking-tight mb-5">LiveLine</h1>
          <p className="text-2xl font-semibold text-white mb-3 leading-tight">Your eyes are the edge.</p>
          <p className="text-sm text-slate-400 leading-relaxed mb-10 max-w-sm mx-auto">
            See the play. Tap the button. Profit before the market knows.
          </p>
          <CTA />
          <div className="text-[10px] uppercase tracking-widest text-slate-600 mt-4">
            Free to try · No credit card
          </div>
        </div>
      </section>

      {/* Preview — shown immediately so the product does the convincing */}
      <section className="px-5 pb-16 max-w-xl mx-auto reveal">
        <SectionLabel>The interface</SectionLabel>
        <Preview />
        <p className="text-xs text-slate-500 text-center mt-4 leading-relaxed">
          One screen. Seven event buttons. Tap what you just saw.
        </p>
      </section>

      {/* Edge — why this works, in one breath */}
      <section className="px-5 py-16 max-w-xl mx-auto reveal">
        <div className="text-center space-y-4">
          <p className="text-2xl font-bold text-white leading-snug">
            Price feeds lag the stadium by <span className="text-emerald-400">5–15 seconds</span>.
          </p>
          <p className="text-2xl font-bold text-white leading-snug">
            Kalshi markets react to price feeds.
          </p>
          <p className="text-2xl font-bold text-white leading-snug">
            You don't have to.
          </p>
        </div>
      </section>

      {/* Stats — the proof */}
      <section className="px-5 py-14 max-w-xl mx-auto reveal">
        <SectionLabel>Backtested</SectionLabel>
        <div className="grid grid-cols-2 gap-3">
          {[
            ['123',    'games',    'emerald'],
            ['22,610', 'trades',   'white'],
            ['72%',    'win rate', 'emerald'],
            ['62%',    'fill rate','white'],
          ].map(([v, l, tone]) => (
            <div key={l} className="rounded-xl border border-slate-800 bg-slate-900/40 px-4 py-6
                                    hover:border-slate-700 transition">
              <div className={`text-3xl font-black tracking-tight ${tone === 'emerald' ? 'text-emerald-400' : 'text-white'}`}>
                {v}
              </div>
              <div className="text-[11px] text-slate-500 mt-2 uppercase tracking-widest">{l}</div>
            </div>
          ))}
        </div>
      </section>

      {/* How it works — now lower, explaining the motion once the viewer is convinced */}
      <section className="px-5 py-14 max-w-xl mx-auto reveal">
        <SectionLabel>How it works</SectionLabel>
        <div className="space-y-6">
          {[
            ['01', 'Watch the game', "You're at the stadium. You see plays 5–15 seconds before any data feed."],
            ['02', 'Tap what you see', 'HR, K, OUT, 1B — one tap, the app handles the rest.'],
            ['03', 'Profit automatically', 'We buy the optimal contract and sell at your target. You watch the game.'],
          ].map(([n, h, p]) => (
            <div key={n} className="flex gap-4">
              <div className="text-xs font-mono text-emerald-400/70 mt-1 font-bold">{n}</div>
              <div>
                <div className="text-base font-bold text-white">{h}</div>
                <div className="text-sm text-slate-400 mt-1 leading-relaxed">{p}</div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Final CTA */}
      <section className="px-5 pt-10 pb-28 max-w-xl mx-auto text-center reveal">
        <h2 className="text-3xl font-black tracking-tight mb-3">Ready to play?</h2>
        <p className="text-sm text-slate-400 mb-8 max-w-sm mx-auto">
          The next pitch is coming. Don't be the last one to know.
        </p>
        <CTA />
      </section>
    </div>
  )
}
