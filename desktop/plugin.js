// muster — who's still running? (Desktop half).
//
// Live fleet pane + status-bar chip. Fully theme-aware: every color is a
// semantic --ui-* token the host paints, so it follows light/dark, accent
// retints and any installed theme (including the bundled "Muster Ops" one)
// with zero hardcoded colors.
//
// Catalog-safe imports only: @hermes/plugin-sdk, react, react/jsx-runtime.

import { host, haptic, cn } from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'
import { useCallback, useEffect, useState } from 'react'

const POLL_MS = 2000

// Semantic state colors — the host repaints these per theme, so a light theme,
// a retinted accent or "Muster Ops" all stay readable with no extra code.
const C = {
  ok: 'var(--ui-green)',
  warn: 'var(--ui-yellow)',
  bad: 'var(--ui-red)',
  info: 'var(--ui-cyan)',
  warm: 'var(--ui-warm, var(--ui-yellow))',
  dim: 'var(--ui-text-tertiary)',
}

// translucent border helper (theme-aware: mixes the token itself)
const mix = (token, pct) => `color-mix(in srgb, ${token} ${pct}%, transparent)`

const STATE_STYLE = {
  RUNNING: { label: 'working', dot: C.ok, text: C.ok, ring: mix(C.ok, 30) },
  STARTING: { label: 'starting', dot: C.info, text: C.info, ring: mix(C.info, 30) },
  PENDING: { label: 'queued', dot: mix(C.info, 70), text: C.info, ring: mix(C.info, 20) },
  STALLED: { label: 'stuck', dot: C.warn, text: C.warn, ring: mix(C.warn, 40) },
  SPINNING: { label: 'failing on repeat', dot: C.bad, text: C.bad, ring: mix(C.bad, 40) },
  LOST: { label: 'gone', dot: C.dim, text: C.dim, ring: mix(C.dim, 30) },
  SUCCEEDED: { label: 'done', dot: mix(C.ok, 60), text: mix(C.ok, 80), ring: mix(C.ok, 20) },
  FAILED: { label: 'failed', dot: mix(C.bad, 70), text: mix(C.bad, 90), ring: mix(C.bad, 25) },
  INTERRUPTED: { label: 'cut short', dot: mix(C.warm, 70), text: mix(C.warm, 90), ring: mix(C.warm, 25) },
  CANCEL_REQUESTED: { label: 'stopping…', dot: C.warm, text: C.warm, ring: mix(C.warm, 40) },
  CANCELLED: { label: 'stopped', dot: mix(C.dim, 70), text: mix(C.dim, 90), ring: mix(C.dim, 25) },
}

function stateStyle(state) {
  return STATE_STYLE[state] || { label: state || '?', dot: C.dim, text: C.dim, ring: mix(C.dim, 20) }
}

const LIVE_FOR_ACTIONS = new Set(['PENDING', 'STARTING', 'RUNNING', 'STALLED', 'SPINNING', 'CANCEL_REQUESTED'])

