"""Catch-up sync, second round: every rate limit stops the pass, shared backoff,
parking keyed on the repo, credential errors, repair-queue rotation, quiet logs."""
import logging
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import build_app
from bridge.clients import MAX_WAIT, GitHubClient, Pacer, PlaneClient
from bridge.store import Link, Store

from test_catchup_sync import PROJ, items, make

RATE_LIMITED = {"error_code": 5900, "error_message": "RATE_LIMIT_EXCEEDED"}


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- 1. rate limits always stop the pass

def test_rate_limit_on_the_project_list_stops_the_pass_cleanly(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 2))})
    plane.rate_limited_after = 0                       # the very first call (the project list) is refused
    assert bridge.sync_linked_modules() == "stopped early: rate limited"   # no exception escapes
    assert plane.total == 4                            # one call plus the client's three retries
    plane.rate_limited_after = None
    assert bridge.sync_linked_modules() == "created acme/a#1, acme/a#2"


def test_rate_limit_during_module_tidy_stops_without_fallback_calls(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    plane.module_patch_status = 429
    assert bridge.sync_linked_modules() == "stopped early: rate limited"
    patches = sum(v for (m, _), v in plane.calls.items() if m == "PATCH")
    assert patches == 4                                # one PATCH and its retries; no name/date fallbacks
    assert not gh.created                              # the pass ended before the issue


# ---------------------------------------------------------------- 2. shared backoff, fail fast

def test_a_429_in_one_thread_holds_the_pacer_for_the_others():
    clock, sleep = FakeClock(), Sleeps()
    answers = [httpx.Response(429, headers={"Retry-After": "5"}, json=RATE_LIMITED),
               httpx.Response(200, json={"results": []})]
    plane = PlaneClient("http://plane.local", "acme", "k", sleep=sleep, clock=clock,
                        transport=httpx.MockTransport(lambda r: answers.pop(0)))
    plane.projects()
    assert sleep.calls == [5]                          # this thread waited once
    other = threading.Thread(target=plane.pacer.wait)  # a second caller, same instant
    other.start()
    other.join()
    assert sleep.calls == [5, 5]                       # ...and so does everyone else


def test_pacer_hold_applies_with_pacing_off():
    clock, sleep = FakeClock(), Sleeps()
    pacer = Pacer(0, clock=clock, sleep=sleep)
    pacer.hold(7)
    assert pacer.wait() == 7 and sleep.calls == [7]
    assert pacer.wait(honor_hold=False) == 0


def test_plane_wait_over_the_cap_fails_fast():
    sleep, calls = Sleeps(), []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "3600"}, json=RATE_LIMITED)

    plane = PlaneClient("http://plane.local", "acme", "k", sleep=sleep, clock=FakeClock(),
                        transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError, match="429"):
        plane.projects()
    assert len(calls) == 1 and sleep.calls == []       # no sleeping just to fail anyway
    assert plane.pacer._hold_until == pytest.approx(100.0 + MAX_WAIT)


def test_github_wait_over_the_cap_fails_fast():
    sleep, calls = Sleeps(), []

    def handler(req):
        calls.append(1)
        return httpx.Response(403, headers={"Retry-After": "3600"},
                              json={"message": "You have exceeded a secondary rate limit."})

    gh = GitHubClient("t", transport=httpx.MockTransport(handler), sleep=sleep)
    with pytest.raises(httpx.HTTPStatusError, match="403"):
        gh.create_issue("acme/x", "t", "b")
    assert len(calls) == 1 and sleep.calls == []


# ---------------------------------------------------------------- 3. parking

def test_park_is_keyed_on_the_repo(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    gh.refuse["acme/a"] = 410
    assert "1 item(s) failed" in bridge.sync_linked_modules()
    assert bridge.store.failure("item-a-1")[0] == "acme/a"
    assert bridge.sync_linked_modules() == "nothing new"           # parked
    plane.modules["mod-a"] = ("acme/b", items("a", 1))            # module relinked to another repo
    assert bridge.sync_linked_modules() == "created acme/b#1"
    assert bridge.store.failure("item-a-1") is None


def test_startup_clears_parked_items(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    bridge.s.sync_interval = 0
    bridge.store.record_failure("item-a-1", "acme/a", "GitHub answered 404", 1.0)
    with TestClient(build_app(bridge)):
        pass
    assert bridge.store.failure("item-a-1") is None


def test_rejected_token_stops_the_pass_with_one_log_line(tmp_path, caplog):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 3)),
                                        "mod-b": ("acme/b", items("b", 3))})
    gh.refuse["acme/a"] = gh.refuse["acme/b"] = 401
    with caplog.at_level(logging.WARNING, logger="bridge"):
        assert bridge.sync_linked_modules() == "stopped: credentials rejected"
    sync_lines = [r for r in caplog.records if r.getMessage().startswith("sync:")]
    assert len(sync_lines) == 1 and "rejected" in sync_lines[0].getMessage()
    assert all(bridge.store.failure(i) is None for i in items("a", 3))    # nothing parked


