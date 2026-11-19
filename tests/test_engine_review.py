"""Fixes from the three-part review of the sync engine (ALL-103): snapshots from both
sides, first contact, duplicate creates, echoes, lost comments, recency, queued
intents, switches, labels that clear, instant sync, check-time failures, large repos,
persistence. Several tests began as the reviewers' repros, turned to assert the fix."""
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import build_app
from bridge.clients import LANE_BACKGROUND, GitHubClient, PlaneClient
from bridge.config import load_settings
from bridge.reconcile import get_snapshot, record_snapshot
from bridge.store import Link, Store
from bridge.sync import Bridge

from test_engine_e2e import MOD, PROJ, REPO, Clock, World, created, env, module_updated, run_queue_after  # noqa: F401


def bridge_on(tmp_path, world, clock, settings):
    b = Bridge(settings, Store(str(tmp_path / "b.db")),
               PlaneClient(settings.plane_base_url, "acme", "k", transport=httpx.MockTransport(world.plane),
                           sleep=lambda _s: None, max_retries=0),
               GitHubClient("t", transport=httpx.MockTransport(world.github), sleep=lambda _s: None))
    b.plane.cache_seconds = 0
    b.clock = clock
    b.debounce.clock = clock
    b.scheduler.clock = clock
    return b


def check_module(bridge, clock):
    bridge.debounce.clock = clock
    bridge.debounce.touch(f"module:{PROJ}:{MOD}")
    clock.t += 31
    return bridge.run_reconciles()


def existing_pair(bridge, world, plane_state, gh_state, plane_upd, gh_upd, **gh_extra):
    """A pair made by the bridge before the upgrade: a link row, no snapshot."""
    item = world.add_item(1, state=plane_state)
    world.items[item]["updated_at"] = plane_upd
    world.issues[1] = {"number": 1, "title": "Item 1", "body": "", "state": gh_state,
                       "html_url": f"https://github.com/{REPO}/issues/1", "created_at": "2026-10-01T00:00:00Z",
                       "closed_at": None, "updated_at": gh_upd, "labels": [], **gh_extra}
    bridge.store.save(Link(item, PROJ, REPO, 1, world.issues[1]["html_url"], plane_linked=True))
    return item


def issue_event(action, title="Item 1", **issue):
    return {"action": action, "repository": {"full_name": REPO},
            "issue": {"number": 1, "title": title, "state": "closed" if action == "closed" else "open", **issue}}


def gh_does(bridge, world, action, delivery, **issue):
    """The issue really changes on GitHub, then its webhook arrives."""
    if action in ("closed", "reopened"):
        world.issues[1]["state"] = "closed" if action == "closed" else "open"
        world.issues[1]["state_reason"] = issue.get("state_reason")
    if action == "edited" and "title" in issue:
        world.issues[1]["title"] = issue["title"]
    event = issue_event(action, **issue)
    if action == "edited":
        event["changes"] = {"title": {"from": "old"}}
    return bridge.handle_github("issues", event, delivery)


def plane_updated(eid, prev, **data):
    e = created(eid)
    e["event"] = "workitem.updated"
    e["previous_attributes"] = prev
    e["data"].update(data)
    return e


# ---------------------------------------------------------------- 1. snapshots from both sides

@pytest.mark.parametrize("state,reason", [("st-done", "completed"), ("st-cancel", "not_planned")])
def test_issue_for_an_item_already_closed_in_plane_is_closed_too(env, state, reason):
    bridge, world, clock = env
    world.add_item(1, state=state)
    assert "closed on GitHub" in bridge.handle_plane(created())
    assert world.issues[1]["state"] == "closed" and world.issues[1]["state_reason"] == reason
    s = get_snapshot(bridge.store, "item-1")
    assert s.p_closed and s.g_closed


def test_membership_created_issue_for_done_item_stays_done_after_a_check(env):
    """Chaperone B1: the check used to reopen a Done item whose issue was created open."""
    bridge, world, clock = env
    world.add_item(1, state="st-done")
    bridge.sync_linked_modules()
    assert world.issues[1]["state"] == "closed"
    check_module(bridge, clock)
    assert world.items["item-1"]["state"] == "st-done"


def merged_pr(number=7, branch="1-scan", body="", base="main", head_repo=REPO):
    pr = {"number": number, "title": "scan", "body": body, "state": "closed", "merged": True, "draft": False,
          "html_url": f"https://github.com/{REPO}/pull/{number}",
          "head": {"ref": branch, "repo": {"full_name": head_repo}},
          "base": {"ref": base}, "updated_at": "u"}
    return {"action": "closed", "pull_request": pr, "sender": {"login": "d"},
            "repository": {"full_name": REPO, "default_branch": "main"}}


