"""A persistent retry queue for actions that failed (create an issue, push an edit,
move a state, add a link or a comment).

Entries are keyed on (kind, key), for example ("push_edit", "<plane item id>"). Putting
the same kind and key again replaces the payload: only the latest intent runs. Retries
back off quickly (seconds, then up to about two minutes), and an entry that has waited
longer than max_age is reported once through on_stale while it keeps retrying.

The queue lives in the bridge's SQLite database, so it survives restarts.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

log = logging.getLogger("bridge")

DEFAULT_BACKOFF = (5, 15, 30, 60, 120)


def ensure_schema(store) -> None:
    with store._lock:
        store._db.execute(
            """CREATE TABLE IF NOT EXISTS retry_queue (
                   id             INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind           TEXT NOT NULL,
                   key            TEXT NOT NULL,
                   payload        TEXT NOT NULL,
                   reason         TEXT NOT NULL DEFAULT '',
                   attempts       INTEGER NOT NULL DEFAULT 0,
                   first_seen     REAL NOT NULL,
                   updated_at     REAL NOT NULL,
                   next_at        REAL NOT NULL,
                   stale_reported INTEGER NOT NULL DEFAULT 0,
                   UNIQUE (kind, key))""")
        store._db.commit()


@dataclass
class Entry:
    id: int
    kind: str
    key: str
    payload: dict
    reason: str
    attempts: int
    first_seen: float
    updated_at: float
    next_at: float
    stale_reported: bool


_COLS = "id, kind, key, payload, reason, attempts, first_seen, updated_at, next_at, stale_reported"


def _entry(row) -> Entry:
    return Entry(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5],
                 row[6], row[7], row[8], bool(row[9]))


class RetryQueue:
    def __init__(self, store, clock: Callable[[], float] = time.time,
                 backoff: tuple[float, ...] = DEFAULT_BACKOFF, max_age: float = 300,
                 on_stale: Callable[[Entry], None] | None = None):
        if not backoff:
            raise ValueError("backoff needs at least one step")
        self.store, self.clock, self.backoff = store, clock, tuple(backoff)
        self.max_age, self.on_stale = max_age, on_stale
        ensure_schema(store)

    # ---------------------------------------------------------------- writes

    def put(self, kind: str, key: str, payload: dict, reason: str = "") -> None:
        """Queue an action, or replace the queued one for the same (kind, key).
        The latest payload wins, first_seen and attempts are kept, and it is due now."""
        now = self.clock()
        data = json.dumps(payload, sort_keys=True)
        with self.store._lock:
            self.store._db.execute(
                """INSERT INTO retry_queue (kind, key, payload, reason, attempts,
                                            first_seen, updated_at, next_at, stale_reported)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0)
                   ON CONFLICT (kind, key) DO UPDATE SET
                       payload = excluded.payload, reason = excluded.reason,
                       updated_at = excluded.updated_at, next_at = excluded.next_at""",
                (kind, key, data, reason, now, now, now))
            self.store._db.commit()

    def done(self, entry_id: int) -> None:
        with self.store._lock:
            self.store._db.execute("DELETE FROM retry_queue WHERE id = ?", (entry_id,))
            self.store._db.commit()

    def failed(self, entry_id: int, reason: str) -> Entry | None:
        """Record a failed attempt and schedule the next one. Returns the updated entry."""
        now = self.clock()
        with self.store._lock:
            row = self.store._db.execute(
                f"SELECT {_COLS} FROM retry_queue WHERE id = ?", (entry_id,)).fetchone()
            if not row:
                return None
            e = _entry(row)
            attempts = e.attempts + 1
            delay = self.backoff[min(attempts - 1, len(self.backoff) - 1)]
            report = (not e.stale_reported) and (now - e.first_seen > self.max_age)
            self.store._db.execute(
                """UPDATE retry_queue SET attempts = ?, reason = ?, updated_at = ?, next_at = ?,
                                          stale_reported = ? WHERE id = ?""",
                (attempts, reason, now, now + delay, int(e.stale_reported or report), entry_id))
            self.store._db.commit()
            row = self.store._db.execute(
                f"SELECT {_COLS} FROM retry_queue WHERE id = ?", (entry_id,)).fetchone()
        updated = _entry(row)
        if report and self.on_stale:
            try:
                self.on_stale(updated)
            except Exception:   # a broken reporter must not stop the queue
                log.exception("retry queue: reporting a stale entry failed")
        return updated

    # ---------------------------------------------------------------- reads

    def due(self, limit: int = 20) -> list[Entry]:
        """Entries whose next attempt is due, oldest first."""
        with self.store._lock:
            rows = self.store._db.execute(
                f"SELECT {_COLS} FROM retry_queue WHERE next_at <= ? "
                "ORDER BY first_seen, id LIMIT ?", (self.clock(), limit)).fetchall()
        return [_entry(r) for r in rows]

    def get(self, kind: str, key: str) -> Entry | None:
        with self.store._lock:
            row = self.store._db.execute(
                f"SELECT {_COLS} FROM retry_queue WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        return _entry(row) if row else None

    def stats(self) -> dict:
        now = self.clock()
        with self.store._lock:
            n, oldest, stale = self.store._db.execute(
                "SELECT COUNT(*), MIN(first_seen), COALESCE(SUM(stale_reported), 0) "
                "FROM retry_queue").fetchone()
        return {"length": n, "oldest_age": round(now - oldest, 1) if oldest is not None else 0.0,
                "stale": int(stale)}


def short_reason(e: Exception) -> str:
    """Status and path for HTTP errors, the exception type otherwise. Never response bodies."""
    if isinstance(e, httpx.HTTPStatusError):
        r = e.response
        return f"{r.status_code} from {r.request.method} {r.request.url.path}"
    if isinstance(e, httpx.HTTPError):
        try:
            return f"{type(e).__name__} on {e.request.method} {e.request.url.path}"
        except RuntimeError:     # no request attached
            return type(e).__name__
    return type(e).__name__


def run_due(queue: RetryQueue, handlers: dict[str, Callable[[dict], None]], *,
            limit: int = 20, stop: Callable[[Exception], bool] = lambda exc: False) -> dict:
    """Run the due entries once. Success removes an entry; a failure reschedules it.
    If stop(exc) is true (a rate limit, say), the run ends after recording that failure."""
    summary = {"done": 0, "failed": 0, "stopped": False}
    for e in queue.due(limit):
        handler = handlers.get(e.kind)
        if handler is None:
            queue.failed(e.id, f"no handler for {e.kind}")
            summary["failed"] += 1
            continue
        try:
            handler(e.payload)
        except Exception as exc:
            queue.failed(e.id, short_reason(exc))
            summary["failed"] += 1
            if stop(exc):
                summary["stopped"] = True
                break
        else:
            queue.done(e.id)
            summary["done"] += 1
    return summary
