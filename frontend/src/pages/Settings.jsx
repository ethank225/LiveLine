import { useState, useEffect } from 'react'
import { api } from '../api'
import { useAuth } from '../AuthContext'
import { SettingsSkeletonContent } from '../components/Skeleton'
import { SettingsHeader } from '../components/Header'

const ALPHA_STEPS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]

export default function Settings() {
  const { user, signOut } = useAuth()
  const [settings, setSettings] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.getSettings().then(setSettings).catch(() => {})
  }, [])

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

        {/* Bet size */}
        <Field label="Max dollars per trade">
          <NumberInput
            value={settings.max_dollars}
            onChange={v => update('max_dollars', v)}
            min={1} max={10000} step={50}
            prefix="$"
          />
        </Field>

        {/* Alpha */}
        <Field
          label="Target capture (alpha)"
          desc="Higher = more profit per trade but lower fill rate"
        >
          <div className="flex gap-2">
            {ALPHA_STEPS.map(a => (
              <button
                key={a}
                onClick={() => update('alpha', a)}
                className={`flex-1 py-1.5 rounded-lg text-xs font-mono font-semibold transition-colors ${
                  settings.alpha === a
                    ? 'bg-blue-500 text-white'
                    : 'bg-slate-800 text-slate-400 active:bg-slate-700'
                }`}
              >
                {a}
              </button>
            ))}
          </div>
        </Field>

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
