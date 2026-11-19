"""PR #9 x #32: one GitHub pacer per credential, capped interactive waits, the
"needs the personal token" memory, and no-credential queue entries that back off
without touching Plane."""
import json

import httpx
import pytest

from bridge.clients import MAX_WAIT, GitHubClient, PaceTooLong
from bridge.github_auth import GitHubAuth

from test_engine_e2e import REPO, created, env, run_queue_after  # noqa: F401

WALL = 1_800_000_000.0


class FakeApp:
    """Just enough of AppTokens: per-account tokens and cached state, no network."""

    def __init__(self, tokens=None, missing=()):
        self.tokens = dict(tokens or {})
        self.missing = {m.lower() for m in missing}
        self.forgotten = []

    def token_for(self, owner, repo=None):
        return None if owner.lower() in self.missing else self.tokens.get(owner.lower())

    def cached_state(self, owner):
        if owner.lower() in self.missing:
            return "missing"
        return "token" if owner.lower() in self.tokens else "unknown"

    def forget(self, owner):
        self.forgotten.append(owner)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def client(handler, auth):
    clock, sleeps = Clock(), []

    def sleep(s):
        sleeps.append(s)
        clock.t += s
    gh = GitHubClient(auth, transport=httpx.MockTransport(handler), sleep=sleep, clock=clock,
                      wall=lambda: WALL)
    return gh, clock, sleeps


def limits(remaining, reset_in, limit=5000):
    return {"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(int(WALL + reset_in)),
            "x-ratelimit-limit": str(limit), "x-ratelimit-resource": "core"}


# ---------------------------------------------------------------- S1

def test_each_credential_has_its_own_pacer():
    """Auditor repro (pacer_mix.py): a nearly spent account no longer slows others."""
    def handler(req):
        token = req.headers["Authorization"].split()[1]
        h = limits(4, 1800) if token == "A" else limits(4990, 3500)
        return httpx.Response(200, headers=h, json={"full_name": "x"})

    gh, clock, sleeps = client(handler, GitHubAuth(pat="P", app=FakeApp({"acme-a": "A", "acme-b": "B"})))
    gh.get_repo("acme-a/one")
    for _ in range(6):
        gh.get_repo("acme-b/two")                  # B's budget is fresh: no waiting
    assert sum(sleeps) < 1
    snaps = gh.pacer_snapshots()
    assert snaps["app:acme-a"]["remaining"] == 4 and snaps["app:acme-b"]["remaining"] > 4900


def test_long_interactive_wait_fails_fast_instead_of_sleeping():
    def handler(req):
        return httpx.Response(200, headers=limits(2, 3600), json={"full_name": "x"})

    gh, clock, sleeps = client(handler, GitHubAuth(pat="P", app=FakeApp({"acme-a": "A"})))
    with pytest.raises(PaceTooLong):              # a slot ~30 minutes away: queued, not slept
        for _ in range(20):
            gh.get_repo("acme-a/one")
    assert all(s <= MAX_WAIT for s in sleeps)


def test_a_rate_limit_hold_pauses_only_that_credential():
    hits = {"A": 0}

    def handler(req):
        token = req.headers["Authorization"].split()[1]
        if token == "A" and hits["A"] == 0:
            hits["A"] += 1
            return httpx.Response(403, headers={"Retry-After": "30"}, json={"message": "secondary rate limit"})
        return httpx.Response(200, headers=limits(4000, 3000), json={"full_name": "x"})

    gh, clock, sleeps = client(handler, GitHubAuth(pat="P", app=FakeApp({"acme-a": "A", "acme-b": "B"})))
    gh.get_repo("acme-a/one")                      # 403 rate limit: A holds and retries
    assert gh.pacer_for("app:acme-a").snapshot()["hold_in"] >= 0
    sleeps.clear()
    gh.get_repo("acme-b/two")
    assert sleeps == []                            # B isn't held by A's limit
    assert gh.pacer_for("app:acme-b").snapshot()["hold_in"] == 0


def test_pat_fallback_is_paced_by_the_pat_and_remembered():
    seen = []

    def handler(req):
        token = req.headers["Authorization"].split()[1]
        seen.append(token)
        if token == "C":                           # the installation can't see this repo
            return httpx.Response(404, headers=limits(4500, 3000), json={"message": "Not Found"})
        return httpx.Response(200, headers=limits(123, 3000), json={"full_name": "acme-c/private"})

    auth = GitHubAuth(pat="P", app=FakeApp({"acme-c": "C"}))
    gh, clock, sleeps = client(handler, auth)
    assert gh.get_repo("acme-c/private")["full_name"] == "acme-c/private"
    assert seen == ["C", "P"]
    snaps = gh.pacer_snapshots()
    assert snaps["pat"]["remaining"] == 123 and snaps["app:acme-c"]["remaining"] == 4500
    seen.clear()
    gh.get_repo("acme-c/private")                  # remembered: one request, with the PAT
    assert seen == ["P"]
    auth.forget("acme-c")                          # an installation event clears it
    seen.clear()
    gh.get_repo("acme-c/private")
    assert seen == ["C", "P"]


