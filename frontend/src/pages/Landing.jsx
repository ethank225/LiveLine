import { useNavigate } from 'react-router-dom'
import { useAuth } from '../AuthContext'
import { supabase } from '../supabase'

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

function CTA() {
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
      className="inline-flex items-center justify-center rounded-xl
                 bg-white text-slate-950 font-semibold px-6 py-3
                 shadow-[0_0_40px_-10px_rgba(255,255,255,0.3)]
                 active:scale-[0.98] hover:bg-slate-200 transition"
    >
      {session ? (
        <>
          Continue
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

export default function Landing() {
  return (
    <div className="min-h-screen bg-slate-950 text-white page-enter overflow-hidden relative flex items-center justify-center px-6">
      <div
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[640px] h-[640px]"
        style={{
          background: 'radial-gradient(circle, rgba(16,185,129,0.12) 0%, rgba(16,185,129,0) 60%)',
        }}
      />
      <div className="relative text-center">
        <h1 className="text-6xl sm:text-7xl font-black tracking-tight mb-10">LiveLine</h1>
        <CTA />
      </div>
    </div>
  )
}
