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
        goals = [truncate(g, GOAL_MAX_CHARS) for g in goals if truncate(g, GOAL_MAX_CHARS)]
        if not goals:
            return json.dumps({"error": "muster_fanout needs 'goals': a list of short goal strings."})

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
            "note": "Children appear in /muster as they start. Cancel or steer from the Desktop pane, "
                    "the dashboard, or /muster cancel|steer <flight id>.",
        }, default=str)

    # -- muster_status --------------------------------------------------------

    def status(self, _args: dict[str, Any]) -> str:
        board = self.ledger.board(limit=self.board_rows, spin_window_seconds=self.spin_window_seconds)
        return json.dumps(_compact_board(board, show_goals=self.show_goals), default=str)

    # -- /muster command -------------------------------------------------------

    def command(self, raw_args: str) -> str:
        parts = (raw_args or "").strip().split(None, 2)
        verb = parts[0].lower() if parts else ""
        if verb in ("cancel", "steer") and len(parts) >= 2:
            return self._action(verb, parts[1], parts[2] if len(parts) > 2 else "")
        if verb in ("help", "h", "?"):
            return (
                "muster — fleet commands\n"
                "  /muster                    fleet table\n"
                "  /muster cancel <id> [why]  cooperative cancel (muster children only)\n"
                "  /muster steer <id> <text>  queue steering text for a muster child\n"
                "  /muster help               this text"
            )
        return self._table()

    def _action(self, verb: str, flight_id: str, extra: str) -> str:
        flight, handle = self.ledger.handle_for_flight(flight_id.strip())
        if flight is None:
            return f"muster: no flight matching '{flight_id.strip()}'."
        if handle is None:
            source = flight.get("source") or "observed"
            return (
                f"muster: flight {flight['id'][:8]} is a {source}-observed child, not a muster launch — "
                "no handle exists, so it cannot be cancelled or steered from here. Cancel from the parent "
                "session (the model's delegate owns it)."
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
                    return (f"muster: handle for {flight['id'][:8]} is no longer known to this process "
                            "(gateway restarted?). The row will be marked LOST on next boot.")
                if terminal:
                    return f"muster: {flight['id'][:8]} already finished; nothing to cancel."
                self.ledger.update_flight(flight["id"], {
                    "state": "CANCEL_REQUESTED", "last_event_kind": "cancel_requested",
                })
                return (f"muster: cancel accepted for {flight['id'][:8]} — the child interrupts at its "
                        "next safe boundary (CANCEL_REQUESTED until it settles).")
            text = (extra or "").strip()
            if not text:
                return "muster: steer needs text, e.g. /muster steer <id> skip the tests, just report."
            accepted = bool(service.steer(handle, text))
            if accepted:
                self.ledger.update_flight(flight["id"], {"last_event_kind": "steered"})
                return f"muster: steering queued for {flight['id'][:8]} (applies at the child's next iteration boundary)."
            return (f"muster: {flight['id'][:8]} is not accepting steering right now "
                    "(finished, unknown handle, or steering closed).")
        except Exception as exc:  # noqa: BLE001 - command surface must not crash the session
            logger.debug("muster: %s failed", verb, exc_info=True)
            return f"muster: {verb} failed — {type(exc).__name__}."

    # -- board rendering ---------------------------------------------------------

    def _table(self) -> str:
        board = self.ledger.board(limit=self.board_rows, spin_window_seconds=self.spin_window_seconds)
        return render_table(board, show_goals=self.show_goals, spin_threshold=self.spin_threshold)


def _compact_board(board: dict[str, Any], *, show_goals: bool) -> dict[str, Any]:
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
    return {"counts": counts, "live_total": board.get("live_total", 0),
            "requeues_1h": board.get("requeues_1h", 0), "flights": flights, "aux": aux}


def render_table(board: dict[str, Any], *, show_goals: bool, spin_threshold: int) -> str:
    counts = chip_counts(board)
    now = board.get("generated_at") or time.time()
    lines: list[str] = []
    header_bits = []
    if counts["run"]:
        header_bits.append(f"{counts['run']} running")
    if counts["stall"]:
        header_bits.append(f"{counts['stall']} STALLED")
    if counts["spin"]:
        header_bits.append(f"{counts['spin']} SPINNING")
    if counts["lost"]:
        header_bits.append(f"{counts['lost']} lost")
    lines.append("muster — " + (" · ".join(header_bits) if header_bits else "fleet idle")
                 + f" (spin alarm at {spin_threshold} strikes / window)")
    flights = board.get("flights", [])
    if not flights:
        lines.append("  no delegated children recorded yet. muster_fanout launches supervised ones;")
        lines.append("  delegate_task children appear here automatically.")
        return "\n".join(lines)
    for f in flights:
        age = last_event_age(f, now)
        age_s = f"{age:.0f}s" if age is not None else "-"
        goal = f.get("goal") if show_goals else ""
        tool = f" · in {f['tool_in_flight']}" if f.get("tool_in_flight") else ""
        tokens = f"{f.get('prompt_tokens', 0)}in/{f.get('completion_tokens', 0)}out"
        line = (f"  {f.get('state', '?'):<9} {f.get('source', '?'):<8} {(f.get('id') or '')[:8]} "
                f"{f.get('role') or '-'} · {age_s} since event{tool} · {f.get('tool_calls', 0)} tools · "
                f"{f.get('api_calls', 0)} api · {tokens}")
        if f.get("requeue_count", 0) >= spin_threshold:
            line += f" · SPIN x{f['requeue_count']}"
        lines.append(line)
        if goal:
            lines.append(f"      └ {goal}")
        if f.get("state") == "LOST":
            lines.append("      └ gateway died before this child finished; marked lost on boot")
    aux_rows = board.get("aux", [])
    if aux_rows:
        lines.append("  auxiliary (compression/titling/vision on the parent):")
        for a in aux_rows[:6]:
            age = a.get("last_event_age")
            lines.append(f"    {a.get('state', '?'):<9} {a.get('task') or 'aux'} · {a.get('api_calls', 0)} calls · "
                         f"{a.get('prompt_tokens', 0)}in/{a.get('completion_tokens', 0)}out · "
                         f"{age:.0f}s ago" if age is not None else
                         f"    {a.get('state', '?'):<9} {a.get('task') or 'aux'}")
    return "\n".join(lines)
