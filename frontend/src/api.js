import { supabase } from './supabase'

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

async function authHeaders() {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// Thrown on any non-2xx response. `.status` lets callers branch on 401
// vs other failures so they can e.g. preserve UI state through an auth
// blip instead of wiping it.
export class ApiError extends Error {
  constructor(status, body) {
    super(`HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function authedFetch(path, init = {}) {
  const headers = {
    ...(init.headers || {}),
    ...(await authHeaders()),
  }
  const res = await fetch(`${BASE_URL}${path}`, { ...init, headers })
  if (res.status === 204) return null
  let body = null
  try { body = await res.json() } catch { /* non-json body */ }
  if (!res.ok) throw new ApiError(res.status, body)
  return body
}

export const api = {
  getGames: () => authedFetch('/games'),
  getGameState: (id) => authedFetch(`/game-state/${id}`),
  getTrades: (id) => authedFetch(`/trades/${id}`),
  refresh: (id) => authedFetch(`/refresh/${id}`, { method: 'POST' }),

  // user_timestamp is the client clock at the moment the user tapped the
  // button. Backend stamps it onto the trade row so we can measure
  // time-to-MLB-event post-hoc.
  buy: (id, event) =>
    authedFetch(`/buy/${id}/${event}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_timestamp: Date.now() }),
    }),

  cancel: (posId) => authedFetch(`/cancel/${posId}`, { method: 'POST' }),
  getPositions: (gameId) => authedFetch(`/positions?game_id=${gameId}`),
  getHistory: (id) => authedFetch(`/history/${id}`),
  getBalance: () => authedFetch('/balance'),
  getSettings: () => authedFetch('/settings'),
  updateSettings: (s) =>
    authedFetch('/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(s),
    }),

  // EventSource cannot set custom headers, so pass the JWT as ?token=.
  // Returns a Promise<EventSource> so the caller can await the token.
  streamGame: async (id) => {
    const { data } = await supabase.auth.getSession()
    const token = data.session?.access_token
    const url = new URL(`${BASE_URL}/stream/${id}`)
    if (token) url.searchParams.set('token', token)
    return new EventSource(url.toString())
  },

  // Kill switch — flattens every open position and blocks new /buy
  // until /kill/reset or a new game session.
  killSwitch: () => authedFetch('/kill', { method: 'POST' }),
  killSwitchReset: () => authedFetch('/kill/reset', { method: 'POST' }),
  killSwitchStatus: () => authedFetch('/kill'),

  // Auth helpers
  me: () => authedFetch('/auth/me'),
  logout: () => authedFetch('/auth/logout', { method: 'POST' }),
}
