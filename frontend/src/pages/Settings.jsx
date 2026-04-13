import { useState, useEffect, useRef } from 'react'
import { api } from '../api'
import { useAuth } from '../AuthContext'
import { SettingsSkeletonContent } from '../components/Skeleton'
import { SettingsHeader } from '../components/Header'

// Keys that auto-save via debounced PUT /settings the moment they change.
// Everything else still goes through the Save button at the bottom.
const AUTO_SAVE_KEYS = ['max_dollars', 'alpha']
const AUTO_SAVE_DEBOUNCE_MS = 500

export default function Settings() {
  const { user, signOut } = useAuth()
  const [settings, setSettings] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  // Tracks the last-saved value for each auto-save key so a change to
  // another control (which would fire the useEffect) doesn't re-PUT an
  // unchanged max_dollars / alpha.
  const lastSaved = useRef({})
  const saveTimer = useRef(null)

  useEffect(() => {
    api.getSettings().then(s => {
      setSettings(s)
      // Seed lastSaved so the initial load isn't treated as a change.
      if (s) {
        AUTO_SAVE_KEYS.forEach(k => { lastSaved.current[k] = s[k] })
      }
    }).catch(() => {})
  }, [])

  // Debounced auto-save for max_dollars / alpha. Fires only when one of
  // them actually changes vs. the last saved value (prevents a spurious
  // PUT every time a toggle toggles).
  useEffect(() => {
    if (!settings) return
    const changed = AUTO_SAVE_KEYS.some(k => settings[k] !== lastSaved.current[k])
    if (!changed) return
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(async () => {
      const payload = {}
      AUTO_SAVE_KEYS.forEach(k => { payload[k] = settings[k] })
      try {
        const result = await api.updateSettings(payload)
        // Mirror the backend's clamped values back into local state so
        // an out-of-range entry (e.g. 99999) visibly snaps to the max.
        setSettings(prev => ({ ...prev, ...result }))
        AUTO_SAVE_KEYS.forEach(k => { lastSaved.current[k] = result[k] })
      } catch { /* best effort — keep the user's typed value */ }
    }, AUTO_SAVE_DEBOUNCE_MS)
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current)
    }
  }, [settings])

  const update = (key, value) => {
    setSettings(prev => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const result = await api.updateSettings(settings)
      setSettings(result)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch { /* best effort */ }
    setSaving(false)
  }

  return (
    <div className="min-h-screen bg-slate-900 page-enter">
      <SettingsHeader />

      {!settings ? (
        <SettingsSkeletonContent />
      ) : (
      <div className="px-4 pb-8 space-y-5">

        {/* Max bet size — typeable, auto-saves 500ms after last keystroke */}
        <InlineNumber
          label="Max bet size ($)"
          value={settings.max_dollars}
          onChange={v => update('max_dollars', v)}
          min={1} max={5000} step={1}
        />

        {/* Alpha — typeable, auto-saves 500ms after last keystroke */}
        <InlineNumber
          label="Alpha (sell aggressiveness)"
          desc="Higher = more profit per trade but lower fill rate"
          value={settings.alpha}
          onChange={v => update('alpha', v)}
          min={0.1} max={1.0} step={0.1}
        />

        {/* Max slippage */}
        <Field
          label="Max slippage (cents)"
          desc="Maximum extra cents above best ask you'll pay"
        >
          <NumberInput
            value={settings.max_slippage_cents}
            onChange={v => update('max_slippage_cents', v)}
            min={0} max={10} step={1}
            suffix="¢"
          />
        </Field>

        <Divider />

        {/* Undo window */}
        <Toggle
          label="3-second undo window"
          desc="Delay sell placement to allow undo. Disable for faster execution."
          value={settings.use_undo_window}
          onChange={v => update('use_undo_window', v)}
        />

        {/* Stop loss */}
        <Toggle
          label="Stop loss"
          desc="Emergency exit if price drops. Recommended: off (timer exit is better)."
          value={settings.use_stop_loss}
          onChange={v => update('use_stop_loss', v)}
        />
        {settings.use_stop_loss && (
          <Field label="Stop loss amount (cents)">
            <NumberInput
              value={settings.stop_loss_cents}
              onChange={v => update('stop_loss_cents', v)}
              min={1} max={50} step={1}
              suffix="¢"
            />
          </Field>
        )}

        <Divider />

        {/* Blowout filter */}
        <Toggle
          label="Skip blowout moneyline"
          desc="Don't trade moneyline when score difference is 5+."
          value={settings.blowout_filter}
          onChange={v => update('blowout_filter', v)}
        />

        {/* Multi-market */}
        <Toggle
          label="Multi-market mode"
          desc="One tap fires every positive-EV market for the event. Budget splits proportional to EV. Undo cancels the whole basket."
          value={settings.multi_market}
          onChange={v => update('multi_market', v)}
        />

        {/* Dry run */}
        <Toggle
          label="Dry run mode"
          desc="Simulate trades without real money. Turn off to go live."
          value={settings.dry_run}
          onChange={v => update('dry_run', v)}
          warning={!settings.dry_run}
          warningText="Real money will be used"
        />

        {/* Save button */}
        <button
          onClick={handleSave}
          disabled={saving}
          className={`w-full py-3 rounded-xl font-semibold text-sm transition-colors ${
            saved
              ? 'bg-emerald-600 text-white'
              : 'bg-blue-600 active:bg-blue-700 text-white disabled:opacity-50'
          }`}
        >
          {saving ? 'Saving...' : saved ? 'Saved' : 'Save settings'}
        </button>

        <Divider />

        {/* Account */}
        <div>
          <div className="text-sm font-medium text-white mb-1">Account</div>
          {user?.email && (
            <div className="text-xs text-slate-500 mb-3 truncate">
              Signed in as {user.email}
            </div>
          )}
          <button
            onClick={signOut}
            className="w-full py-3 rounded-xl font-semibold text-sm
                       bg-slate-800 text-slate-300
                       border border-slate-700
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
// Sub-components
// ---------------------------------------------------------------------------

function Field({ label, desc, children }) {
  return (
    <div>
      <div className="text-sm font-medium text-white mb-1">{label}</div>
      {desc && <div className="text-xs text-slate-500 mb-2">{desc}</div>}
      {children}
    </div>
  )
}

function Toggle({ label, desc, value, onChange, warning, warningText }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-white">{label}</div>
        {desc && <div className="text-xs text-slate-500 mt-0.5">{desc}</div>}
        {warning && warningText && (
          <div className="text-xs text-red-400 mt-1 font-medium">{warningText}</div>
        )}
      </div>
      <button
        onClick={() => onChange(!value)}
        className={`relative w-11 h-6 rounded-full flex-shrink-0 transition-colors ${
          value ? 'bg-blue-500' : 'bg-slate-700'
        }`}
      >
        <div
          className={`absolute top-0.5 w-5 h-5 bg-white rounded-full transition-transform ${
            value ? 'translate-x-[22px]' : 'translate-x-0.5'
          }`}
        />
      </button>
    </div>
  )
}

// Typeable number input laid out like a Toggle row (label left, control
// right). Used for settings that auto-save on change — the native
// <input type="number"> gives keyboard + mobile stepper support, and
// we coerce to a number before bubbling up so downstream comparisons
// (lastSaved) aren't fooled by string vs. number.
function InlineNumber({ label, desc, value, onChange, min, max, step }) {
  const handleChange = (e) => {
    const raw = e.target.value
    if (raw === '') {
      onChange('')   // let the user clear the field while typing
      return
    }
    const n = Number(raw)
    if (Number.isNaN(n)) return
    onChange(n)
  }
  const handleBlur = () => {
    // On blur, snap an empty / out-of-range value back to something
    // sensible so we never PUT garbage to the backend.
    if (value === '' || value == null || Number.isNaN(Number(value))) {
      onChange(min)
      return
    }
    const n = Number(value)
    if (n < min) onChange(min)
    else if (n > max) onChange(max)
  }
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-white">{label}</div>
        {desc && <div className="text-xs text-slate-500 mt-0.5">{desc}</div>}
      </div>
      <input
        type="number"
        inputMode="decimal"
        value={value ?? ''}
        onChange={handleChange}
        onBlur={handleBlur}
        min={min}
        max={max}
        step={step}
        className="w-24 bg-slate-800 rounded-lg px-3 py-2 text-center
                   text-sm font-mono font-semibold text-white
                   border border-slate-700 focus:border-blue-500
                   focus:outline-none"
      />
    </div>
  )
}

function NumberInput({ value, onChange, min, max, step, prefix, suffix }) {
  return (
    <div className="flex items-center gap-2">
      <button
        onClick={() => onChange(Math.max(min, (value || 0) - step))}
        className="w-9 h-9 rounded-lg bg-slate-800 text-slate-300 text-lg font-bold active:bg-slate-700"
      >
        -
      </button>
      <div className="flex-1 bg-slate-800 rounded-lg px-3 py-2 text-center">
        <span className="text-sm font-mono font-semibold text-white">
          {prefix}{typeof value === 'number' ? value : 0}{suffix}
        </span>
      </div>
      <button
        onClick={() => onChange(Math.min(max, (value || 0) + step))}
        className="w-9 h-9 rounded-lg bg-slate-800 text-slate-300 text-lg font-bold active:bg-slate-700"
      >
        +
      </button>
    </div>
  )
}

function Divider() {
  return <div className="border-t border-slate-800" />
}
