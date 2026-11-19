import threading
from email.utils import format_datetime
from datetime import datetime, timezone

import httpx
import pytest

from bridge.clients import (DEFAULT_WAIT, MAX_WAIT, GitHubClient, Pacer, PlaneClient,
                            retry_after)
from bridge.config import load_settings

RATE_LIMITED = {"error_code": 5900, "error_message": "RATE_LIMIT_EXCEEDED"}


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def resp(status=429, headers=None, json=None, text=None):
    req = httpx.Request("GET", "http://plane.local/api/v1/workspaces/acme/projects/")
    if text is not None:
        return httpx.Response(status, headers=headers or {}, text=text, request=req)
    return httpx.Response(status, headers=headers or {}, json=json or {}, request=req)


# ---------------------------------------------------------------- retry_after

def test_retry_after_seconds():
    assert retry_after(resp(headers={"Retry-After": "7"})) == 7


def test_retry_after_http_date():
    now = 1_800_000_000.0
    when = format_datetime(datetime.fromtimestamp(now + 25, timezone.utc), usegmt=True)
    assert retry_after(resp(headers={"Retry-After": when}), now=now) == pytest.approx(25, abs=1)


def test_retry_after_falls_back_to_reset_then_default():
    now = 1_800_000_000.0
    assert retry_after(resp(headers={"X-RateLimit-Reset": str(int(now + 12))}), now=now) == 12
    assert retry_after(resp(headers={"X-RateLimit-Reset": "4"}), now=now) == 4
    assert retry_after(resp()) == DEFAULT_WAIT
    assert retry_after(resp(headers={"Retry-After": "nonsense"})) == DEFAULT_WAIT


def test_retry_after_is_capped_and_never_negative():
    now = 1_800_000_000.0
    assert retry_after(resp(headers={"Retry-After": "3600"})) == MAX_WAIT
    past = format_datetime(datetime.fromtimestamp(now - 30, timezone.utc), usegmt=True)
    assert retry_after(resp(headers={"Retry-After": past}), now=now) == 0


# ---------------------------------------------------------------- Plane 429 backoff

def plane(handler, sleep, **kw):
    return PlaneClient("http://plane.local", "acme", "k", transport=httpx.MockTransport(handler),
                       sleep=sleep, **kw)


def test_plane_429_waits_then_succeeds(caplog):
    answers = [httpx.Response(429, headers={"Retry-After": "3"}, json=RATE_LIMITED),
               httpx.Response(429, json=RATE_LIMITED),
               httpx.Response(200, json={"results": [{"id": "p1"}]})]
    seen = []

    def handler(req):
        seen.append(req.method)
        return answers.pop(0)

    sleep = Sleeps()
    with caplog.at_level("WARNING", logger="bridge"):
        assert plane(handler, sleep).projects() == [{"id": "p1"}]
    assert sleep.calls == [3, DEFAULT_WAIT] and len(seen) == 3
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "GET /api/v1/workspaces/acme/projects/ answered 429" in warnings[0]
    assert "per_page" not in warnings[0] and "k" not in warnings[0].split()  # no query, no key


def test_plane_429_gives_up_after_max_retries():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "1"}, json=RATE_LIMITED)

    sleep = Sleeps()
    with pytest.raises(httpx.HTTPStatusError, match="429"):
        plane(handler, sleep, max_retries=3).projects()
    assert len(calls) == 4 and sleep.calls == [1, 1, 1]


def test_plane_writes_are_retried_on_429_with_the_same_body():
    bodies = []

    def handler(req):
        bodies.append(req.content)
        if len(bodies) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json=RATE_LIMITED)
        return httpx.Response(201, json={})

    plane(handler, Sleeps()).add_comment("p", "i", "<p>hi</p>")
    assert len(bodies) == 2 and bodies[0] == bodies[1] and b"hi" in bodies[0]


def test_plane_other_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(403, json={"detail": "no"})

    sleep = Sleeps()
    with pytest.raises(httpx.HTTPStatusError, match="403"):
        plane(handler, sleep).projects()
    assert len(calls) == 1 and sleep.calls == []


# ---------------------------------------------------------------- pacing

