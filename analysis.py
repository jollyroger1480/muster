"""Muster analysis — pure classification over flight rows.

No I/O, no clocks of its own: every function takes ``now`` so tests can drive
time deterministically.
"""

from __future__ import annotations

from typing import Any, Optional

# Map the structured child-completion contract (tools/delegate_tool.py:
# status ∈ {completed, interrupted, failed}) onto ledger states.
STOP_STATUS_TO_STATE = {
    "completed": "SUCCEEDED",
    "succeeded": "SUCCEEDED",
    "ok": "SUCCEEDED",
    "interrupted": "INTERRUPTED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "failed": "FAILED",
    "error": "FAILED",
    "timeout": "FAILED",
}

LIVE_STATES = ("PENDING", "STARTING", "RUNNING", "STALLED", "SPINNING")
# States the stall thread is allowed to move in or out of.
STALL_ELIGIBLE = ("RUNNING",)


def stop_status_to_state(payload: dict[str, Any]) -> str:
    """Pick a terminal state from a subagent_stop payload without guessing.

    Checks the documented fields in order; an unknown payload maps to FAILED
    only when a failure marker is present, otherwise INTERRUPTED stays
    reserved for explicit interruption. Anything unrecognized -> 'UNKNOWN'
    and the caller keeps the previous state.
    """
    for key in ("status", "child_status", "outcome", "exit_reason"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            state = STOP_STATUS_TO_STATE.get(value.strip().lower())
            if state:
                return state
            if value.strip().lower() in ("rate_limited",):
                return "FAILED"
    if payload.get("failed") is True or payload.get("error"):
        return "FAILED"
    if payload.get("completed") is True:
        return "SUCCEEDED"
    return "UNKNOWN"


def is_stalled(flight: dict[str, Any], now: float, stall_seconds: float) -> bool:
    """A live flight is stalled when its last API/tool event is older than the
    threshold AND no tool call is in flight (a quiet child may be a long
    script — in-flight tools keep the row honest instead)."""
    if flight.get("kind") != "flight" or flight.get("state") not in STALL_ELIGIBLE:
        return False
    if flight.get("tool_in_flight"):
        return False
    last = flight.get("last_event_at") or flight.get("started_at") or 0
    try:
        return (now - float(last)) > stall_seconds
    except (TypeError, ValueError):
        return False


def stall_recovery_needed(flight: dict[str, Any], now: float, stall_seconds: float) -> bool:
    """A STALLED row that got fresh activity (or has an in-flight tool) goes
    back to RUNNING — the badge self-heals, it is never sticky."""
    if flight.get("state") != "STALLED":
        return False
    last = flight.get("last_event_at") or 0
    fresh = False
    try:
        fresh = (now - float(last)) <= stall_seconds
    except (TypeError, ValueError):
        fresh = False
    return bool(flight.get("tool_in_flight")) or fresh


def spin_key_active(requeue_count: int, spin_threshold: int) -> bool:
    return spin_threshold > 0 and requeue_count >= spin_threshold


def last_event_age(flight: dict[str, Any], now: float) -> Optional[float]:
    last = flight.get("last_event_at") or flight.get("started_at")
    if not last:
        return None
    try:
        return max(0.0, now - float(last))
    except (TypeError, ValueError):
        return None


def chip_counts(board: dict[str, Any]) -> dict[str, int]:
    """Aggregate a board() payload into the four numbers the chip shows.
    SPINNING is reported only as `spin` — never also counted as working."""
    counts = board.get("counts") or {}
    return {
        "run": sum(counts.get(s, 0) for s in ("PENDING", "STARTING", "RUNNING")),
        "stall": counts.get("STALLED", 0),
        "lost": counts.get("LOST", 0),
        "spin": counts.get("SPINNING", 0),
    }
