// muster — air-traffic control for delegated subagent fleets (Desktop half).
//
// A layout pane (live fleet board) + a status-bar chip (run/stall/lost counts).
// Data comes from muster's own backend namespace (/api/plugins/muster/*), fed
// by the agent-side ledger that observes Hermes' subagent/LLM/tool hooks.
//
// Catalog-safe imports only: @hermes/plugin-sdk, react, react/jsx-runtime.
// No JSX (disk plugins load uncompiled) — jsx()/jsxs() from react/jsx-runtime.

import { host, haptic, cn } from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'
import { useCallback, useEffect, useState } from 'react'

const POLL_MS = 2000

const STATE_STYLE = {
  RUNNING: { label: 'RUN', dot: 'bg-emerald-400', text: 'text-emerald-400', ring: 'border-emerald-500/30' },
  STARTING: { label: 'START', dot: 'bg-sky-400', text: 'text-sky-400', ring: 'border-sky-500/30' },
  PENDING: { label: 'QUEUE', dot: 'bg-sky-400/70', text: 'text-sky-400/90', ring: 'border-sky-500/20' },
  STALLED: { label: 'STALLED', dot: 'bg-amber-400', text: 'text-amber-400', ring: 'border-amber-500/40' },
  SPINNING: { label: 'SPINNING', dot: 'bg-red-400', text: 'text-red-400', ring: 'border-red-500/40' },
  LOST: { label: 'LOST', dot: 'bg-zinc-400', text: 'text-zinc-400', ring: 'border-zinc-500/30' },
  SUCCEEDED: { label: 'DONE', dot: 'bg-emerald-400/60', text: 'text-emerald-400/80', ring: 'border-emerald-500/20' },
  FAILED: { label: 'FAILED', dot: 'bg-red-400/70', text: 'text-red-400/90', ring: 'border-red-500/25' },
  INTERRUPTED: { label: 'CUT', dot: 'bg-orange-400/70', text: 'text-orange-400/90', ring: 'border-orange-500/25' },
  CANCEL_REQUESTED: { label: 'CANCELLING', dot: 'bg-orange-400', text: 'text-orange-400', ring: 'border-orange-500/40' },
  CANCELLED: { label: 'CANCELLED', dot: 'bg-zinc-400/70', text: 'text-zinc-400/90', ring: 'border-zinc-500/25' },
}

function stateStyle(state) {
  return STATE_STYLE[state] || { label: state || '?', dot: 'bg-zinc-500', text: 'text-zinc-400', ring: 'border-zinc-500/20' }
}

const LIVE_FOR_ACTIONS = new Set(['PENDING', 'STARTING', 'RUNNING', 'STALLED', 'SPINNING', 'CANCEL_REQUESTED'])

function ageOf(ts) {
  if (!ts) return '–'
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts))
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
  const counts = (board && board.counts) || {}
  const run = (counts.PENDING || 0) + (counts.STARTING || 0) + (counts.RUNNING || 0) + (counts.SPINNING || 0)
  const stall = counts.STALLED || 0
  const spin = counts.SPINNING || 0
  const lost = counts.LOST || 0
  const alarm = stall + spin + lost
  const parts = []
  if (run) parts.push(`${run} run`)
  if (stall) parts.push(`${stall} stall`)
  if (spin) parts.push(`${spin} spin`)
  if (lost) parts.push(`${lost} lost`)
  return jsxs('button', {
    type: 'button',
    className: cn('flex items-center gap-1 px-1.5 text-[0.6875rem]', alarm ? 'text-amber-400' : run ? 'text-emerald-400' : 'text-(--ui-text-tertiary)'),
    onClick: () => {
      haptic('tap')
      host.notify({ kind: alarm ? 'warning' : 'info', message: parts.length ? `muster · ${parts.join(' · ')}` : 'muster · fleet idle' })
    },
    children: [
      jsx('span', { className: cn('inline-block h-1.5 w-1.5 rounded-full', alarm ? 'bg-amber-400' : run ? 'bg-emerald-400' : 'bg-zinc-500') }),
      jsx('span', { children: parts.length ? `muster · ${parts.join(' · ')}` : 'muster' }),
    ],
  })
}

function StateBadge({ state }) {
  const s = stateStyle(state)
  return jsxs('span', {
    className: cn('inline-flex items-center gap-1 rounded border px-1 py-[1px] text-[0.625rem] font-medium', s.ring, s.text),
    children: [jsx('span', { className: cn('inline-block h-1.5 w-1.5 rounded-full', s.dot) }), s.label],
  })
}