def test_merged_branch_pr_closes_the_issue_and_says_why_once(env):
    """Aaban's decision (round 2): a merge from branch 1-scan moves Plane to Done, so
    the bridge closes issue #1 as completed with one comment, notes it, and records
    both real sides."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    r = bridge.handle_github("pull_request", merged_pr(), "p1")
    assert "moved to done" in r and "closed on GitHub" in r and "comment posted" in r
    assert world.items["item-1"]["state"] == "st-done"
    assert world.issues[1]["state"] == "closed" and world.issues[1]["state_reason"] == "completed"
    (comment,) = world.gh_comments[1]
    assert "pull request #7" in comment and "Done" in comment
    s = get_snapshot(bridge.store, "item-1")
    assert s.p_closed is True and s.g_closed is True
    notes = bridge.diag.open_problems(kind="closed_by_merge", include_resolved_since=0)
    assert len(notes) == 1 and bridge.diag.counts()["open"] == 0      # informational, no label
    assert world.issues[1]["labels"] == []
    # GitHub's close webhook is the bridge's own; a redelivered merge doesn't comment again
    assert gh_does(bridge, world, "closed", "d1", state_reason="completed") == "ignored: the bridge's own change"
    bridge.handle_github("pull_request", merged_pr(), "p1-again")
    assert len(world.gh_comments[1]) == 1
    check_module(bridge, clock)
    assert world.items["item-1"]["state"] == "st-done" and world.issues[1]["state"] == "closed"


def test_merged_pr_with_a_closing_keyword_leaves_closing_to_github(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    bridge.handle_github("pull_request", merged_pr(branch="feature-x", body="Closes #1"), "p1")
    assert world.items["item-1"]["state"] == "st-done"
    assert world.issues[1]["state"] == "open" and 1 not in world.gh_comments


def test_failed_comment_after_merge_close_is_retried_once(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.fail[("POST", rf"github:/repos/{REPO}/issues/1/comments$")] = [503]
    assert "queued" in bridge.handle_github("pull_request", merged_pr(), "p1")
    assert world.issues[1]["state"] == "closed" and 1 not in world.gh_comments
    run_queue_after(bridge, clock, 6)
    run_queue_after(bridge, clock, 60)
    assert len(world.gh_comments[1]) == 1


# ---------------------------------------------------------------- 2. first contact

def test_first_scan_doesnt_reopen_a_done_item(env):
    """Auditor: Done in Plane, issue open and touched later on GitHub."""
    bridge, world, clock = env
    item = existing_pair(bridge, world, "st-done", "open", "2026-11-01T00:00:00Z", "2026-11-10T00:00:00Z")
    bridge.run_org_scan()
    assert world.items[item]["state"] == "st-done" and world.issues[1]["state"] == "open"
    (problem,) = bridge.diag.open_problems(kind="state_differs")
    assert problem.plane_item_id == item
    assert "plane-sync-problem" in world.issues[1]["labels"]


def test_first_scan_doesnt_close_the_github_issue(env):
    bridge, world, clock = env
    existing_pair(bridge, world, "st-done", "open", "2026-11-10T00:00:00Z", "2026-11-01T00:00:00Z")
    bridge.run_org_scan()
    assert world.issues[1]["state"] == "open"
    assert bridge.diag.open_problems(kind="state_differs")


def test_first_contact_fix_closes_where_times_are_reliable(env):
    bridge, world, clock = env
    bridge.s.first_contact_fix = True
    bridge.reconciler.first_contact_fix = True
    item = existing_pair(bridge, world, "st-done", "open", "2026-11-01T00:00:00Z", "2026-11-10T00:00:00Z")
    world.items[item]["completed_at"] = "2026-11-05T00:00:00Z"     # after the issue was opened
    bridge.run_org_scan()
    assert world.issues[1]["state"] == "closed" and world.issues[1]["state_reason"] == "completed"
    assert bridge.diag.open_problems(kind="state_differs") == []


def test_state_differs_clears_once_someone_settles_it(env):
    bridge, world, clock = env
    existing_pair(bridge, world, "st-done", "open", "2026-11-01T00:00:00Z", "2026-11-10T00:00:00Z")
    bridge.run_org_scan()
    world.issues[1]["state"] = "closed"                     # a person closes the issue
    bridge.run_org_scan()
    assert bridge.diag.open_problems(kind="state_differs") == []
    assert "plane-sync-problem" not in world.issues[1]["labels"]


# ---------------------------------------------------------------- 3. no duplicate creates

def test_check_while_a_create_is_queued_doesnt_duplicate(env):
    """Auditor N2: the org scan ran while a lost-response create waited in the queue."""
    bridge, world, clock = env
    world.add_item(1)
    world.lose_create_response = 1
    assert bridge.handle_plane(created()).startswith("queued:")
    clock.t += 1
    bridge.run_org_scan()
    check_module(bridge, clock)
    run_queue_after(bridge, clock, 10)
    assert len(world.issues) == 1
    assert bridge.store.by_plane_item("item-1").issue_number == 1


def test_edit_after_a_lost_create_doesnt_duplicate(env):
    """Chaperone: an update webhook for the still-unlinked item."""
    bridge, world, clock = env
    world.add_item(1)
    world.lose_create_response = 1
    bridge.handle_plane(created())
    assert bridge.handle_plane(plane_updated("e2", {"name": "x"})).startswith("queued: already waiting")
    run_queue_after(bridge, clock, 6)
    assert len(world.issues) == 1


def test_membership_pass_after_a_lost_create_adopts_the_issue(env):
    bridge, world, clock = env
    world.add_item(1)
    world.lose_create_response = 1
    assert "1 item(s) failed" in bridge.sync_linked_modules()
    clock.t += 1
    bridge.sync_linked_modules()          # the queued entry is due: this pass finds the issue
    assert len(world.issues) == 1 and bridge.store.by_plane_item("item-1").issue_number == 1
    run_queue_after(bridge, clock, 10)
    assert len(world.issues) == 1 and bridge.queue.stats()["length"] == 0


def test_every_create_path_searches_for_an_earlier_issue_first(env):
    bridge, world, clock = env
    world.add_item(1)
    # An issue from an attempt the bridge has no record of (e.g. a crash after the POST)
    world.issues[1] = {"number": 1, "title": "Item 1", "state": "open", "labels": [],
                       "body": "Synced from Plane work item [API-1](https://x)",
                       "html_url": f"https://github.com/{REPO}/issues/1", "created_at": "c",
                       "closed_at": None, "updated_at": "2026-11-19T10:00:00Z"}
    assert bridge.handle_plane(created()) == f"linked {REPO}#1"
    assert len(world.issues) == 1


# ---------------------------------------------------------------- 4. echoes

def test_bridge_reopen_echo_doesnt_move_in_progress_to_todo(env):
    """Auditor B3."""
    bridge, world, clock = env
    item = world.add_item(1)
    bridge.handle_plane(created())
    world.issues[1]["state"] = "closed"
    record_snapshot(bridge.store, item, REPO, 1, p_title="Item 1", g_title="Item 1",
                    p_closed=True, g_closed=True, synced_at=clock.t)
    world.items[item]["state"] = "st-prog"          # reopened in Plane
    bridge.run_org_scan()
    assert world.issues[1]["state"] == "open"
    assert bridge.handle_github("issues", issue_event("reopened"), "d9") == "ignored: the bridge's own change"
    assert world.items[item]["state"] == "st-prog"


def test_reopen_while_plane_is_already_open_doesnt_move(env):
    bridge, world, clock = env
    world.add_item(1, state="st-prog")
    bridge.handle_plane(created())
    assert bridge.handle_github("issues", issue_event("reopened"), "d1") == "unchanged: already open in Plane"
    assert world.items["item-1"]["state"] == "st-prog"


def test_close_while_plane_is_already_done_doesnt_cancel(env):
    bridge, world, clock = env
    world.add_item(1, state="st-done")
    bridge.handle_plane(created())                  # creates and closes (completed)
    r = bridge.handle_github("issues", issue_event("closed", state_reason="not_planned"), "d1")
    assert r.startswith(("ignored", "unchanged"))
    assert world.items["item-1"]["state"] == "st-done"


def test_plane_echo_of_the_bridges_own_move_is_ignored(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    gh_does(bridge, world, "closed", "d1", state_reason="completed")
    assert world.items["item-1"]["state"] == "st-done"
    patches = world.calls[("github", "PATCH")]
    echo = plane_updated("e2", {"state": "st-todo"}, state="st-done")
    assert bridge.handle_plane(echo) == "ignored: linked item, no title/description/state change"
    assert world.calls[("github", "PATCH")] == patches


def test_title_echo_from_github_is_ignored(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.items["item-1"]["name"] = "Renamed"
    bridge.handle_plane(plane_updated("e2", {"name": "Item 1"}))
    assert world.issues[1]["title"] == "Renamed"
    r = bridge.handle_github("issues", {**issue_event("edited", "Renamed"),
                                        "changes": {"title": {"from": "Item 1"}}}, "d2")
    assert r == "ignored: the bridge's own change"


# ---------------------------------------------------------------- 5. lost comment response

def test_lost_comment_response_isnt_posted_twice(env):
    """Chaperone N3: the comment was posted, but the answer was a 502."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    orig = world.plane
    lost = {"n": 1}

    def plane(req):
        r = orig(req)
        if req.method == "POST" and req.url.path.endswith("/comments/") and lost["n"]:
            lost["n"] -= 1
            return httpx.Response(502, json={})
        return r

    bridge.plane.http._transport = httpx.MockTransport(plane)
    branch = {"ref_type": "branch", "ref": "1-scan", "repository": {"full_name": REPO}, "sender": {"login": "d"}}
    bridge.handle_github("create", branch, "d1")
    run_queue_after(bridge, clock, 6)
    assert len(world.comments["item-1"]) == 1
    assert bridge.queue.stats()["length"] == 0