def test_header_only_secondary_limit_is_not_parked(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 2))})
    gh.refuse["acme/a"] = 403
    gh.refuse_headers = {"X-RateLimit-Remaining": "0"}
    gh.refuse_message = "Forbidden"                   # no "rate limit" text at all
    assert bridge.sync_linked_modules() == "stopped early: rate limited"
    assert bridge.store.failure("item-a-1") is None


def test_parked_repo_is_logged_once_per_pass(tmp_path, caplog):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 4))})
    gh.refuse["acme/a"] = 410
    with caplog.at_level(logging.WARNING, logger="bridge"):
        assert "4 item(s) failed" in bridge.sync_linked_modules()
    parked = [r for r in caplog.records if "refused items from" in r.getMessage()]
    assert len(parked) == 1


# ---------------------------------------------------------------- 4. repair queue

def missing(store, prefix, n, first=100):
    for i in range(1, n + 1):
        store.save(Link(f"item-{prefix}-{i}", PROJ, "acme/a", first + i,
                        f"https://github.com/acme/a/issues/{first + i}", plane_linked=False))


def test_repair_queue_rotates_past_rows_that_keep_failing(tmp_path):
    bridge, plane, gh = make(tmp_path, {})
    missing(bridge.store, "bad", 10)                  # saved first: oldest rows
    missing(bridge.store, "good", 2, first=200)
    plane.fail_link_for = set(items("bad", 10))
    assert bridge.sync_linked_modules() == "nothing new"           # ten tried, all failed
    assert bridge.sync_linked_modules() == "repaired 2 Plane link(s)"
    assert {i for i, _ in plane.links} == set(items("good", 2))


def test_repair_gives_up_on_a_deleted_work_item(tmp_path, caplog):
    bridge, plane, gh = make(tmp_path, {})
    missing(bridge.store, "gone", 1)
    plane.gone = {"item-gone-1"}
    with caplog.at_level(logging.WARNING, logger="bridge"):
        bridge.sync_linked_modules()
        bridge.sync_linked_modules()
    assert bridge.store.missing_plane_links(10) == []
    assert len([r for r in caplog.records if "gone from Plane" in r.getMessage()]) == 1


# ---------------------------------------------------------------- 5. quiet error logs

def test_error_bodies_stay_out_of_error_logs(caplog):
    body = {"name": "Secret roadmap item", "error": "bad"}
    plane = PlaneClient("http://plane.local", "acme", "k", sleep=Sleeps(),
                        transport=httpx.MockTransport(lambda r: httpx.Response(400, json=body)))
    with caplog.at_level(logging.DEBUG, logger="bridge"), pytest.raises(httpx.HTTPStatusError) as err:
        plane.add_comment("p", "i", "<p>x</p>")
    assert "Secret" not in str(err.value)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and all("Secret" not in m for m in errors)
    assert "400" in errors[0] and "/comments/" in errors[0]
    assert any("Secret" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


# ---------------------------------------------------------------- 6. BRIDGE_RECHECK_LINKS

def test_recheck_marks_every_link_and_duplicates_count_as_repaired(tmp_path):
    bridge, plane, gh = make(tmp_path, {})
    for i in (1, 2):
        bridge.store.save(Link(f"item-ok-{i}", PROJ, "acme/a", i, f"https://github.com/acme/a/issues/{i}"))
    assert bridge.store.missing_plane_links(10) == []
    bridge.s.sync_interval = 0
    bridge.s.recheck_links = True
    plane.already_linked = {"item-ok-1"}              # Plane already has this one
    with TestClient(build_app(bridge)):
        pass
    assert len(bridge.store.missing_plane_links(10)) == 2
    assert bridge.sync_linked_modules() == "repaired 2 Plane link(s)"
    assert plane.links == [("item-ok-2", "https://github.com/acme/a/issues/2")]   # only the missing one added


def test_recheck_on_the_store_counts_rows(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.save(Link("i1", PROJ, "acme/a", 1, "u1"))
    store.save(Link("i2", PROJ, "acme/a", 2, "u2"))
    assert store.recheck_all_links() == 2
