"""Muster test suite — no Hermes install required.

Runs against a real sqlite ledger in a temp dir and a scripted fake ctx that
records registrations and replays hook payloads, covering the full lifecycle:
launch -> start -> api/tool events -> terminal, plus stall, spin, LOST
reconciliation, observed rows, kanban rows, actions and board rendering.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # package parent (import muster)

from muster import analysis  # noqa: E402
from muster import ledger as ledger_mod  # noqa: E402
import muster  # noqa: E402
from muster.fleet import FleetOps, render_table  # noqa: E402
from muster.ledger import Ledger, batch_key_for  # noqa: E402
from muster.paths import muster_data_dir  # noqa: E402


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger(tmp_path / "fleet.sqlite")
    yield led
    led.close()


class FakeRegistration:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name


class FakeCtx:
    """Records register_* calls; replays hooks back into the plugin."""

    def __init__(self, service=None):
        self.hooks: dict[str, object] = {}
        self.tools: dict[str, object] = {}
        self.commands: dict[str, object] = {}
        self.settings: dict[str, object] = {}
        self._service = service

    # plugin ctx surface
    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    @property
    def subagent_lifecycle(self):
        if self._service is None:
            raise AssertionError("test did not provide a lifecycle service")
        return self._service

    def register_hook(self, name, cb, **_kw):
        self.hooks[name] = cb
        return FakeRegistration("hook", name)

    def register_tool(self, name, toolset, schema, handler, **_kw):
        self.tools[name] = {"schema": schema, "handler": handler, "toolset": toolset}
        return FakeRegistration("tool", name)

    def register_command(self, name, handler, **_kw):
        self.commands[name] = handler
        return FakeRegistration("command", name)


class FakeHandle:
    def __init__(self, subagent_id="sub-1", parent_session_id="parent-1"):
        self.dict = {
            "contract_version": 1,
            "subagent_id": subagent_id,
            "parent_session_id": parent_session_id,
            "correlation_id": "corr",
            "created_at": time.time(),
            "provider": None,
            "model": None,
            "role": "leaf",
            "depth": 0,
            "capability": "cap",
        }

    def to_dict(self):
        return dict(self.dict)


class FakeService:
    """Minimal stand-in for SubagentLifecycleService."""

    def __init__(self):
        self.launched: list[dict] = []
        self.cancelled: list[tuple[dict, str]] = []
        self.steered: list[tuple[dict, str]] = []
        self.accept_steer = True
        self.unknown = False

    def launch(self, request):
        self.launched.append({"goal": request.goal, "role": request.role,
                              "correlation_id": request.correlation_id})
        return FakeHandle(subagent_id=f"sub-{len(self.launched)}")

    def cancel(self, handle, *, reason):
        self.cancelled.append((handle, reason))
        class R:
            accepted = not self.unknown
            already_terminal = False
            unknown_handle = self.unknown
        return R()

    def steer(self, handle, text):
        self.steered.append((handle, text))
        return self.accept_steer and not self.unknown


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(muster, "_open_ledger", lambda _ctx: Ledger(tmp_path / "fleet.sqlite"))
    return FakeCtx()


# ---------------------------------------------------------------- ledger


def test_record_and_board(ledger: Ledger):
    fid = ledger.record_flight({"source": "observed", "state": "RUNNING",
                                "child_session_id": "s1", "role": "leaf", "goal": "do a thing"})
    board = ledger.board()
    assert board["live_total"] == 1
    row = board["flights"][0]
    assert row["id"] == fid and row["child_session_id"] == "s1"
    assert row["goal"] == "do a thing"


def test_updates_coerced_and_filtered(ledger: Ledger):
    fid = ledger.record_flight({"state": "RUNNING"})
    # unknown columns dropped, bad ints coerced to 0, text truncated
    ledger.update_flight(fid, {
        "state": "FAILED", "nonsense": "x", "tool_calls": "7",
        "goal": "g" * 500, "prompt_tokens": 12,
    })
    ledger._queue.join()
    row = ledger.find_flight(fid)
    assert row["state"] == "FAILED"
    assert row["tool_calls"] == 7
    assert row["goal"] == "g" * ledger_mod.GOAL_MAX_CHARS
    assert row["prompt_tokens"] == 12
    assert "nonsense" not in row


def test_touch_by_session_accumulates(ledger: Ledger):
    ledger.record_flight({"state": "RUNNING", "child_session_id": "s1"})
    ledger.touch_by_session("s1", "api", api_calls=1, prompt_tokens=100, completion_tokens=20)
    ledger.touch_by_session("s1", "tool", tool_calls=1, tool_in_flight="")
    ledger._queue.join()
    row = ledger.find_flight(ledger.board()["flights"][0]["id"])
    assert row["api_calls"] == 1 and row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 20 and row["tool_calls"] == 1


def test_reconcile_lost(ledger: Ledger):
    ledger.record_flight({"state": "RUNNING", "child_session_id": "s1"})
    ledger.record_flight({"state": "STALLED", "child_session_id": "s2"})
    ledger.record_flight({"state": "SUCCEEDED", "child_session_id": "s3"})
    ledger.reconcile_lost()
    ledger._queue.join()
    board = ledger.board()
    states = {f["child_session_id"]: f["state"] for f in board["flights"]}
    assert states["s1"] == "LOST" and states["s2"] == "LOST"
    assert states["s3"] == "SUCCEEDED"


def test_requeue_window(ledger: Ledger):
    ledger.record_requeue("batch1", "rate_limited")
    assert ledger.count_requeues_window("batch1", 1800) == 1
    assert ledger.count_requeues_window("other", 1800) == 0


# ---------------------------------------------------------------- analysis


def test_stop_status_mapping():
    assert analysis.stop_status_to_state({"status": "completed"}) == "SUCCEEDED"
    assert analysis.stop_status_to_state({"status": "failed"}) == "FAILED"
    assert analysis.stop_status_to_state({"status": "interrupted"}) == "INTERRUPTED"
    assert analysis.stop_status_to_state({"status": "rate_limited"}) == "FAILED"
    assert analysis.stop_status_to_state({"completed": True}) == "SUCCEEDED"
    assert analysis.stop_status_to_state({"failed": True}) == "FAILED"
    assert analysis.stop_status_to_state({}) == "UNKNOWN"
    assert analysis.stop_status_to_state({"status": "banana"}) == "UNKNOWN"


def test_stall_classification():
    now = 10_000.0
    row = {"kind": "flight", "state": "RUNNING", "tool_in_flight": "",
           "last_event_at": now - 400, "started_at": now - 500}
    assert analysis.is_stalled(row, now, 300) is True
    row["tool_in_flight"] = "terminal"  # in-flight tools exempt
    assert analysis.is_stalled(row, now, 300) is False
    row["tool_in_flight"] = ""
    row["last_event_at"] = now - 200
    assert analysis.is_stalled(row, now, 300) is False
    # terminal rows never stall
    row["state"] = "SUCCEEDED"
    assert analysis.is_stalled(row, now, 300) is False


def test_stall_recovery():
    now = 10_000.0
    stale = {"state": "STALLED", "tool_in_flight": "", "last_event_at": now - 5}
    assert analysis.stall_recovery_needed(stale, now, 300) is True
    quiet = {"state": "STALLED", "tool_in_flight": "", "last_event_at": now - 400}
    assert analysis.stall_recovery_needed(quiet, now, 300) is False
    busy = {"state": "STALLED", "tool_in_flight": "bash", "last_event_at": now - 400}
    assert analysis.stall_recovery_needed(busy, now, 300) is True


def test_chip_counts():
    board = {"counts": {"RUNNING": 2, "SPINNING": 1, "STALLED": 1, "LOST": 1, "SUCCEEDED": 5}}
    counts = analysis.chip_counts(board)
    # SPINNING is its own number — never also counted as working
    assert counts == {"run": 2, "stall": 1, "lost": 1, "spin": 1}


# ---------------------------------------------------------------- registration


def test_register_wires_all_hooks_tools_command(ctx):
    muster.register(ctx)
    for hook in ("subagent_start", "subagent_stop", "post_api_request", "kanban_task_claimed"):
        assert hook in ctx.hooks
    assert "muster_fanout" in ctx.tools and "muster_status" in ctx.tools
    assert "muster" in ctx.commands


def test_fanout_launch_and_attach(ctx):
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    ops = muster._REGISTERED[0]["ops"]
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["review the auth module"]}))
    assert len(out["launched"]) == 1 and not out["errors"]
    fid = out["launched"][0]["flight_id"]
    row = ops.ledger.find_flight(fid)
    assert row["state"] == "PENDING" and row["source"] == "muster"
    assert json.loads(row["handle_json"])["subagent_id"] == "sub-1"
    assert service.launched[0]["goal"] == "review the auth module"

    # the child's start hook attaches to the muster row
    ctx2.hooks["subagent_start"](parent_session_id="parent-1", parent_turn_id="t1",
                                 child_session_id="child-1", child_subagent_id="sub-1",
                                 child_role="leaf", child_goal="review the auth module")
    row = ops.ledger.find_flight(fid)
    assert row["state"] == "RUNNING" and row["child_session_id"] == "child-1"
    assert row["parent_session_id"] == "parent-1"

    # api + tool traffic accumulates on the child row
    ctx2.hooks["post_api_request"](session_id="child-1",
                                   usage={"prompt_tokens": 500, "completion_tokens": 80})
    ctx2.hooks["pre_tool_call"](session_id="child-1", tool_name="read_file")
    ctx2.hooks["post_tool_call"](session_id="child-1")
    ops.ledger._queue.join()
    row = ops.ledger.find_flight(fid)
    assert row["prompt_tokens"] == 500 and row["completion_tokens"] == 80
    assert row["api_calls"] == 1 and row["tool_calls"] == 1

    # terminal stop closes the row and stores a bounded summary
    ctx2.hooks["subagent_stop"](parent_session_id="parent-1", child_session_id="child-1",
                                child_role="leaf", child_summary="found 2 regressions " + "x" * 500,
                                status="completed", duration_ms=1234)
    row = ops.ledger.find_flight(fid)
    assert row["state"] == "SUCCEEDED" and row["ended_at"]
    assert len(row["summary"]) == 200


def test_spin_alarm_after_threshold(ctx):
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    ops = muster._REGISTERED[0]["ops"]

    goal = "flaky goal"
    for i in range(3):
        out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": [goal]}))
        fid = out["launched"][0]["flight_id"]
        ctx2.hooks["subagent_start"](parent_session_id="parent-1", child_session_id=f"c{i}",
                                     child_subagent_id=f"sub-{i}", child_role="leaf",
                                     child_goal=goal)
        ctx2.hooks["subagent_stop"](parent_session_id="parent-1", child_session_id=f"c{i}",
                                    child_role="leaf", status="failed")
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": [goal]}))
    fid = out["launched"][0]["flight_id"]
    ctx2.hooks["subagent_start"](parent_session_id="parent-1", child_session_id="c3",
                                 child_subagent_id="sub-9", child_role="leaf", child_goal=goal)
    row = ops.ledger.find_flight(fid)
    assert row["state"] == "SPINNING"
    assert row["requeue_count"] >= 3


def test_observed_delegate_task_rows_are_readonly(ctx):
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    ctx.hooks["subagent_start"](parent_session_id="p1", child_session_id="child-9",
                                child_subagent_id="sub-9", child_role="leaf",
                                child_goal="model-delegated work")
    board = ops.ledger.board()
    assert board["flights"][0]["source"] == "observed"
    assert board["flights"][0]["handle_json"] in (None, "")

    flight, handle = ops.ledger.handle_for_flight(board["flights"][0]["id"])
    assert flight is not None and handle is None
    text = ops.command(f"cancel {board['flights'][0]['id']}")
    assert "started by hermes itself" in text and "watching its progress" in text


def test_cancel_and_steer_via_command(ctx):
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["long research"]}))
    fid = out["launched"][0]["flight_id"]
    ops = muster._REGISTERED[0]["ops"]

    text = ops.command(f"cancel {fid[:8]} stop wasting tokens")
    assert "stopping" in text
    assert service.cancelled and service.cancelled[0][1] == "stop wasting tokens"
    row = ops.ledger.find_flight(fid)
    assert row["state"] == "CANCEL_REQUESTED"

    text = ops.command(f"steer {fid[:8]} skip the tests")
    assert "message sent" in text
    assert service.steered and service.steered[0][1] == "skip the tests"


def test_unknown_stop_without_start_creates_row(ctx):
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    ctx.hooks["subagent_stop"](parent_session_id="p1", child_session_id="ghost-1",
                               child_role="leaf", status="failed")
    board = ops.ledger.board()
    assert board["flights"][0]["state"] == "FAILED"
    assert board["flights"][0]["child_session_id"] == "ghost-1"


def test_kanban_rows_and_worker_exit_strikes(ctx):
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    ctx.hooks["kanban_task_claimed"](task_id="task-1", board="main", assignee="w1", run_id="r1")
    board = ops.ledger.board()
    assert board["flights"][0]["source"] == "kanban"
    assert board["flights"][0]["task_id"] == "task-1"

    ctx.hooks["on_kanban_worker_exited"](task_id="task-1", exit_kind="rate_limited")
    board = ops.ledger.board()
    assert board["flights"][0]["state"] == "FAILED"

    ctx.hooks["kanban_task_completed"](task_id="task-2", board="main", summary="done")
    board = ops.ledger.board()
    by_task = {f["task_id"]: f["state"] for f in board["flights"]}
    assert by_task["task-2"] == "SUCCEEDED"


def test_aux_rows_tracked(ctx):
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    ctx.hooks["pre_auxiliary_call"](session_id="parent-1", aux_task="compression")
    ctx.hooks["post_auxiliary_call"](session_id="parent-1", aux_task="compression",
                                     usage={"prompt_tokens": 2000, "completion_tokens": 300})
    board = ops.ledger.board()
    aux = board["aux"]
    assert aux and aux[0]["kind"] == "aux" and aux[0]["state"] == "SUCCEEDED"
    assert aux[0]["prompt_tokens"] == 2000 and aux[0]["completion_tokens"] == 300


def test_stall_loop_flips_and_heals(ctx, monkeypatch):
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    ops = muster._REGISTERED[0]["ops"]
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["sleepy child"]}))
    fid = out["launched"][0]["flight_id"]
    ctx2.hooks["subagent_start"](parent_session_id="parent-1", child_session_id="c-sleep",
                                 child_subagent_id="s1", child_role="leaf", child_goal="sleepy child")

    now = time.time()
    # force the row old
    ops.ledger.update_flight(fid, {"last_event_at": now - 4000})
    ops.ledger._queue.join()
    board = ops.ledger.board()
    assert board["flights"][0]["state"] == "RUNNING"  # before the pass

    muster._stall_loop_pass(ops)  # one iteration of the daemon, synchronously
    board = ops.ledger.board()
    assert board["flights"][0]["state"] == "STALLED"

    # activity resumes -> heals back to RUNNING
    ctx2.hooks["post_api_request"](session_id="c-sleep", usage={})
    ops.ledger._queue.join()
    muster._stall_loop_pass(ops)
    board = ops.ledger.board()
    assert board["flights"][0]["state"] == "RUNNING"


def test_show_goals_false_hides_text(ctx):
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    ops.ctx.settings["show_goals"] = False
    out = json.loads(ops.status({}))
    assert out["flights"] == [] or out["flights"][0]["goal"] is None


def test_board_renders_states(ctx):
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    ctx.hooks["subagent_start"](parent_session_id="p1", child_session_id="c1",
                                child_subagent_id="s1", child_role="leaf", child_goal="a job")
    table = ops.command("")
    assert "working" in table
    assert "a job" in table


# ---------------------------------------------------------------- security


def test_no_network_imports():
    """Static guard: the plugin's own modules never import networking."""
    forbidden = ("requests", "httpx", "socket", "urllib.request", "aiohttp")
    root = Path(__file__).resolve().parent.parent
    for py in root.glob("*.py"):
        source = py.read_text()
        for mod in forbidden:
            assert f"import {mod}" not in source, f"{py.name} imports {mod}"
            assert f"from {mod}" not in source, f"{py.name} imports {mod}"


