"""Fixes from the second review of the sync engine (ALL-103): queued intents in
arrival order, one echo per write, the done() race, the create look-back, per-module
`since`, and quiet-hours validation. Several tests began as the reviewers' repros
(auditor test_aud2.py, chaperone test_r2.py / test_r2b.py), turned to assert the fix."""
import httpx
import pytest

from bridge.config import load_settings
from bridge.reconcile import get_snapshot, last_checked, set_checked

from test_engine_e2e import PROJ, REPO, created, env, run_queue_after  # noqa: F401
from test_engine_review import gh_does, plane_updated


def plane_state(eid, new, old):
    return plane_updated(eid, {"state": old}, state=new)


def plane_name(eid, new, old):
    return plane_updated(eid, {"name": old}, name=new)


# ---------------------------------------------------------------- 1. arrival order

def test_newer_plane_move_beats_an_older_queued_github_close(env):
    """Auditor N1 / chaperone N7: a queued "move to Done" from GitHub must not
    overwrite a later move in Plane."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.fail[("PATCH", r"plane:.*/issues/item-1/$")] = [503]
    assert gh_does(bridge, world, "closed", "d1", state_reason="completed").startswith("queued")
    world.items["item-1"]["state"] = "st-prog"                  # the user, later
    assert bridge.handle_plane(plane_state("e2", "st-prog", "st-todo")).startswith("queued: behind")
    entry = bridge.queue.get("sync_item", "item-1")
    assert "target" not in entry.payload and entry.payload["prefer"] == {"closed": "plane"}
    run_queue_after(bridge, clock, 6)
    assert world.items["item-1"]["state"] == "st-prog"
    assert world.issues[1]["state"] == "open"                   # the newer Plane side won
    s = get_snapshot(bridge.store, "item-1")
    assert (s.p_closed, s.g_closed) == (False, False)


def test_newer_github_event_beats_an_older_queued_plane_change(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.fail[("PATCH", rf"github:/repos/{REPO}/issues/1$")] = [503]
    world.items["item-1"]["state"] = "st-done"
    assert bridge.handle_plane(plane_state("e2", "st-done", "st-todo")).startswith("queued")
    world.fail.clear()
    gh_does(bridge, world, "reopened", "d1")                    # someone reopens on GitHub, later
    entry = bridge.queue.get("sync_item", "item-1")
    assert entry.payload["target"] == "todo" and "closed" not in entry.payload["prefer"]
    run_queue_after(bridge, clock, 6)
    assert world.items["item-1"]["state"] == "st-todo" and world.issues[1]["state"] == "open"


def test_queued_github_move_is_rechecked_against_github_before_running(env):
    """The issue was reopened again (webhook lost): the old "move to Done" is
    superseded, and the snapshot records GitHub as it is now."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.fail[("PATCH", r"plane:.*/issues/item-1/$")] = [503]
    gh_does(bridge, world, "closed", "d1", state_reason="completed")
    world.issues[1]["state"] = "open"                           # no webhook for this one
    run_queue_after(bridge, clock, 6)
    assert world.items["item-1"]["state"] == "st-todo"
    s = get_snapshot(bridge.store, "item-1")
    assert s.g_closed is False and s.p_closed is False


# ---------------------------------------------------------------- 2. echoes

def test_github_flip_back_within_the_window_is_not_an_echo(env):
    """Auditor: the bridge closed the issue; a person reopens, then closes again."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.items["item-1"]["state"] = "st-done"
    bridge.handle_plane(plane_state("e2", "st-done", "st-todo"))
    assert world.issues[1]["state"] == "closed"
    clock.t += 60
    gh_does(bridge, world, "reopened", "u1")
    assert world.items["item-1"]["state"] == "st-todo"
    clock.t += 60
    assert gh_does(bridge, world, "closed", "u2", state_reason="completed") != "ignored: the bridge's own change"
    assert world.items["item-1"]["state"] == "st-done"


def test_plane_state_round_trip_reaches_github(env):
    """Chaperone: closed on GitHub, Plane to In Progress, then back to Done."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    gh_does(bridge, world, "closed", "g1", state_reason="completed")
    assert world.items["item-1"]["state"] == "st-done"
    clock.t += 60
    world.items["item-1"]["state"] = "st-prog"
    bridge.handle_plane(plane_state("p2", "st-prog", "st-done"))
    assert world.issues[1]["state"] == "open"
    clock.t += 60
    world.items["item-1"]["state"] = "st-done"
    r = bridge.handle_plane(plane_state("p3", "st-done", "st-prog"))
    assert "closed on GitHub" in r and world.issues[1]["state"] == "closed"


