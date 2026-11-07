# Muster

**You gave your AI helpers some jobs. Muster shows you who's actually
working, who's stuck, and who's gone — and lets you stop them with one
click.**

"Who's still running?" is the whole product.

## Why

Two things go wrong with background agents, and hermes can't tell you about
either one (see
[NousResearch/hermes-agent#149110](https://github.com/NousResearch/hermes-agent/issues/149110)):

1. **Quiet deaths.** A worker that dies on its first API call gets retried
   silently, forever. And if hermes itself crashes, every "running" row is a
   lie — nothing survived.
2. **The hang.** A worker sitting silent for ten minutes might be thinking
   or might be dead. Nothing tells you which.

Muster watches from the outside and gives every worker one of four plain
answers: **working**, **stuck**, **failing on repeat**, or **gone** — then
lets you stop the stuck ones with a click.

## How it differs from adjacent plugins

The catalog has good watchers and a roster-steerer. Muster is the only one that
combines a **durable fleet ledger** with **derived health states** and **actions**:

| | agent-station · protean-subagent-viewer | hermes-teammates | core Ctrl+T viewer & `/agents` | **muster** |
|---|---|---|---|---|
| Shows delegated children | ✔ (focused chat) | ✔ (its own roster) | ✔ per conversation, live + finished | ✔ **every** child, fleet-wide |
| stuck / failing-on-repeat / gone badges | read-only statuses (running/done/interrupted/errored) | — | live vs finished only | ✔ durable, in-flight-tool exempt, self-healing |
| **survives a gateway crash** | ✗ | ✗ | ✗ — rows die with the process | ✔ reconciled on boot, with the reason |
| Stop/Message from the view | ✗ ("never steers, interrupts, or spawns") | ✔ — but only workers *it* launched | ✔ in the CLI monitor | ✔ muster workers, from pane/dashboard/chat |
| Works outside the CLI (Desktop pane, dashboard, WhatsApp/Telegram/Slack via `/muster`) | Desktop only | Desktop | classic CLI | ✔ all of them |
| Kanban worker-exit feed | ✗ | ✗ | ✗ | ✔ |
| Cross-process durable ledger | ✗ | ✗ | ✗ | ✔ sqlite, every surface agrees |

Core's monitoring keeps getting better — the Ctrl+T viewer now reads finished
subagents' runs and steers them from the monitor, and `/agents` interrupts
quiet children mid-run. Muster is the layer core's own issue
[#149110](https://github.com/NousResearch/hermes-agent/issues/149110) (still
open) asks for: **what survives the crash** (a dead host emits no
`subagent_stop` — muster reconciles to gone on boot), the **stuck /
failing-on-repeat / gone** health states, and the same fleet on **every
surface** — Desktop pane, web dashboard, and any chat platform. Muster reads
the same public hooks and drives the same public lifecycle API; if core grows
a native stuck/gone ledger later, muster's rows remain a display layer over
it and delete cleanly.

## What you get

| Surface | What it shows / does |
|---|---|
| **Desktop pane** | Live fleet board: every child with state, role, goal, last-event age, tool/API counts, tokens; **Stop** and **Message** buttons on muster-launched children |
| **Status-bar chip** | `muster · 2 working · 1 stuck` — turns amber the moment anything needs attention |
| **Dashboard tab** | The same board in the web dashboard (same process as the agent = read + act; a split deployment is read-only with a clear note) |
| **`/muster`** | Fleet table in any chat, including messaging platforms; `/muster stop <id>` and `/muster note <id> <text>` act on muster workers (`cancel`/`steer` still work as aliases) |
| **`/muster-digest`** | The foreman's report: fleet status, token spend (and cost, at your rates), and the workers that need a decision |
| **Fleet watch + morning report** | Two fill-in-the-blanks automations (`/blueprint muster:watch`, `/blueprint muster:morning`): muster checks the fleet on your schedule and speaks up in any chat only when something needs you — you stop watching, it watches |
| **`muster_status` tool** | Fleet JSON for the agent itself, so it can report health honestly |
| **`muster_fanout` tool** | Launch background workers through the public subagent lifecycle API — those workers get real Stop/Message because muster keeps their handles |

## States

Every worker gets one plain answer:

- **working** — events are flowing. The row shows its tool calls and token count.
- **stuck** — silent for `stall_seconds` (default 300) with nothing in progress.
  A badge, never an auto-kill; it clears itself the moment work resumes.
- **failing on repeat** — the same job failed `spin_threshold` times (default 3)
  within `spin_window_seconds` (default 30 minutes). Hermes treats each retry
  as no big deal; muster keeps count — visible at the third failure, no
  fourth attempt required.
- **gone** — hermes crashed before the worker finished. Found and marked at the
  next start-up (only when the previous process is really dead — a hot reload
  never marks live workers gone). Its work needs a re-run decision. The chip
  counts the last 24 hours of gone, not the graveyard.
- **done / failed / stopped** — finished states. "Stopped" means someone pressed
  Stop (it finishes its current step, then quits safely).

## Control model

- **Workers you started** (`muster_fanout`) carry a handle: press **Stop**
  (cooperative — the worker finishes its current step, then quits safely) or
  **Message** (delivered at its next step), from the pane, the dashboard, or
  chat (`/muster stop <id>`, `/muster note <id> <message>`).
- **Workers hermes started itself are watched only.** Muster never touches
  them and says so instead of pretending — stop those from the chat that
  started them.
- **Kanban tasks** show up too (claim/complete/blocked/worker exit), and
  rate-limited worker exits feed the failing-on-repeat count. Muster does not
  touch the dispatcher.

## Disclosures

- **No network.** Muster never opens a socket, never phones home, no
  telemetry, opt-in or otherwise.
- **Nothing to install.** The agent half is Python stdlib only (`sqlite3`,
  `threading`, `queue`, `json`, `hashlib`). The dashboard half uses the
  FastAPI install Hermes already ships — no extra pip packages.
- **No secrets.** No `requires_env`, no OAuth, no credential reads of any kind.
- **Reads outside its own data: only Hermes' config.** The ledger lives at
  `<HERMES_HOME>/plugin-data/muster/fleet.sqlite` (created on first run).
  The dashboard half reads that same file and Hermes' own config to pick up
  `plugins.entries.muster.settings`. Nothing else on disk is touched.
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
| `stall_seconds` | `300` | How long a worker can sit silent before muster calls it stuck (in-flight tools exempt) |
| `spin_threshold` | `3` | Repeat failures per job before muster flags it "failing on repeat" |
| `spin_window_seconds` | `1800` | How far back repeat failures are counted |
| `show_goals` | `true` | Show each worker's job description in the pane |
| `board_rows` | `120` | Rows returned by the board API and `/muster` |
| `price_input_per_mtok` / `price_output_per_mtok` | `0` | **Your own** blended token rates; set both and muster shows what each worker (and the fleet) costs. Leave 0 and it shows tokens only — muster never invents a price |
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
python -m pytest tests/ -q        # 32 unit tests (incl. review regressions), no Hermes needed
```

License: MIT.