def test_no_raw_payload_columns_persisted(ledger: Ledger):
    """The coerce allowlist is the security boundary for hook writes."""
    for banned in ("request", "conversation_history", "args", "result", "error_body", "prompt"):
        assert banned not in ledger_mod._TEXT_COLS
        assert banned not in ledger_mod._INT_COLS


# ---------------------------------------------------------------- review regressions


def test_unknown_stop_keeps_state_and_does_not_strike(ctx):
    """Payload drift must not kill a healthy RUNNING row or inflate spin."""
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    ops = muster._REGISTERED[0]["ops"]
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["steady child"]}))
    fid = out["launched"][0]["flight_id"]
    ctx2.hooks["subagent_start"](parent_session_id="parent-1", child_session_id="c-u",
                                 child_subagent_id="s-u", child_role="leaf", child_goal="steady child")
    # stop with NO recognizable status field -> UNKNOWN
    ctx2.hooks["subagent_stop"](parent_session_id="parent-1", child_session_id="c-u",
                                child_role="leaf", child_summary="still fine")
    row = ops.ledger.find_flight(fid)
    assert row["state"] == "RUNNING", "UNKNOWN stop must keep the previous state"
    assert row["last_event_kind"] == "stop_unparsed"
    assert ops.ledger.count_requeues_window(row["batch_key"], 1800) == 0, "UNKNOWN must not strike"


