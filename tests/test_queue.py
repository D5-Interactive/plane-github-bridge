import threading

import httpx
import pytest

from bridge.queue import RetryQueue, run_due, short_reason
from bridge.store import Store


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "b.db"))


def test_put_coalesces_on_kind_and_key(store):
    clock = Clock()
    q = RetryQueue(store, clock=clock)
    q.put("push_edit", "item-1", {"title": "a"}, "429")
    e = q.due()[0]
    q.failed(e.id, "429")
    clock.t += 3
    q.put("push_edit", "item-1", {"title": "b"}, "again")     # latest intent wins
    q.put("push_edit", "item-2", {"title": "x"})
    entries = q.due()
    assert [x.key for x in entries] == ["item-1", "item-2"]
    first = entries[0]
    assert first.payload == {"title": "b"} and first.reason == "again"
    assert first.first_seen == 1_000.0 and first.attempts == 1   # kept
    assert first.next_at == 1_003.0                               # due again now
    assert q.stats()["length"] == 2


def test_different_kinds_do_not_coalesce(store):
    q = RetryQueue(store, clock=Clock())
    q.put("push_edit", "item-1", {})
    q.put("add_link", "item-1", {})
    assert q.stats()["length"] == 2


def test_backoff_schedule_caps_at_last_step(store):
    clock = Clock()
    q = RetryQueue(store, clock=clock, backoff=(5, 15, 30, 60, 120), max_age=10_000)
    q.put("k", "a", {})
    e = q.due()[0]
    delays = []
    for _ in range(7):
        e = q.failed(e.id, "boom")
        delays.append(e.next_at - clock.t)
    assert delays == [5, 15, 30, 60, 120, 120, 120]
    assert q.due() == []                    # not due until the clock moves
    clock.t += 120
    assert [x.key for x in q.due()] == ["a"]


def test_due_is_oldest_first_and_limited(store):
    clock = Clock()
    q = RetryQueue(store, clock=clock)
    for i, key in enumerate(["c", "a", "b"]):
        clock.t = 1_000 + i
        q.put("k", key, {})
    assert [e.key for e in q.due()] == ["c", "a", "b"]
    assert [e.key for e in q.due(limit=2)] == ["c", "a"]


def test_done_removes(store):
    q = RetryQueue(store, clock=Clock())
    q.put("k", "a", {})
    q.done(q.due()[0].id)
    assert q.stats() == {"length": 0, "oldest_age": 0.0, "stale": 0}
    assert q.failed(999, "x") is None


def test_survives_restart(tmp_path):
    clock = Clock()
    q = RetryQueue(Store(str(tmp_path / "b.db")), clock=clock)
    q.put("create_issue", "item-9", {"repo": "acme/a"}, "503")
    q.failed(q.due()[0].id, "503")
    q2 = RetryQueue(Store(str(tmp_path / "b.db")), clock=clock)
    e = q2.get("create_issue", "item-9")
    assert e.payload == {"repo": "acme/a"} and e.attempts == 1 and e.reason == "503"
    clock.t += 5
    assert [x.key for x in q2.due()] == ["item-9"]


def test_on_stale_reported_once(store):
    clock = Clock()
    seen = []
    q = RetryQueue(store, clock=clock, max_age=300, on_stale=seen.append)
    q.put("k", "a", {})
    e = q.due()[0]
    clock.t += 100
    q.failed(e.id, "x")
    assert seen == []
    clock.t += 250                     # 350 s old now
    q.failed(e.id, "x")
    clock.t += 200
    q.failed(e.id, "x")
    assert [s.key for s in seen] == ["a"]
    assert q.stats()["stale"] == 1
    assert q.stats()["oldest_age"] == 550.0


def test_broken_on_stale_does_not_break_the_queue(store):
    clock = Clock()

    def boom(_e):
        raise RuntimeError("reporter down")

    q = RetryQueue(store, clock=clock, max_age=1, on_stale=boom)
    q.put("k", "a", {})
    clock.t += 5
    assert q.failed(q.due()[0].id, "x").attempts == 1


def test_thread_safe_puts(store):
    q = RetryQueue(store, clock=Clock())

    def worker(n):
        for i in range(50):
            q.put("k", f"{n}-{i % 10}", {"i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert q.stats()["length"] == 80


def _status(code):
    req = httpx.Request("POST", "https://api.github.com/repos/acme/a/issues?x=1")
    return httpx.HTTPStatusError("boom with body text", request=req,
                                 response=httpx.Response(code, request=req, text="secret body"))


def test_run_due_success_failure_and_unknown_kind(store):
    clock = Clock()
    q = RetryQueue(store, clock=clock)
    q.put("ok", "1", {"n": 1})
    q.put("bad", "2", {})
    q.put("mystery", "3", {})
    ran = []

    def bad(_p):
        raise _status(502)

    summary = run_due(q, {"ok": ran.append, "bad": bad})
    assert summary == {"done": 1, "failed": 2, "stopped": False}
    assert ran == [{"n": 1}]
    assert q.get("ok", "1") is None
    bad_e = q.get("bad", "2")
    assert bad_e.reason == "502 from POST /repos/acme/a/issues" and "secret" not in bad_e.reason
    assert q.get("mystery", "3").reason == "no handler for mystery"


def test_run_due_stops_early(store):
    clock = Clock()
    q = RetryQueue(store, clock=clock)
    for i, key in enumerate(["a", "b", "c"]):
        clock.t = 1_000 + i
        q.put("k", key, {"key": key})
    calls = []

    def handler(p):
        calls.append(p["key"])
        if p["key"] == "b":
            raise _status(429)

    summary = run_due(q, {"k": handler},
                      stop=lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429)
    assert summary == {"done": 1, "failed": 1, "stopped": True}
    assert calls == ["a", "b"]                   # c not tried
    assert q.get("k", "c").attempts == 0
    assert q.get("k", "b").attempts == 1


def test_short_reason_never_has_bodies():
    assert short_reason(_status(500)) == "500 from POST /repos/acme/a/issues"
    req = httpx.Request("GET", "http://plane.local/api/v1/x")
    assert short_reason(httpx.ConnectError("refused", request=req)) == "ConnectError on GET /api/v1/x"
    assert short_reason(httpx.ConnectError("refused")) == "ConnectError"
    assert short_reason(ValueError("item title here")) == "ValueError"
