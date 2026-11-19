"""PR #9 (GitHub App auth) merged into the sync engine: with no credential for a
repo, actions are queued and retried (never parked or refused), logged once."""
import logging

from bridge.github_auth import NoGitHubCredentials

from test_engine_e2e import REPO, created, env, run_queue_after  # noqa: F401


def no_creds_for_creates(world):
    orig = world.github
    state = {"on": True}

    def github(req):
        if state["on"] and req.url.path.startswith(f"/repos/{REPO}/issues") and req.method in ("GET", "POST"):
            raise NoGitHubCredentials(f"not sending {req.method} {req.url.path}: the GitHub App isn't "
                                      f"installed on acme and there's no GITHUB_TOKEN")
        return orig(req)
    return github, state


def test_no_credentials_queues_the_action_and_logs_once(env, caplog):
    bridge, world, clock = env
    for n in (1, 2):
        world.add_item(n)
    github, state = no_creds_for_creates(world)
    bridge.gh.http._transport = __import__("httpx").MockTransport(github)
    with caplog.at_level(logging.WARNING, logger="bridge"):
        r1 = bridge.handle_plane(created("e1", "item-1"))
        r2 = bridge.handle_plane(created("e2", "item-2"))
        run_queue_after(bridge, clock, 6)
    assert r1 == r2 == "queued: no GitHub credentials for this repo yet"
    assert bridge.queue.stats()["length"] == 2
    assert bridge.store.failure("item-1") is None                 # not parked
    assert bridge.diag.open_problems(kind="refused") == []        # not refused
    lines = [r for r in caplog.records if "no GITHUB_TOKEN" in r.getMessage() or "NoGitHubCredentials" in r.getMessage()]
    assert len(lines) == 1 and lines[0].exc_info is None
    state["on"] = False                                           # the app gets installed
    run_queue_after(bridge, clock, 20)
    assert len(world.issues) == 2 and bridge.queue.stats()["length"] == 0


def test_membership_pass_skips_the_repo_without_parking(env):
    bridge, world, clock = env
    world.add_item(1)
    github, state = no_creds_for_creates(world)
    bridge.gh.http._transport = __import__("httpx").MockTransport(github)
    result = bridge.sync_linked_modules()
    assert "failed" in result and bridge.store.failure("item-1") is None
    state["on"] = False
    bridge.sync_linked_modules()
    assert len(world.issues) == 1
