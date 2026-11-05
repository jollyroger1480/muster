"""Muster ledger — durable fleet state, one writer thread, zero dependencies.

Every hook callback only *enqueues* a small, allowlisted dict here; a single
writer thread owns the sqlite connection so a slow disk never blocks an agent
turn. Readers share a second connection opened in WAL mode.

The schema intentionally stores metadata only: ids, roles, truncated goals,
timestamps, counters, tokens. Raw hook payloads (requests, conversation
history, tool arguments, tool results, error bodies) are never persisted.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

GOAL_MAX_CHARS = 80
SUMMARY_MAX_CHARS = 200

# Terminal and live flight states (superset of the public SubagentState names
# plus Muster's derived states: STALLED, SPINNING, LOST).
LIVE_STATES = ("PENDING", "STARTING", "RUNNING", "STALLED", "SPINNING")
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "INTERRUPTED", "CANCEL_REQUESTED", "CANCELLED", "LOST")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL DEFAULT 'flight',   -- 'flight' | 'aux'
    source            TEXT NOT NULL DEFAULT 'observed', -- 'muster' | 'observed' | 'kanban'
    parent_session_id TEXT,
    child_session_id  TEXT,
    subagent_id       TEXT,
    correlation_id    TEXT,
    batch_key         TEXT,
    role              TEXT,
    goal              TEXT,
    model             TEXT,
    provider          TEXT,
    state             TEXT NOT NULL DEFAULT 'RUNNING',
    started_at        REAL,
    ended_at          REAL,
    last_event_at     REAL,
    last_event_kind   TEXT,
    tool_in_flight    TEXT NOT NULL DEFAULT '',
    tool_calls        INTEGER NOT NULL DEFAULT 0,
    api_calls         INTEGER NOT NULL DEFAULT 0,
    error_count       INTEGER NOT NULL DEFAULT 0,
    last_status_code  INTEGER,
    last_error_class  TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    exit_reason       TEXT,
    summary           TEXT,
    requeue_count     INTEGER NOT NULL DEFAULT 0,
    handle_json       TEXT,
    task_id           TEXT,
    board             TEXT,
    updated_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_flights_child ON flights(child_session_id);
CREATE INDEX IF NOT EXISTS idx_flights_parent ON flights(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_flights_state ON flights(state);
CREATE INDEX IF NOT EXISTS idx_flights_batch ON flights(batch_key);
CREATE INDEX IF NOT EXISTS idx_flights_task ON flights(task_id);
CREATE INDEX IF NOT EXISTS idx_flights_started ON flights(started_at DESC);
CREATE TABLE IF NOT EXISTS requeues (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL,
    at        REAL NOT NULL,
    reason    TEXT,
    flight_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_requeues_batch ON requeues(batch_key, at);
"""

# Allowlisted columns hooks may write, with coercion and per-column caps.
# Anything outside this set is dropped — raw payload text never reaches the
# ledger, and bounded text keeps rows small.
_TEXT_CAPS = {
    "goal": GOAL_MAX_CHARS,
    "summary": SUMMARY_MAX_CHARS,
    "last_error_class": 64,
    "last_event_kind": 32,
    "tool_in_flight": 64,
}
_DEFAULT_TEXT_CAP = 128
_TEXT_COLS = {
    "kind", "source", "parent_session_id", "child_session_id", "subagent_id",
    "correlation_id", "batch_key", "role", "goal", "model", "provider",
    "state", "last_event_kind", "tool_in_flight", "exit_reason", "summary",
    "last_error_class", "handle_json", "task_id", "board",
}
_INT_COLS = {
    "tool_calls", "api_calls", "error_count", "last_status_code",
    "prompt_tokens", "completion_tokens", "requeue_count",
}
_REAL_COLS = {"started_at", "ended_at", "last_event_at", "updated_at"}

