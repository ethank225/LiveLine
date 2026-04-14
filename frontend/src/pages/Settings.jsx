import { useState, useEffect, useRef } from 'react'
import { api } from '../api'
import { useAuth } from '../AuthContext'
import { SettingsSkeletonContent } from '../components/Skeleton'
import { SettingsHeader } from '../components/Header'

// Every writable setting auto-saves via a debounced PUT /settings the
// moment it changes. No manual Save button — the mockup has none.
const AUTO_SAVE_KEYS = [
  'max_dollars',
  'alpha',
  'max_slippage_cents',
  'min_move_cents',
  'dry_run',
  'multi_market',
  'use_undo_window',
  'blowout_filter',
  'use_stop_loss',
  'stop_loss_cents',
]
const AUTO_SAVE_DEBOUNCE_MS = 500

export default function Settings() {
  const { user, signOut } = useAuth()
  const [settings, setSettings] = useState(null)

  // Last-saved values so a re-render from the server response doesn't
  // kick the debounce timer back into life.
  const lastSaved = useRef({})
  const saveTimer = useRef(null)

  useEffect(() => {
    api.getSettings().then(s => {
      setSettings(s)
      if (s) {
        AUTO_SAVE_KEYS.forEach(k => { lastSaved.current[k] = s[k] })
      }
    }).catch(() => {})
  }, [])

  // Debounced auto-save. Fires a PUT with only the keys that actually
  // changed, so racing toggles don't clobber each other.
  useEffect(() => {
    if (!settings) return
    const changedKeys = AUTO_SAVE_KEYS.filter(
      k => settings[k] !== lastSaved.current[k]
    )
    if (changedKeys.length === 0) return
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(async () => {
      const payload = {}
      changedKeys.forEach(k => { payload[k] = settings[k] })
      try {
        const result = await api.updateSettings(payload)
        // Mirror backend-clamped values back into state so out-of-range
        // entries visibly snap to the valid bound.
        setSettings(prev => ({ ...prev, ...result }))
        AUTO_SAVE_KEYS.forEach(k => { lastSaved.current[k] = result[k] })
      } catch { /* best effort — keep the user's chosen value */ }
    }, AUTO_SAVE_DEBOUNCE_MS)
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current)
    }
  }, [settings])

  const update = (key, value) => {
    setSettings(prev => ({ ...prev, [key]: value }))
  }

  return (
    <div className="min-h-screen bg-slate-900 page-enter">
      <SettingsHeader />

      {!settings ? (
        <SettingsSkeletonContent />
      ) : (
        <div className="px-4 pb-10 space-y-7">

          {/* --- Trading -------------------------------------------------- */}
          <Section title="Trading">
            <Row
              label="Max bet size"
              desc="Maximum dollars per trade"
              control={
                <Stepper
                  value={settings.max_dollars}
                  onChange={v => update('max_dollars', v)}
                  min={1} max={5000} step={50}
                  editable
                  prefix="$"
                />
              }
            />
            <Row
              label="Alpha"
              desc="Higher = more profit, fewer fills"
              control={
                <Stepper
                  value={settings.alpha}
                  onChange={v => update('alpha', round1(v))}
                  min={0.1} max={1.0} step={0.1}
                  format={v => (+v).toFixed(1)}
                  valueClass="text-emerald-400"
                />
              }
            />
            <Row
              label="Max slippage"
              desc="Extra cents above best ask"
              control={
                <Stepper
                  value={settings.max_slippage_cents}
                  onChange={v => update('max_slippage_cents', v)}
                  min={0} max={10} step={1}
                  format={v => `${v}¢`}
                />
              }
            />
            <Row
              label="Min move"
              desc="Skip trades below this profit to clear fees"
              control={
                <Stepper
                  value={settings.min_move_cents}
                  onChange={v => update('min_move_cents', v)}
                  min={0} max={20} step={1}
                  format={v => `${v}¢`}
                />
              }
              last
            />
          </Section>

          {/* --- Modes ---------------------------------------------------- */}
          <Section title="Modes">
            <Row
              label="Dry run"
              desc="Simulate trades without real money"
              control={
                <Switch
                  value={settings.dry_run}
                  onChange={v => update('dry_run', v)}
                />
              }
            />
            <Row
              label="Multi-market"
              desc="Trade all profitable markets per tap"
              control={
                <Switch
                  value={settings.multi_market}
                  onChange={v => update('multi_market', v)}
                />
              }
            />
            <Row
              label="Undo window"
              desc="3-second cancel window after tap"
              control={
                <Switch
                  value={settings.use_undo_window}
                  onChange={v => update('use_undo_window', v)}
                />
              }
            />
            <Row
              label="Blowout filter"
              desc="Skip moneyline in lopsided games"
              control={
                <Switch
                  value={settings.blowout_filter}
                  onChange={v => update('blowout_filter', v)}
                />
              }
              last
            />
          </Section>

          {/* --- Risk ----------------------------------------------------- */}
          <Section title="Risk">
            <Row
              label="Stop loss"
              desc="Auto-exit if price drops"
              control={
                <Switch
                  value={settings.use_stop_loss}
                  onChange={v => update('use_stop_loss', v)}
                />
              }
            />
            <Row
              label="Stop loss amount"
              desc="Cents below entry to trigger"
              dimmed={!settings.use_stop_loss}
              control={
                <Stepper
                  value={settings.stop_loss_cents}
                  onChange={v => update('stop_loss_cents', v)}
                  min={1} max={50} step={1}
                  format={v => `${v}¢`}
                  disabled={!settings.use_stop_loss}
                />
              }
              last
            />
          </Section>

          {/* --- Account -------------------------------------------------- */}
          <div className="pt-2">
            {user?.email && (
              <div className="text-[11px] text-slate-500 mb-2 truncate px-1">
                Signed in as {user.email}
              </div>
            )}
            <button
              onClick={signOut}
              className="w-full py-3 rounded-xl text-sm font-medium
                         bg-slate-800 text-slate-300
                         border border-white/5
                         active:bg-slate-700 transition-colors"
            >
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Layout primitives
// ---------------------------------------------------------------------------

// Section header + rounded card wrapper. Rows inside separate themselves
// with border-b; the last row passes `last` to skip the border.
function Section({ title, children }) {
  return (
    <div>
      <div
        className="text-[11px] font-medium text-slate-500 uppercase mb-3 px-1"
        style={{ letterSpacing: '1px' }}
      >
        {title}
      </div>
      <div className="bg-slate-800 rounded-xl overflow-hidden">
        {children}
      </div>
    </div>
  )
}

// Single row: label + description on the left, control on the right.
// `dimmed` fades the whole row (used for stop-loss amount when the
// parent toggle is off). `last` suppresses the bottom divider.
function Row({ label, desc, control, dimmed = false, last = false }) {
  return (
    <div
      className={`flex items-center justify-between gap-3 px-3.5 py-3.5 transition-opacity ${
        last ? '' : 'border-b border-white/5'
      } ${dimmed ? 'opacity-40' : ''}`}
    >
      <div className="flex-1 min-w-0">
        <div className="text-[14px] font-medium text-white leading-tight">
          {label}
        </div>
        {desc && (
          <div className="text-[11px] text-slate-500 mt-0.5 leading-tight">
            {desc}
          </div>
        )}
      </div>
      <div className="flex-shrink-0">{control}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

// Stepper pill — bg-slate-900 with − and + buttons and the value centered.
// `format` controls the displayed string (e.g. `$500`, `1¢`, `0.6`), and
// `valueClass` lets callers tint the value (alpha shows in emerald).
//
// When `editable` is passed, the center becomes a typeable input. A
// local `draft` string holds keystrokes while the input has focus so
// partial edits (e.g. the empty string while deleting) don't fight the
// user, and don't kick the auto-save debounce on every character —
// onChange only fires once, at commit time (blur or Enter). Escape
// discards the draft. `prefix` renders a non-editable character (e.g.
// "$") to the left of the digits so the input itself can stay
// number-only.
function Stepper({
  value, onChange, min, max, step,
  format = v => String(v),
  valueClass = 'text-white',
  disabled = false,
  editable = false,
  prefix = '',
}) {
  const v = typeof value === 'number' ? value : Number(value) || 0
  const clamped = Math.min(max, Math.max(min, v))
  const dec = () => { if (!disabled) onChange(Math.max(min, round(clamped - step, step))) }
  const inc = () => { if (!disabled) onChange(Math.min(max, round(clamped + step, step))) }

  // null when not being edited; a string while the input has focus.
  const [draft, setDraft] = useState(null)
  const commit = () => {
    if (draft == null) return
    // Strip anything not a digit, dot, or minus so stray characters
    // (accidental keystrokes, pasted "$500") don't NaN out.
    const n = Number(String(draft).replace(/[^\d.-]/g, ''))
    if (Number.isFinite(n)) {
      onChange(Math.min(max, Math.max(min, round(n, step))))
    }
    setDraft(null)   // back to the formatted, non-focused display
  }

  return (
    <div
      className={`flex items-center bg-slate-900 rounded-full h-9 ${
        disabled ? 'pointer-events-none' : ''
      }`}
    >
      <button
        onClick={dec}
        aria-label="Decrease"
        className="w-9 h-9 flex items-center justify-center text-slate-500
                   active:text-slate-300 transition-colors"
      >
        −
      </button>
      {editable ? (
        <div
          className={`flex items-center justify-center min-w-[60px] text-sm
                      font-semibold font-mono ${valueClass}`}
        >
          {prefix && <span className="text-slate-500 pr-0.5">{prefix}</span>}
          <input
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            value={draft ?? String(clamped)}
            onFocus={e => { setDraft(String(clamped)); e.target.select() }}
            onChange={e => setDraft(e.target.value)}
            onBlur={commit}
            onKeyDown={e => {
              if (e.key === 'Enter') e.currentTarget.blur()
              else if (e.key === 'Escape') { setDraft(null); e.currentTarget.blur() }
            }}
            disabled={disabled}
            aria-label="Value"
            className="w-14 bg-transparent outline-none text-center font-mono"
          />
        </div>
      ) : (
        <div
          className={`min-w-[50px] text-center text-sm font-semibold font-mono ${valueClass}`}
        >
          {format(clamped)}
        </div>
      )}
      <button
        onClick={inc}
        aria-label="Increase"
        className="w-9 h-9 flex items-center justify-center text-slate-500
                   active:text-slate-300 transition-colors"
      >
        +
      </button>
    </div>
  )
}

// 44×24 toggle pill. Emerald when on, slate when off; white dot slides.
function Switch({ value, onChange }) {
  return (
    <button
      onClick={() => onChange(!value)}
      role="switch"
      aria-checked={value}
      className={`relative w-11 h-6 rounded-full flex-shrink-0 transition-colors ${
        value ? 'bg-emerald-500' : 'bg-slate-700'
      }`}
    >
      <div
        className={`absolute top-0.5 w-5 h-5 rounded-full transition-transform ${
          value ? 'bg-white translate-x-[22px]' : 'bg-slate-400 translate-x-0.5'
        }`}
      />
    </button>
  )
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

// Rounds to the precision of `step` so repeated 0.1 adds don't accumulate
// floating-point drift (0.1 + 0.1 + 0.1 === 0.30000000000000004).
function round(n, step) {
  const decimals = (String(step).split('.')[1] || '').length
  const m = 10 ** decimals
  return Math.round(n * m) / m
}

function round1(n) {
  return Math.round(n * 10) / 10
}