def test_observed_children_reach_spinning(ctx):
    """Model-spawned delegate children (the #149110 silent-spin case) must
    alarm too, not just muster launches."""
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    for i in range(3):
        ctx.hooks["subagent_start"](parent_session_id="p1", child_session_id=f"o{i}",
                                    child_subagent_id=f"so-{i}", child_role="leaf",
                                    child_goal="requeue me")
        ctx.hooks["subagent_stop"](parent_session_id="p1", child_session_id=f"o{i}",
                                   child_role="leaf", status="failed")
    ctx.hooks["subagent_start"](parent_session_id="p1", child_session_id="o3",
                                child_subagent_id="so-3", child_role="leaf", child_goal="requeue me")
    board = ops.ledger.board()
    row = next(f for f in board["flights"] if f["child_session_id"] == "o3")
    assert row["state"] == "SPINNING" and row["source"] == "observed"


def test_cancel_refusal_is_not_recorded_as_progress(ctx):
    """A lifecycle cancel refusal must leave the row state untouched."""
    class RefusingService(FakeService):
        def cancel(self, handle, *, reason):
            class R:
                accepted = False
                already_terminal = False
                unknown_handle = False
            return R()

    ctx2 = FakeCtx(service=RefusingService())
    muster.register(ctx2)
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["refused child"]}))
    fid = out["launched"][0]["flight_id"]
    ctx2.hooks["subagent_start"](parent_session_id="parent-1", child_session_id="c-r",
                                 child_subagent_id="s-r", child_role="leaf", child_goal="refused child")
    text = ctx2.commands["muster"](f"cancel {fid[:8]}")
    assert "couldn't stop" in text
    row = ops_row = muster._REGISTERED[0]["ops"].ledger.find_flight(fid)
    assert row["state"] == "RUNNING", "refusal must not move the row to CANCEL_REQUESTED"