class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.t

    def sleep(self, seconds):
        with self.lock:
            self.t += seconds


def test_pacer_spaces_requests_evenly():
    clock = FakeClock()
    pacer = Pacer(60, clock=clock, sleep=clock.sleep)      # one per second
    starts = []
    for _ in range(5):
        pacer.wait()
        starts.append(clock())
    assert starts == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_pacer_doesnt_wait_after_idle_time():
    clock = FakeClock()
    pacer = Pacer(60, clock=clock, sleep=clock.sleep)
    pacer.wait()
    clock.t += 30                                           # idle
    assert pacer.wait() == 0


def test_pacer_off_when_zero():
    sleep = Sleeps()
    pacer = Pacer(0, sleep=sleep)
    for _ in range(10):
        assert pacer.wait() == 0
    assert sleep.calls == []


def test_pacer_is_thread_safe():
    """Many threads at once still get distinct, evenly spaced slots."""
    t0 = 1000.0
    pacer = Pacer(120, clock=lambda: t0, sleep=lambda s: None)   # every 0.5 s, frozen clock
    delays, lock = [], threading.Lock()

    def worker():
        for _ in range(10):
            d = pacer.wait()
            with lock:
                delays.append(d)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(delays) == [i * 0.5 for i in range(80)]


def test_plane_client_paces_every_call():
    clock = FakeClock()
    pc = PlaneClient("http://plane.local", "acme", "k", rate_limit=30,      # every 2 s
                     transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []})),
                     sleep=clock.sleep, clock=clock)
    pc.projects()
    pc.projects()
    pc.projects()
    assert clock() == 104.0


def test_rate_limit_setting(monkeypatch, tmp_path):
    for k, v in {"PLANE_BASE_URL": "http://p", "PLANE_PUBLIC_URL": "https://p", "PLANE_WORKSPACE": "w",
                 "PLANE_API_KEY": "k", "GITHUB_TOKEN": "t", "GITHUB_WEBHOOK_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PLANE_RATE_LIMIT", raising=False)
    assert load_settings(str(tmp_path / "none.yaml")).plane_rate_limit == 50
    monkeypatch.setenv("PLANE_RATE_LIMIT", "0")
    assert load_settings(str(tmp_path / "none.yaml")).plane_rate_limit == 0


# ---------------------------------------------------------------- GitHub rate limits

def github(handler, sleep):
    return GitHubClient("gh-token", transport=httpx.MockTransport(handler), sleep=sleep)


def test_github_secondary_rate_limit_is_retried_once():
    answers = [httpx.Response(403, headers={"Retry-After": "60"},
                              json={"message": "You have exceeded a secondary rate limit."}),
               httpx.Response(201, json={"number": 7, "html_url": "u"})]
    sleep = Sleeps()
    issue = github(lambda r: answers.pop(0), sleep).create_issue("acme/x", "t", "b")
    assert issue["number"] == 7 and sleep.calls == [60]


def test_github_429_is_retried_once_then_raises():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "5"}, json={})

    sleep = Sleeps()
    with pytest.raises(httpx.HTTPStatusError, match="429"):
        github(handler, sleep).create_issue("acme/x", "t", "b")
    assert len(calls) == 2 and sleep.calls == [5]


def test_github_secondary_limit_detected_by_message_alone():
    answers = [httpx.Response(403, json={"message": "API rate limit exceeded for installation"}),
               httpx.Response(200, json={"name": "x"})]
    sleep = Sleeps()
    assert github(lambda r: answers.pop(0), sleep).get_repo("acme/x") == {"name": "x"}
    assert sleep.calls == [DEFAULT_WAIT]


def test_github_plain_403_and_404_unchanged():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})

    sleep = Sleeps()
    gh = github(handler, sleep)
    assert gh.get_repo("acme/x") is None
    with pytest.raises(httpx.HTTPStatusError, match="403"):
        gh.create_issue("acme/x", "t", "b")
    assert len(calls) == 2 and sleep.calls == []
    gh404 = github(lambda r: httpx.Response(404, json={}), sleep)
    assert gh404.get_repo("acme/missing") is None and sleep.calls == []
