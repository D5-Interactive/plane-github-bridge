"""Debounced checks and the self-scheduling org-wide scan."""
from datetime import datetime
from zoneinfo import ZoneInfo

from bridge.scheduler import DEFER_SECONDS, HEAVY, LIGHT, SLOW, Debouncer, ScanScheduler


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def at(tz, *args) -> float:
    return datetime(*args, tzinfo=ZoneInfo(tz)).timestamp()


def local(sched, ts):
    return datetime.fromtimestamp(ts, sched.tz)


# ---------------------------------------------------------------- debouncer

def test_debouncer_runs_once_per_burst():
    c = Clock(0.0)
    d = Debouncer(delay=30, clock=c)
    for t in (0, 5, 10, 20):                # a burst of edits
        c.t = t
        d.touch("mod-1")
    c.t = 45
    assert d.due() == []                    # only 25 s since the last edit
    c.t = 50
    assert d.due() == ["mod-1"]
    assert d.due() == []                    # returned once, then forgotten


def test_debouncer_keys_are_independent():
    c = Clock(0.0)
    d = Debouncer(delay=30, clock=c)
    d.touch("a")
    c.t = 20
    d.touch("b")
    c.t = 31
    assert d.due() == ["a"] and d.pending() == 1
    c.t = 51
    assert d.due() == ["b"]


# ---------------------------------------------------------------- classification

def test_classes():
    s = ScanScheduler(clock=Clock(0), cpu_count=4)
    assert s.classify(30, 100, 2, 1.0) == LIGHT
    assert s.classify(30, 900, 2, 1.0) == HEAVY          # many calls
    assert s.classify(30, 100, 60, 1.0) == HEAVY         # CPU spike
    assert s.classify(30, 100, 2, 8.0) == HEAVY          # 2.0 per CPU > 1.5
    assert s.classify(600, 100, 2, 1.0) == SLOW          # took long


# ---------------------------------------------------------------- next run

def test_first_scan_waits_for_quiet_hours():
    tz = "America/Chicago"
    c = Clock(at(tz, 2026, 11, 19, 16, 0))
    s = ScanScheduler(clock=c, tz=tz, quiet_hours=(1, 5))
    assert local(s, s.next_run) == datetime(2026, 11, 20, 1, 0, tzinfo=ZoneInfo(tz))
    assert not s.due()
    s.force()
    assert s.due()


def test_first_scan_before_quiet_window_same_day():
    tz = "UTC"
    c = Clock(at(tz, 2026, 11, 19, 0, 30))
    s = ScanScheduler(clock=c, tz=tz, quiet_hours=(1, 5))
    assert local(s, s.next_run).hour == 1 and local(s, s.next_run).day == 19


def test_light_scan_runs_again_in_six_hours():
    c = Clock(at("UTC", 2026, 11, 19, 10, 0))
    s = ScanScheduler(clock=c)
    nxt = s.record(20, 50, 1, 0.2)
    assert nxt - c.t == 6 * 3600 and s.last_class == LIGHT


def test_heavy_scan_goes_to_a_slot_at_least_twelve_hours_out():
    tz = "America/Chicago"
    c = Clock(at(tz, 2026, 11, 19, 7, 0))   # just after the 06:00 slot
    s = ScanScheduler(clock=c, tz=tz, heavy_slots=(6, 18))
    nxt = s.record(30, 5000, 1, 0.2)
    # +12 h is 19:00, so the next slot is 06:00 the next day
    assert local(s, nxt) == datetime(2026, 11, 20, 6, 0, tzinfo=ZoneInfo(tz))
    c.t = at(tz, 2026, 11, 19, 5, 30)
    nxt = s.record(30, 5000, 1, 0.2)
    # +12 h is 17:30, so the 18:00 slot the same day
    assert local(s, nxt) == datetime(2026, 11, 19, 18, 0, tzinfo=ZoneInfo(tz))
    assert nxt - c.t >= 12 * 3600


def test_slow_scan_goes_to_the_next_quiet_window_across_midnight():
    tz = "Asia/Tokyo"
    c = Clock(at(tz, 2026, 12, 31, 23, 50))
    s = ScanScheduler(clock=c, tz=tz, quiet_hours=(2, 5))
    nxt = s.record(900, 50, 1, 0.2)
    assert local(s, nxt) == datetime(2027, 1, 1, 2, 0, tzinfo=ZoneInfo(tz))
    assert s.last_class == SLOW


def test_slow_scan_during_quiet_window_waits_for_the_next_night():
    tz = "UTC"
    c = Clock(at(tz, 2026, 11, 19, 3, 0))
    s = ScanScheduler(clock=c, tz=tz, quiet_hours=(1, 5))
    nxt = s.record(900, 50, 1, 0.2)
    assert local(s, nxt) == datetime(2026, 11, 20, 1, 0, tzinfo=ZoneInfo(tz))


def test_quiet_window_respects_daylight_saving():
    tz = "America/Chicago"                  # DST ends 2026-11-01 at 02:00
    c = Clock(at(tz, 2026, 10, 31, 12, 0))
    s = ScanScheduler(clock=c, tz=tz, quiet_hours=(3, 5))
    nxt = s.record(900, 50, 1, 0.2)
    assert local(s, nxt).hour == 3 and local(s, nxt).day == 1


# ---------------------------------------------------------------- defer and health

def test_defer_when_host_is_busy():
    c = Clock(1000.0)
    s = ScanScheduler(clock=c, cpu_count=2, max_light_load=1.5)
    s.force()
    assert s.defer(4.0) is True and s.next_run == 1000.0 + DEFER_SECONDS
    assert not s.due()
    s.force()
    assert s.defer(1.0) is False and s.due()


def test_snapshot_for_health():
    tz = "America/Chicago"
    c = Clock(at(tz, 2026, 11, 19, 10, 0))
    s = ScanScheduler(clock=c, tz=tz)
    snap = s.snapshot()
    assert snap["class"] is None and snap["last"] is None and snap["last_run"] is None
    s.record(12.34, 40, 0.5, 0.25)
    snap = s.snapshot()
    assert snap["class"] == LIGHT
    assert snap["next_run"] == "2026-11-19T16:00-06:00"
    assert snap["last"] == {"duration_s": 12.3, "api_calls": 40, "cpu_s": 0.5, "load_avg": 0.25}
