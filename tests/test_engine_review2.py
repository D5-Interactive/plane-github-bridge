"""Fixes from the second review of the sync engine (ALL-103): queued intents in
arrival order, one echo per write, the done() race, the create look-back, per-module
`since`, and quiet-hours validation. Several tests began as the reviewers' repros
(auditor test_aud2.py, chaperone test_r2.py / test_r2b.py), turned to assert the fix."""
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
