---
name: muster
description: >
  Use the muster fleet board whenever the user delegates work to child agents
  (delegate_task or muster_fanout): check fleet health before and after
  delegating, watch for STALLED / SPINNING / LOST children, and steer or cancel
  muster-launched children. Trigger phrases: "check my agents", "is anything
  stuck?", "what's running?", "cancel that worker", "fleet status".
---

# Muster — fleet awareness for delegated children

Muster keeps a live ledger of every child agent Hermes spawns. Use it to be
honest about fleet health instead of assuming delegation worked.

## Reading the fleet

Call `muster_status` (no arguments) before reporting delegation results. It
returns every child with:

- `state` — `RUNNING`, `STALLED` (no API/tool event for `stall_seconds`, an
  in-flight tool call exempts it), `SPINNING` (same goal failed and requeued
  `spin_threshold` times in the spin window), `LOST` (gateway died before the
  child finished), or a terminal state (`SUCCEEDED`, `FAILED`, `INTERRUPTED`,
  `CANCELLED`).
- `last_event_age` — seconds since the child's last API or tool event.
- `prompt_tokens` / `completion_tokens` — accumulated from provider usage.
- `requeue_count` — strikes against this goal in the spin window.
- `has_handle` — true only for children **you** launched with `muster_fanout`.

Rules of interpretation:

- A child with `has_handle: false` is a model-spawned `delegate_task` child.
  You cannot cancel or steer it through muster — it is observed, read-only.
- `STALLED` is a badge, not a verdict: a long quiet script can be legitimate.
  Surface it to the user with its `last_event_age`, never auto-kill it.
- `SPINNING` means the same goal keeps dying (usually rate limits) and core
  requeues it as a neutral outcome. Report it; do not keep waiting on it.
- `LOST` means the gateway restarted mid-flight and the child will never
  report. Its work needs a re-run decision from the user.

## Launching supervised children

`muster_fanout` launches children through the public subagent lifecycle API:

```json
{
  "goals": ["Review auth module for regressions", "Audit dependency pins"],
  "role": "leaf",
  "allowed_toolsets": ["file"]
}
```

- Keep each goal short and self-contained (80 chars). Children run
  asynchronously; the tool returns flight ids immediately.
- Only `muster_fanout` children get cancel/steer — their handles live in the
  muster ledger for the process lifetime (and are listed as expired after a
  gateway restart; they never relaunch on their own).
- `allowed_toolsets` is the only restriction the lifecycle API accepts;
  per-launch timeouts, blocked tools and working directories are rejected.
- Do not pass `model`/`provider` unless the user asked for a specific route.

## Talking to the user

- With the Desktop app, point them at the muster pane and the status-bar chip
  ("muster · 2 run · 1 stall"); the pane is the live view.
- In chat or messaging platforms, `/muster` prints the fleet table and
  `/muster cancel <id>` / `/muster steer <id> <text>` act on muster children.
- Include the numbers, not vibes: "1 succeeded, 1 stalled 6m, 1 lost" beats
  "your agents are working on it".