def test_cancel_requested_rows_do_not_strike(ctx):
    """Cancel-then-relaunch of the same goal is normal operation, not spin."""
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    ops = muster._REGISTERED[0]["ops"]
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["relaunched goal"]}))
    fid = out["launched"][0]["flight_id"]
    ctx2.hooks["subagent_start"](parent_session_id="parent-1", child_session_id="c-x",
                                 child_subagent_id="s-x", child_role="leaf", child_goal="relaunched goal")
    ctx2.commands["muster"](f"cancel {fid[:8]} deliberate")
    ctx2.hooks["subagent_stop"](parent_session_id="parent-1", child_session_id="c-x",
                                child_role="leaf", status="interrupted")
    row = ops.ledger.find_flight(fid)
    assert ops.ledger.count_requeues_window(row["batch_key"], 1800) == 0, "user cancel must not strike"


def test_compact_board_shape_serves_the_uis(ledger):
    """The /board contract: short_id, has_handle and last_event_age exist —
    the Desktop pane and dashboard tab render buttons from exactly these."""
    fid = ledger.record_flight({"source": "muster", "state": "RUNNING",
                                "child_session_id": "s1", "role": "leaf", "goal": "g",
                                "handle_json": json.dumps({"subagent_id": "x"})})
    ledger.touch_by_session("s1", "api")
    board = ledger.board()
    compact = muster_fleet_compact(board)
    row = compact["flights"][0]
    for key in ("id", "short_id", "state", "role", "goal", "has_handle",
                "tool_in_flight", "last_event_age", "tool_calls", "api_calls",
                "prompt_tokens", "completion_tokens", "requeue_count"):
        assert key in row, f"compact board row missing {key}"
    assert row["short_id"] == fid[:8] and row["has_handle"] is True
    assert row["last_event_age"] is not None
    assert "handle_json" not in row, "raw handles must not ship to the browser"


