"""Muster dashboard backend — routes mounted at /api/plugins/muster/.

Serves the COMPACT board shape the Desktop pane and dashboard tab consume
(``fleet._compact_board``), reads the same sqlite ledger the agent-side hooks
write (WAL mode makes cross-process reads safe) through a single cached
Ledger instance, and, when this process runs agent turns, executes
cancel/steer through the public subagent lifecycle API. Refusals are reported
honestly — never recorded as progress.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import types
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger(__name__)

BOARD_ROWS_MAX = 500


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


# One Ledger per dashboard process: the per-request construction leaked a
# sqlite connection + writer thread + atexit hook on every poll.
_LEDGER: Any = None
_LEDGER_LOCK = threading.Lock()


def _load_plugin_module(name: str):
    """Import a plugin-local module under a synthetic package namespace so its
    relative imports (``from .analysis import …``) resolve — never by sys.path
    injection, which would expose generic top-level names (``ledger``,
    ``paths``) to the whole web-server process."""
    root = _plugin_root()
    pkg_name = "_muster_plugin"
    pkg = sys.modules.get(pkg_name)
    if pkg is None:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(root)]
        sys.modules[pkg_name] = pkg
    full = f"{pkg_name}.{name}"
    module = sys.modules.get(full)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(full, root / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load muster plugin module {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


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
        "chip_enabled": True,
    }
    out = dict(defaults)
    for key in defaults:
        if key in settings:
            try:
                out[key] = type(defaults[key])(settings[key])
            except (TypeError, ValueError):
                continue
    out["board_rows"] = max(10, min(int(out["board_rows"]), BOARD_ROWS_MAX))
    return out


def _ledger():
    global _LEDGER
    if _LEDGER is None:
        with _LEDGER_LOCK:
            if _LEDGER is None:
                ledger_mod = _load_plugin_module("ledger")
                paths_mod = _load_plugin_module("paths")
                _LEDGER = ledger_mod.Ledger(paths_mod.muster_data_dir() / "fleet.sqlite")
    return _LEDGER


def _fleet():
    return _load_plugin_module("fleet")


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
    """Compact board shape — the same one muster_status serves — so the pane,
    the chip and the web tab all render ids, ages and action buttons."""
    cfg = _load_config()
    empty = {"generated_at": 0, "counts": {}, "live_total": 0, "requeues_1h": 0,
             "flights": [], "aux": []}
    try:
        raw = _ledger().board(limit=cfg["board_rows"], spin_window_seconds=cfg["spin_window_seconds"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("muster: board read failed", exc_info=True)
        return {**empty, "error": f"ledger unavailable: {type(exc).__name__}",
                "show_goals": cfg["show_goals"], "service_available": False,
                "chip_enabled": cfg["chip_enabled"]}
    try:
        # The UI contract: compact rows only. Never fall back to raw sqlite
        # rows — a shape the pane cannot render is worse than an explicit
        # error payload.
        data = _fleet()._compact_board(raw, show_goals=cfg["show_goals"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("muster: board compaction failed", exc_info=True)
        return {"generated_at": 0, "counts": {}, "live_total": 0, "requeues_1h": 0,
                "flights": [], "aux": [], "error": f"board compaction failed: {type(exc).__name__}",
                "show_goals": cfg["show_goals"], "service_available": False,
                "chip_enabled": cfg["chip_enabled"]}
    data["show_goals"] = cfg["show_goals"]
    data["chip_enabled"] = cfg["chip_enabled"]
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
        return {"ok": False, "message": f"no worker called “{flight_id[:8]}” — check /muster for ids"}
    if handle is None:
        return {"ok": False,
                "message": "that one was started by hermes itself — muster only watches it. stop it from the chat that started it."}
    service = _service()
    if service is None:
        return {"ok": False, "message": "stop/message buttons work where the agent runs — this window can't reach them"}
    try:
        result = service.cancel(handle, reason=reason)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"cancel failed: {type(exc).__name__}"}
    if bool(getattr(result, "unknown_handle", False)):
        return {"ok": False, "message": "that worker's stop button died with the last restart — it shows as gone after start-up"}
    if bool(getattr(result, "already_terminal", False)):
        return {"ok": True, "message": "already finished"}
    if not bool(getattr(result, "accepted", False)):
        # refused by the lifecycle service — never fake progress
        detail = str(getattr(result, "diagnostic", "") or "refused by the lifecycle service")
        return {"ok": False, "message": f"couldn't stop it ({detail}). its row is unchanged."}
    ledger.update_flight(flight["id"], {"state": "CANCEL_REQUESTED", "last_event_kind": "cancel_requested"})
    return {"ok": True, "message": "stopping — it finishes its current step, then quits safely"}


@router.post("/action/steer")
async def action_steer(payload: dict[str, Any]) -> dict[str, Any]:
    flight_id = str(payload.get("flight_id") or "")
    text = str(payload.get("text") or payload.get("reason") or "").strip()
    if not flight_id or not text:
        return {"ok": False, "message": "flight_id and text required"}
    ledger = _ledger()
    flight, handle = _handle_for(ledger, flight_id)
    if flight is None:
        return {"ok": False, "message": f"no worker called “{flight_id[:8]}” — check /muster for ids"}
    if handle is None:
        return {"ok": False, "message": "that one was started by hermes itself — muster only watches it"}
    service = _service()
    if service is None:
        return {"ok": False, "message": "stop/message buttons work where the agent runs — this window can't reach them"}
    try:
        accepted = bool(service.steer(handle, text))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"steer failed: {type(exc).__name__}"}
    if not accepted:
        return {"ok": False, "message": "it can't take messages right now (just finished, or already gone)"}
    ledger.update_flight(flight["id"], {"last_event_kind": "steered"})
    return {"ok": True, "message": "message sent — it reads it at its next step"}
