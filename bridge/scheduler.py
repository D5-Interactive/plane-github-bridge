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

import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LIGHT, HEAVY, SLOW = "light", "heavy", "slow"
DEFER_SECONDS = 15 * 60


class Debouncer:
    """One check per burst of edits: a key is due once it has been quiet for `delay` seconds."""

    def __init__(self, delay: float = 30.0, clock=time.time):
        self.delay, self.clock = delay, clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def touch(self, key: str) -> None:
        with self._lock:
            self._last[key] = self.clock()

    def due(self) -> list[str]:
        """Keys quiet for at least `delay` seconds; each is returned once, then forgotten."""
        now = self.clock()
        with self._lock:
            ready = [k for k, t in self._last.items() if now - t >= self.delay]
            for k in ready:
                del self._last[k]
        return ready

    def pending(self) -> int:
        with self._lock:
            return len(self._last)


class ScanScheduler:
    def __init__(self, clock=time.time, tz: str = "UTC", quiet_hours: tuple[int, int] = (1, 5),
                 light_every_h: float = 6, heavy_every_h: float = 12,
                 heavy_slots: tuple[int, ...] = (6, 18), max_light_seconds: float = 120,
                 max_light_calls: int = 300, max_light_cpu: float = 30.0,
                 max_light_load: float = 1.5, cpu_count: int | None = None):
        self.clock = clock
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
        return self.next_run

    def due(self) -> bool:
        return self.clock() >= self.next_run

    def force(self) -> None:
        """Run at the next check (an admin asked for a scan now)."""
        self.next_run = self.clock()

    def defer(self, load_avg: float | None = None) -> bool:
        """True (and push the run back 15 minutes) if the host is busy right now."""
        if load_avg is None:
            try:
                load_avg = os.getloadavg()[0]
            except OSError:
                return False
        if load_avg / self.cpus > self.max_light_load:
            self.next_run = self.clock() + DEFER_SECONDS
            return True
        return False

    def snapshot(self) -> dict:
        return {
            "class": self.last_class,
            "next_run": self._local(self.next_run).isoformat(timespec="minutes"),
            "last_run": self._local(self.last_run).isoformat(timespec="minutes") if self.last_run else None,
            "last": self.last_stats,
        }