# ---------------------------------------------------------------- 6. recency

def test_github_label_bump_doesnt_reopen_a_done_item(env):
    """Chaperone B2, with a snapshot."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.items["item-1"].update(state="st-done", updated_at="2026-11-19T11:00:00Z")
    world.issues[1]["updated_at"] = "2026-11-19T23:00:00Z"     # a label, later
    check_module(bridge, clock)
    assert world.items["item-1"]["state"] == "st-done" and world.issues[1]["state"] == "closed"


def test_without_a_snapshot_a_newer_github_bump_doesnt_reopen(env):
    """Chaperone B2, no snapshot: reported instead."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    with bridge.store._lock:
        bridge.store._db.execute("DELETE FROM pair_snapshot")
        bridge.store._db.commit()
    world.items["item-1"].update(state="st-done", updated_at="2026-11-19T11:00:00Z")
    world.issues[1]["updated_at"] = "2026-11-19T23:00:00Z"
    check_module(bridge, clock)
    assert world.items["item-1"]["state"] == "st-done"
    assert bridge.diag.open_problems(kind="state_differs")


# ---------------------------------------------------------------- 7. queued intents recompute

def test_queued_close_is_dropped_if_plane_was_reopened_meanwhile(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.items["item-1"]["state"] = "st-done"
    world.fail[("PATCH", rf"github:/repos/{REPO}/issues/1$")] = [503]
    assert bridge.handle_plane(plane_updated("e2", {"state": "st-todo"}, state="st-done")).startswith("queued")
    world.items["item-1"]["state"] = "st-prog"             # reopened before the retry
    patches = world.calls[("github", "PATCH")]
    run_queue_after(bridge, clock, 6)
    assert world.issues[1]["state"] == "open"
    assert world.calls[("github", "PATCH")] == patches     # decided again: nothing to do


def test_opposite_intents_coalesce_under_one_key(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.fail[("PATCH", r"plane:.*/issues/item-1/$")] = [503]
    gh_does(bridge, world, "closed", "d1", state_reason="completed")
    assert gh_does(bridge, world, "reopened", "d2").startswith("queued: behind")
    entries = [e for e in bridge.queue.due(50) if e.key == "item-1"]
    assert len(entries) == 1 and entries[0].payload["target"] == "todo"
    run_queue_after(bridge, clock, 6)
    assert world.items["item-1"]["state"] == "st-todo"


# ---------------------------------------------------------------- 8. separate switches

def test_membership_interval_zero_leaves_queue_and_checks_running(env):
    bridge, world, clock = env
    assert bridge.s.sync_interval == 0
    before = {t.name for t in threading.enumerate()}
    with TestClient(build_app(bridge)):
        names = [t.name for t in threading.enumerate()]
        assert "retry-queue" in names and "checks" in names
        assert names.count("module-sync") == len([n for n in before if n == "module-sync"])


@pytest.mark.parametrize("env_vars,interval,reconcile", [
    ({"BRIDGE_MEMBERSHIP_INTERVAL": "0"}, 0, True),
    ({"BRIDGE_SYNC_INTERVAL": "0"}, 0, True),
    ({"BRIDGE_RECONCILE": "0"}, 120, False),
    ({"BRIDGE_RECONCILE": ""}, 120, True),
])
def test_switch_settings(monkeypatch, env_vars, interval, reconcile):
    base = dict(PLANE_BASE_URL="x", PLANE_PUBLIC_URL="x", PLANE_WORKSPACE="w", PLANE_API_KEY="k",
                GITHUB_TOKEN="t", GITHUB_WEBHOOK_SECRET="s")
    for k in ("BRIDGE_MEMBERSHIP_INTERVAL", "BRIDGE_SYNC_INTERVAL", "BRIDGE_RECONCILE",
              "BRIDGE_FIRST_CONTACT_FIX"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**base, **env_vars}.items():
        monkeypatch.setenv(k, v)
    s = load_settings("/nonexistent")
    assert s.sync_interval == interval and s.reconcile is reconcile and s.first_contact_fix is False


# ---------------------------------------------------------------- 9. labels that clear

def test_refused_clears_when_the_item_is_edited_and_then_syncs(env):
    bridge, world, clock = env
    world.add_item(1)
    world.fail[("POST", rf"github:/repos/{REPO}/issues$")] = [410]
    bridge.handle_plane(created())
    assert bridge.diag.open_problems(kind="refused")
    bridge.handle_plane(plane_updated("e2", {"name": "x"}))      # edited, and GitHub allows it now
    assert bridge.diag.open_problems(kind="refused") == []
    assert len(world.issues) == 1


def test_issue_missing_clears_when_the_issue_is_found(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    saved = world.issues.pop(1)
    bridge.run_org_scan()
    assert bridge.diag.open_problems(kind="issue_missing")
    world.issues[1] = saved
    bridge.run_org_scan()
    assert bridge.diag.open_problems(kind="issue_missing") == []


# ---------------------------------------------------------------- 10. instant sync

def test_plane_state_change_closes_the_issue_at_once(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.items["item-1"]["state"] = "st-cancel"
    r = bridge.handle_plane(plane_updated("e2", {"state": "st-todo"}, state="st-cancel"))
    assert "closed on GitHub" in r
    assert world.issues[1]["state"] == "closed" and world.issues[1]["state_reason"] == "not_planned"
    # GitHub's webhook for that close is the bridge's own change
    assert bridge.handle_github("issues", issue_event("closed", state_reason="not_planned"), "d1") \
        == "ignored: the bridge's own change"


def test_plane_state_change_on_a_pair_without_snapshot_still_syncs(env):
    bridge, world, clock = env
    item = existing_pair(bridge, world, "st-todo", "open", "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z")
    world.items[item]["state"] = "st-done"
    bridge.handle_plane(plane_updated("e2", {"state": "st-todo"}, state="st-done"))
    assert world.issues[1]["state"] == "closed"


def test_github_title_edit_reaches_plane_at_once(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.issues[1]["title"] = "Better title"
    r = bridge.handle_github("issues", {**issue_event("edited", "Better title"),
                                        "changes": {"title": {"from": "Item 1"}}}, "d1")
    assert "title to Plane" in r and world.items["item-1"]["name"] == "Better title"
    # Plane's webhook for that rename is the bridge's own change: no push back
    patches = world.calls[("github", "PATCH")]
    assert bridge.handle_plane(plane_updated("e2", {"name": "Item 1"}, name="Better title")) \
        == "ignored: linked item, no title/description/state change"
    assert world.calls[("github", "PATCH")] == patches


def test_github_body_edit_is_ignored(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    r = bridge.handle_github("issues", {**issue_event("edited"), "changes": {"body": {"from": "x"}}}, "d1")
    assert r == "ignored: issue edited (not the title)"


# ---------------------------------------------------------------- 11. check-time write failures

def test_rate_limit_on_a_check_fix_queues_it_in_the_background_lane_and_stops(env):
    bridge, world, clock = env
    for n in (1, 2):
        world.add_item(n)
        bridge.handle_plane(created(f"e{n}", f"item-{n}"))
        world.items[f"item-{n}"]["state"] = "st-done"          # both need closing on GitHub
    world.fail[("PATCH", rf"github:/repos/{REPO}/issues/\d+$")] = [429] * 10
    summary = bridge.run_org_scan()
    assert summary.startswith("stopped early (rate)")
    entries = bridge.queue.due(50)
    assert [e.key for e in entries] == ["item-1"]               # the second pair wasn't tried
    assert entries[0].payload["lane"] == LANE_BACKGROUND
    assert bridge.scheduler.last_class is None


# ---------------------------------------------------------------- 12. large repos

def test_debounced_check_uses_since_and_org_scan_lists_everything(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    seen = []
    orig = world.github

    def github(req):
        if req.method == "GET" and req.url.path == f"/repos/{REPO}/issues" and "sort" not in req.url.params:
            seen.append(req.url.params.get("since"))
        return orig(req)

    bridge.gh.http._transport = httpx.MockTransport(github)
    check_module(bridge, clock)            # first check of the repo: full listing
    check_module(bridge, clock)            # then only changes since
    bridge.run_org_scan()                  # the org scan: full listing again
    assert seen[0] is None and seen[1] is not None and seen[2] is None


# ---------------------------------------------------------------- 13. persistence

def test_pending_checks_and_scan_schedule_survive_a_restart(env, tmp_path):
    bridge, world, clock = env
    bridge.handle_plane(module_updated("m1"))
    bridge.scheduler.record(10, 10, 0.1, 0.0)               # light: next in 6 hours
    next_run = bridge.scheduler.next_run
    again = bridge_on(tmp_path, world, clock, bridge.s)     # same database
    assert again.debounce.pending() == 1
    assert again.scheduler.next_run == next_run and again.scheduler.last_class == "light"
    clock.t += 31
    assert again.run_reconciles() == [REPO]


def test_slow_scan_missed_its_quiet_window_waits_for_the_next(env):
    bridge, world, clock = env
    s = bridge.scheduler
    s.quiet_start, s.quiet_end = 1, 5
    clock.t = 1_800_000_000.0                                 # some time of day, UTC
    s.record(600, 10, 1.0, 0.0)                              # slow: next quiet window
    assert s.quiet_only and s.in_quiet(s.next_run)
    clock.t = s.next_run + 5 * 3600                          # the bridge was down all night
    assert not s.in_quiet(clock.t)
    assert s.due() is False
    assert s.in_quiet(s.next_run) and s.next_run > clock.t


# ---------------------------------------------------------------- nits

def test_status_pages_are_rate_limited_per_address(env):
    bridge, world, clock = env
    from bridge.diagnostics import make_router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(make_router(bridge.diag, "http://plane.local", "acme", per_minute=3,
                                   trust_proxy_headers=True,
                                   transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))))
    with TestClient(app) as client:
        codes = [client.get("/status/API-1", headers={"x-api-key": "k", "cf-connecting-ip": "1.2.3.4"}).status_code
                 for _ in range(4)]
        other = client.get("/status/API-1", headers={"x-api-key": "k", "cf-connecting-ip": "5.6.7.8"})
    assert codes == [404, 404, 404, 429] and other.status_code == 404


def test_proxy_headers_are_ignored_unless_trusted(env):
    bridge, world, clock = env
    from bridge.diagnostics import make_router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(make_router(bridge.diag, "http://plane.local", "acme", per_minute=2,
                                   transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))))
    with TestClient(app) as client:
        # A caller rotating forged addresses is still one socket address.
        codes = [client.get("/status/API-1", headers={"x-api-key": "k", "cf-connecting-ip": f"9.9.9.{i}"}).status_code
                 for i in range(3)]
    assert codes == [404, 404, 429]


def test_remembered_addresses_are_capped(env):
    bridge, world, clock = env
    from bridge.diagnostics import make_router
    from fastapi import FastAPI
    t = [0.0]
    router = make_router(bridge.diag, "http://plane.local", "acme", per_minute=1, trust_proxy_headers=True,
                         max_clients=5, clock=lambda: t[0],
                         transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for i in range(20):
            t[0] += 0.1
            client.get("/status/API-1", headers={"x-api-key": "k", "cf-connecting-ip": f"10.0.0.{i}"})
        # the oldest address was forgotten, so it may ask again; the newest may not
        assert client.get("/status/API-1", headers={"x-api-key": "k", "cf-connecting-ip": "10.0.0.0"}).status_code == 404
        assert client.get("/status/API-1", headers={"x-api-key": "k", "cf-connecting-ip": "10.0.0.19"}).status_code == 429


def test_org_scan_stops_between_pairs_on_shutdown(env):
    bridge, world, clock = env
    world.add_item(1)
    summary = bridge.run_org_scan(stop=lambda: True)
    assert summary.startswith("stopped for shutdown")
    assert not world.issues and bridge.scheduler.last_class is None