// takes an AGE IN SECONDS (board serves last_event_age as an age, not a timestamp)
function ageOf(ageSeconds) {
  if (ageSeconds == null) return '–'
  const s = Math.max(0, Math.floor(ageSeconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60 ? (s % 60) + 's' : ''}`
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
}

function fmtTokens(n) {
  if (!n) return '0'
  if (n < 1000) return String(n)
  if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k'
  return (n / 1000000).toFixed(1) + 'M'
}

function Chip({ board }) {
  const counts = (board && board.chip) || {}
  const run = counts.run || 0
  const stall = counts.stall || 0
  const spin = counts.spin || 0
  const lost = counts.lost || 0
  const alarm = stall + spin + lost
  const parts = []
  if (run) parts.push(`${run} working`)
  if (stall) parts.push(`${stall} stuck`)
  if (spin) parts.push(`${spin} failing`)
  if (lost) parts.push(`${lost} gone`)
  const tone = alarm ? C.warn : run ? C.ok : C.dim
  return jsxs('button', {
    type: 'button',
    className: 'flex items-center gap-1 px-1.5 text-[0.6875rem]',
    style: { color: tone },
    onClick: () => {
      haptic('tap')
      host.notify({ kind: alarm ? 'warning' : 'info', message: parts.length ? `muster · ${parts.join(' · ')}` : 'muster · all quiet' })
    },
    children: [
      jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full', style: { background: tone } }),
      jsx('span', { children: parts.length ? `muster · ${parts.join(' · ')}` : 'muster' }),
    ],
  })
}

function StateBadge({ state }) {
  const s = stateStyle(state)
  return jsxs('span', {
    className: 'inline-flex items-center gap-1 rounded border px-1 py-[1px] text-[0.625rem] font-medium',
    style: { color: s.text, borderColor: s.ring },
    children: [jsx('span', { className: 'inline-block h-1.5 w-1.5 rounded-full', style: { background: s.dot } }), s.label],
  })
}

function FlightRow({ flight, serviceAvailable, onAction, showGoals }) {
  const s = stateStyle(flight.state)
  const [steering, setSteering] = useState(false)
  const [text, setText] = useState('')
  const canAct = serviceAvailable && flight.has_handle && LIVE_FOR_ACTIONS.has(flight.state)

  const doStop = useCallback(() => {
    haptic('tap')
    onAction('cancel', flight.short_id, '')
  }, [flight.short_id, onAction])

  const sendNote = useCallback(() => {
    const t = text.trim()
    if (!t) return
    haptic('tap')
    onAction('steer', flight.short_id, t)
    setText('')
    setSteering(false)
  }, [text, flight.short_id, onAction])

  const btn = (color, label, onClick) => jsx('button', {
    type: 'button',
    onClick,
    className: 'rounded border px-2 py-0.5 text-[0.6875rem]',
    style: { color, borderColor: mix(color, 40) },
    children: label,
  })

  return jsxs('div', {
    className: 'rounded-md border px-2.5 py-2 flex flex-col gap-1.5 bg-(--ui-bg-elevated)',
    style: { borderColor: s.ring },
    children: [
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsx(StateBadge, { state: flight.state }),
        jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary) font-mono', children: flight.short_id }),
        jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: flight.role || '' }),
        jsx('span', { className: 'flex-1' }),
        jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', title: 'age of last API/tool event', children: `${ageOf(flight.last_event_age)} quiet` }),
      ] }),
      showGoals && flight.goal ? jsx('div', { className: 'text-xs text-(--ui-text-secondary) leading-snug break-words', children: flight.goal }) : null,
      jsxs('div', { className: 'flex items-center gap-2 text-[0.625rem] text-(--ui-text-tertiary)', children: [
        jsx('span', { children: `${flight.tool_calls || 0} tools` }),
        jsx('span', { children: `${flight.api_calls || 0} api` }),
        jsx('span', { children: `${fmtTokens(flight.prompt_tokens)}→${fmtTokens(flight.completion_tokens)} tok` }),
        (flight.requeue_count || 0) >= 2 ? jsx('span', { style: { color: C.bad }, className: 'font-medium', children: `failed ${flight.requeue_count}×` }) : null,
        flight.tool_in_flight ? jsx('span', { style: { color: C.info }, children: `in ${flight.tool_in_flight}` }) : null,
        flight.source && flight.source !== 'muster' ? jsx('span', { className: 'ml-auto', children: flight.source === 'observed' ? 'hermes-spawned · read-only' : flight.source }) : null,
      ] }),
      flight.state === 'LOST' ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children: 'hermes crashed before this finished — found at restart' }) : null,
      flight.state === 'SPINNING' ? jsx('div', { className: 'text-[0.625rem]', style: { color: C.bad }, children: 'tried and failed several times — something upstream is broken' }) : null,
      canAct || (!flight.has_handle && LIVE_FOR_ACTIONS.has(flight.state)) ? jsx('div', { className: 'flex items-center gap-1.5', children:
        canAct ? jsxs(Fragment, { children: [
          btn(C.bad, 'Stop', doStop),
          btn(C.info, 'Message', () => setSteering((v) => !v)),
        ] }) : jsx('span', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children: 'started by hermes itself · stop it from the chat' }),
      }) : null,
      steering && canAct ? jsxs('div', { className: 'flex items-center gap-1.5', children: [
        jsx('input', {
          value: text,
          onChange: (e) => setText(e.target.value),
          onKeyDown: (e) => { if (e.key === 'Enter') sendNote() },
          placeholder: 'send it a message…',
          className: 'flex-1 rounded border border-(--ui-border) bg-transparent px-2 py-1 text-xs outline-none',
        }),
        btn(C.info, 'Send', sendNote),
      ] }) : null,
    ],
  })
}

function FleetPane({ ctx }) {
  const [board, setBoard] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const data = await ctx.rest('/board')
      setBoard(data)
      setError(null)
    } catch (e) {
      setError(String((e && e.message) || e))
    }
  }, [ctx])

  useEffect(() => {
    load()
    const stopPoll = ctx.setInterval(load, POLL_MS)
    // a settings save broadcasts this — re-read instantly instead of waiting a tick
    const offSettings = ctx.onEvent('plugin.muster.settings.changed', load)
    return () => { stopPoll(); offSettings() }
  }, [load, ctx])

  const onAction = useCallback(async (verb, flightId, extra) => {
    try {
      const res = await ctx.rest(`/action/${verb}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ flight_id: flightId, reason: extra || 'muster pane', text: extra || '' }),
      })
      host.notify({ kind: res && res.ok ? 'info' : 'warning', message: (res && res.message) || 'done' })
      load()
    } catch (e) {
      host.notify({ kind: 'error', message: `that didn't work (${String((e && e.message) || e)}). try again in a second.` })
    }
  }, [ctx, load])

  const counts = (board && board.chip) || {}
  const serviceAvailable = !board || board.service_available !== false
  const flights = (board && board.flights) || []
  const showGoals = !board || board.show_goals !== false
  const active = flights.filter((f) => LIVE_FOR_ACTIONS.has(f.state) || f.state === 'LOST')
  const recent = flights.filter((f) => !LIVE_FOR_ACTIONS.has(f.state) && f.state !== 'LOST').slice(0, 12)

  return jsxs('div', { className: 'flex h-full flex-col gap-2 p-2 text-sm', children: [
    jsxs('div', { className: 'flex items-center gap-2 px-1', children: [
      jsx('span', { className: 'text-xs font-semibold tracking-wide text-(--ui-text-secondary)', children: 'MUSTER' }),
      jsx('span', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children: 'who\u2019s still running' }),
      jsx('span', { className: 'flex-1' }),
      (counts.STALLED || 0) + (counts.SPINNING || 0) + (counts.LOST || 0) > 0
        ? jsx('span', { className: 'rounded px-1.5 py-0.5 text-[0.625rem]', style: { color: C.warn, background: mix(C.warn, 15) }, children: 'needs attention' })
        : null,
    ] }),
    error ? jsx('div', { className: 'rounded border px-2 py-1.5 text-xs', style: { color: C.bad, borderColor: mix(C.bad, 30) }, children: `backend unavailable: ${error}` }) : null,
    jsx('div', { className: 'flex-1 overflow-y-auto flex flex-col gap-1.5 pr-0.5', children:
      active.length === 0 && recent.length === 0
        ? jsx('div', { className: 'flex h-full items-center justify-center text-xs text-(--ui-text-tertiary)', children: 'nothing running right now — all quiet' })
        : jsxs(Fragment, { children: [
          active.map((f) => jsx(FlightRow, { flight: f, serviceAvailable, onAction, showGoals }, f.id)),
          recent.length ? jsx('div', { className: 'mt-1 px-1 text-[0.625rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'recent' }) : null,
          recent.map((f) => jsx(FlightRow, { flight: f, serviceAvailable, onAction, showGoals }, f.id)),
        ] }),
    }),
    board && board.aux && board.aux.length ? jsx('div', { className: 'px-1 text-[0.625rem] text-(--ui-text-tertiary)', children:
      `hermes chores: ${board.aux.slice(0, 3).map((a) => `${a.task || 'aux'} ${fmtTokens(a.prompt_tokens)}→${fmtTokens(a.completion_tokens)}`).join(' · ')}`
    }) : null,
  ] })
}