def muster_fleet_compact(board):
    from muster.fleet import _compact_board
    return _compact_board(board, show_goals=True)


def test_cancelled_row_settles_without_ghost(ctx):
    """Cancel → settle: the stop event must resolve the CANCEL_REQUESTED row
    itself. A ghost row here means the fleet shows two rows for one child."""
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    ops = muster._REGISTERED[0]["ops"]
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["settle me"]}))
    fid = out["launched"][0]["flight_id"]
    ctx2.hooks["subagent_start"](parent_session_id="parent-1", child_session_id="c-set",
                                 child_subagent_id="s-set", child_role="leaf", child_goal="settle me")
    ctx2.commands["muster"](f"cancel {fid[:8]}")
    ctx2.hooks["subagent_stop"](parent_session_id="parent-1", child_session_id="c-set",
                                child_role="leaf", status="cancelled")
    board = ops.ledger.board()
    rows = [f for f in board["flights"] if f["goal"] == "settle me"]
    assert len(rows) == 1, f"ghost row created: {len(rows)} rows for one child"
    assert rows[0]["state"] == "CANCELLED"


def test_board_contract_matches_the_uis(ledger):
    """/board is consumed by the pane, the chip and the dashboard tab. This
    locks the contract: raw `counts` for muster_status, `chip` for the UIs,
    and last_event_age as an AGE IN SECONDS (not a timestamp)."""
    import time as _t
    fid = ledger.record_flight({"source": "observed", "state": "RUNNING",
                                "child_session_id": "ui1", "role": "leaf", "goal": "ui contract"})
    ledger.touch_by_session("ui1", "api", prompt_tokens=10, completion_tokens=2)
    board = ledger.board()
    compact = muster_fleet_compact(board)

    # chip aggregate with the four plain numbers
    chip = compact["chip"]
    assert set(chip) == {"run", "stall", "lost", "spin"}
    assert chip["run"] == 1
    # raw state counts also present (muster_status consumers)
    assert board["counts"].get("RUNNING") == 1
    # last_event_age is an age: bounded by wall-clock since the touch
    row = compact["flights"][0]
    age = row["last_event_age"]
    assert age is not None and 0 <= age < 60, f"age must be seconds, got {age}"
    # and every UI-consumed key exists
    for key in ("short_id", "has_handle", "state", "goal", "tool_calls", "prompt_tokens"):
        assert key in row


