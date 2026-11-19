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
        self.since_seen: list[str] = []
        self.issues_since: list[dict] = []
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
            if "since" in req.url.params:
                self.since_seen.append(req.url.params["since"])
                return httpx.Response(200, json=self.issues_since)
            return httpx.Response(200, json=self.issues[(page - 1) * 100: page * 100])
        if p.startswith(f"/repos/{REPO}/issues/"):
            n = int(p.rsplit("/", 1)[1])
            for i in self.issues:
                if i["number"] == n:
                    return httpx.Response(200, json=i)
            return httpx.Response(404)
        return httpx.Response(404)


def item(iid, name, state="s-todo", updated=OLD, **extra):
    return {"id": iid, "name": name, "state": state, "updated_at": updated, **extra}


def issue(n, title, state="open", updated=OLD, **extra):
    return {"number": n, "title": title, "state": state, "updated_at": updated,
            "created_at": "2026-11-01T00:00:00Z", **extra}


@pytest.fixture
def env(tmp_path):
    world = World()
    plane = PlaneClient("http://plane.local", "ws", "k", transport=httpx.MockTransport(world.plane))
    gh = GitHubClient("t", transport=httpx.MockTransport(world.github), sleep=lambda s: None)
    store = Store(str(tmp_path / "b.db"))
    fixes, problems, resolved = [], [], []
    rec = Reconciler(plane, gh, store, apply_fix=lambda k, key, p: fixes.append((k, key, p)),
                     report_problem=problems.append,
                     resolve_problem=lambda kind, i: resolved.append((kind, i)),
                     clock=lambda: 1000.0)
    rec.resolved = resolved
    return world, store, rec, fixes, problems


def link(store, iid, n, plane_linked=True, repo=REPO):
    store.save(Link(iid, PID, repo, n, f"https://github.com/{repo}/issues/{n}", plane_linked=plane_linked))


def snap(store, iid, title, closed, g_title=None, g_closed=None, synced_at=None):
    record_snapshot(store, iid, REPO, 1, p_title=title, g_title=title if g_title is None else g_title,
                    p_closed=closed, g_closed=closed if g_closed is None else g_closed, synced_at=synced_at)


def run(rec, since=None):
    return rec.reconcile_pair(PID, {"id": MID}, REPO, since=since)


def kinds(fixes):
    return [f[0] for f in fixes]