def test_plane_title_round_trip_reaches_github(env):
    """Chaperone: B written to Plane by the bridge, then C, then back to B."""
    bridge, world, clock = env
    world.add_item(1, name="A")
    bridge.handle_plane(created())
    gh_does(bridge, world, "edited", "t1", title="B")
    assert world.items["item-1"]["name"] == "B"
    clock.t += 30
    world.items["item-1"]["name"] = "C"
    bridge.handle_plane(plane_name("t2", "C", "B"))
    assert world.issues[1]["title"] == "C"
    clock.t += 30
    world.items["item-1"]["name"] = "B"
    bridge.handle_plane(plane_name("t3", "B", "C"))
    assert world.issues[1]["title"] == "B"


def test_github_title_round_trip_reaches_plane(env):
    bridge, world, clock = env
    world.add_item(1, name="A")
    bridge.handle_plane(created())
    world.items["item-1"]["name"] = "B"
    bridge.handle_plane(plane_name("t1", "B", "A"))             # the bridge writes B on GitHub
    assert world.issues[1]["title"] == "B"
    gh_does(bridge, world, "edited", "g1", title="C")
    assert world.items["item-1"]["name"] == "C"
    r = gh_does(bridge, world, "edited", "g2", title="B")       # back to B, within the window
    assert r != "ignored: the bridge's own change" and world.items["item-1"]["name"] == "B"


def test_one_write_matches_one_echo(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.items["item-1"]["state"] = "st-done"
    bridge.handle_plane(plane_state("e2", "st-done", "st-todo"))       # the bridge closes #1
    issue = {"state_reason": "completed"}
    assert gh_does(bridge, world, "closed", "d1", **issue) == "ignored: the bridge's own change"
    # A second delivery of "closed" (different id) isn't swallowed as an echo again
    assert gh_does(bridge, world, "closed", "d2", **issue) != "ignored: the bridge's own change"


def test_echo_needs_the_snapshot_to_still_hold_the_value(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    bridge.store.record_own_write("item-1", "gh_closed", True, clock.t)   # a write the snapshot never saw
    r = gh_does(bridge, world, "closed", "d1", state_reason="completed")
    assert r != "ignored: the bridge's own change" and world.items["item-1"]["state"] == "st-done"


# ---------------------------------------------------------------- 4. done() race

def test_intent_merged_while_the_handler_ran_is_not_deleted(env):
    """Auditor N3: a reopen merged in after the handler, before done()."""
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.fail[("PATCH", r"plane:.*/issues/item-1/$")] = [503]
    gh_does(bridge, world, "closed", "d1", state_reason="completed")
    real_done = bridge.queue.done
    fired = []

    def done(entry_id, version=None):
        if not fired:
            fired.append(1)
            assert gh_does(bridge, world, "reopened", "d2").startswith("queued: behind")
        return real_done(entry_id, version=version)

    bridge.queue.done = done
    run_queue_after(bridge, clock, 6)
    assert bridge.queue.stats()["length"] == 1                  # the reopen is still there
    assert bridge.queue.due(10)                                 # and due now
    run_queue_after(bridge, clock, 1)
    assert world.items["item-1"]["state"] == "st-todo" and bridge.queue.stats()["length"] == 0


# ---------------------------------------------------------------- 6. create look-back

def test_retry_after_a_long_wait_finds_its_issue_past_the_newest_30(env):
    bridge, world, clock = env
    world.add_item(1)
    world.lose_create_response = 1
    assert bridge.handle_plane(created()).startswith("queued")
    clock.t += 600
    for n in range(2, 45):                       # 43 newer issues made by people meanwhile
        world.issues[n] = {"number": n, "title": f"Other {n}", "body": "", "state": "open", "labels": [],
                           "html_url": f"https://github.com/{REPO}/issues/{n}", "closed_at": None,
                           "created_at": world.stamp(), "updated_at": world.stamp()}
    run_queue_after(bridge, clock, 6)
    assert len(world.issues) == 44
    assert bridge.store.by_plane_item("item-1").issue_number == 1


# ---------------------------------------------------------------- 7. since per module

def test_since_stamp_is_per_module(env):
    bridge, world, clock = env
    set_checked(bridge.store, REPO, "mod-a", 1000.0)
    assert last_checked(bridge.store, REPO, "mod-a") == 1000.0
    assert last_checked(bridge.store, REPO, "mod-b") is None     # mod-b still gets a full listing


# ---------------------------------------------------------------- 8. quiet hours

def test_quiet_hours_start_must_differ_from_end(monkeypatch):
    for k, v in dict(PLANE_BASE_URL="x", PLANE_PUBLIC_URL="x", PLANE_WORKSPACE="w", PLANE_API_KEY="k",
                     GITHUB_TOKEN="t", GITHUB_WEBHOOK_SECRET="s", BRIDGE_QUIET_HOURS="3-3").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match="start and end must differ"):
        load_settings("/nonexistent")


def test_trust_proxy_headers_setting_defaults_off(monkeypatch):
    for k, v in dict(PLANE_BASE_URL="x", PLANE_PUBLIC_URL="x", PLANE_WORKSPACE="w", PLANE_API_KEY="k",
                     GITHUB_TOKEN="t", GITHUB_WEBHOOK_SECRET="s").items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BRIDGE_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.delenv("BRIDGE_QUIET_HOURS", raising=False)
    assert load_settings("/nonexistent").trust_proxy_headers is False
    monkeypatch.setenv("BRIDGE_TRUST_PROXY_HEADERS", "1")
    assert load_settings("/nonexistent").trust_proxy_headers is True


# ---------------------------------------------------------------- round 3: merge-close limits

from test_engine_review import merged_pr  # noqa: E402


def _linked(bridge, world):
    world.add_item(1)
    bridge.handle_plane(created())


def test_merge_into_a_non_default_branch_moves_to_in_review(env):
    """Aaban's decision: 1-scan merged into develop is In Review, the issue stays
    open with no comment, and checks treat that as in sync."""
    bridge, world, clock = env
    _linked(bridge, world)
    r = bridge.handle_github("pull_request", merged_pr(base="develop"), "p1")
    assert r.endswith("moved to review")
    assert world.items["item-1"]["state"] == "st-review"
    assert world.issues[1]["state"] == "open" and 1 not in world.gh_comments
    bridge.debounce.clock = clock
    bridge.debounce.touch(f"module:{PROJ}:mod-1")
    clock.t += 31
    bridge.run_reconciles()
    bridge.run_org_scan()
    assert world.items["item-1"]["state"] == "st-review" and world.issues[1]["state"] == "open"
    assert bridge.diag.counts()["open"] == 0 and world.issues[1]["labels"] == []


def test_merge_without_default_branch_in_the_payload_fails_closed(env):
    bridge, world, clock = env
    _linked(bridge, world)
    event = merged_pr()
    del event["repository"]["default_branch"]
    bridge.handle_github("pull_request", event, "p1")
    assert world.items["item-1"]["state"] == "st-review"
    assert world.issues[1]["state"] == "open" and 1 not in world.gh_comments


def test_merge_into_the_default_branch_is_unchanged(env):
    bridge, world, clock = env
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(), "p1")
    assert world.items["item-1"]["state"] == "st-done"
    assert world.issues[1]["state"] == "closed" and len(world.gh_comments[1]) == 1