// ---- bundled theme: "Muster Ops" — a warm control-room look, light + dark ----

const OPS_DARK = {
  background: '#0c0b09',
  foreground: '#e7e2d8',
  card: '#15130f',
  cardForeground: '#e7e2d8',
  muted: '#1d1a15',
  mutedForeground: '#9a927f',
  popover: '#191612',
  popoverForeground: '#e7e2d8',
  primary: '#f59e0b',
  primaryForeground: '#1c1408',
  secondary: '#211d16',
  secondaryForeground: '#cdc6b8',
  accent: '#e8a33d',
  accentForeground: '#1c1408',
  border: '#2a251d',
  input: '#2a251d',
  ring: '#f59e0b',
  midground: '#f59e0b',
  midgroundForeground: '#1c1408',
  destructive: '#ef4444',
  destructiveForeground: '#ffffff',
  sidebarBackground: '#100e0b',
  sidebarBorder: '#2a251d',
  userBubble: '#211d16',
  userBubbleBorder: '#2a251d',
}

const OPS_LIGHT = {
  background: '#faf8f4',
  foreground: '#1f1b15',
  card: '#ffffff',
  cardForeground: '#1f1b15',
  muted: '#f1ece2',
  mutedForeground: '#6f675a',
  popover: '#ffffff',
  popoverForeground: '#1f1b15',
  primary: '#b45309',
  primaryForeground: '#ffffff',
  secondary: '#f1ece2',
  secondaryForeground: '#44403c',
  accent: '#d97706',
  accentForeground: '#ffffff',
  border: '#e2ddd2',
  input: '#e2ddd2',
  ring: '#b45309',
  midground: '#b45309',
  midgroundForeground: '#ffffff',
  destructive: '#dc2626',
  destructiveForeground: '#ffffff',
  sidebarBackground: '#f3efe7',
  sidebarBorder: '#e2ddd2',
  userBubble: '#f1ece2',
  userBubbleBorder: '#e2ddd2',
}

