"""ALL-104: pacing learned from Plane's and GitHub's rate-limit headers, with lanes."""
import threading

import httpx
import pytest

from bridge.clients import (LANE_BACKGROUND, LANE_INTERACTIVE, GitHubClient, Pacer, PlaneClient,
                            current_lane, lane)

WALL0 = 1_800_000_000.0


class Clock:
    """Monotonic and wall clocks that move together; sleep advances both."""

    def __init__(self):
        self.t = 100.0
        self.lock = threading.Lock()
        self.sleeps = []

    def __call__(self):
        with self.lock:
            return self.t

    def wall(self):
        with self.lock:
            return WALL0 + (self.t - 100.0)

    def sleep(self, s):
        with self.lock:
            self.sleeps.append(s)
            self.t += s


def plane_headers(clock, remaining, reset_in, limit=None):
    h = {"X-RateLimit-Remaining": str(remaining), "X-RateLimit-Reset": str(int(clock.wall() + reset_in))}
    if limit is not None:
        h["X-RateLimit-Limit"] = str(limit)
    return h


# ---------------------------------------------------------------- learning

def test_learns_the_pace_from_plane_headers():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall, burst=0)
    p.observe(plane_headers(c, remaining=60, reset_in=60))
    snap = p.snapshot()
    assert snap["source"] == "headers" and snap["remaining"] == 60
    assert snap["interval"] == pytest.approx(1.0)            # 60 s left / 60 calls
    starts = []
    for _ in range(3):
        p.wait()
        starts.append(c())
    assert starts == pytest.approx([100.0, 101.0, 102.0], abs=0.05)   # remaining shrinks as calls go


def test_reset_given_as_seconds_until_reset_is_accepted():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall)
    p.observe({"X-RateLimit-Remaining": "30", "X-RateLimit-Reset": "60"})
    assert p.snapshot()["reset_in"] == pytest.approx(60.0)
    assert p.snapshot()["interval"] == pytest.approx(2.0)


def test_a_raised_limit_speeds_the_pacer_up():
    """The configured pace is only a fallback: Plane's headers win."""
    c = Clock()
    p = Pacer(50, clock=c, sleep=c.sleep, wall=c.wall)       # configured: 1.2 s
    assert p.snapshot()["source"] == "configured" and p.snapshot()["interval"] == pytest.approx(1.2)
    p.observe(plane_headers(c, remaining=300, reset_in=60, limit=300))
    assert p.snapshot()["interval"] == pytest.approx(0.2)


def test_learns_from_github_core_headers_and_ignores_other_resources():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall)
    p.observe({"x-ratelimit-limit": "30", "x-ratelimit-remaining": "29", "x-ratelimit-used": "1",
               "x-ratelimit-reset": str(int(c.wall() + 60)), "x-ratelimit-resource": "search"})
    assert p.snapshot()["source"] == "off"                   # search doesn't pace core calls
    assert p.snapshot()["other_resources"]["search"]["remaining"] == 29
    p.observe({"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4000",
               "x-ratelimit-reset": str(int(c.wall() + 3600)), "x-ratelimit-resource": "core"})
    snap = p.snapshot()
    assert snap["source"] == "headers" and snap["limit"] == 5000
    assert snap["interval"] == pytest.approx(3600 / 4000)


def test_stale_headers_fall_back_to_the_configured_pace():
    c = Clock()
    p = Pacer(60, clock=c, sleep=c.sleep, wall=c.wall)
    p.observe(plane_headers(c, remaining=10, reset_in=5))
    assert p.snapshot()["source"] == "headers"
    c.t += 6                                                 # the window reset
    snap = p.snapshot()
    assert snap["source"] == "configured" and snap["interval"] == pytest.approx(1.0)
    assert snap["remaining"] is None


def test_headers_already_past_their_reset_are_ignored():
    c = Clock()
    p = Pacer(60, clock=c, sleep=c.sleep, wall=c.wall)
    p.observe({"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": str(int(c.wall() - 10))})
    assert p.snapshot()["source"] == "configured"


def test_out_of_order_responses_never_raise_the_budget_within_a_window():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall)
    p.observe(plane_headers(c, remaining=10, reset_in=60))
    p.observe(plane_headers(c, remaining=12, reset_in=60))   # older response, arriving late
    assert p.snapshot()["remaining"] == 10


def test_exhausted_budget_waits_for_the_reset():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall)
    p.observe(plane_headers(c, remaining=0, reset_in=30))
    assert p.wait() == pytest.approx(30.0)


def test_interactive_calls_may_burst_when_ahead_of_schedule():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall, burst=3)
    p.observe(plane_headers(c, remaining=60, reset_in=60))
    for _ in range(3):
        assert p.wait() == 0                                 # three back to back
    assert p.wait() > 0                                      # then the spacing applies


# ---------------------------------------------------------------- lanes

def test_lane_context_manager():
    assert current_lane() == LANE_INTERACTIVE
    with lane("background"):
        assert current_lane() == LANE_BACKGROUND
    assert current_lane() == LANE_INTERACTIVE
    with pytest.raises(ValueError):
        with lane("bulk"):
            pass


