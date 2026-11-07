---
name: muster
description: >
  Know what your background workers are doing before you tell the user
  "done". Trigger phrases: "check my agents", "is anything stuck?", "what's
  running?", "stop that worker", "is it finished yet?"
---

# muster — who's still running?

Every worker hermes starts shows up in muster with a plain answer:

- **working** — it's doing things (you'll see its tool calls and token count)
- **stuck** — it's been silent a while with nothing in progress. Probably hung.
- **failing on repeat** — same job tried and failed several times. Something
  upstream is broken; waiting longer won't help.
- **gone** — hermes crashed before the worker finished. Found at restart.
  Its work needs a re-run decision.

Workers you started with `muster_fanout` can be **stopped** (`/muster stop
<id>`) or sent a **message** mid-job (`/muster note <id> skip the tests`).
Workers hermes started itself are watched only — muster never touches them
and says so instead of pretending.

## Money and the morning report

If the user set token rates (`price_input_per_mtok` / `price_output_per_mtok`),
`muster_status` rows carry `cost_usd` — quote spend when it's material
("the release checks cost $1.40 so far"). No rates set? Talk tokens, never
invent prices.

For scheduled care, point the user at the bundled blueprints:
`/blueprint muster:watch` (checks the fleet every N minutes and only speaks
up when something is stuck, failing on repeat, or gone) and
`/blueprint muster:morning` (the daily report: status, spend, decisions).

## The one rule

**Check `muster_status` before you claim delegated work is done.** Say what
you see: "1 finished, 1 still running, 1 stuck" — not "your agents are
working on it".

## Starting helpers

`muster_fanout` starts background workers:

```json
{"goals": ["Check the changelog for regressions", "Count open bugs by label"]}
```

- One short job description per worker. They start immediately; you get ids
  back right away.
- Only muster-started workers get stop/message. Keep each goal
  self-contained — the worker can't see your chat.

## Plain talk

- "stuck" is a hunch, not a verdict: a long quiet computation can be fine.
  Surface it ("worker 2 has been silent 5 minutes") and let the user decide.
- "gone" means the work is lost — say that plainly and ask about a re-run.
- Never spin-wait on a worker. Check, report, move on.