const MUSTER_OPS_THEME = {
  name: 'muster-ops',
  label: 'Muster Ops',
  colors: OPS_LIGHT,
  darkColors: OPS_DARK,
}

export default {
  id: 'muster',
  name: 'Muster',
  register(ctx) {
    // theme contribution — appears in the theme picker as "Muster Ops"
    ctx.register({ id: 'ops', area: 'themes', data: MUSTER_OPS_THEME })

    ctx.register({
      id: 'fleet',
      area: 'panes',
      title: 'Muster · fleet',
      data: { placement: 'right', width: 'min(400px, 36vw)' },
      render: () => jsx(FleetPane, { ctx }),
    })

    let chipBoard = null
    let chipRef = { current: null }
    const ChipRender = () => {
      const [, force] = useState(0)
      chipRef.current = force
      if (chipBoard && chipBoard.chip_enabled === false) return null
      return jsx(Chip, { board: chipBoard })
    }
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 150,
      render: () => jsx(ChipRender, {}),
    })
    const pollChip = async () => {
      try {
        chipBoard = await ctx.rest('/board')
        if (chipRef.current) chipRef.current((n) => n + 1)
      } catch {
        /* chip stays dim on fetch failure */
      }
    }
    pollChip()
    ctx.setInterval(pollChip, 5000)
  },
}