def test_a_404_both_credentials_give_isnt_remembered():
    seen = []

    def handler(req):
        seen.append(req.headers["Authorization"].split()[1])
        return httpx.Response(404, json={"message": "Not Found"})

    gh, clock, sleeps = client(handler, GitHubAuth(pat="P", app=FakeApp({"acme": "A"})))
    gh._request("GET", "/repos/acme/one/issues/99")
    gh._request("GET", "/repos/acme/one/issues/98")
    assert seen == ["A", "P", "A", "P"]            # a deleted issue doesn't send the repo to the PAT


# ---------------------------------------------------------------- S2

def no_creds(bridge, app):
    bridge.gh.auth.pat = ""
    bridge.gh.auth.app = app


def test_queued_entries_without_credentials_back_off_without_reading_plane(env):
    bridge, world, clock = env
    world.add_item(1)
    app = FakeApp(missing=["acme"])
    no_creds(bridge, app)
    assert bridge.handle_plane(created()) == "queued: no GitHub credentials for this repo yet"
    assert bridge.queue.get("create_issue", "item-1").payload["cred_repo"] == REPO
    plane_reads = world.calls[("plane", "GET")]
    bridge.queue.max_age = 10_000                  # keep the stuck flag (Plane label calls) out of the count
    for _ in range(4):
        run_queue_after(bridge, clock, 130)        # every retry falls due (within 10 minutes)
    assert world.calls[("plane", "GET")] == plane_reads     # no Plane calls while it can't sign
    entry = bridge.queue.get("create_issue", "item-1")
    assert entry.attempts == 4 and entry.next_at > clock.t
    # The app is installed on acme: the installation event makes the entry due now
    app.missing.clear()
    app.tokens["acme"] = "A"
    assert bridge.github_account_changed("acme") == 1
    assert app.forgotten == ["acme"]
    bridge.run_queue()
    assert len(world.issues) == 1 and bridge.queue.stats()["length"] == 0


def test_stuck_label_isnt_retried_against_github_without_credentials(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    no_creds(bridge, FakeApp(missing=["acme"]))
    bridge.diag.report("stuck", plane_project_id="proj-1", plane_item_id="item-1", repo=REPO,
                       issue_number=1, key="x")
    label_calls = lambda: sum(n for (side, m), n in world.calls.items() if side == "github")  # noqa: E731
    before = label_calls()
    bridge.diag.reflag_pending()
    bridge.diag.reflag_pending()
    assert label_calls() == before                 # GitHub never called for the label
    assert bridge.diag.counts()["flags_pending"] >= 1
    no_creds(bridge, FakeApp({"acme": "A"}))       # credentials arrive
    bridge.diag.reflag_pending()
    assert "plane-sync-problem" in world.issues[1]["labels"]


def _queue_for_repo_a(bridge, world, monkeypatch):
    world.add_item(1)
    where = {"repo": "other/api"}
    monkeypatch.setattr(bridge, "_repo_for", lambda pid, item: where["repo"])
    no_creds(bridge, FakeApp({"acme": "A"}, missing=["other", "third"]))
    assert bridge.handle_plane(created()).startswith("queued: no GitHub credentials")
    entry = bridge.queue.get("create_issue", "item-1")
    assert entry.payload["cred_repo"] == "other/api" and "repo" not in entry.payload
    return where


def _watch_github(bridge, world):
    paths = []
    orig = world.github

    def github(req):
        paths.append(req.url.path)
        return orig(req)
    bridge.gh.http._transport = httpx.MockTransport(github)
    return paths


def test_item_moved_while_queued_gets_its_issue_in_the_new_repo(env, monkeypatch):
    """Queued with no credentials for repo A, then moved to a module for repo B
    (which has credentials): the issue is made in B, nothing in A. The retry backs
    off on A without Plane reads, and re-resolves the item's repo every 10 minutes."""
    bridge, world, clock = env
    where = _queue_for_repo_a(bridge, world, monkeypatch)
    paths = _watch_github(bridge, world)
    where["repo"] = REPO                                   # moved to the acme/api module
    run_queue_after(bridge, clock, 6)
    assert bridge.queue.get("create_issue", "item-1").attempts == 1 and not world.issues
    clock.t += 601                                         # time to look the repo up again
    bridge.run_queue()
    assert bridge.queue.get("create_issue", "item-1") is None
    assert bridge.store.by_plane_item("item-1").repo == REPO and len(world.issues) == 1
    assert not any("/other/" in p for p in paths)


def test_membership_pass_notices_a_queued_item_moved_to_another_repo(env, monkeypatch):
    bridge, world, clock = env
    _queue_for_repo_a(bridge, world, monkeypatch)
    monkeypatch.undo()                                     # the real module lookup: acme/api
    no_creds(bridge, FakeApp({"acme": "A"}, missing=["other"]))
    paths = _watch_github(bridge, world)
    assert bridge.sync_linked_modules() == f"created {REPO}#1"
    run_queue_after(bridge, clock, 601)                    # the queued entry finds it, adds nothing
    assert len(world.issues) == 1 and bridge.queue.stats()["length"] == 0
    assert not any("/other/" in p for p in paths)


def test_moved_to_another_repo_without_credentials_updates_the_check(env, monkeypatch):
    bridge, world, clock = env
    where = _queue_for_repo_a(bridge, world, monkeypatch)
    where["repo"] = "third/api"
    run_queue_after(bridge, clock, 601)
    entry = bridge.queue.get("create_issue", "item-1")
    assert entry.payload["cred_repo"] == "third/api" and not world.issues
