"""muster — air-traffic control for delegated subagent fleets.

Observes Hermes' subagent, LLM, tool, auxiliary and kanban observer hooks into
a durable sqlite ledger, classifies fleet health (RUNNING / STALLED / SPINNING
/ LOST), and exposes it through a Desktop pane + status-bar chip, a dashboard
tab (``dashboard/``), the ``muster_status`` tool and the ``/muster`` command.
``muster_fanout`` launches supervised children through the PUBLIC subagent
lifecycle API; because muster keeps those handles, those children get real
cancel/steer actions. Model-spawned ``delegate_task`` children are observed
only — muster never touches what it does not own.

Hooks only enqueue small allowlisted dicts (a slow disk can never stall an
agent turn); a single writer thread owns the ledger connection. A 5-second
daemon thread derives STALLED badges; nothing here ever auto-cancels, retries,
relaunches, or touches the network.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import analysis
from .ledger import GOAL_MAX_CHARS, Ledger, batch_key_for, truncate
from .fleet import FleetOps

logger = logging.getLogger(__name__)

_HOOKS = (
    "subagent_start",
    "subagent_stop",
    "pre_tool_call",
    "post_tool_call",
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "pre_auxiliary_call",
    "post_auxiliary_call",
    "on_session_finalize",
    "kanban_task_claimed",
    "kanban_task_completed",
    "kanban_task_blocked",
    "on_kanban_worker_exited",
)

_REGISTERED: list[dict[str, Any]] = []


def register(ctx: Any) -> None:
    """Plugin entrypoint. Builds the ledger, replays dead-process rows to
    LOST, starts the stall daemon, and registers hooks, tools and commands."""

    # Hot reload / re-registration: tear down the previous generation first —
    # its daemon thread and writer connection are otherwise leaked.
    if _REGISTERED:
        try:
            _REGISTERED[0]["stop"].set()
        except Exception:  # noqa: BLE001
            pass
        try:
            _REGISTERED[0]["ledger"].close()
        except Exception:  # noqa: BLE001
            pass
        _REGISTERED.clear()

    ledger = _open_ledger(ctx)
    ledger.reconcile_lost()

    def get_config(key: str, default: Any = None) -> Any:
        try:
            return ctx.get_config(key, default)
        except Exception:  # noqa: BLE001 - config surface must never break registration
            return default

    ops = FleetOps(ctx, ledger, get_config)

    # ---- hooks (fast: enqueue only) -------------------------------------

    def on_subagent_start(**payload: Any) -> None:
        child_session_id = truncate(payload.get("child_session_id"), 128)
        parent_session_id = truncate(payload.get("parent_session_id"), 128)
        role = truncate(payload.get("child_role"), 64)
        goal = truncate(payload.get("child_goal"), GOAL_MAX_CHARS)
        now = time.time()
        if not (child_session_id or goal):
            return
        batch = batch_key_for(
            truncate(payload.get("parent_session_id"), 128),
            truncate(payload.get("child_role"), 64),
            truncate(payload.get("child_goal"), GOAL_MAX_CHARS),
        )
        strikes = ledger.count_requeues_window(batch, ops.spin_window_seconds)
        if child_session_id:
            # attach: a muster PENDING row for this (role, goal) claims the
            # first subagent_start seen — atomically, so two concurrent start
            # events cannot both claim the same row.
            attach = _find_pending_muster(ledger, parent_session_id, role, goal)
            if attach is not None:
                state = "SPINNING" if analysis.spin_key_active(strikes, ops.spin_threshold) else "RUNNING"
                claimed = ledger.claim_flight(attach["id"], {
                    "child_session_id": child_session_id,
                    "subagent_id": truncate(payload.get("child_subagent_id"), 128) or None,
                    "parent_session_id": parent_session_id,
                    "batch_key": batch,
                    "state": state,
                    "started_at": now,
                    "last_event_at": now,
                    "last_event_kind": "start",
                    "requeue_count": strikes,
                })
                if claimed:
                    return
                # lost the claim race: fall through and record an observed row
        ledger.record_flight({
            "source": "observed",
            "state": ("SPINNING" if analysis.spin_key_active(strikes, ops.spin_threshold) else "RUNNING"),
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id or None,
            "subagent_id": truncate(payload.get("child_subagent_id"), 128) or None,
            "role": role,
            "goal": goal,
            "batch_key": batch,
            "last_event_kind": "start",
            "requeue_count": strikes,
        })

    def on_subagent_stop(**payload: Any) -> None:
        child_session_id = truncate(payload.get("child_session_id"), 128)
        now = time.time()
        if not child_session_id:
            return
        flight = _find_live_by_child(ledger, child_session_id)
        state = analysis.stop_status_to_state(payload)
        if state == "UNKNOWN":
            # Unrecognized payload shape: per the analysis contract we keep the
            # previous state and never strike — payload drift must not kill a
            # healthy row or inflate the spin alarm. Log for diagnosis only.
            logger.debug("muster: subagent_stop with unrecognized status fields for %s", child_session_id)
            if flight is not None:
                ledger.update_flight(flight["id"], {
                    "last_event_at": now, "last_event_kind": "stop_unparsed",
                })
            return
        if flight is None:
            # stop for a child we never saw start (plugin loaded mid-flight).
            ledger.record_flight({
                "source": "observed",
                "state": state,
                "child_session_id": child_session_id,
                "parent_session_id": truncate(payload.get("parent_session_id"), 128),
                "role": truncate(payload.get("child_role"), 64),
                "goal": truncate(payload.get("child_goal"), GOAL_MAX_CHARS),
                "batch_key": batch_key_for(
                    truncate(payload.get("parent_session_id"), 128),
                    truncate(payload.get("child_role"), 64),
                    truncate(payload.get("child_goal"), GOAL_MAX_CHARS),
                ),
                "ended_at": now,
                "exit_reason": truncate(payload.get("exit_reason") or payload.get("status"), 64),
                "summary": truncate(payload.get("child_summary"), 200),
                "last_event_kind": "stop",
            })
            return
        ledger.update_flight(flight["id"], {
            "state": state,
            "ended_at": now,
            "last_event_at": now,
            "last_event_kind": "stop",
            "exit_reason": truncate(payload.get("exit_reason") or payload.get("status"), 64),
            "summary": truncate(payload.get("child_summary"), 200),
        })
        # A failed terminal outcome is a strike against this batch goal —
        # repeated rate-limited requeues are exactly the "silent spin" core
        # records as neutral (issue #149110, pain A). Deliberate user cancels
        # are not failures: a cancel then relaunch of the same goal is normal
        # operation, so only organic FAILED/INTERRUPTED outcomes strike.
        organic_failure = (state in ("FAILED", "INTERRUPTED")
                           and flight.get("last_event_kind") != "cancel_requested")
        if organic_failure and flight.get("batch_key"):
            ledger.record_requeue(flight["batch_key"],
                                  truncate(payload.get("status") or payload.get("exit_reason") or state, 64),
                                  flight["id"])

    def on_pre_tool_call(**payload: Any) -> None:
        sid = truncate(payload.get("session_id"), 128)
        if sid:
            ledger.touch_by_session(sid, "tool", tool_in_flight=truncate(payload.get("tool_name"), 64))

    def on_post_tool_call(**payload: Any) -> None:
        sid = truncate(payload.get("session_id"), 128)
        if sid:
            ledger.touch_by_session(sid, "tool", tool_calls=1, tool_in_flight="")

    def _session_tokens(payload: dict[str, Any]) -> tuple[int, int]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return 0, 0
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        try:
            return max(0, int(prompt)), max(0, int(completion))
        except (TypeError, ValueError):
            return 0, 0

    def on_pre_api_request(**payload: Any) -> None:
        sid = truncate(payload.get("session_id"), 128)
        if sid:
            ledger.touch_by_session(sid, "api")

    def on_post_api_request(**payload: Any) -> None:
        sid = truncate(payload.get("session_id"), 128)
        if not sid:
            return
        prompt, completion = _session_tokens(payload)
        ledger.touch_by_session(sid, "api", api_calls=1, prompt_tokens=prompt,
                                completion_tokens=completion)

    def on_api_request_error(**payload: Any) -> None:
        sid = truncate(payload.get("session_id"), 128)
        if not sid:
            return
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        status_code = payload.get("status_code")
        ledger.touch_by_session(
            sid, "api_error", error_count=1,
            status_code=status_code if isinstance(status_code, int) else None,
            error_class=truncate(error.get("type"), 64),
        )

    def _aux_row(payload: dict[str, Any]) -> str:
        sid = truncate(payload.get("session_id"), 128)
        task = truncate(payload.get("aux_task"), 64) or "aux"
        return f"aux:{sid}:{task}"

    _AUX_ROWS: dict[str, str] = {}

    def on_pre_auxiliary_call(**payload: Any) -> None:
        key = _aux_row(payload)
        if key not in _AUX_ROWS:
            _AUX_ROWS[key] = ledger.record_flight({
                "kind": "aux",
                "source": "observed",
                "state": "RUNNING",
                "parent_session_id": truncate(payload.get("session_id"), 128),
                "role": "aux",
                "goal": truncate(payload.get("aux_task"), 64),
                "batch_key": None,
                "last_event_kind": "aux",
            })
        else:
            ledger.update_flight(_AUX_ROWS[key], {
                "state": "RUNNING", "last_event_at": time.time(), "last_event_kind": "aux",
            })

    def on_post_auxiliary_call(**payload: Any) -> None:
        key = _aux_row(payload)
        flight_id = _AUX_ROWS.get(key)
        if not flight_id:
            return
        prompt, completion = _session_tokens(payload)
        errored = payload.get("error") is not None
        updates: dict[str, Any] = {
            "state": "FAILED" if errored else "SUCCEEDED",
            "last_event_at": time.time(),
            "last_event_kind": "aux_error" if errored else "aux_done",
            "api_calls": 1,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        }
        if not errored:
            updates["ended_at"] = time.time()
        ledger.update_flight(flight_id, updates)

    def on_session_finalize(**payload: Any) -> None:
        sid = truncate(payload.get("session_id"), 128)
        if not sid:
            return
        now = time.time()
        # close aux rows owned by this session
        for key, flight_id in list(_AUX_ROWS.items()):
            if f":{sid}:" in key:
                ledger.update_flight(flight_id, {"state": "ENDED", "ended_at": now})
                _AUX_ROWS.pop(key, None)

    def on_kanban_claimed(**payload: Any) -> None:
        task_id = truncate(payload.get("task_id"), 128)
        if not task_id:
            return
        existing = ledger.find_by_task(task_id)
        if existing is not None:
            board = truncate(payload.get("board"), 64) or None
            batch = batch_key_for("kanban", truncate(payload.get("board"), 64), task_id)
            strikes = ledger.count_requeues_window(batch, ops.spin_window_seconds)
            ledger.update_flight(existing["id"], {
                "state": ("SPINNING" if analysis.spin_key_active(strikes, ops.spin_threshold) else "RUNNING"),
                "last_event_at": time.time(), "last_event_kind": "claimed",
                "board": board,
                # a re-claim is a fresh run: drop the previous run's residue
                "ended_at": 0, "summary": "", "exit_reason": "",
            })
            return
        board = truncate(payload.get("board"), 64) or None
        batch = batch_key_for("kanban", truncate(payload.get("board"), 64), task_id)
        strikes = ledger.count_requeues_window(batch, ops.spin_window_seconds)
        ledger.record_flight({
            "source": "kanban",
            "state": ("SPINNING" if analysis.spin_key_active(strikes, ops.spin_threshold) else "RUNNING"),
            "role": "kanban",
            "goal": truncate(payload.get("task_id"), GOAL_MAX_CHARS),  # task id only; task text is not in the payload
            "batch_key": batch,
            "task_id": task_id,
            "board": board,
            "last_event_kind": "claimed",
        })

    def _kanban_row(task_id: str) -> Any:
        return ledger.find_by_task(task_id)

    def on_kanban_completed(**payload: Any) -> None:
        task_id = truncate(payload.get("task_id"), 128)
        if not task_id:
            return
        flight = _kanban_row(task_id)
        if flight is not None:
            ledger.update_flight(flight["id"], {
                "state": "SUCCEEDED", "ended_at": time.time(),
                "last_event_at": time.time(), "last_event_kind": "completed",
                "summary": truncate(payload.get("summary"), 200),
            })
        else:
            # completion for a task we never saw claimed (plugin loaded
            # mid-run): record the terminal fact itself.
            ledger.record_flight({
                "source": "kanban", "state": "SUCCEEDED", "ended_at": time.time(),
                "role": "kanban", "goal": truncate(task_id, GOAL_MAX_CHARS),
                "task_id": task_id, "board": truncate(payload.get("board"), 64) or None,
                "summary": truncate(payload.get("summary"), 200),
                "last_event_kind": "completed",
            })

    def on_kanban_blocked(**payload: Any) -> None:
        task_id = truncate(payload.get("task_id"), 128)
        if not task_id:
            return
        flight = _kanban_row(task_id)
        reason = truncate(payload.get("reason"), 64)
        if flight is not None:
            ledger.update_flight(flight["id"], {
                "state": "FAILED", "ended_at": time.time(),
                "last_event_at": time.time(), "last_event_kind": "blocked",
                "exit_reason": reason or "blocked",
            })
        else:
            ledger.record_flight({
                "source": "kanban", "state": "FAILED", "ended_at": time.time(),
                "role": "kanban", "goal": truncate(task_id, GOAL_MAX_CHARS),
                "task_id": task_id, "board": truncate(payload.get("board"), 64) or None,
                "exit_reason": reason or "blocked",
                "last_event_kind": "blocked",
            })
        if flight is not None and flight.get("batch_key") and _looks_quota(reason):
            ledger.record_requeue(flight["batch_key"], reason or "blocked", flight["id"])

    def on_kanban_worker_exited(**payload: Any) -> None:
        task_id = truncate(payload.get("task_id"), 128)
        exit_kind = truncate(payload.get("exit_kind"), 64)
        if not task_id or not exit_kind:
            return
        flight = _kanban_row(task_id)
        if exit_kind in ("rate_limited", "signaled", "nonzero_exit") and flight is not None:
            ledger.update_flight(flight["id"], {
                "state": "FAILED", "ended_at": time.time(),
                "last_event_at": time.time(), "last_event_kind": "worker_exit",
                "exit_reason": exit_kind,
            })
            if flight.get("batch_key"):
                ledger.record_requeue(flight["batch_key"], exit_kind, flight["id"])

    for name, cb in (
        ("subagent_start", on_subagent_start),
        ("subagent_stop", on_subagent_stop),
        ("pre_tool_call", on_pre_tool_call),
        ("post_tool_call", on_post_tool_call),
        ("pre_api_request", on_pre_api_request),
        ("post_api_request", on_post_api_request),
        ("api_request_error", on_api_request_error),
        ("pre_auxiliary_call", on_pre_auxiliary_call),
        ("post_auxiliary_call", on_post_auxiliary_call),
        ("on_session_finalize", on_session_finalize),
        ("kanban_task_claimed", on_kanban_claimed),
        ("kanban_task_completed", on_kanban_completed),
        ("kanban_task_blocked", on_kanban_blocked),
        ("on_kanban_worker_exited", on_kanban_worker_exited),
    ):
        try:
            ctx.register_hook(name, cb)
        except Exception:  # noqa: BLE001 - an unknown hook name on an older core
            logger.debug("muster: hook %s not registrable on this Hermes", name)

    # ---- tools + command ---------------------------------------------------

    try:
        ctx.register_tool(
            "muster_fanout", TOOLSET_NAME, _FANOUT_SCHEMA, ops.fanout,
            description="Launch supervised child agents (Hermes subagent lifecycle) and track them in muster.",
            emoji="🛫",
        )
        ctx.register_tool(
            "muster_status", TOOLSET_NAME, _STATUS_SCHEMA, ops.status,
            description="Report the muster fleet: every delegated child, its state, tokens and health.",
            emoji="📋",
        )
        ctx.register_command("muster", ops.command,
                             description="Fleet status and control for delegated children (muster).",
                             args_hint="[cancel|steer <id> ...]")
    except Exception:  # noqa: BLE001
        logger.exception("muster: tool/command registration failed")

    # ---- stall daemon --------------------------------------------------------

    stop_event = threading.Event()
    thread = threading.Thread(target=_stall_loop,
                              args=(ledger, ops, stop_event), name="muster-stall", daemon=True)
    thread.start()

    _REGISTERED.clear()
    _REGISTERED.append({"ledger": ledger, "stop": stop_event, "ops": ops})


TOOLSET_NAME = "muster"


def _open_ledger(ctx: Any) -> Ledger:
    from .paths import muster_data_dir
    return Ledger(muster_data_dir() / "fleet.sqlite")


def _looks_quota(reason: str) -> bool:
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in ("rate", "quota", "429", "limit"))


def _find_pending_muster(ledger: Ledger, parent_session_id: str, role: str, goal: str) -> dict[str, Any] | None:
    def _op(conn) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM flights WHERE source = 'muster' AND state = 'PENDING' "
            "AND child_session_id IS NULL AND goal = :goal AND role = :role "
            "AND (parent_session_id = :parent OR parent_session_id = '' OR parent_session_id IS NULL) "
            "AND started_at >= :cutoff ORDER BY started_at DESC LIMIT 1",
            {"goal": truncate(goal, GOAL_MAX_CHARS), "role": truncate(role, 64),
             "parent": truncate(parent_session_id, 128), "cutoff": time.time() - 600.0},
        ).fetchone()
        return dict(row) if row else None

    try:
        return ledger._read(_op)
    except Exception:  # noqa: BLE001
        return None


def _find_live_by_child(ledger: Ledger, child_session_id: str) -> dict[str, Any] | None:
    def _op(conn) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM flights WHERE child_session_id = :sid "
            "AND state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING') "
            "ORDER BY started_at DESC LIMIT 1",
            {"sid": truncate(child_session_id, 128)},
        ).fetchone()
        return dict(row) if row else None

    try:
        return ledger._read(_op)
    except Exception:  # noqa: BLE001
        return None


def _stall_loop_pass(ops: FleetOps) -> None:
    """One stall pass over EVERY live row (not just the board window):
    derive STALLED badges, self-heal stale ones, age out launches whose
    start event never arrived."""
    ledger = ops.ledger
    now = time.time()
    threshold = ops.stall_seconds
    pending_cutoff = max(120.0, threshold)
    for flight in ledger.live_flights():
        state = flight.get("state")
        if state == "PENDING" and flight.get("source") == "muster":
            # a muster launch that never started (refused/lost race): stop
            # ghosting the run count after the cutoff.
            started = flight.get("started_at") or 0
            if now - float(started or 0) > pending_cutoff:
                ledger.update_flight(flight["id"], {
                    "state": "FAILED", "ended_at": now,
                    "exit_reason": "launch_never_started",
                })
            continue
        if analysis.is_stalled(flight, now, threshold):
            ledger.update_flight(flight["id"], {
                "state": "STALLED", "last_event_kind": flight.get("last_event_kind") or "stalled",
            })
        elif analysis.stall_recovery_needed(flight, now, threshold):
            ledger.update_flight(flight["id"], {"state": "RUNNING"})


def _stall_loop(ledger: Ledger, ops: FleetOps, stop_event: threading.Event) -> None:
    """5s daemon wrapper around :func:`_stall_loop_pass`; never blocks a turn."""
    while not stop_event.wait(5.0):
        try:
            _stall_loop_pass(ops)
        except Exception:  # noqa: BLE001 - the daemon must survive anything
            logger.debug("muster: stall pass failed", exc_info=True)


_FANOUT_SCHEMA = {
    "name": "muster_fanout",
    "description": (
        "Launch one or more supervised child Hermes agents through the public subagent "
        "lifecycle API and track them in muster. Each child runs to completion "
        "asynchronously; watch them with /muster or muster_status, and cancel or steer "
        "them from the Desktop pane or /muster cancel|steer. Use for parallel research, "
        "reviews or long checks you want visible and controllable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short, self-contained goal per child (80 chars each).",
            },
            "context": {
                "type": "string",
                "description": "Optional shared context for every child.",
            },
            "role": {
                "type": "string",
                "description": "Child role; defaults to 'leaf'.",
            },
            "model": {
                "type": "string",
                "description": "Optional model override per child.",
            },
            "provider": {
                "type": "string",
                "description": "Optional provider override per child.",
            },
            "allowed_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional toolset narrowing (the only restriction the lifecycle API accepts).",
            },
        },
        "required": ["goals"],
    },
}

_STATUS_SCHEMA = {
    "name": "muster_status",
    "description": (
        "Report the muster fleet: every delegated child agent with its state "
        "(running/stalled/spinning/lost/terminal), last-event age, tool calls, "
        "tokens and spin strikes. Use before and after delegating to check fleet health."
    ),
    "parameters": {"type": "object", "properties": {}},
}