def test_background_keeps_the_reserve_for_interactive_calls():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall, reserve_share=0.3)
    p.observe(plane_headers(c, remaining=10, reset_in=60, limit=10))   # reserve: 3 calls
    with lane("background"):
        for _ in range(7):
            p.wait()
        t = c()
        p.wait()                                              # 8th would eat the reserve
    assert c() >= 100.0 + 60 - 1e-6                           # so it waited for the reset
    assert c() > t
    # Interactive calls could still have used the reserve in the meantime.


def test_interactive_can_use_the_reserve_background_cannot():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall, reserve_share=0.5, burst=10)
    p.observe(plane_headers(c, remaining=4, reset_in=60, limit=4))     # reserve: 2
    with lane("background"):
        p.wait()
        p.wait()                                              # background used its 2
    snap = p.snapshot()
    assert snap["remaining"] == 2 and snap["background_blocked_for"] > 0
    assert p.wait() < 60                                      # interactive still gets a slot


def test_background_yields_to_a_waiting_interactive_call():
    c = Clock()
    p = Pacer(60, clock=c, sleep=c.sleep, wall=c.wall)        # one per second
    p._waiting[LANE_INTERACTIVE] = 1                          # an interactive call is waiting
    released = []

    def sleep(s):
        c.sleep(s)
        if len(c.sleeps) == 3:                                # it gets its turn...
            p._waiting[LANE_INTERACTIVE] = 0
            released.append(c())

    p.sleep = sleep
    with lane("background"):
        p.wait()
    assert released and c() >= released[0]                    # ...and background went after it


def test_interactive_is_delayed_at_most_one_background_slot():
    c = Clock()
    p = Pacer(60, clock=c, sleep=c.sleep, wall=c.wall)        # one per second
    with lane("background"):
        p.wait()                                              # background takes the slot at 100
    # Many background callers would be queued, but they don't reserve ahead,
    # so an interactive call waits only for the one slot already taken.
    assert p.wait() == pytest.approx(1.0)


def test_snapshot_reports_waiting_and_hold():
    c = Clock()
    p = Pacer(0, clock=c, sleep=c.sleep, wall=c.wall)
    p.hold(5)
    snap = p.snapshot()
    assert snap["hold_in"] == pytest.approx(5.0)
    assert snap["waiting"] == {LANE_INTERACTIVE: 0, LANE_BACKGROUND: 0}


# ---------------------------------------------------------------- clients

def test_plane_client_paces_from_its_responses():
    c = Clock()
    headers = lambda: plane_headers(c, remaining=120, reset_in=60)   # noqa: E731
    pc = PlaneClient("http://plane.local", "acme", "k", rate_limit=0, clock=c, wall=c.wall,
                     sleep=c.sleep, transport=httpx.MockTransport(
                         lambda r: httpx.Response(200, headers=headers(), json={"results": []})))
    pc.pacer.burst = 0
    pc.projects()
    assert pc.pacer.snapshot()["source"] == "headers"
    pc.projects()
    pc.projects()
    assert c() == pytest.approx(100.5, abs=0.05)              # learned after call 1: 0.5 s apart


def test_plane_429_and_hold_still_work_with_learned_headers():
    c = Clock()
    answers = [httpx.Response(429, headers={"Retry-After": "5", **plane_headers(c, 0, 5)},
                              json={"error_code": 5900}),
               httpx.Response(200, headers=plane_headers(c, 59, 60), json={"results": []})]
    pc = PlaneClient("http://plane.local", "acme", "k", clock=c, wall=c.wall, sleep=c.sleep,
                     transport=httpx.MockTransport(lambda r: answers.pop(0)))
    pc.projects()
    assert c.sleeps[0] == pytest.approx(5.0)
    other = []
    t = threading.Thread(target=lambda: other.append(pc.pacer.wait()))
    t.start()
    t.join()
    assert other[0] < 1.5                                     # hold already passed for others


def test_github_client_paces_and_shares_a_hold():
    c = Clock()
    answers = [httpx.Response(403, headers={"Retry-After": "4"},
                              json={"message": "You have exceeded a secondary rate limit."}),
               httpx.Response(200, headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4999",
                                            "x-ratelimit-reset": str(int(c.wall() + 3600)),
                                            "x-ratelimit-resource": "core"}, json={"name": "x"})]
    gh = GitHubClient("t", clock=c, wall=c.wall, sleep=c.sleep,
                      transport=httpx.MockTransport(lambda r: answers.pop(0)))
    assert gh.get_repo("acme/x") == {"name": "x"}
    assert c.sleeps == [4.0]                                  # waited once, no double wait
    snap = gh.pacer.snapshot()
    assert snap["source"] == "headers" and snap["remaining"] == 4999


# ---------------------------------------------------------------- thread safety

def test_learned_pacing_is_thread_safe():
    t0 = 1000.0
    p = Pacer(0, clock=lambda: t0, sleep=lambda s: None, wall=lambda: WALL0, burst=0)
    p.observe({"X-RateLimit-Remaining": "100", "X-RateLimit-Reset": str(int(WALL0 + 50))})
    delays, lock = [], threading.Lock()

    def worker():
        for _ in range(10):
            d = p.wait()
            with lock:
                delays.append(d)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(delays) == 80 and len(set(round(d, 6) for d in delays)) == 80   # all distinct slots
    assert p.snapshot()["remaining"] == 20
