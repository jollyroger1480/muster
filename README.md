# Muster

**Air-traffic control for delegated subagent fleets.** "Who's still running?"

Muster keeps a durable ledger of every child agent Hermes spawns — whether the
model delegated it via `delegate_task`, you launched it with `muster_fanout`,
or a kanban board dispatched it — and makes fleet health answerable at a
glance instead of a guess.

## Why

Two failure modes bite every fleet operator (see
[NousResearch/hermes-agent#149110](https://github.com/NousResearch/hermes-agent/issues/149110)):

1. **Silent deaths.** A child that dies on its first provider call is recorded
   as a neutral `rate_limited` and requeued; the dispatcher can spin for hours
   producing zero tokens with no escalation anywhere. A dead gateway emits no
   `subagent_stop` at all, so a "running" row outlives its process. Core's
   `/agents` stall monitor interrupts quiet children mid-run, but nothing
   durable survives the process, and nothing distinguishes stalled from
   spinning from lost after the fact.
2. **Stall blindness.** Nothing distinguishes "working quietly" from "hung" —
   and a tool call in flight looks exactly like a wedge.

Muster closes both from the observer side, without patching core.

## How it differs from adjacent plugins

The catalog has good watchers and a roster-steerer. Muster is the only one that
combines a **durable fleet ledger** with **derived health states** and **actions**:

| | agent-station · protean-subagent-viewer | hermes-teammates | core `/agents` stall monitor | **muster** |
|---|---|---|---|---|
| Shows delegated children | ✔ (focused chat) | ✔ (its own roster) | progress bar | ✔ **every** child, fleet-wide |
| STALLED vs RUNNING | read-only statuses (running/done/interrupted/errored) | — | interrupts on no progress (in-memory, per-run) | ✔ durable badge, in-flight-tool exempt, self-healing |
| SPINNING (requeue-spin alarm) | ✗ | ✗ | ✗ | ✔ strike count per goal |
| **LOST after gateway death** | ✗ (rows vanish or lie) | ✗ | ✗ | ✔ reconciled on boot, with the reason |
| Cancel/steer from the view | ✗ ("never steers, interrupts, or spawns") | ✔ — but only children *it* launched | parent-side interrupt | ✔ muster children; observed rows honestly read-only |
| Kanban worker-exit strike feed | ✗ | ✗ | ✗ | ✔ |
| Cross-process durable ledger | ✗ | ✗ | ✗ | ✔ sqlite, pane/dashboard/chat agree |

Core already interrupts quiet background children (`/agents` shows
`stalling · no progress … — interrupting`) — muster does not replace that; it
adds what issue
[#149110](https://github.com/NousResearch/hermes-agent/issues/149110) asks for
and none of the above provide: the durable record (a dead host emits no
`subagent_stop`), the derived states, the spin alarm, and fleet-level actions.
Different lineage from the plugins above; cancel/steer uses the same public
lifecycle API hermes-teammates uses, applied to a fleet ledger rather than a
predefined roster.

## What you get

| Surface | What it shows / does |
|---|---|
| **Desktop pane** | Live fleet board: every child with state, role, goal, last-event age, tool/API counts, tokens; cancel/steer buttons on muster-launched children |
| **Status-bar chip** | `muster · 2 run · 1 stall` — turns amber the moment anything needs attention |
| **Dashboard tab** | The same board in the web dashboard (read + act) |
| **`/muster`** | Fleet table in any chat, including messaging platforms; `/muster cancel <id>` and `/muster steer <id> <text>` act on muster children |
| **`muster_status` tool** | Fleet JSON for the agent itself, so it can report health honestly |
| **`muster_fanout` tool** | Launch supervised children through the public subagent lifecycle API — those children get real cancel/steer because muster keeps their handles |

## States

- `RUNNING` — events are flowing.
- `STALLED` — no API or tool event for `stall_seconds` (default 300) **and**
  no tool call in flight. A badge, never an auto-kill; it self-heals the
  moment activity resumes.
- `SPINNING` — the same goal (parent+role+goal key) accumulated
  `spin_threshold` (default 3) failed/rate-limited outcomes within
  `spin_window_seconds` (default 1800). Core treats each requeue as neutral;
  muster makes the count visible.
- `LOST` — rows left live by a dead process are reconciled to `LOST` at the
  next boot. Never relaunched by muster.
- Terminal: `SUCCEEDED` / `FAILED` / `INTERRUPTED` / `CANCEL_REQUESTED` /
  `CANCELLED`.

## Control model

- **Muster-launched children** (via `muster_fanout`) carry a subagent-lifecycle
  handle: cancel (cooperative, `CANCEL_REQUESTED` → child interrupts at its
  next safe boundary) and steer (text queued for the child's next iteration
  boundary) work from the pane, the dashboard and chat.
- **Model-spawned `delegate_task` children are observed, read-only.** Muster
  never claims actions it cannot take; their rows say "cancel from the
  parent".
- **Kanban tasks** appear as rows from `kanban_task_claimed` /
  `kanban_task_completed` / `kanban_task_blocked` / `on_kanban_worker_exited`;
  `rate_limited` worker exits feed the spin alarm. Muster does not touch the
  dispatcher.

## Disclosures

- **No network.** Muster never opens a socket, never phones home, no
  telemetry, opt-in or otherwise.
- **No dependencies.** Python stdlib only (`sqlite3`, `threading`, `queue`,
  `json`, `hashlib`). Nothing to consent to at install.
- **No secrets.** No `requires_env`, no OAuth, no credential reads of any kind.
- **Reads outside its own data: none.** The ledger lives at
  `<HERMES_HOME>/plugin-data/muster/fleet.sqlite` (created on first run) and
  nothing else on disk is touched. The dashboard half reads that same file
  plus Hermes' own config to pick up `plugins.entries.muster.settings`.
- **What is stored:** ids, roles, goals **truncated to 80 chars**, models,
  providers, timestamps, counters, token totals, terminal summaries truncated
  to 200 chars, and (for muster launches only) the subagent-lifecycle handle.
  Raw hook payloads — requests, conversation history, tool arguments, tool
  results, error bodies — are never persisted; the update path is an
  allowlist that drops everything else.
- **Goal text on screen:** the pane/table show truncated goals by default;
  set `show_goals: false` (Settings → Plugins → muster) for shared screens.
- **Approvals respected.** Muster auto-approves nothing, disables nothing,
  spawns nothing outside the lifecycle API, and never routes around Hermes'
  approval system. The stall daemon only flips a state label.
- **Actions are cooperative and narrow.** Cancel/steer work only on children
  muster launched, only through the public
  [subagent lifecycle API](https://hermes-agent.nousresearch.com/docs/developer-guide/subagent-lifecycle-api),
  and fail honestly (`UNKNOWN_HANDLE`, "already terminal", "not accepting
  steering") instead of guessing.
- **Unattended-safe.** Hooks enqueue and return; a slow disk cannot stall an
  agent turn. The daemon is a 5-second loop that never blocks, never retries
  launches, never auto-cancels.
- **Handles expire with the process.** After a gateway restart, action
  attempts on old handles answer "not known to this process"; the rows go
  `LOST` on boot. Muster never starts a replacement child.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `stall_seconds` | `300` | Quiet period before the STALLED badge (in-flight tools exempt) |
| `spin_threshold` | `3` | Failed/rate-limited strikes per goal that trip SPINNING |
| `spin_window_seconds` | `1800` | Sliding window for spin strikes |
| `show_goals` | `true` | Show truncated goals in pane/table/chip tooltip |
| `board_rows` | `120` | Rows returned by the board API and `/muster` |
| `chip_enabled` | `true` | Status-bar chip in the Desktop app |

## Compatibility

- `requires_hermes: >=0.22.5` (the public subagent lifecycle API era).
  Missing hook names on a future core are skipped with a warning, not a
  failure; unknown manifest fields are ignored by design.
- If core grows a native stalled/lost state, muster's derived states are a
  display layer over observer hooks — delete cleanly, no core seam to unwind.

## Install

```bash
hermes plugins install muster
```

(From source: drop the `muster/` folder into `$HERMES_HOME/plugins/` and run
`hermes plugins validate ~/.hermes/plugins/muster --install-deps`.)

## Development

```bash
python -m pytest tests/ -q        # 29 unit tests (incl. review regressions), no Hermes needed
```

License: MIT.