def test_branch_pr_that_says_closes_is_left_to_github(env):
    bridge, world, clock = env
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(body="Closes #1"), "p1")
    assert world.issues[1]["state"] == "open" and 1 not in world.gh_comments


def test_fork_pr_with_a_matching_branch_name_doesnt_close_the_issue(env):
    bridge, world, clock = env
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(head_repo="someone/api-fork"), "p1")
    assert world.issues[1]["state"] == "open" and 1 not in world.gh_comments


def test_merge_comment_accepted_but_answer_lost_isnt_posted_twice(env):
    bridge, world, clock = env
    _linked(bridge, world)
    orig = world.github
    lost = {"n": 1}

    def github(req):
        r = orig(req)
        if req.method == "POST" and req.url.path.endswith("/1/comments") and lost["n"]:
            lost["n"] -= 1
            return httpx.Response(502, json={})        # GitHub kept it; the answer was lost
        return r

    bridge.gh.http._transport = httpx.MockTransport(github)
    assert "queued" in bridge.handle_github("pull_request", merged_pr(), "p1")
    assert len(world.gh_comments[1]) == 1
    run_queue_after(bridge, clock, 6)
    assert len(world.gh_comments[1]) == 1 and bridge.queue.stats()["length"] == 0


def test_closing_keyword_pr_merged_into_a_non_default_branch_moves_to_in_review(env):
    """Same rule for issues linked only by "Closes #1": GitHub leaves them open on a
    non-default merge, and so does the bridge; the item goes to In Review."""
    bridge, world, clock = env
    _linked(bridge, world)
    r = bridge.handle_github("pull_request", merged_pr(branch="feature-x", body="Closes #1", base="develop"), "p1")
    assert r.endswith("moved to review") and world.items["item-1"]["state"] == "st-review"
    assert world.issues[1]["state"] == "open" and 1 not in world.gh_comments
    bridge.run_org_scan()
    assert world.items["item-1"]["state"] == "st-review" and bridge.diag.counts()["open"] == 0