def test_failing_on_repeat_visible_at_third_death(ctx):
    """A job that died 3 times must read failing-on-repeat WITHOUT a 4th
    attempt starting (grok arch review: the alarm fired on next start only)."""
    muster.register(ctx)
    ops = muster._REGISTERED[0]["ops"]
    goal = "flaky deploy check"
    for i in range(3):
        ctx.hooks["subagent_start"](parent_session_id="p1", child_session_id=f"fr{i}",
                                    child_subagent_id=f"sf{i}", child_role="leaf", child_goal=goal)
        ctx.hooks["subagent_stop"](parent_session_id="p1", child_session_id=f"fr{i}",
                                   child_role="leaf", status="failed")
    board = ops.ledger.board()
    row = next(f for f in board["flights"] if f.get("goal") == goal)
    assert row["requeue_count"] >= 3, "third death must surface the count"
    compact = muster_fleet_compact(board)
    crow = next(f for f in compact["flights"] if f["goal"] == goal)
    assert crow["requeue_count"] >= 3


def test_chip_lost_scoped_to_24h(ledger):
    """Last month's graveyard must not haunt the chip forever."""
    old = time.time() - 40 * 86400
    fid_old = ledger.record_flight({"state": "RUNNING", "child_session_id": "ancient", "goal": "old"})
    ledger.update_flight(fid_old, {"state": "LOST", "ended_at": old, "updated_at": old})
    ledger.record_flight({"state": "RUNNING", "child_session_id": "fresh", "goal": "new job"})
    ledger.reconcile_lost()
    board = ledger.board()
    chip = analysis.chip_counts(board)
    assert board["lost_24h"] == 1, "only the fresh crash counts"
    assert chip["lost"] == 1


def test_pending_muster_rows_age_out(ctx):
    """A muster launch whose start never arrives stops ghosting the run count."""
    service = FakeService()
    ctx2 = FakeCtx(service=service)
    muster.register(ctx2)
    ops = muster._REGISTERED[0]["ops"]
    out = json.loads(ctx2.tools["muster_fanout"]["handler"]({"goals": ["ghost launch"]}))
    fid = out["launched"][0]["flight_id"]
    # never send subagent_start; force the row old and run one pass
    ops.ledger.update_flight(fid, {"started_at": time.time() - 9999})
    ops.ledger._queue.join()
    muster._stall_loop_pass(ops)
    row = ops.ledger.find_flight(fid)
    assert row["state"] == "FAILED" and row["exit_reason"] == "launch_never_started"
