import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Analytics } from '@vercel/analytics/react'
import './index.css'
import { AuthProvider, useAuth } from './AuthContext'
import Landing from './pages/Landing'
import GameSelector from './pages/GameSelector'
import Trading from './pages/Trading'
import Settings from './pages/Settings'

function RequireAuth({ children }) {
  const { session, loading } = useAuth()
  if (loading) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="h-8 w-8 rounded-full border-2 border-slate-700 border-t-slate-300 animate-spin" />
      </div>
    )
  }
  if (!session) return <Navigate to="/" replace />
  return children
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/games" element={<RequireAuth><GameSelector /></RequireAuth>} />
          <Route path="/game/:gameId" element={<RequireAuth><Trading /></RequireAuth>} />
          <Route path="/settings" element={<RequireAuth><Settings /></RequireAuth>} />
        </Routes>
      </BrowserRouter>
      <Analytics />
    </AuthProvider>
  </StrictMode>,
)