def test_closing_keyword_pr_without_default_branch_fails_closed(env):
    bridge, world, clock = env
    _linked(bridge, world)
    event = merged_pr(branch="feature-x", body="Closes #1")
    del event["repository"]["default_branch"]
    bridge.handle_github("pull_request", event, "p1")
    assert world.items["item-1"]["state"] == "st-review"


def test_closing_keyword_pr_merged_into_the_default_branch_still_moves_to_done(env):
    bridge, world, clock = env
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(branch="feature-x", body="Closes #1"), "p1")
    assert world.items["item-1"]["state"] == "st-done"
    assert world.issues[1]["state"] == "open" and 1 not in world.gh_comments   # GitHub closes it itself


# ---------------------------------------------------------------- auditor round 4

def test_pr_merged_into_develop_doesnt_pull_a_done_item_back(env):
    """Auditor F1 (test_done_item_pulled_back_by_dev_merge)."""
    bridge, world, clock = env
    _linked(bridge, world)
    world.items["item-1"]["state"] = "st-done"
    world.issues[1]["state"] = "closed"
    r = bridge.handle_github("pull_request", merged_pr(base="develop"), "p1")
    assert r.endswith("unchanged: already done or cancelled in Plane")
    assert world.items["item-1"]["state"] == "st-done" and world.issues[1]["state"] == "closed"
    s = get_snapshot(bridge.store, "item-1")
    assert s.p_closed and s.g_closed and bridge.diag.counts()["open"] == 0


@pytest.mark.parametrize("action", ["opened", "ready_for_review", "reopened"])
def test_open_pr_events_dont_pull_a_cancelled_item_back(env, action):
    bridge, world, clock = env
    _linked(bridge, world)
    world.items["item-1"]["state"] = "st-cancel"
    event = merged_pr(branch="1-scan")
    event["action"] = action
    event["pull_request"].update(state="open", merged=False)
    bridge.handle_github("pull_request", event, f"p-{action}")
    assert world.items["item-1"]["state"] == "st-cancel"


def test_issue_reopen_still_reopens_a_done_item(env):
    bridge, world, clock = env
    _linked(bridge, world)
    gh_does(bridge, world, "closed", "d1", state_reason="completed")
    assert world.items["item-1"]["state"] == "st-done"
    gh_does(bridge, world, "reopened", "d2")
    assert world.items["item-1"]["state"] == "st-todo"


def test_divergent_pair_is_never_recorded_silently(env):
    """Auditor F1 (test_done_pullback_divergence_flagged): whatever records a pair
    open on one side and closed on the other raises state_differs, and a later
    matching record clears it."""
    bridge, world, clock = env
    _linked(bridge, world)
    link = bridge.store.by_plane_item("item-1")
    bridge._record_pair(link, "Item 1", False, "Item 1", True)
    (problem,) = bridge.diag.open_problems(kind="state_differs")
    assert problem.plane_item_id == "item-1" and "plane-sync-problem" in world.issues[1]["labels"]
    bridge._record_pair(link, "Item 1", False, "Item 1", True)            # not raised twice
    assert bridge.diag.open_problems(kind="state_differs")[0].count == 1
    bridge._record_pair(link, "Item 1", True, "Item 1", True)
    assert bridge.diag.open_problems(kind="state_differs") == []
    assert "plane-sync-problem" not in world.issues[1]["labels"]


def test_keyword_merge_never_flags_when_github_closes_in_time(env):
    """Default-branch merge with "Closes #1": Plane moves to Done before GitHub has
    closed the issue. That gap is expected and never labelled."""
    bridge, world, clock = env
    bridge.debounce.clock = clock
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(branch="feature-x", body="Closes #1"), "p1")
    assert world.items["item-1"]["state"] == "st-done" and world.issues[1]["state"] == "open"
    assert bridge.diag.open_problems(kind="state_differs") == []
    assert world.issues[1]["labels"] == []
    clock.t += 3
    gh_does(bridge, world, "closed", "d1", state_reason="completed")    # GitHub's own close
    clock.t += bridge.debounce.delay + 1
    bridge.run_reconciles()                                             # the delayed check
    assert bridge.diag.counts()["open"] == 0 and bridge.diag.counts()["flags_pending"] == 0
    assert world.issues[1]["labels"] == [] and world.items["item-1"]["labels"] == []