function FlightRow({ flight, serviceAvailable, onAction, showGoals }) {
  const s = stateStyle(flight.state)
  const [steering, setSteering] = useState(false)
  const [text, setText] = useState('')
  const canAct = serviceAvailable && flight.has_handle && LIVE_FOR_ACTIONS.has(flight.state)
  const observed = !flight.has_handle

  const doCancel = useCallback(() => {
    haptic('tap')
    onAction('cancel', flight.short_id, '')
  }, [flight.short_id, onAction])

  const sendSteer = useCallback(() => {
    const t = text.trim()
    if (!t) return
    haptic('tap')
    onAction('steer', flight.short_id, t)
    setText('')
    setSteering(false)
  }, [text, flight.short_id, onAction])

  return jsxs('div', {
    className: cn('rounded-md border px-2.5 py-2 flex flex-col gap-1.5', s.ring, 'bg-(--ui-bg-primary)'),
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
        (flight.requeue_count || 0) >= 2 ? jsx('span', { className: 'text-red-400 font-medium', children: `spin x${flight.requeue_count}` }) : null,
        flight.tool_in_flight ? jsx('span', { className: 'text-sky-400', children: `in ${flight.tool_in_flight}` }) : null,
        flight.source && flight.source !== 'muster' ? jsx('span', { className: 'ml-auto', children: flight.source === 'observed' ? 'observed · read-only' : flight.source }) : null,
      ] }),
      flight.state === 'LOST' ? jsx('div', { className: 'text-[0.625rem] text-zinc-400', children: 'hermes crashed before this finished — found at restart' }) : null,
      flight.state === 'SPINNING' ? jsx('div', { className: 'text-[0.625rem] text-red-400', children: 'tried and failed several times — something upstream is broken' }) : null,
      canAct || observed && LIVE_FOR_ACTIONS.has(flight.state) ? jsxs('div', { className: 'flex items-center gap-1.5', children: [
        canAct ? jsxs(Fragment, { children: [
          jsx('button', {
            type: 'button', onClick: doCancel,
            className: 'rounded border border-red-500/40 px-2 py-0.5 text-[0.6875rem] text-red-400 hover:bg-red-500/10',
            children: 'Stop',
          }),
          jsx('button', {
            type: 'button', onClick: () => setSteering((v) => !v),
            className: 'rounded border border-sky-500/40 px-2 py-0.5 text-[0.6875rem] text-sky-400 hover:bg-sky-500/10',
            children: steering ? 'Close' : 'Message',
          }),
        ] }) : jsx('span', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children: 'started by hermes itself · stop it from the chat' }),
      ] }) : null,
      steering && canAct ? jsxs('div', { className: 'flex items-center gap-1.5', children: [
        jsx('input', {
          value: text,
          onChange: (e) => setText(e.target.value),
          onKeyDown: (e) => { if (e.key === 'Enter') sendSteer() },
          placeholder: 'send it a message…',
          className: 'flex-1 rounded border border-(--ui-border) bg-transparent px-2 py-1 text-xs outline-none focus:border-sky-500/60',
        }),
        jsx('button', {
          type: 'button', onClick: sendSteer,
          className: 'rounded border border-sky-500/40 px-2 py-0.5 text-[0.6875rem] text-sky-400 hover:bg-sky-500/10',
          children: 'Send',
        }),
      ] }) : null,
    ],
  })
}

function FleetPane({ ctx }) {
  const [board, setBoard] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

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
    return ctx.setInterval(load, POLL_MS)
  }, [load, ctx])

  const onAction = useCallback(async (verb, flightId, extra) => {
    setBusy(true)
    try {
      const res = await ctx.rest(`/action/${verb}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ flight_id: flightId, reason: extra || 'muster pane', text: extra || '' }),
      })
      host.notify({ kind: res && res.ok ? 'info' : 'warning', message: (res && res.message) || `${verb} sent` })
      load()
    } catch (e) {
      host.notify({ kind: 'error', message: `muster: ${verb} failed — ${String((e && e.message) || e)}` })
    } finally {
      setBusy(false)
    }
  }, [ctx, load])

  const counts = (board && board.counts) || {}
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
        ? jsx('span', { className: 'rounded bg-amber-500/15 px-1.5 py-0.5 text-[0.625rem] text-amber-400', children: 'needs attention' })
        : null,
    ] }),
    error ? jsx('div', { className: 'rounded border border-red-500/30 px-2 py-1.5 text-xs text-red-400', children: `backend unavailable: ${error}` }) : null,
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
      `auxiliary: ${board.aux.slice(0, 3).map((a) => `${a.task || 'aux'} ${fmtTokens(a.prompt_tokens)}→${fmtTokens(a.completion_tokens)}`).join(' · ')}`
    }) : null,
  ] })
}

export default {
  id: 'muster',
  name: 'Muster',
  register(ctx) {
    ctx.register({
      id: 'fleet',
      area: 'panes',
      title: 'Muster · fleet',
      data: { placement: 'right', width: '380px' },
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
