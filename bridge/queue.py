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
                   version        INTEGER NOT NULL DEFAULT 0,
                   held_by        TEXT NOT NULL DEFAULT '',
                   UNIQUE (kind, key))""")
        cols = {r[1] for r in store._db.execute("PRAGMA table_info(retry_queue)")}
        if "version" not in cols:
            store._db.execute("ALTER TABLE retry_queue ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
        if "held_by" not in cols:
            # The credential whose rate limit an entry is waiting out ('' = none).
            store._db.execute("ALTER TABLE retry_queue ADD COLUMN held_by TEXT NOT NULL DEFAULT ''")
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
    version: int = 0        # bumped by every put: tells a merged-in payload from the one that ran


_COLS = ("id, kind, key, payload, reason, attempts, first_seen, updated_at, next_at, "
         "stale_reported, version")


def _entry(row) -> Entry:
    return Entry(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5],
                 row[6], row[7], row[8], bool(row[9]), row[10])


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
                       updated_at = excluded.updated_at, next_at = excluded.next_at,
                       version = retry_queue.version + 1, held_by = ''""",
                (kind, key, data, reason, now, now, now))
            self.store._db.commit()

    def hold_group(self, group: Callable[[Entry], str], key: str, until: float) -> int:
        """Every entry in this group (e.g. one GitHub credential's) waits until `until`
        without counting an attempt: it leaves the due window, so other groups'
        entries get their turn. Returns how many."""
        with self.store._lock:
            rows = self.store._db.execute(f"SELECT {_COLS} FROM retry_queue WHERE next_at < ?",
                                          (until,)).fetchall()
            ids = [r[0] for r in rows if group(_entry(r)) == key]
            self.store._db.executemany("UPDATE retry_queue SET next_at = ?, held_by = ? WHERE id = ?",
                                       [(until, key, i) for i in ids])
            self.store._db.commit()
        return len(ids)

    def held(self) -> dict[str, int]:
        """Entries waiting out a credential's rate limit, per credential (for /health)."""
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT held_by, COUNT(*) FROM retry_queue WHERE held_by != '' AND next_at > ? "
                "GROUP BY held_by", (self.clock(),)).fetchall()
        return dict(rows)

    def set_payload_field(self, kind: str, key: str, field: str, value) -> None:
        """Add a detail learned while running an entry (e.g. its repo) without
        counting it as a new intent (the version isn't bumped)."""
        with self.store._lock:
            row = self.store._db.execute("SELECT payload FROM retry_queue WHERE kind = ? AND key = ?",
                                         (kind, key)).fetchone()
            if row is None:
                return
            payload = json.loads(row[0])
            payload[field] = value
            self.store._db.execute("UPDATE retry_queue SET payload = ? WHERE kind = ? AND key = ?",
                                   (json.dumps(payload, sort_keys=True), kind, key))
            self.store._db.commit()

    def wake_owner(self, owner: str) -> int:
        """Make every entry for repos of this GitHub account due now (its credentials
        just changed). Returns how many."""
        prefix = owner.lower() + "/"
        with self.store._lock:
            cur = self.store._db.execute(
                "UPDATE retry_queue SET next_at = ? WHERE "
                "substr(lower(json_extract(payload, '$.repo')), 1, ?) = ? OR "
                "substr(lower(json_extract(payload, '$.cred_repo')), 1, ?) = ?",
                (self.clock(), len(prefix), prefix, len(prefix), prefix))
            self.store._db.commit()
            return cur.rowcount

    def done(self, entry_id: int, version: int | None = None) -> bool:
        """Remove a finished entry. With version: only if nothing was merged into it
        since it was read (a newer intent put while the handler ran stays queued,
        due now). Returns True if it was removed."""
        with self.store._lock:
            if version is None:
                cur = self.store._db.execute("DELETE FROM retry_queue WHERE id = ?", (entry_id,))
            else:
                cur = self.store._db.execute("DELETE FROM retry_queue WHERE id = ? AND version = ?",
                                             (entry_id, version))
            self.store._db.commit()
            return cur.rowcount > 0

    # A rate-limit wait is known and heals itself: it only turns into a "stuck"
    # report this long past max_age.
    rate_limit_grace = 3600.0

    def failed(self, entry_id: int, reason: str, version: int | None = None,
               rate_limited: bool = False) -> Entry | None:
        """Record a failed attempt and schedule the next one. Returns the updated entry.
        If a newer payload was merged in since `version`, it stays due now.
        rate_limited: the failure was a rate limit, which counts toward "stuck" only
        after rate_limit_grace more."""
        now = self.clock()
        with self.store._lock:
            row = self.store._db.execute(
                f"SELECT {_COLS} FROM retry_queue WHERE id = ?", (entry_id,)).fetchone()
            if not row:
                return None
            e = _entry(row)
            attempts = e.attempts + 1
            delay = self.backoff[min(attempts - 1, len(self.backoff) - 1)]
            if version is not None and e.version != version:
                delay = 0
            allowed = self.max_age + (self.rate_limit_grace if rate_limited else 0)
            report = (not e.stale_reported) and (now - e.first_seen > allowed)
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
            limit: int = 20, stop: Callable[[Exception], bool] = lambda exc: False,
            group: Callable[[Entry], str] | None = None,
            skip: Callable[[Exception, Entry], str | None] | None = None,
            hold: Callable[[Exception, str], float] | None = None,
            rate_limited: Callable[[Exception], bool] | None = None) -> dict:
    """Run the due entries once. Success removes an entry; a failure reschedules it.

    stop(exc) true (a rate limit on something every entry needs): the run ends
    after recording that failure. skip(exc, entry) returning a group key (a rate
    limit on one GitHub credential): the rest of that group's entries are left
    for the next round, untouched, and the run carries on with the others.
    group(entry) gives an entry's group key. A skipped group's entries are also
    held (moved out of the due window until hold(exc, key), by default the wait
    the error names), and the run looks past `limit` so a group with many old
    entries can't keep the others from ever running.
    """
    summary = {"done": 0, "failed": 0, "stopped": False}
    if group is not None:
        summary["skipped"] = 0
    skipped: dict[str, float] = {}
    attempted = 0
    window = queue.due(limit * 10 if group is not None else limit)
    for e in window:
        if attempted >= limit:
            break
        if group is not None and skipped and group(e) in skipped:
            summary["skipped"] += 1
            continue
        attempted += 1
        handler = handlers.get(e.kind)
        if handler is None:
            queue.failed(e.id, f"no handler for {e.kind}")
            summary["failed"] += 1
            continue
        try:
            handler(e.payload)
        except Exception as exc:
            key = skip(exc, e) if skip is not None else None
            soft = rate_limited(exc) if rate_limited is not None else key is not None
            queue.failed(e.id, short_reason(exc), version=e.version, rate_limited=soft)
            summary["failed"] += 1
            if stop(exc):
                summary["stopped"] = True
                break
            if key is not None and key not in skipped:
                until = hold(exc, key) if hold is not None else \
                    queue.clock() + min(max(float(getattr(exc, "seconds", 60) or 60), 5.0), 3600.0)
                skipped[key] = until
        else:
            queue.done(e.id, version=e.version)
            summary["done"] += 1
    if group is not None:
        for key, until in skipped.items():
            queue.hold_group(group, key, until)
    return summary
