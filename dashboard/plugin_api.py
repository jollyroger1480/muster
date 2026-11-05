"""Muster dashboard backend — routes mounted at /api/plugins/muster/.

Reads the same sqlite ledger the agent-side hooks write (WAL mode makes
cross-process reads safe) and, when this process is the one that owns the
subagent-lifecycle registry, executes cancel/steer actions. In a split
deployment where the dashboard server is not the gateway process, the
lifecycle service reports UNKNOWN_HANDLE and the route answers with a clear
"actions unavailable here" instead of pretending.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger(__name__)

_LIVE_STATES = ("PENDING", "STARTING", "RUNNING", "STALLED", "SPINNING")


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_config() -> dict[str, Any]:
    """Read plugins.entries.muster.settings without mutating anything."""
    settings: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore

        cfg = load_config_readonly() or {}
        settings = ((cfg.get("plugins") or {}).get("entries") or {}).get("muster", {}).get("settings") or {}
    except Exception:  # noqa: BLE001 - fall back to schema defaults
        pass
    defaults = {
        "stall_seconds": 300,
        "spin_threshold": 3,
        "spin_window_seconds": 1800,
        "show_goals": True,
        "board_rows": 120,
    }
    out = dict(defaults)
    for key in defaults:
        if key in settings:
            try:
                out[key] = type(defaults[key])(settings[key])
            except (TypeError, ValueError):
                continue
    return out


def _ledger():
    import sys

    root = str(_plugin_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    from ledger import Ledger  # noqa: E402 - plugin-local module
    from paths import muster_data_dir  # noqa: E402

    return Ledger(muster_data_dir() / "fleet.sqlite")


def _service():
    """The lifecycle facade is stateless over a process-local registry, so the
    same construction ctx.subagent_lifecycle uses is correct here: it works
    when this process runs agent turns and answers UNKNOWN_HANDLE otherwise."""
    try:
        from agent.subagent_lifecycle import SubagentLifecycleService, get_active_subagent_parent
    except ImportError:
        return None
    try:
        return SubagentLifecycleService(get_active_subagent_parent)
    except Exception:  # noqa: BLE001
        return None


@router.get("/health")
async def health() -> dict[str, Any]:
    try:
        _ledger()
        ledger_ok = True
    except Exception:  # noqa: BLE001
        ledger_ok = False
    return {"ok": ledger_ok, "actions": _service() is not None, "plugin": "muster"}


@router.get("/board")
async def board() -> dict[str, Any]:
    cfg = _load_config()
    try:
        data = _ledger().board(limit=cfg["board_rows"], spin_window_seconds=cfg["spin_window_seconds"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("muster: board read failed", exc_info=True)
        return {"generated_at": 0, "counts": {}, "live_total": 0, "requeues_1h": 0,
                "flights": [], "aux": [], "error": f"ledger unavailable: {type(exc).__name__}",
                "show_goals": cfg["show_goals"], "service_available": False}
    data["show_goals"] = cfg["show_goals"]
    data["service_available"] = _service() is not None
    return data


def _handle_for(ledger: Any, flight_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    flight = ledger.find_flight(flight_id)
    if not flight or not flight.get("handle_json"):
        return flight, None
    try:
        handle = json.loads(flight["handle_json"])
    except (TypeError, ValueError):
        return flight, None
    return flight, handle if isinstance(handle, dict) else None


@router.post("/action/cancel")
async def action_cancel(payload: dict[str, Any]) -> dict[str, Any]:
    flight_id = str(payload.get("flight_id") or "")
    reason = str(payload.get("reason") or "muster dashboard")
    if not flight_id:
        return {"ok": False, "message": "flight_id required"}
    ledger = _ledger()
    flight, handle = _handle_for(ledger, flight_id)
    if flight is None:
        return {"ok": False, "message": f"no flight matching {flight_id[:8]}"}
    if handle is None:
        return {"ok": False,
                "message": f"{flight['id'][:8]} is an observed child (source: {flight.get('source')}) — "
                           "only muster-launched children carry a handle. Cancel from the parent session."}
    service = _service()
    if service is None:
        return {"ok": False, "message": "subagent lifecycle service not reachable from this process"}
    try:
        result = service.cancel(handle, reason=reason)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"cancel failed: {type(exc).__name__}"}
    unknown = bool(getattr(result, "unknown_handle", False))
    if unknown:
        return {"ok": False, "message": "handle not known to this process (gateway restarted?) — "
                                        "the row will be marked LOST on next boot"}
    terminal = bool(getattr(result, "already_terminal", False))
    if not terminal:
        ledger.update_flight(flight["id"], {"state": "CANCEL_REQUESTED", "last_event_kind": "cancel_requested"})
    return {"ok": True, "message": ("already finished" if terminal
                                    else "cancel accepted — child interrupts at its next safe boundary")}


@router.post("/action/steer")
async def action_steer(payload: dict[str, Any]) -> dict[str, Any]:
    flight_id = str(payload.get("flight_id") or "")
    text = str(payload.get("text") or payload.get("reason") or "").strip()
    if not flight_id or not text:
        return {"ok": False, "message": "flight_id and text required"}
    ledger = _ledger()
    flight, handle = _handle_for(ledger, flight_id)
    if flight is None:
        return {"ok": False, "message": f"no flight matching {flight_id[:8]}"}
    if handle is None:
        return {"ok": False, "message": "observed child — steering is only available for muster launches"}
    service = _service()
    if service is None:
        return {"ok": False, "message": "subagent lifecycle service not reachable from this process"}
    try:
        accepted = bool(service.steer(handle, text))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"steer failed: {type(exc).__name__}"}
    if not accepted:
        return {"ok": False, "message": "child is not accepting steering right now"}
    ledger.update_flight(flight["id"], {"last_event_kind": "steered"})
    return {"ok": True, "message": "steering queued for the child's next iteration boundary"}