def test_keyword_merge_still_open_after_the_check_is_flagged(env):
    bridge, world, clock = env
    bridge.debounce.clock = clock
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(branch="feature-x", body="Closes #1"), "p1")
    assert bridge.diag.open_problems(kind="state_differs") == []
    clock.t += bridge.debounce.delay + 1                                # GitHub never closed it
    bridge.run_reconciles()
    (problem,) = bridge.diag.open_problems(kind="state_differs")
    assert problem.plane_item_id == "item-1" and "plane-sync-problem" in world.issues[1]["labels"]
    gh_does(bridge, world, "closed", "d1", state_reason="completed")    # someone closes it
    assert bridge.diag.open_problems(kind="state_differs") == []


def test_expected_catch_up_doesnt_hide_other_divergence_after_it_expires(env):
    bridge, world, clock = env
    _linked(bridge, world)
    link = bridge.store.by_plane_item("item-1")
    bridge.store.expect_catch_up("item-1", clock.t + 10)
    bridge._record_pair(link, "Item 1", True, "Item 1", False)
    assert bridge.diag.open_problems(kind="state_differs") == []
    clock.t += 11
    bridge._record_pair(link, "Item 1", True, "Item 1", False)
    assert bridge.diag.open_problems(kind="state_differs")


def test_main_then_release_merge_keeps_the_item_done(env):
    """Chaperone (test_main_then_release_merge): 1-scan merged to main, then the same
    branch merged to release/1.x, must not pull the item back to In Review."""
    bridge, world, clock = env
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(number=7), "p1")
    assert world.items["item-1"]["state"] == "st-done" and world.issues[1]["state"] == "closed"
    bridge.handle_github("pull_request", merged_pr(number=8, base="release/1.x"), "p2")
    bridge.run_org_scan()
    assert world.items["item-1"]["state"] == "st-done" and world.issues[1]["state"] == "closed"
    assert bridge.diag.counts()["open"] == 0


def test_fork_pr_merged_into_default_is_flagged_not_silent(env):
    """Chaperone (test_fork_default): a fork PR from branch 1-x merged into main with
    no keyword moves Plane to Done while the issue stays open; that pair is flagged."""
    bridge, world, clock = env
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(head_repo="someone/api-fork"), "p1")
    bridge.run_org_scan()
    assert world.items["item-1"]["state"] == "st-done" and world.issues[1]["state"] == "open"
    (problem,) = bridge.diag.open_problems(kind="state_differs")
    assert problem.plane_item_id == "item-1" and "plane-sync-problem" in world.issues[1]["labels"]


def test_reconcile_off_still_flags_a_keyword_merge_github_never_closes(env, monkeypatch):
    """BRIDGE_RECONCILE=0: the item check still runs from the checks thread."""
    import time as _time

    import bridge.app as app_module
    from fastapi.testclient import TestClient
    bridge, world, clock = env
    bridge.s.reconcile = False
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(branch="feature-x", body="Closes #1"), "p1")
    assert bridge.diag.open_problems(kind="state_differs") == []
    clock.t += bridge.debounce.delay + 1                # GitHub never closed it
    monkeypatch.setattr(app_module, "CHECK_TICK", 0.02)
    with TestClient(app_module.build_app(bridge)):
        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline and not bridge.diag.open_problems(kind="state_differs"):
            _time.sleep(0.02)
    (problem,) = bridge.diag.open_problems(kind="state_differs")
    assert problem.plane_item_id == "item-1"


def test_item_checks_run_without_pair_checks(env):
    bridge, world, clock = env
    bridge.debounce.clock = clock
    _linked(bridge, world)
    bridge.handle_github("pull_request", merged_pr(branch="feature-x", body="Closes #1"), "p1")
    bridge.debounce.touch(f"module:{PROJ}:mod-1")
    clock.t += bridge.debounce.delay + 1
    assert bridge.run_reconciles(pairs=False) == []     # the module check is dropped, not run
    assert bridge.debounce.pending() == 0
    assert bridge.diag.open_problems(kind="state_differs")


def test_expired_catch_up_rows_are_pruned(env):
    bridge, world, clock = env
    bridge.store.expect_catch_up("old", clock.t - 1)
    bridge.store.expect_catch_up("fresh", clock.t + 100)
    assert bridge.store.prune_catch_up(clock.t) == 1
    with bridge.store._lock:
        left = [r[0] for r in bridge.store._db.execute("SELECT plane_item_id FROM expected_catch_up")]
    assert left == ["fresh"]
    # the delayed check prunes too
    bridge.store.expect_catch_up("old", clock.t - 1)
    bridge._check_caught_up("fresh")
    with bridge.store._lock:
        assert bridge.store._db.execute("SELECT COUNT(*) FROM expected_catch_up").fetchone()[0] == 0
