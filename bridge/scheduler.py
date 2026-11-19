"""When to run checks: debounced per-pair checks and a self-scheduling org-wide scan.

The org-wide scan measures itself and picks its own schedule:

▸ light (quick, few API calls, little CPU, quiet host) → again in `light_every_h` hours
▸ quick but heavy (many calls, CPU spike or busy host)  → at the next `heavy_slots`
  hour, local time, at least `heavy_every_h` hours after the last scan
▸ slow (took longer than `max_light_seconds`)           → at the start of the next
  quiet-hours window, local time

Before running, `defer(load)` pushes the scan back 15 minutes if the host is busy.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LIGHT, HEAVY, SLOW = "light", "heavy", "slow"
DEFER_SECONDS = 15 * 60


class Debouncer:
    """One check per burst of edits: a key is due once it has been quiet for `delay` seconds.

    With a store, pending keys are kept in the database, so a restart doesn't lose
    a check that was waiting for its burst to settle.
    """

    def __init__(self, delay: float = 30.0, clock=time.time, store=None):
        self.delay, self.clock, self.store = delay, clock, store
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        if store is not None:
            with store._lock:
                store._db.execute("CREATE TABLE IF NOT EXISTS pending_checks "
                                  "(key TEXT PRIMARY KEY, touched_at REAL NOT NULL)")
                store._db.commit()

    def touch(self, key: str) -> None:
        now = self.clock()
        if self.store is not None:
            with self.store._lock:
                self.store._db.execute("INSERT OR REPLACE INTO pending_checks VALUES (?, ?)", (key, now))
                self.store._db.commit()
            return
        with self._lock:
            self._last[key] = now

    def due(self) -> list[str]:
        """Keys quiet for at least `delay` seconds; each is returned once, then forgotten."""
        now = self.clock()
        if self.store is not None:
            with self.store._lock:
                rows = self.store._db.execute(
                    "SELECT key FROM pending_checks WHERE touched_at <= ? ORDER BY touched_at",
                    (now - self.delay,)).fetchall()
                ready = [r[0] for r in rows]
                self.store._db.executemany("DELETE FROM pending_checks WHERE key = ?", [(k,) for k in ready])
                self.store._db.commit()
            return ready
        with self._lock:
            ready = [k for k, t in self._last.items() if now - t >= self.delay]
            for k in ready:
                del self._last[k]
        return ready

    def pending(self) -> int:
        if self.store is not None:
            with self.store._lock:
                return self.store._db.execute("SELECT COUNT(*) FROM pending_checks").fetchone()[0]
        with self._lock:
            return len(self._last)


class ScanScheduler:
    def __init__(self, clock=time.time, tz: str = "UTC", quiet_hours: tuple[int, int] = (1, 5),
                 light_every_h: float = 6, heavy_every_h: float = 12,
                 heavy_slots: tuple[int, ...] = (6, 18), max_light_seconds: float = 120,
                 max_light_calls: int = 300, max_light_cpu: float = 30.0,
                 max_light_load: float = 1.5, cpu_count: int | None = None, store=None):
        self.clock, self.store = clock, store
        self.tz = ZoneInfo(tz)
        self.quiet_start, self.quiet_end = quiet_hours
        self.light_every = light_every_h * 3600
        self.heavy_every = heavy_every_h * 3600
        self.heavy_slots = tuple(sorted(heavy_slots))
        self.max_light_seconds = max_light_seconds
        self.max_light_calls = max_light_calls
        self.max_light_cpu = max_light_cpu
        self.max_light_load = max_light_load            # load average per CPU
        self.cpus = cpu_count or os.cpu_count() or 1
        self.last_class: str | None = None
        self.last_stats: dict | None = None
        self.last_run: float | None = None
        # No history yet: the first scan waits for the quiet window (or force()).
        self.next_run: float = self._next_quiet(self.clock())
        self.quiet_only = True      # this run may only start inside the quiet window
        if store is not None:
            self._load()

    # ----------------------------------------------------------------- persistence

    def _load(self) -> None:
        with self.store._lock:
            self.store._db.execute("CREATE TABLE IF NOT EXISTS scan_state (k TEXT PRIMARY KEY, v TEXT)")
            self.store._db.commit()
            rows = dict(self.store._db.execute("SELECT k, v FROM scan_state").fetchall())
        if "state" not in rows:
            return
        st = json.loads(rows["state"])
        self.next_run = float(st["next_run"])
        self.quiet_only = bool(st.get("quiet_only"))
        self.last_class, self.last_run, self.last_stats = st.get("last_class"), st.get("last_run"), st.get("last_stats")

    def _save(self) -> None:
        if self.store is None:
            return
        st = {"next_run": self.next_run, "quiet_only": self.quiet_only, "last_class": self.last_class,
              "last_run": self.last_run, "last_stats": self.last_stats}
        with self.store._lock:
            self.store._db.execute("INSERT OR REPLACE INTO scan_state VALUES ('state', ?)", (json.dumps(st),))
            self.store._db.commit()

    def in_quiet(self, ts: float) -> bool:
        """True inside the quiet window [start, end) in local time (it may wrap midnight)."""
        h = self._local(ts).hour
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= h < self.quiet_end
        return h >= self.quiet_start or h < self.quiet_end

    # ----------------------------------------------------------------- time helpers

    def _local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)

    def _next_quiet(self, after: float) -> float:
        """Start of the next quiet window strictly after `after`."""
        local = self._local(after)
        cand = local.replace(hour=self.quiet_start, minute=0, second=0, microsecond=0)
        if cand.timestamp() <= after:
            cand = (local + timedelta(days=1)).replace(
                hour=self.quiet_start, minute=0, second=0, microsecond=0)
        return cand.timestamp()

    def _next_slot(self, not_before: float) -> float:
        """The first heavy-slot hour (local time) at or after `not_before`."""
        local = self._local(not_before)
        for day in range(0, 3):
            base = (local + timedelta(days=day)).replace(minute=0, second=0, microsecond=0)
            for hour in self.heavy_slots:
                cand = base.replace(hour=hour)
                if cand.timestamp() >= not_before:
                    return cand.timestamp()
        return not_before + self.heavy_every   # unreachable with sane slots

    # ----------------------------------------------------------------- public

    def classify(self, duration_s: float, api_calls: int, cpu_s: float, load_avg: float) -> str:
        if duration_s > self.max_light_seconds:
            return SLOW
        if (api_calls > self.max_light_calls or cpu_s > self.max_light_cpu
                or load_avg / self.cpus > self.max_light_load):
            return HEAVY
        return LIGHT

    def record(self, duration_s: float, api_calls: int, cpu_s: float, load_avg: float) -> float:
        """Record a finished scan (ending now) and work out the next run. Returns next_run."""
        now = self.clock()
        cls = self.classify(duration_s, api_calls, cpu_s, load_avg)
        self.last_class, self.last_run = cls, now
        self.last_stats = {"duration_s": round(duration_s, 1), "api_calls": api_calls,
                           "cpu_s": round(cpu_s, 2), "load_avg": round(load_avg, 2)}
        if cls == LIGHT:
            self.next_run = now + self.light_every
        elif cls == HEAVY:
            self.next_run = self._next_slot(now + self.heavy_every)
        else:
            self.next_run = self._next_quiet(now)
        self.quiet_only = cls == SLOW
        self._save()
        return self.next_run

    def retry_in(self, seconds: float) -> None:
        """A scan was cut short (rate limit, rejected key): try again soon."""
        self.next_run = self.clock() + seconds
        self._save()

    def due(self) -> bool:
        """Time for the scan. A run that must stay in the quiet hours and finds them
        over (the bridge was down, or the scan was deferred past their end) moves to
        the next quiet window instead of running in the day."""
        now = self.clock()
        if now < self.next_run:
            return False
        if self.quiet_only and not self.in_quiet(now):
            self.next_run = self._next_quiet(now)
            self._save()
            return False
        return True

    def force(self) -> None:
        """Run at the next check (an admin asked for a scan now)."""
        self.next_run = self.clock()
        self.quiet_only = False
        self._save()

    def defer(self, load_avg: float | None = None) -> bool:
        """True (and push the run back 15 minutes) if the host is busy right now."""
        if load_avg is None:
            try:
                load_avg = os.getloadavg()[0]
            except OSError:
                return False
        if load_avg / self.cpus > self.max_light_load:
            self.next_run = self.clock() + DEFER_SECONDS
            self._save()
            return True
        return False

    def snapshot(self) -> dict:
        return {
            "class": self.last_class,
            "next_run": self._local(self.next_run).isoformat(timespec="minutes"),
            "last_run": self._local(self.last_run).isoformat(timespec="minutes") if self.last_run else None,
            "last": self.last_stats,
            "quiet_hours_only": self.quiet_only,
        }