def _set_int(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _set_text(v: Any) -> str:
    return "" if v is None else str(v)


_SETTERS: dict[str, Callable[[Any], Any]] = {}
for _c in _TEXT_COLS:
    _SETTERS[_c] = _set_text
for _c in _INT_COLS:
    _SETTERS[_c] = _set_int
for _c in _REAL_COLS:
    _SETTERS[_c] = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _coerce_int(value: Any) -> int:
    return _set_int(value)


def coerce_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Filter an incoming update dict down to known columns with safe values."""
    clean: dict[str, Any] = {}
    for key, value in updates.items():
        setter = _SETTERS.get(key)
        if setter is None:
            continue
        try:
            clean[key] = setter(value)
        except (TypeError, ValueError):
            continue
    for key, cap in _TEXT_CAPS.items():
        if key in clean:
            clean[key] = clean[key][:cap]
    return clean


def truncate(text: Any, limit: int) -> str:
    if text is None:
        return ""
    return str(text)[:limit]


def batch_key_for(parent_session_id: str, role: str, goal: str) -> str:
    """Stable per-goal key used for spin detection (not a secret, not user PII
    beyond the truncated goal already stored in the row)."""
    raw = f"{parent_session_id or ''}|{role or ''}|{goal or ''}"
    # stdlib sha1 is fine here: the key only needs stability, not security.
    import hashlib
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


class Ledger:
    """sqlite-backed fleet ledger with a single writer thread."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=busy_timeout_ms / 1000.0
        )
        self._write_conn.row_factory = sqlite3.Row
        self._write_conn.execute("PRAGMA journal_mode=WAL")
        self._write_conn.execute("PRAGMA synchronous=NORMAL")
        self._write_conn.executescript(_SCHEMA)
        self._write_conn.commit()
        # Dedicated WAL read connection: readers never share a connection with
        # the writer thread (each sqlite3 connection is used by one thread only).
        self._read_conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=busy_timeout_ms / 1000.0
        )
        self._read_conn.row_factory = sqlite3.Row
        self._read_conn.execute("PRAGMA journal_mode=WAL")
        self._read_lock = threading.Lock()
        self._queue: "queue.Queue[Optional[tuple[Callable[[sqlite3.Connection], None], Optional[tuple]]]]" = queue.Queue()
        self._closed = threading.Event()
        self._writer = threading.Thread(target=self._writer_loop, name="muster-ledger-writer", daemon=True)
        self._writer.start()
        # Last-write insurance: a hard exit (gateway killed, interpreter shutdown)
        # drains pending hook writes instead of losing them with the daemon.
        import atexit
        atexit.register(self._drain_at_exit)

    def _drain_at_exit(self) -> None:
        """Bounded drain: never hang interpreter shutdown on a wedged writer."""
        deadline = time.time() + 3.0
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.02)

    # -- writer ----------------------------------------------------------

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._closed.is_set():
                    break
                continue
            try:
                if item is None:
                    break
                fn, done = item
                try:
                    fn(self._write_conn)
                    self._write_conn.commit()
                except sqlite3.Error:
                    logger.debug("muster: ledger write failed", exc_info=True)
                    try:
                        self._write_conn.rollback()
                    except sqlite3.Error:
                        pass
                finally:
                    if done is not None:
                        done.set()
            finally:
                self._queue.task_done()

    def _submit(self, fn: Callable[[sqlite3.Connection], None], wait: bool = False) -> None:
        done = threading.Event() if wait else None
        self._queue.put((fn, done))
        if done is not None:
            done.wait(timeout=10)

    def close(self) -> None:
        if self._closed.is_set():
            return
        # Drain pending writes BEFORE the sentinel: anything enqueued after the
        # sentinel would never reach task_done() and could hang join()s forever.
        deadline = time.time() + 3.0
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.02)
        self._closed.set()
        self._queue.put(None)
        self._writer.join(timeout=5)
        try:
            self._write_conn.close()
        except sqlite3.Error:
            pass
        try:
            self._read_conn.close()
        except sqlite3.Error:
            pass

    # -- hooks API (fast, non-blocking) -----------------------------------

    def record_flight(self, row: dict[str, Any]) -> str:
        flight_id = row.get("id") or uuid.uuid4().hex
        now = time.time()
        values = {
            "id": flight_id,
            "kind": row.get("kind", "flight"),
            "source": row.get("source", "observed"),
            "parent_session_id": truncate(row.get("parent_session_id"), 128),
            "child_session_id": truncate(row.get("child_session_id"), 128) or None,
            "subagent_id": truncate(row.get("subagent_id"), 128) or None,
            "correlation_id": truncate(row.get("correlation_id"), 128) or None,
            "batch_key": truncate(row.get("batch_key"), 32) or None,
            "role": truncate(row.get("role"), 64),
            "goal": truncate(row.get("goal"), GOAL_MAX_CHARS),
            "model": truncate(row.get("model"), 128) or None,
            "provider": truncate(row.get("provider"), 64) or None,
            "state": row.get("state", "RUNNING"),
            "started_at": float(row.get("started_at", now)),
            "last_event_at": float(row.get("last_event_at", now)),
            "last_event_kind": truncate(row.get("last_event_kind"), 32),
            "task_id": truncate(row.get("task_id"), 128) or None,
            "board": truncate(row.get("board"), 64) or None,
            "handle_json": row.get("handle_json"),
            "updated_at": now,
        }
        self._submit(lambda conn: conn.execute(
            f"INSERT OR IGNORE INTO flights ({', '.join(values)}) VALUES ({', '.join(':' + k for k in values)})",
            values,
        ))
        return flight_id

    def update_flight(self, flight_id: str, updates: dict[str, Any]) -> None:
        clean = coerce_updates(updates)
        if not clean:
            return
        clean["updated_at"] = time.time()
        self._submit(lambda conn: conn.execute(
            f"UPDATE flights SET {', '.join(k + ' = :' + k for k in clean)} WHERE id = :fid",
            {**clean, "fid": flight_id},
        ))

    def touch_by_session(self, session_id: str, kind: str, *,
                         api_calls: int = 0, prompt_tokens: int = 0,
                         completion_tokens: int = 0, tool_calls: int = 0,
                         error_count: int = 0, status_code: int | None = None,
                         error_class: str = "", tool_in_flight: str | None = None,
                         also_parent: bool = False) -> None:
        """Fast-path touch used by LLM/tool hooks. Enqueues one statement."""
        now = time.time()
        sets = ["last_event_at = :now", "last_event_kind = :kind", "updated_at = :now"]
        params: dict[str, Any] = {"now": now, "kind": truncate(kind, 32), "sid": truncate(session_id, 128)}
        if api_calls:
            sets.append("api_calls = api_calls + :ac")
            params["ac"] = _coerce_int(api_calls)
        if tool_calls:
            sets.append("tool_calls = tool_calls + :tc")
            params["tc"] = _coerce_int(tool_calls)
        if error_count:
            sets.append("error_count = error_count + :ec")
            params["ec"] = _coerce_int(error_count)
        if prompt_tokens:
            sets.append("prompt_tokens = prompt_tokens + :pt")
            params["pt"] = _coerce_int(prompt_tokens)
        if completion_tokens:
            sets.append("completion_tokens = completion_tokens + :ct")
            params["ct"] = _coerce_int(completion_tokens)
        if status_code is not None:
            sets.append("last_status_code = :sc")
            params["sc"] = _coerce_int(status_code)
        if error_class:
            sets.append("last_error_class = :errc")
            params["errc"] = truncate(error_class, 64)
        if tool_in_flight is not None:
            sets.append("tool_in_flight = :tif")
            params["tif"] = truncate(tool_in_flight, 64)

        targets = [":sid"]
        if also_parent:
            # session ids observed from aux hooks are parents; children carry
            # their own ids, so one match attempt on child_session_id plus a
            # fallback match on parent_session_id covers both shapes.
            self._submit(lambda conn: conn.execute(
                f"UPDATE flights SET {', '.join(sets)} WHERE child_session_id = :sid AND state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING')",
                params,
            ))
            self._submit(lambda conn: conn.execute(
                f"UPDATE flights SET {', '.join(sets)} WHERE parent_session_id = :sid AND kind = 'aux' AND state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING')",
                params,
            ))
        else:
            self._submit(lambda conn: conn.execute(
                f"UPDATE flights SET {', '.join(sets)} WHERE child_session_id = :sid AND state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING')",
                params,
            ))

    def record_requeue(self, batch_key: str, reason: str, flight_id: str = "") -> None:
        self._submit(lambda conn: conn.execute(
            "INSERT INTO requeues (batch_key, at, reason, flight_id) VALUES (:bk, :at, :reason, :fid)",
            {"bk": truncate(batch_key, 32), "at": time.time(),
             "reason": truncate(reason, 64), "fid": truncate(flight_id, 64)},
        ))

    def count_requeues(self, conn: sqlite3.Connection, batch_key: str, window_seconds: float) -> int:
        cutoff = time.time() - max(1.0, float(window_seconds))
        row = conn.execute(
            "SELECT COUNT(*) FROM requeues WHERE batch_key = :bk AND at >= :cutoff",
            {"bk": truncate(batch_key, 32), "cutoff": cutoff},
        ).fetchone()
        return int(row[0]) if row else 0

    def count_requeues_window(self, batch_key: str, window_seconds: float) -> int:
        """Read-path strike count (drains pending writes first)."""
        def _op(conn: sqlite3.Connection) -> int:
            return self.count_requeues(conn, batch_key, window_seconds)
        return self._read(_op) or 0

    def find_by_task(self, task_id: str) -> Optional[dict[str, Any]]:
        def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
            row = conn.execute(
                "SELECT * FROM flights WHERE task_id = :tid AND kind = 'flight' ORDER BY started_at DESC LIMIT 1",
                {"tid": truncate(task_id, 128)},
            ).fetchone()
            return dict(row) if row else None

        return self._read(_op)

    def claim_flight(self, flight_id: str, updates: dict[str, Any]) -> bool:
        """Atomically claim a PENDING row (attach race safety): applies
        ``updates`` only if the row is still PENDING. Returns True on claim."""
        clean = coerce_updates(updates)
        if not clean:
            return False
        clean["updated_at"] = time.time()
        claimed = threading.Event()
        result: list[bool] = []

        def _op(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                f"UPDATE flights SET {', '.join(k + ' = :' + k for k in clean)} "
                "WHERE id = :fid AND state = 'PENDING'",
                {**clean, "fid": flight_id},
            )
            result.append(cur.rowcount > 0)

        self._submit(_op, wait=True)
        return bool(result and result[0])

    def live_flights(self) -> list[dict[str, Any]]:
        """Every live flight row, newest activity first — no LIMIT, so the
        stall pass can age out and badge rows beyond the board window."""
        def _op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM flights WHERE kind = 'flight' "
                    "AND state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING') "
                    "ORDER BY COALESCE(last_event_at, started_at, updated_at) DESC"
                )
            ]

        return self._read(_op) or []

    # -- maintenance -------------------------------------------------------

    def reconcile_lost(self) -> int:
        """Rows left live by a dead process become LOST. Never relaunches."""
        now = time.time()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE flights SET state = 'LOST', ended_at = :now, exit_reason = 'process_died', updated_at = :now "
                "WHERE kind = 'flight' AND state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING')",
                {"now": now},
            )
            conn.execute(
                "UPDATE flights SET state = 'ENDED', ended_at = :now, updated_at = :now "
                "WHERE kind = 'aux' AND state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING')",
                {"now": now},
            )

        self._submit(_op, wait=True)
        return 0

    # -- reads --------------------------------------------------------------

    def _read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        # read-your-writes: bounded drain of pending writes before reading
        deadline = time.time() + 2.0
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.005)
        with self._read_lock:
            try:
                return fn(self._read_conn)
            except sqlite3.Error:
                logger.debug("muster: ledger read failed", exc_info=True)
                return None

    def board(self, limit: int = 120, spin_window_seconds: float = 1800.0) -> dict[str, Any]:
        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            now = time.time()
            counts: dict[str, int] = {}
            for row in conn.execute(
                "SELECT state, COUNT(*) FROM flights WHERE kind = 'flight' GROUP BY state"
            ):
                counts[row[0]] = int(row[1])
            flights = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM flights WHERE kind = 'flight' "
                    "ORDER BY (state IN ('PENDING','STARTING','RUNNING','STALLED','SPINNING')) DESC, "
                    "COALESCE(last_event_at, started_at, updated_at) DESC LIMIT :lim",
                    {"lim": _coerce_int(limit)},
                )
            ]
            aux = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM flights WHERE kind = 'aux' "
                    "ORDER BY COALESCE(last_event_at, updated_at) DESC LIMIT 40"
                )
            ]
            # refresh spin counts for live rows so the board is current even
            # without a fresh start event.
            for flight in flights:
                if flight.get("state") in ("RUNNING", "STALLED", "SPINNING") and flight.get("batch_key"):
                    flight["requeue_count"] = self.count_requeues(
                        conn, flight["batch_key"], spin_window_seconds
                    )
            requeues_1h = int(conn.execute(
                "SELECT COUNT(*) FROM requeues WHERE at >= :cutoff",
                {"cutoff": now - 3600.0},
            ).fetchone()[0])
            return {
                "generated_at": now,
                "counts": counts,
                "live_total": sum(counts.get(s, 0) for s in ("PENDING", "STARTING", "RUNNING", "STALLED", "SPINNING")),
                "requeues_1h": requeues_1h,
                "flights": flights,
                "aux": aux,
            }

        return self._read(_op) or {
            "generated_at": time.time(), "counts": {}, "live_total": 0,
            "requeues_1h": 0, "flights": [], "aux": [],
        }

    def find_flight(self, flight_id_prefix: str) -> Optional[dict[str, Any]]:
        def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
            row = conn.execute(
                "SELECT * FROM flights WHERE id = :fid OR (id LIKE :pat AND :plen > 5)",
                {"fid": flight_id_prefix, "pat": flight_id_prefix + "%", "plen": len(flight_id_prefix)},
            ).fetchone()
            return dict(row) if row else None

        return self._read(_op)

    def handle_for_flight(self, flight_id_prefix: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        """Return (flight_row, handle_dict) for a muster-launched child."""
        flight = self.find_flight(flight_id_prefix)
        if not flight or not flight.get("handle_json"):
            return flight, None
        try:
            handle = json.loads(flight["handle_json"])
        except (TypeError, ValueError):
            return flight, None
        return flight, handle if isinstance(handle, dict) else None
