"""Muster fleet ops — launch supervised children, report the fleet.

``muster_fanout`` launches children through the PUBLIC SubagentLifecycle API
(``ctx.subagent_lifecycle``) and records their handles, which is what makes
real cancel/steer possible from the pane and from chat:

    /muster                  -> fleet table
    /muster cancel <id>      -> cooperative cancel (muster children only)
    /muster steer <id> <txt> -> queue steering text for the child

Model-spawned ``delegate_task`` children are OBSERVED only — their rows never
carry a handle, and the UI says so instead of pretending a button exists.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

from .analysis import chip_counts, last_event_age
from .ledger import GOAL_MAX_CHARS, SUMMARY_MAX_CHARS, Ledger, batch_key_for, truncate

logger = logging.getLogger(__name__)

TOOLSET = "muster"


class FleetOps:
    """Tool/command layer above the ledger. One instance per registration."""

    def __init__(self, ctx: Any, ledger: Ledger, get_config: Callable[[str, Any], Any]):
        self.ctx = ctx
        self.ledger = ledger
        self._cfg = get_config

    # -- config ------------------------------------------------------------

    @property
    def stall_seconds(self) -> float:
        try:
            return max(5.0, float(self._cfg("stall_seconds", 300)))
        except (TypeError, ValueError):
            return 300.0

    @property
    def spin_threshold(self) -> int:
        try:
            return max(1, int(self._cfg("spin_threshold", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def spin_window_seconds(self) -> float:
        try:
            return max(60.0, float(self._cfg("spin_window_seconds", 1800)))
        except (TypeError, ValueError):
            return 1800.0

    @property
    def show_goals(self) -> bool:
        return bool(self._cfg("show_goals", True))

    @property
    def price_in(self) -> float:
        try:
            return max(0.0, float(self._cfg("price_input_per_mtok", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def price_out(self) -> float:
        try:
            return max(0.0, float(self._cfg("price_output_per_mtok", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
        """Worker cost at the USER'S OWN rates (settings). 0/absent rates mean
        muster never invents a price — it shows tokens only."""
        if self.price_in <= 0 and self.price_out <= 0:
            return None
        return (prompt_tokens / 1e6) * self.price_in + (completion_tokens / 1e6) * self.price_out

    @property
    def board_rows(self) -> int:
        try:
            return max(10, int(self._cfg("board_rows", 120)))
        except (TypeError, ValueError):
            return 120

    # -- lifecycle service ---------------------------------------------------

    def _service(self) -> Any:
        return getattr(self.ctx, "subagent_lifecycle", None)

    # -- muster_fanout -------------------------------------------------------

    def fanout(self, args: dict[str, Any]) -> str:
        """Tool handler: launch supervised children via the public lifecycle API."""
        goals = args.get("goals") or ([args["goal"]] if args.get("goal") else [])
        if isinstance(goals, str):
            goals = [g.strip() for g in goals.split(";") if g.strip()]
        if not isinstance(goals, list):
            goals = []
        goals = [g.strip()[:GOAL_MAX_CHARS] for g in goals if isinstance(g, str) and g.strip()]
        if not goals:
            return json.dumps({"error": "tell me what the workers should do: pass 'goals', a list of short jobs."})

        service = self._service()
        if service is None:
            return json.dumps({
                "error": "This Hermes build does not expose the public subagent lifecycle API "
                         "(ctx.subagent_lifecycle). Muster can still observe; it cannot launch.",
            })

        role = truncate(args.get("role") or "leaf", 64)
        context = args.get("context")
        model = args.get("model") or None
        provider = args.get("provider") or None
        allowed_toolsets = args.get("allowed_toolsets") or None
        if allowed_toolsets is not None:
            if isinstance(allowed_toolsets, str):
                allowed_toolsets = tuple(t.strip() for t in allowed_toolsets.split(",") if t.strip())
            else:
                allowed_toolsets = tuple(str(t) for t in allowed_toolsets)

        # Import lazily and fail soft: an unknown import path here means the
        # installed Hermes predates the public API (and tests supply their own
        # service). The request object only needs the launch fields as attrs.
        try:
            from agent.subagent_lifecycle import SubagentLaunchRequest
        except ImportError:
            from types import SimpleNamespace as SubagentLaunchRequest  # type: ignore

        parent_session_id = ""
        launched: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for goal in goals:
            correlation_id = "muster-" + uuid.uuid4().hex[:10]
            flight_id = self.ledger.record_flight({
                "source": "muster",
                "state": "PENDING",
                "role": role,
                "goal": goal,
                "model": model,
                "provider": provider,
                "correlation_id": correlation_id,
                "batch_key": batch_key_for(parent_session_id, role, goal),
                "last_event_kind": "launch_requested",
            })
            request: dict[str, Any] = {
                "goal": goal,
                "role": role,
                "correlation_id": correlation_id,
                "metadata": {"muster_flight_id": flight_id},
            }
            if context:
                request["context"] = truncate(context, 2000)
            if model:
                request["model"] = truncate(model, 128)
            if provider:
                request["provider"] = truncate(provider, 64)
            if allowed_toolsets:
                request["allowed_toolsets"] = allowed_toolsets
            try:
                handle = service.launch(SubagentLaunchRequest(**request))
            except Exception as exc:  # noqa: BLE001 - report, never crash the turn
                logger.debug("muster: launch failed for %r", goal, exc_info=True)
                self.ledger.update_flight(flight_id, {
                    "state": "FAILED", "ended_at": time.time(),
                    "exit_reason": "launch_refused",
                    "summary": truncate(str(exc), SUMMARY_MAX_CHARS),
                })
                errors.append({"goal": goal, "flight_id": flight_id, "error": type(exc).__name__})
                continue
            handle_dict = handle.to_dict() if hasattr(handle, "to_dict") else dict(handle)
            handle_parent = truncate(handle_dict.get("parent_session_id"), 128) or ""
            updates: dict[str, Any] = {
                "handle_json": json.dumps(handle_dict),
                "subagent_id": truncate(handle_dict.get("subagent_id"), 128) or None,
                "last_event_kind": "launched",
            }
            if handle_parent:
                # re-key the batch with the real parent so spin strikes line up
                # with the rows the start hook will attach to this goal.
                updates["parent_session_id"] = handle_parent
                updates["batch_key"] = batch_key_for(handle_parent, role, goal)
            self.ledger.update_flight(flight_id, updates)
            launched.append({"goal": goal, "flight_id": flight_id, "subagent_id": handle_dict.get("subagent_id")})

        return json.dumps({
            "launched": launched,
            "errors": errors,
            "note": "They're off. Watch them with /muster — stop any of them with "
                    "/muster stop <id> or the buttons in the desktop pane.",
        }, default=str)

    # -- muster_status --------------------------------------------------------

    def status(self, _args: dict[str, Any]) -> str:
        board = self.ledger.board(limit=self.board_rows, spin_window_seconds=self.spin_window_seconds)
        out = _compact_board(board, show_goals=self.show_goals, cost_fn=self.cost_usd)
        return json.dumps(out, default=str)

    # ---- /muster digest: the foreman's morning report -----------------------

    def digest(self, _raw: str = "") -> str:
        board = self.ledger.board(limit=self.board_rows, spin_window_seconds=self.spin_window_seconds)
        from .analysis import chip_counts
        c = chip_counts(board)
        now = board.get("generated_at") or time.time()
        lines: list[str] = []
        parts = []
        if c["run"]:
            parts.append(f"{c['run']} working")
        if c["stall"]:
            parts.append(f"{c['stall']} stuck")
        if c["spin"]:
            parts.append(f"{c['spin']} failing on repeat")
        if c["lost"]:
            parts.append(f"{c['lost']} gone")
        lines.append("fleet: " + (", ".join(parts) if parts else "all quiet"))

        spend_in = sum(f.get("prompt_tokens", 0) for f in board.get("flights", []))
        spend_out = sum(f.get("completion_tokens", 0) for f in board.get("flights", []))
        cost = self.cost_usd(spend_in, spend_out)
        tokens = f"{spend_in:,} in / {spend_out:,} out tokens"
        lines.append(f"tokens: {tokens}" + (f" (~${cost:.2f} at your rates)" if cost is not None else ""))

        needs = [f for f in board.get("flights", [])
                 if f.get("state") in ("STALLED", "SPINNING", "LOST")]
        if needs:
            lines.append("")
            lines.append("needs a decision:")
            for f in needs[:5]:
                state = {"STALLED": "stuck", "SPINNING": "failing on repeat", "LOST": "gone"}.get(f.get("state"), "?")
                line = f"  - {state}: {(f.get('goal') or '')[:60]}"
                if f.get("state") == "LOST":
                    line += " (re-run it? it will not come back on its own)"
                elif f.get("state") == "STALLED" and f.get("handle_json"):
                    line += f" (/muster stop {(f.get('id') or '')[:8]})"
                lines.append(line)
        else:
            lines.append("nothing needs you right now.")
        lines.append("")
        lines.append("watch it for you: /blueprint muster:watch  ·  morning report: /blueprint muster:morning")
        return "\n".join(lines)

    # -- /muster command -------------------------------------------------------

    def command(self, raw_args: str) -> str:
        parts = (raw_args or "").strip().split(None, 2)
        verb = parts[0].lower() if parts else ""
        aliases = {"stop": "cancel", "cancel": "cancel", "note": "steer", "steer": "steer"}
        if verb in aliases and len(parts) >= 2:
            return self._action(aliases[verb], parts[1], parts[2] if len(parts) > 2 else "")
        if verb in ("help", "h", "?"):
            return (
                "muster — who's still running?\n"
                "  /muster                      who's working, stuck or gone\n"
                "  /muster stop <id>            stop one of your workers\n"
                "  /muster note <id> <message>  send a running worker a message\n"
                "  /muster help                 this text"
            )
        return self._table()

    def _action(self, verb: str, flight_id: str, extra: str) -> str:
        flight, handle = self.ledger.handle_for_flight(flight_id.strip())
        if flight is None:
            return f"no worker called “{flight_id.strip()}”. check /muster for the ids."
        if handle is None:
            source = flight.get("source") or "observed"
            return (
                f"that one (“{flight['id'][:8]}”) was started by hermes itself, not by muster — "
                "so only hermes' own chat can stop it. muster is just watching its progress."
            )
        service = self._service()
        if service is None:
            return "muster: subagent lifecycle service unavailable in this process."
        try:
            if verb == "cancel":
                result = service.cancel(handle, reason=extra or "muster cancel")
                accepted = bool(getattr(result, "accepted", False))
                unknown = bool(getattr(result, "unknown_handle", False))
                terminal = bool(getattr(result, "already_terminal", False))
                if unknown:
                    return ("that worker's stop button died with the last restart. "
                            "it will show up as gone after the next start-up.")
                if terminal:
                    return f"already done — “{flight['id'][:8]}” finished on its own."
                if not accepted:
                    # the lifecycle API refused — never fake progress: leave the
                    # row's state untouched and say so.
                    detail = str(getattr(result, "diagnostic", "") or "it said no")
                    return f"couldn't stop “{flight['id'][:8]}” ({detail}). its row is unchanged."
                self.ledger.update_flight(flight["id"], {
                    "state": "CANCEL_REQUESTED", "last_event_kind": "cancel_requested",
                })
                return (f"stopping “{flight['id'][:8]}” — it finishes its current step, then quits safely.")
            text = (extra or "").strip()
            if not text:
                return "add the message, like: /muster note <id> skip the tests, just report"
            accepted = bool(service.steer(handle, text))
            if accepted:
                self.ledger.update_flight(flight["id"], {"last_event_kind": "steered"})
                return f"message sent to “{flight['id'][:8]}” — it reads it at its next step."
            return "it can't take messages right now (just finished, or already gone)."
        except Exception as exc:  # noqa: BLE001 - command surface must not crash the session
            logger.debug("muster: %s failed", verb, exc_info=True)
            return f"that didn't work ({type(exc).__name__}). try again in a second."

    # -- board rendering ---------------------------------------------------------

    def _table(self) -> str:
        board = self.ledger.board(limit=self.board_rows, spin_window_seconds=self.spin_window_seconds)
        return render_table(board, show_goals=self.show_goals, spin_threshold=self.spin_threshold, cost_fn=self.cost_usd)


def _compact_board(board: dict[str, Any], *, show_goals: bool, cost_fn=None) -> dict[str, Any]:
    counts = chip_counts(board)
    now = board.get("generated_at") or time.time()
    flights = []
    for f in board.get("flights", []):
        flights.append({
            "id": f.get("id"),
            "short_id": (f.get("id") or "")[:8],
            "source": f.get("source"),
            "state": f.get("state"),
            "role": f.get("role"),
            "goal": f.get("goal") if show_goals else None,
            "model": f.get("model"),
            "provider": f.get("provider"),
            "has_handle": bool(f.get("handle_json")),
            "tool_in_flight": f.get("tool_in_flight") or "",
            "last_event_age": last_event_age(f, now),
            "tool_calls": f.get("tool_calls", 0),
            "api_calls": f.get("api_calls", 0),
            "prompt_tokens": f.get("prompt_tokens", 0),
            "completion_tokens": f.get("completion_tokens", 0),
            "requeue_count": f.get("requeue_count", 0),
            "exit_reason": f.get("exit_reason"),
            "summary": f.get("summary"),
            "started_at": f.get("started_at"),
            "ended_at": f.get("ended_at"),
        })
        if cost_fn is not None:
            cost = cost_fn(f.get("prompt_tokens", 0), f.get("completion_tokens", 0))
            flights[-1]["cost_usd"] = round(cost, 4) if cost is not None else None
    aux = []
    for a in board.get("aux", []):
        aux.append({
            "short_id": (a.get("id") or "")[:8],
            "role": a.get("role"),
            "state": a.get("state"),
            "task": a.get("goal"),
            "api_calls": a.get("api_calls", 0),
            "prompt_tokens": a.get("prompt_tokens", 0),
            "completion_tokens": a.get("completion_tokens", 0),
            "last_event_age": last_event_age(a, now),
        })
    out = {"counts": board.get("counts", {}), "chip": counts,
           "live_total": board.get("live_total", 0),
           "requeues_1h": board.get("requeues_1h", 0), "flights": flights, "aux": aux}
    if cost_fn is not None:
        total = cost_fn(sum(f.get("prompt_tokens", 0) for f in board.get("flights", [])),
                        sum(f.get("completion_tokens", 0) for f in board.get("flights", [])))
        out["cost_usd_total"] = round(total, 4) if total is not None else None
    return out


def render_table(board: dict[str, Any], *, show_goals: bool, spin_threshold: int, cost_fn=None) -> str:
    counts = chip_counts(board)
    now = board.get("generated_at") or time.time()

    icon = {"RUNNING": "▶", "STARTING": "▶", "PENDING": "…", "STALLED": "⚠",
            "SPINNING": "↻", "LOST": "✖", "SUCCEEDED": "✓", "FAILED": "✗",
            "INTERRUPTED": "✂", "CANCEL_REQUESTED": "⏳", "CANCELLED": "✓"}

    def one(n, word):
        return f"{n} {word}" if n else ""

    header = ", ".join(filter(None, [
        one(counts["run"], "working"),
        one(counts["stall"], "stuck"),
        one(counts["spin"], "failing on repeat"),
        one(counts["lost"], "gone"),
    ]))
    lines: list[str] = [f"muster: {header}" if header else "muster: all quiet — nothing running."]

    flights = board.get("flights", [])
    if not flights:
        lines.append("start some with muster_fanout, or just delegate work — they all show up here.")
        return "\n".join(lines)

    label = {"RUNNING": "working", "STARTING": "starting", "PENDING": "queued",
             "STALLED": "stuck", "SPINNING": "failing on repeat", "LOST": "gone",
             "SUCCEEDED": "done", "FAILED": "failed", "INTERRUPTED": "cut short",
             "CANCEL_REQUESTED": "stopping…", "CANCELLED": "stopped"}
    for f in flights:
        state = f.get("state", "?")
        short = (f.get("id") or "")[:8]
        goal = (f.get("goal") or "") if show_goals else ""
        mark = icon.get(state, "·")
        lines.append(f"{mark} {label.get(state, state.lower())}  “{short}”  {goal}".rstrip())
        if state == "RUNNING":
            if f.get("tool_in_flight"):
                lines.append(f"    right now: running the “{f['tool_in_flight']}” tool · {f.get('tool_calls', 0)} done so far · {f.get('prompt_tokens', 0)}→{f.get('completion_tokens', 0)} tokens")
            else:
                lines.append(f"    busy · {f.get('tool_calls', 0)} tool calls · {f.get('prompt_tokens', 0)}→{f.get('completion_tokens', 0)} tokens")
        elif state == "STALLED":
            lines.append("    silent for a while, nothing in progress — probably stuck. stop it, or give it a moment.")
        elif state == "SPINNING":
            lines.append(f"    tried {f.get('requeue_count', 0)} times and failed every time — something upstream is broken.")
        elif state == "LOST":
            lines.append("    hermes crashed before this one finished. found at restart — it needs a re-run.")
        elif state == "CANCELLED":
            lines.append("    stopped on purpose.")
        elif state == "SUCCEEDED":
            lines.append("    finished.")
        # only muster's own kids can be stopped / messaged
        if state in ("RUNNING", "STALLED", "SPINNING") and f.get("handle_json"):
            lines.append(f"    stop it: /muster stop {short}   ·   send it a note: /muster note {short} <message>")

    if cost_fn is not None:
        tin = sum(f.get("prompt_tokens", 0) for f in flights)
        tout = sum(f.get("completion_tokens", 0) for f in flights)
        total = cost_fn(tin, tout)
        if total is not None:
            lines.append(f"spend so far: ~${total:.2f} ({tin:,} in / {tout:,} out tokens)")

    aux_rows = board.get("aux", [])
    if aux_rows:
        tokens = sum(a.get("prompt_tokens", 0) for a in aux_rows)
        outs = sum(a.get("completion_tokens", 0) for a in aux_rows)
        lines.append(f"  (hermes' own background chores used {tokens}→{outs} tokens)")
    return "\n".join(lines)
