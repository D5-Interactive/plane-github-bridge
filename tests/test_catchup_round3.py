"""Catch-up sync, third round: startup under a rate limit, confirming a deleted work
item before giving up on it, the shared hold's edge cases, and rate-limited repo reads."""
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import build_app
from bridge.clients import GitHubClient, Pacer, PlaneClient
from bridge.store import Link

from test_catchup_round2 import FakeClock, Sleeps, missing
from test_catchup_sync import PROJ, items, make

RATE_LIMITED = {"error_code": 5900, "error_message": "RATE_LIMIT_EXCEEDED"}


def sync_threads():
    return [t for t in threading.enumerate() if t.name == "module-sync"]


# ---------------------------------------------------------------- 1. startup

def test_startup_under_a_rate_limit_is_quick_and_still_starts_the_sync(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    bridge.s.sync_interval = 3600              # one pass at startup, then wait
    plane.rate_limited_after = 0               # every Plane call answers 429
    started = time.monotonic()
    with TestClient(build_app(bridge)) as client:
        assert time.monotonic() - started < 5
        assert sync_threads()                  # the catch-up sync runs anyway
        body = client.get("/health").json()
    assert body["plane_api_ok"] is False
    assert body["plane_problem_temporary"] is True
    assert body["plane_problem"].startswith("temporary:") and "429" in body["plane_problem"]


def test_startup_check_makes_one_attempt():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "30"}, json=RATE_LIMITED)

    sleep = Sleeps()
    plane = PlaneClient("http://plane.local", "acme", "k", sleep=sleep, clock=FakeClock(),
                        transport=httpx.MockTransport(handler))
    problem, setup_problem = plane.workspace_status()
    assert len(calls) == 1 and sleep.calls == []      # no retries, no sleeping
    assert "429" in problem and setup_problem is False
    assert plane.pacer._hold_until == pytest.approx(130.0)   # but the others still back off


@pytest.mark.parametrize("status", [500, 503])
def test_plane_outage_at_startup_is_temporary(status):
    plane = PlaneClient("http://plane.local", "acme", "k", sleep=Sleeps(),
                        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})))
    problem, setup_problem = plane.workspace_status()
    assert str(status) in problem and setup_problem is False


def test_unreachable_plane_at_startup_is_temporary():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    plane = PlaneClient("http://plane.local", "acme", "k", transport=httpx.MockTransport(handler))
    problem, setup_problem = plane.workspace_status()
    assert "can't reach" in problem and setup_problem is False


def test_rejected_key_at_startup_still_blocks_the_sync(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    bridge.s.sync_interval = 3600
    plane.projects_status = 401
    before = len(sync_threads())
    with TestClient(build_app(bridge)) as client:
        body = client.get("/health").json()
        assert len(sync_threads()) == before   # no catch-up sync for a setup mistake
    assert body["plane_api_ok"] is False and "plane_problem_temporary" not in body
    assert "PLANE_API_KEY" in body["plane_problem"]
    assert not gh.created


def test_temporary_problem_clears_after_a_good_pass(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    bridge.s.sync_interval = 0.05
    plane.rate_limited_after = 0
    with TestClient(build_app(bridge)) as client:
        assert client.get("/health").json()["plane_api_ok"] is False
        plane.rate_limited_after = None        # Plane recovers
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not client.get("/health").json()["plane_api_ok"]:
            time.sleep(0.05)
        assert client.get("/health").json()["plane_api_ok"] is True
    assert gh.created == [("acme/a", "Item 1")]


# ---------------------------------------------------------------- 2. deleted work items

def test_proxy_404_does_not_abandon_a_link(tmp_path):
    bridge, plane, gh = make(tmp_path, {})
    missing(bridge.store, "x", 1)
    plane.proxy_404 = {"item-x-1"}             # Plane restarting behind a proxy
    bridge.sync_linked_modules()
    assert [l.plane_item_id for l in bridge.store.missing_plane_links(10)] == ["item-x-1"]
    plane.proxy_404 = set()                    # Plane is back
    assert bridge.sync_linked_modules() == "repaired 1 Plane link(s)"


def test_deleted_work_item_in_a_module_is_parked_not_read_every_pass(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 2))})
    plane.gone = {"item-a-1"}                  # still listed in the module, but deleted
    first = bridge.sync_linked_modules()
    assert "created acme/a#1" in first and "1 item(s) failed" in first
    assert bridge.store.failure("item-a-1") is not None
    reads = plane.calls[("GET", "/projects/*/issues/*/")]
    assert bridge.sync_linked_modules() == "nothing new"
    assert plane.calls[("GET", "/projects/*/issues/*/")] == reads


def test_proxy_404_on_a_work_item_is_not_parked(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    plane.proxy_404 = {"item-a-1"}
    assert "1 item(s) failed" in bridge.sync_linked_modules()
    assert bridge.store.failure("item-a-1") is None        # retried next pass
    plane.proxy_404 = set()
    assert bridge.sync_linked_modules() == "created acme/a#1"


# ---------------------------------------------------------------- 3. shared hold

def test_last_429_still_holds_the_other_threads():
    clock, sleep = FakeClock(), Sleeps()
    plane = PlaneClient("http://plane.local", "acme", "k", sleep=sleep, clock=clock, max_retries=0,
                        transport=httpx.MockTransport(
                            lambda r: httpx.Response(429, headers={"Retry-After": "5"}, json=RATE_LIMITED)))
    with pytest.raises(httpx.HTTPStatusError, match="429"):
        plane.projects()
    assert plane.pacer._hold_until == pytest.approx(105.0)


def test_retrying_thread_honors_a_longer_hold_set_by_another():
    clock, sleep = FakeClock(), Sleeps()
    pacer = Pacer(0, clock=clock, sleep=sleep)
    mine = pacer.hold(5)                       # this thread's own 429...
    assert pacer.wait(own_hold=mine) == 0      # ...which it already slept through
    pacer.hold(20)                             # another thread then got a longer one
    assert pacer.wait(own_hold=mine) == pytest.approx(20)


# ---------------------------------------------------------------- 4. rate-limited repo reads

def test_rate_limited_repo_read_raises_instead_of_looking_invisible():
    gh = GitHubClient("t", sleep=Sleeps(), transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."})))
    with pytest.raises(httpx.HTTPStatusError, match="403"):
        gh.get_repo("acme/x")


def test_rate_limited_repo_read_stops_the_pass(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    gh.repo_status, gh.repo_message = 403, "You have exceeded a secondary rate limit."
    assert bridge.sync_linked_modules() == "stopped early: rate limited"
    assert not gh.created