def test_unlinked_module_item_gets_an_issue(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha")]
    rep = run(rec)
    assert kinds(fixes) == ["create_issue"]
    assert fixes[0][2] == {"project_id": PID, "item_id": "a", "repo": REPO}
    assert rep.checked == 1 and world.writes == []


def test_item_whose_create_is_queued_is_left_to_the_queue(env):
    world, store, rec, fixes, _ = env
    world.items = [item("a", "Alpha")]
    rec.in_progress = lambda item_id: item_id == "a"
    run(rec)
    assert fixes == []


def test_missing_plane_link_is_added(env):
    world, store, rec, fixes, _ = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha")]
    link(store, "a", 1, plane_linked=False)
    run(rec)
    assert kinds(fixes) == ["add_plane_link"]
    assert fixes[0][2]["issue_url"].endswith("/issues/1")


# ---------------------------------------------------------------- first contact

def test_first_contact_matching_values_records_baseline_from_both_sides(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha")]
    link(store, "a", 1)
    run(rec)
    assert fixes == [] and problems == []
    s = get_snapshot(store, "a")
    assert (s.p_title, s.g_title, s.p_closed, s.g_closed) == ("Alpha", "Alpha", False, False)


def test_first_contact_state_mismatch_is_reported_never_written(env):
    """A pair from before the upgrade: Done in Plane, issue never closed. Whichever
    side's updated_at is newer, nothing is written; the mismatch is reported."""
    world, store, rec, fixes, problems = env
    for p_upd, g_upd in ((NEW, OLD), (OLD, NEW)):
        fixes.clear(), problems.clear()
        with store._lock:
            store._db.execute("DELETE FROM pair_snapshot")
        world.items = [item("a", "Alpha", state="s-done", updated=p_upd)]
        world.issues = [issue(1, "Alpha", state="open", updated=g_upd)]
        link(store, "a", 1)
        run(rec)
        assert fixes == [] and world.writes == []
        assert [(p["kind"], p["first_contact"]) for p in problems] == [("state_differs", True)]
        s = get_snapshot(store, "a")        # baseline: both sides as they are
        assert (s.p_closed, s.g_closed) == (True, False)


def test_first_contact_title_mismatch_is_reported(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha v2")]
    link(store, "a", 1)
    run(rec)
    assert fixes == [] and [p["kind"] for p in problems] == ["title_differs"]
    assert "plane_value" not in problems[0]          # no work item text in problems


def test_reported_divergence_is_not_reported_again_or_fixed(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha", state="s-done")]
    world.issues = [issue(1, "Alpha")]
    link(store, "a", 1)
    run(rec)
    problems.clear()
    run(rec)                                 # nothing changed on either side since
    assert fixes == [] and problems == []


def test_first_contact_fix_closes_when_plane_completed_after_the_issue_was_opened(env):
    world, store, rec, fixes, problems = env
    rec.first_contact_fix = True
    world.items = [item("a", "Alpha", state="s-done", completed_at="2026-11-10T00:00:00Z")]
    world.issues = [issue(1, "Alpha")]            # never closed, opened 2026-11-01
    link(store, "a", 1)
    run(rec)
    assert kinds(fixes) == ["sync_item"] and problems == []


def test_first_contact_fix_reports_when_times_are_unreliable(env):
    world, store, rec, fixes, problems = env
    rec.first_contact_fix = True
    # Cancelled in Plane: no reliable time. Reopened issue: no reliable time either.
    world.items = [item("a", "Alpha", state="s-cancel"), item("b", "Beta", state="s-done",
                                                            completed_at="2026-11-10T00:00:00Z")]
    world.issues = [issue(1, "Alpha"), issue(2, "Beta", closed_at="2026-11-05T00:00:00Z")]
    link(store, "a", 1), link(store, "b", 2)
    run(rec)
    assert fixes == [] and [p["kind"] for p in problems] == ["state_differs", "state_differs"]


def test_first_contact_fix_never_fixes_titles(env):
    world, store, rec, fixes, problems = env
    rec.first_contact_fix = True
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha v2")]
    link(store, "a", 1)
    run(rec)
    assert fixes == [] and [p["kind"] for p in problems] == ["title_differs"]


# ---------------------------------------------------------------- with a snapshot

def test_title_changed_in_plane_only(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha 2")], [issue(1, "Alpha")]
    link(store, "a", 1)
    snap(store, "a", "Alpha", False)
    run(rec)
    assert fixes == [("sync_item", "a", {"project_id": PID, "item_id": "a", "repo": REPO,
                                          "issue_number": 1, "reconcile": True})]
    assert problems == []


def test_title_changed_on_github_only(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha (gh)")]
    link(store, "a", 1)
    snap(store, "a", "Alpha", False)
    run(rec)
    assert kinds(fixes) == ["sync_item"]


def test_title_changed_on_both_sides_is_reported_not_guessed(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Plane title", updated=MID_T)]
    world.issues = [issue(1, "GitHub title", updated=NEW)]
    link(store, "a", 1)
    snap(store, "a", "Original", False)
    run(rec)
    assert fixes == [] and [p["kind"] for p in problems] == ["title_differs"]


def test_label_bump_on_github_does_not_win_a_state_conflict(env):
    """updated_at is never used: a newer GitHub updated_at (a label) doesn't reopen Plane."""
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha", state="s-done", updated=MID_T)]
    world.issues = [issue(1, "Alpha", state="closed", updated=NEW)]
    link(store, "a", 1)
    snap(store, "a", "Alpha", True)
    run(rec)
    assert fixes == [] and problems == []


def test_closed_in_plane_only_closes_issue(env):
    world, store, rec, fixes, _ = env
    world.items, world.issues = [item("a", "Alpha", state="s-cancel")], [issue(1, "Alpha")]
    link(store, "a", 1)
    snap(store, "a", "Alpha", False)
    run(rec)
    assert kinds(fixes) == ["sync_item"]


def test_known_divergence_after_a_merge_is_left_alone(env):
    """Plane moved to Done by a merged branch PR while the issue stays open: the
    snapshot holds both sides as they are, so nothing reopens the item."""
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha", state="s-done")]
    world.issues = [issue(1, "Alpha", state="open")]
    link(store, "a", 1)
    snap(store, "a", "Alpha", True, g_closed=False)
    run(rec)
    assert fixes == [] and problems == []


def test_both_sides_closed_since_last_sync_agree(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha", state="s-done", updated=NEW)]
    world.issues = [issue(1, "Alpha", state="closed", updated=OLD)]
    link(store, "a", 1)
    snap(store, "a", "Alpha", False)
    run(rec)
    assert fixes == [] and problems == []
    s = get_snapshot(store, "a")
    assert s.p_closed and s.g_closed
    assert ("state_differs", "a") in rec.resolved


def test_state_conflict_with_reliable_times_is_settled(env):
    world, store, rec, fixes, problems = env
    # Snapshot: Plane open, GitHub closed (known divergence). Then Plane completes
    # and GitHub reopens... no: GitHub's reopen time is unknown, so it's reported.
    world.items = [item("a", "Alpha", state="s-done", completed_at=NEW)]
    world.issues = [issue(1, "Alpha", state="open", closed_at=OLD)]
    link(store, "a", 1)
    snap(store, "a", "Alpha", False, g_closed=True)
    run(rec)
    assert fixes == [] and [p["kind"] for p in problems] == ["state_differs"]


def test_matching_titles_resolve_title_differs(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "New")], [issue(1, "New")]
    link(store, "a", 1)
    snap(store, "a", "Old", False)
    run(rec)
    assert fixes == [] and problems == []
    assert get_snapshot(store, "a").p_title == "New"
    assert ("title_differs", "a") in rec.resolved


def test_nothing_changed_does_nothing(env):
    world, store, rec, fixes, problems = env
    world.items, world.issues = [item("a", "Alpha")], [issue(1, "Alpha")]
    link(store, "a", 1)
    snap(store, "a", "Alpha", False, synced_at=5.0)
    run(rec)
    assert fixes == [] and problems == []
    assert get_snapshot(store, "a").synced_at == 5.0


def test_missing_issue_is_reported_and_found_issue_resolves_it(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha")]
    link(store, "a", 7)
    run(rec)
    assert fixes == [] and problems == [{"kind": "issue_missing", "project_id": PID,
                                         "item_id": "a", "repo": REPO, "issue_number": 7}]
    world.issues = [issue(7, "Alpha")]
    run(rec)
    assert ("issue_missing", "a") in rec.resolved


def test_unlinked_issues_and_pull_requests(env):
    world, store, rec, fixes, _ = env
    world.items = [item("a", "Alpha")]
    world.issues = [issue(1, "Alpha"), issue(2, "Stray"),
                    {"number": 3, "title": "A PR", "state": "open", "pull_request": {}}]
    link(store, "a", 1)
    link(store, "elsewhere", 4)                     # linked, even if not in this module
    world.issues.append(issue(4, "Other"))
    rep = run(rec)
    assert rep.unlinked_issues == [{"repo": REPO, "issue_number": 2}]
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
    snap(store, "a", "w", False)
    run(rec)
    assert world.writes == [] and fixes


# ---------------------------------------------------------------- large repos

def test_capped_listing_looks_up_linked_issues_individually(env, monkeypatch):
    world, store, rec, fixes, problems = env
    real = listing.repo_issues
    monkeypatch.setattr(listing, "repo_issues", lambda gh, repo, since=None: real(gh, repo, since, max_pages=1))
    world.issues = [issue(n, f"i{n}") for n in range(1, 151)]
    world.items = [item("a", "i140")]
    link(store, "a", 140)
    rep = run(rec)
    assert problems == [] and not rep.complete
    assert get_snapshot(store, "a").g_title == "i140"       # read on its own
    assert rep.unlinked_issues == []                        # not counted from a partial list


def test_debounced_check_lists_only_changed_issues(env):
    world, store, rec, fixes, problems = env
    world.items = [item("a", "Alpha"), item("b", "Beta 2")]
    world.issues = [issue(1, "Alpha"), issue(2, "Beta")]
    link(store, "a", 1), link(store, "b", 2)
    snap(store, "a", "Alpha", False)
    record_snapshot(store, "b", REPO, 2, p_title="Beta", g_title="Beta", p_closed=False, g_closed=False)
    world.since_seen = []
    world.issues_since = []                 # GitHub: nothing changed since the last check
    rep = run(rec, since=999.0)
    assert world.since_seen and world.since_seen[0].startswith("1970-01-01T00:16")
    # a: unchanged on both sides, not read. b: changed in Plane, read on its own.
    assert kinds(fixes) == ["sync_item"] and fixes[0][1] == "b"
    assert problems == [] and not rep.complete


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
