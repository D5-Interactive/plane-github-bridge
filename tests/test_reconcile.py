"""The reconcile engine: which side changed, what gets fixed, what is reported."""
import httpx
import pytest

from bridge import listing
from bridge.clients import GitHubClient, PlaneClient
from bridge.reconcile import Reconciler, get_snapshot, record_snapshot
from bridge.store import Link, Store

PID, MID, REPO = "proj-1", "mod-1", "acme/app"
STATES = [{"id": "s-todo", "group": "unstarted"}, {"id": "s-done", "group": "completed"},
          {"id": "s-cancel", "group": "cancelled"}]
OLD, MID_T, NEW = "2026-11-19T10:00:00Z", "2026-11-19T11:00:00Z", "2026-11-19T12:00:00Z"


class World:
    """Fake Plane and GitHub behind the real clients."""

    def __init__(self):
        self.items: list[dict] = []
        self.issues: list[dict] = []
        self.relation_only = False      # module-issues returns only relations (older Plane)
        self.writes: list[tuple[str, str]] = []
        self.calls = 0

    def plane(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        p = req.url.path
        if req.method != "GET":
            self.writes.append((req.method, p))
            return httpx.Response(500)
        if p.endswith(f"/modules/{MID}/module-issues/"):
            if self.relation_only:
                return httpx.Response(200, json={"results": [{"issue": i["id"]} for i in self.items]})
            return httpx.Response(200, json={"results": self.items})
        if p.endswith("/states/"):
            return httpx.Response(200, json={"results": STATES})
        for i in self.items:
            if p.endswith(f"/issues/{i['id']}/"):
                return httpx.Response(200, json=i)
        return httpx.Response(404, json={"error": "not found"})

    def github(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        p = req.url.path
        if req.method != "GET":
            self.writes.append((req.method, p))
            return httpx.Response(500)
        if p == f"/repos/{REPO}/issues":
            page = int(req.url.params.get("page", "1"))
            return httpx.Response(200, json=self.issues[(page - 1) * 100: page * 100])
        if p.startswith(f"/repos/{REPO}/issues/"):
            n = int(p.rsplit("/", 1)[1])
            for i in self.issues:
                if i["number"] == n:
                    return httpx.Response(200, json=i)
            return httpx.Response(404)
        return httpx.Response(404)


def item(iid, name, state="s-todo", updated=OLD):
    return {"id": iid, "name": name, "state": state, "updated_at": updated}


def issue(n, title, state="open", updated=OLD, **extra):
    return {"number": n, "title": title, "state": state, "updated_at": updated, **extra}


@pytest.fixture
def env(tmp_path):
    world = World()
    plane = PlaneClient("http://plane.local", "ws", "k", transport=httpx.MockTransport(world.plane))
    gh = GitHubClient("t", transport=httpx.MockTransport(world.github), sleep=lambda s: None)
    store = Store(str(tmp_path / "b.db"))
    fixes, problems = [], []
    rec = Reconciler(plane, gh, store, apply_fix=lambda k, key, p: fixes.append((k, key, p)),
                     report_problem=problems.append, clock=lambda: 1000.0)
    return world, store, rec, fixes, problems


def link(store, iid, n, plane_linked=True, repo=REPO):
    store.save(Link(iid, PID, repo, n, f"https://github.com/{repo}/issues/{n}", plane_linked=plane_linked))


def run(rec):
    return rec.reconcile_pair(PID, {"id": MID}, REPO)


def kinds(fixes):
    return [f[0] for f in fixes]


def test_unlinked_module_item_gets_an_issue(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha")]
    rep = run(rec)
    assert kinds(fixes) == ["create_issue"]
    assert fixes[0][2] == {"project_id": PID, "item_id": "a", "repo": REPO}
    assert rep.checked == 1 and world.writes == []


def test_missing_plane_link_is_added(env):
    world, store, rec, fixes, _ = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha")]
    link(store, "a", 1, plane_linked=False)
    run(rec)
    assert kinds(fixes) == ["add_plane_link"]
    assert fixes[0][2]["issue_url"].endswith("/issues/1")


def test_no_snapshot_and_matching_values_records_baseline(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha")]
    link(store, "a", 1)
    run(rec)
    assert fixes == [] and problems == []
    snap = get_snapshot(store, "a")
    assert snap.title == "Alpha" and snap.closed is False and snap.issue_number == 1


def test_no_snapshot_closed_mismatch_most_recent_wins(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha", state="s-done", updated=NEW)]
    world.issues = [issue(1, "Alpha", state="open", updated=OLD)]
    link(store, "a", 1)
    run(rec)
    assert fixes[0][0] == "push_state_to_github" and fixes[0][2]["closed"] is True
    # GitHub newer this time
    fixes.clear()
    world.items[0]["updated_at"], world.issues[0]["updated_at"] = OLD, NEW
    run(rec)
    assert fixes[0][0] == "push_state_to_plane" and fixes[0][2]["closed"] is False


def test_no_snapshot_title_mismatch_is_reported_not_fixed(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha v2")]
    link(store, "a", 1)
    run(rec)
    assert fixes == []
    assert problems[0]["kind"] == "title_differs"
    assert problems[0]["plane_value"] == "Alpha" and problems[0]["github_value"] == "Alpha v2"
    assert get_snapshot(store, "a") is None        # not a baseline until they agree


def test_title_changed_in_plane_only(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha 2")], [issue(1, "Alpha")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Alpha", False)
    run(rec)
    assert fixes == [("push_title_to_github", "a",
                      {"project_id": PID, "item_id": "a", "repo": REPO, "issue_number": 1,
                       "title": "Alpha 2"})]
    assert problems == []


def test_title_changed_on_github_only(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha (gh)")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Alpha", False)
    run(rec)
    assert kinds(fixes) == ["push_title_to_plane"] and fixes[0][2]["title"] == "Alpha (gh)"


def test_title_changed_on_both_sides_most_recent_wins_and_is_a_conflict(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Plane title", updated=MID_T)]
    world.issues = [issue(1, "GitHub title", updated=NEW)]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Original", False)
    rep = run(rec)
    assert kinds(fixes) == ["push_title_to_plane"] and fixes[0][2]["title"] == "GitHub title"
    assert problems[0]["kind"] == "conflict" and problems[0]["field"] == "title"
    assert problems[0]["winner"] == "github"
    assert problems[0]["plane_value"] == "Plane title" and problems[0]["github_value"] == "GitHub title"
    assert rep.conflicts == problems


def test_closed_in_plane_only_closes_issue(env):
    world, store, rec, fixes, _ = env
    world.items, world.issues = [item("a", "Alpha", state="s-cancel")], [issue(1, "Alpha")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Alpha", False)
    run(rec)
    assert fixes[0][0] == "push_state_to_github" and fixes[0][2]["closed"] is True


def test_reopened_on_github_only_reopens_plane_item(env):
    world, store, rec, fixes, _ = env
    world.items = [item("a", "Alpha", state="s-done")]
    world.issues = [issue(1, "Alpha", state="open")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Alpha", True)
    run(rec)
    assert fixes[0][0] == "push_state_to_plane" and fixes[0][2]["closed"] is False


def test_closed_on_github_carries_state_reason(env):
    world, store, rec, fixes, _ = env
    world.items = [item("a", "Alpha")]
    world.issues = [issue(1, "Alpha", state="closed", state_reason="not_planned")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Alpha", False)
    run(rec)
    assert fixes[0][2]["state_reason"] == "not_planned"


def test_both_sides_closed_since_last_sync_agree(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha", state="s-done", updated=NEW)]
    world.issues = [issue(1, "Alpha", state="closed", updated=OLD)]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Alpha", False)
    run(rec)
    # Both moved to closed: they agree, so no fix, snapshot refreshed.
    assert fixes == [] and problems == []
    assert get_snapshot(store, "a").closed is True


def test_both_sides_agree_on_new_title_refreshes_snapshot(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "New")], [issue(1, "New")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Old", False)
    run(rec)
    assert fixes == [] and problems == []
    assert get_snapshot(store, "a").title == "New"


def test_nothing_changed_does_nothing(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "Alpha", False, synced_at=5.0)
    run(rec)
    assert fixes == [] and problems == []
    assert get_snapshot(store, "a").synced_at == 5.0


def test_missing_issue_is_reported(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha")]
    link(store, "a", 7)
    run(rec)
    assert fixes == [] and problems == [{"kind": "issue_missing", "project_id": PID,
                                         "item_id": "a", "repo": REPO, "issue_number": 7}]


def test_unlinked_issues_and_pull_requests(env):
    world, store, rec, fixes, _ = env
    world.items = [item("a", "Alpha")]
    world.issues = [issue(1, "Alpha"), issue(2, "Stray"),
                    {"number": 3, "title": "A PR", "state": "open", "pull_request": {}}]
    link(store, "a", 1)
    link(store, "elsewhere", 4)                     # linked, even if not in this module
    world.issues.append(issue(4, "Other"))
    rep = run(rec)
    assert rep.unlinked_issues == [{"repo": REPO, "issue_number": 2, "title": "Stray"}]
    assert "create_issue" not in kinds(fixes)


def test_item_linked_to_another_repo_is_skipped(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha")]
    link(store, "a", 1, repo="acme/other")
    run(rec)
    assert fixes == [] and problems == []


def test_engine_never_writes(env):
    world, store, rec, fixes, _ = env
    world.items = [item("a", "x", state="s-done", updated=NEW), item("b", "y")]
    world.issues = [issue(1, "z")]
    link(store, "a", 1)
    record_snapshot(store, "a", REPO, 1, "w", False)
    run(rec)
    assert world.writes == [] and fixes


def test_relation_only_module_listing_fetches_items(env):
    world, store, rec, fixes, _ = env
    world.relation_only = True
    world.items = [item("a", "Alpha"), item("b", "Beta")]
    rep = run(rec)
    assert kinds(fixes) == ["create_issue", "create_issue"]
    # module listing 1 + 2 items + states 1 + GitHub issues 1
    assert rep.calls == 5


def test_calls_are_counted_and_pages_followed(env):
    world, store, rec, fixes, _ = env
    world.issues = [issue(n, f"i{n}") for n in range(1, 151)]
    rep = run(rec)
    assert rep.calls == 1 + 1 + 2          # module, states, two GitHub pages
    assert len(rep.unlinked_issues) == 150
    rep = run(rec)
    assert rep.calls == 1 + 0 + 2          # states now cached


def test_listing_issue_handles_missing_and_transferred(env):
    world, store, rec, _, _ = env
    world.issues = [issue(1, "Alpha")]
    gh = rec.gh
    assert listing.issue(gh, REPO, 1)[0]["title"] == "Alpha"
    assert listing.issue(gh, REPO, 9) == (None, 1)
