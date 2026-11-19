"""The sync engine wired together (ALL-103): instant actions with a retry queue,
debounced checks of edited modules and repos, the org scan, the membership pass,
sync problems and /health. Plane and GitHub are in-memory fakes behind httpx."""
import json
import re
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import build_app
from bridge.clients import GitHubClient, PlaneClient
from bridge.config import Settings
from bridge.store import Store
from bridge.sync import Bridge

PROJ = "proj-1"
MOD = "mod-1"
REPO = "acme/api"
STATES = [
    {"id": "st-todo", "name": "Todo", "group": "unstarted"},
    {"id": "st-prog", "name": "In Progress", "group": "started"},
    {"id": "st-done", "name": "Done", "group": "completed"},
    {"id": "st-cancel", "name": "Cancelled", "group": "cancelled"},
]
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class World:
    """One Plane project with one module linked to one GitHub repo."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.module_items: list[str] = []
        self.links: dict[str, list[str]] = {}
        self.comments: dict[str, list[str]] = {}
        self.plane_labels: list[dict] = []
        self.issues: dict[int, dict] = {}
        self.gh_labels: set[str] = set()
        self.gh_comments: dict[int, list[str]] = {}
        self.fail: dict[tuple[str, str], list[int]] = {}   # (method, path regex) -> statuses to answer
        self.lose_create_response = 0                     # create the issue, then answer 502
        self.calls: Counter = Counter()
        self.tick = 0                                     # fake time for updated_at stamps
        self.clock = None                                 # the bridge's clock, when set

    def stamp(self) -> str:
        self.tick += 1
        if self.clock is not None:
            from bridge.reconcile import iso
            return iso(self.clock.t + self.tick)
        return f"2026-11-19T12:{self.tick // 60:02d}:{self.tick % 60:02d}Z"

    def add_item(self, n: int, name: str | None = None, state: str = "st-todo", in_module=True):
        item_id = f"item-{n}"
        self.items[item_id] = {"id": item_id, "name": name or f"Item {n}", "sequence_id": n,
                               "description_html": "<p>x</p>", "state": state, "labels": [],
                               "updated_at": "2026-11-19T10:00:00Z"}
        if in_module:
            self.module_items.append(item_id)
        return item_id

    def _failing(self, method: str, path: str):
        for (m, rx), statuses in self.fail.items():
            if m == method and re.search(rx, path) and statuses:
                return httpx.Response(statuses.pop(0), json={"message": "injected"})
        return None

    # ------------------------------------------------------------ Plane
    def plane(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path.split("/workspaces/acme", 1)[1]
        self.calls[("plane", req.method)] += 1
        hit = self._failing(req.method, "plane:" + p)
        if hit:
            return hit
        body = json.loads(req.content) if req.content else {}
        if p == "/projects/":
            return httpx.Response(200, json={"results": [{"id": PROJ, "identifier": "API"}]})
        if p == f"/projects/{PROJ}/":
            return httpx.Response(200, json={"id": PROJ, "identifier": "API"})
        if p == f"/projects/{PROJ}/modules/" and req.method == "GET":
            return httpx.Response(200, json={"results": [
                {"id": MOD, "name": "api", "description_html": f"https://github.com/{REPO}"}]})
        if p == f"/projects/{PROJ}/modules/{MOD}/" and req.method == "PATCH":
            return httpx.Response(200, json={})
        if p == f"/projects/{PROJ}/modules/{MOD}/module-issues/":
            return httpx.Response(200, json={"results": [dict(self.items[i]) for i in self.module_items]})
        if p.endswith("/module-links/"):
            return httpx.Response(404, json={})
        if p == f"/projects/{PROJ}/states/":
            return httpx.Response(200, json={"results": STATES})
        if p == f"/projects/{PROJ}/labels/":
            if req.method == "POST":
                label = {"id": f"lab-{len(self.plane_labels) + 1}", "name": body["name"]}
                self.plane_labels.append(label)
                return httpx.Response(201, json=label)
            return httpx.Response(200, json={"results": self.plane_labels})
        m = re.fullmatch(rf"/projects/{PROJ}/issues/([^/]+)/", p)
        if m:
            item = self.items.get(m.group(1))
            if item is None:
                return httpx.Response(404, json={"error": "Issue does not exist"})
            if req.method == "PATCH":
                item.update(body)
                item["updated_at"] = self.stamp()
                if "state" in body:
                    done = body["state"] == "st-done"
                    item["completed_at"] = item["updated_at"] if done else None
            return httpx.Response(200, json=dict(item))
        m = re.fullmatch(rf"/projects/{PROJ}/issues/([^/]+)/links/", p)
        if m:
            urls = self.links.setdefault(m.group(1), [])
            if req.method == "POST":
                if body["url"] in urls:
                    return httpx.Response(400, json={"error": "URL already exists for this Issue"})
                urls.append(body["url"])
                return httpx.Response(201, json={})
            return httpx.Response(200, json={"results": [{"url": u} for u in urls]})
        m = re.fullmatch(rf"/projects/{PROJ}/issues/([^/]+)/comments/", p)
        if m and req.method == "POST":
            self.comments.setdefault(m.group(1), []).append(body.get("comment_html", ""))
            return httpx.Response(201, json={})
        if m and req.method == "GET":
            return httpx.Response(200, json={"results": [{"comment_html": c}
                                                         for c in self.comments.get(m.group(1), [])]})
        return httpx.Response(404, json={"path": p})

    # ------------------------------------------------------------ GitHub
    def github(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        self.calls[("github", req.method)] += 1
        hit = self._failing(req.method, "github:" + p)
        if hit:
            return hit
        body = json.loads(req.content) if req.content else {}
        if p == f"/repos/{REPO}":
            return httpx.Response(200, json={"full_name": REPO, "name": "api", "description": "",
                                             "html_url": f"https://github.com/{REPO}",
                                             "created_at": "2026-01-01T00:00:00Z"})
        if p == f"/repos/{REPO}/issues" and req.method == "POST":
            n = len(self.issues) + 1
            made = self.stamp()
            self.issues[n] = {"number": n, "title": body["title"], "body": body.get("body", ""),
                              "state": "open", "html_url": f"https://github.com/{REPO}/issues/{n}",
                              "created_at": made, "closed_at": None, "updated_at": made, "labels": []}
            if self.lose_create_response:
                self.lose_create_response -= 1
                return httpx.Response(502, json={"message": "Bad gateway"})
            return httpx.Response(201, json=dict(self.issues[n]))
        if p == f"/repos/{REPO}/issues" and req.method == "GET":
            out = [dict(i) for i in self.issues.values()]
            if "since" in req.url.params:
                out = [i for i in out if i["updated_at"] >= req.url.params["since"]]
            if req.url.params.get("direction") == "desc":
                out.reverse()
            return httpx.Response(200, json=out)
        m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)", p)
        if m:
            issue = self.issues.get(int(m.group(1)))
            if issue is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if req.method == "PATCH":
                if body.get("state") == "closed" and issue["state"] != "closed":
                    issue["closed_at"] = self.stamp()
                issue.update(body)
                issue["updated_at"] = self.stamp()
            return httpx.Response(200, json=dict(issue))
        m = re.fullmatch(rf"/repos/{REPO}/labels/(.+)", p)
        if m:
            ok = m.group(1) in self.gh_labels
            return httpx.Response(200 if ok else 404, json={"name": m.group(1)})
        if p == f"/repos/{REPO}/labels" and req.method == "POST":
            self.gh_labels.add(body["name"])
            return httpx.Response(201, json={})
        m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments", p)
        if m and req.method == "POST":
            self.gh_comments.setdefault(int(m.group(1)), []).append(body["body"])
            return httpx.Response(201, json={})
        m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/labels(?:/(.+))?", p)
        if m:
            labels = self.issues[int(m.group(1))]["labels"]
            if req.method == "POST":
                labels.extend(l for l in body["labels"] if l not in labels)
            elif req.method == "DELETE" and m.group(2) in labels:
                labels.remove(m.group(2))
            return httpx.Response(200, json=[{"name": l} for l in labels])
        return httpx.Response(404, json={"path": p})


@pytest.fixture
def env(tmp_path):
    world, clock = World(), Clock()
    s = Settings(plane_base_url="http://plane.local", plane_public_url="https://plane.example.com",
                 plane_workspace="acme", plane_api_key="k", plane_webhook_secret="ps",
                 github_token="t", github_webhook_secret="gs")
    s.sync_interval = 0             # no background threads in tests: each test drives the engine
    bridge = Bridge(s, Store(str(tmp_path / "b.db")),
                    PlaneClient(s.plane_base_url, "acme", "k", transport=httpx.MockTransport(world.plane),
                                sleep=lambda _s: None, max_retries=0),
                    GitHubClient("t", transport=httpx.MockTransport(world.github), sleep=lambda _s: None))
    bridge.plane.cache_seconds = 0
    bridge.clock = clock
    world.clock = clock
    return bridge, world, clock


def created(eid="e1", item="item-1"):
    return {"version": "v2", "event": "workitem.created", "event_id": eid, "entity_type": "issue",
            "entity_id": item, "data": {"id": item, "project_id": PROJ}, "previous_attributes": {}}


def run_queue_after(bridge, clock, seconds):
    clock.t += seconds
    return bridge.run_queue()


# ---------------------------------------------------------------- retry queue

def test_503_is_queued_retried_and_done_without_a_duplicate(env):
    bridge, world, clock = env
    world.add_item(1)
    world.fail[("POST", rf"github:/repos/{REPO}/issues$")] = [503]
    result = bridge.handle_plane(created())
    assert result.startswith("queued:") and "503" in result
    assert bridge.queue.stats()["length"] == 1 and not world.issues
    # The event counts as handled: its action is in the queue, not lost.
    assert bridge.handle_plane(created()) == "ignored: duplicate delivery"

    run_queue_after(bridge, clock, 6)
    assert [i["title"] for i in world.issues.values()] == ["Item 1"]
    assert world.links["item-1"] == [f"https://github.com/{REPO}/issues/1"]
    assert bridge.queue.stats()["length"] == 0
    run_queue_after(bridge, clock, 60)          # nothing left to do, nothing done twice
    assert len(world.issues) == 1


def test_lost_create_response_adopts_the_issue_instead_of_making_another(env):
    bridge, world, clock = env
    world.add_item(1)
    world.lose_create_response = 1              # GitHub made the issue but the answer was lost
    assert bridge.handle_plane(created()).startswith("queued:")
    assert len(world.issues) == 1
    run_queue_after(bridge, clock, 6)
    assert len(world.issues) == 1               # adopted, not created again
    link = bridge.store.by_plane_item("item-1")
    assert link.issue_number == 1 and link.plane_linked
    assert world.links["item-1"] == [f"https://github.com/{REPO}/issues/1"]


def test_queued_comment_is_not_posted_twice(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.fail[("POST", r"plane:.*/comments/$")] = [503]
    branch = {"ref_type": "branch", "ref": "1-scan", "repository": {"full_name": REPO},
              "sender": {"login": "dev"}}
    assert bridge.handle_github("create", branch, "d1") == "branch 1-scan posted"
    assert bridge.queue.stats()["length"] == 1 and "item-1" not in world.comments
    run_queue_after(bridge, clock, 6)
    assert len(world.comments["item-1"]) == 1
    # GitHub redelivers the event, and a new delivery of the same activity arrives
    assert bridge.handle_github("create", branch, "d1") == "ignored: duplicate delivery"
    bridge.handle_github("create", branch, "d2")
    assert len(world.comments["item-1"]) == 1


def test_queued_link_already_present_is_not_added_again(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    pr = {"number": 7, "title": "Fix #1", "body": "", "state": "open", "draft": False,
          "html_url": f"https://github.com/{REPO}/pull/7", "head": {"ref": "x"}, "updated_at": "u"}
    # Plane added the link but the answer was a 502: the retry must see it's there.
    world.fail[("POST", r"plane:.*/links/$")] = [502]
    world.links["item-1"].append(pr["html_url"])
    bridge.handle_github("pull_request", {"action": "opened", "pull_request": pr,
                                          "repository": {"full_name": REPO}, "sender": {"login": "d"}}, "p1")
    assert bridge.queue.get("add_link", f"item-1:{pr['html_url']}") is not None
    run_queue_after(bridge, clock, 6)
    assert world.links["item-1"].count(pr["html_url"]) == 1
    assert bridge.store.pr_link_known("item-1", pr["html_url"])


def test_rate_limit_stops_the_queue_round(env):
    bridge, world, clock = env
    for n in (1, 2, 3):
        world.add_item(n)
    world.fail[("POST", rf"github:/repos/{REPO}/issues$")] = [503, 503, 503]
    for n in (1, 2, 3):
        bridge.handle_plane(created(f"e{n}", f"item-{n}"))
    assert bridge.queue.stats()["length"] == 3
    world.fail[("GET", r"plane:.*/issues/item-\d/$")] = [429]
    run_queue_after(bridge, clock, 6)
    assert bridge.queue.stats()["length"] == 3 and not world.issues   # stopped at the first


def test_stuck_entry_is_flagged_then_resolved(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    bridge.handle_plane({**created("e2"), "event": "workitem.updated",
                         "previous_attributes": {"name": "old"}})      # linked now: push_edit
    world.fail[("PATCH", rf"github:/repos/{REPO}/issues/1$")] = [503] * 20
    world.items["item-1"]["name"] = "Renamed"
    assert bridge.handle_plane({**created("e3"), "event": "workitem.updated",
                                "previous_attributes": {"name": "Item 1"}}).startswith("queued:")
    for _ in range(8):                          # retries keep failing past BRIDGE_QUEUE_MAX_AGE
        run_queue_after(bridge, clock, 61)
    stuck = bridge.diag.open_problems(kind="stuck")
    assert len(stuck) == 1 and stuck[0].plane_item_id == "item-1"
    assert any(l["name"] == "sync problem" for l in world.plane_labels)
    label_id = world.plane_labels[0]["id"]
    assert label_id in world.items["item-1"]["labels"]
    assert "plane-sync-problem" in world.issues[1]["labels"]

    world.fail.clear()                          # GitHub recovers
    run_queue_after(bridge, clock, 121)
    assert world.issues[1]["title"] == "Renamed"
    assert bridge.diag.open_problems(kind="stuck") == []
    assert label_id not in world.items["item-1"]["labels"]
    assert "plane-sync-problem" not in world.issues[1]["labels"]


def test_permanent_refusal_is_reported_not_queued(env):
    bridge, world, clock = env
    world.add_item(1)
    world.fail[("POST", rf"github:/repos/{REPO}/issues$")] = [410]
    assert bridge.handle_plane(created()).startswith("refused:")
    assert bridge.queue.stats()["length"] == 0
    assert [p.kind for p in bridge.diag.open_problems()] == ["refused"]


# ---------------------------------------------------------------- checks (reconcile)

def module_updated(eid):
    return {"version": "v2", "event": "module.updated", "event_id": eid, "entity_type": "module",
            "data": {"id": MOD, "project_id": PROJ, "name": "api",
                     "description_html": f"https://github.com/{REPO}"}}


def test_module_edits_run_one_debounced_check(env, monkeypatch):
    bridge, world, clock = env
    bridge.debounce.clock = clock
    runs = []
    real = bridge.reconciler.reconcile_pair
    monkeypatch.setattr(bridge.reconciler, "reconcile_pair",
                        lambda *a, **k: runs.append(a[2]) or real(*a, **k))
    for i in range(3):                          # a burst of edits
        bridge.handle_plane(module_updated(f"m{i}"))
        clock.t += 5
    assert bridge.run_reconciles() == [] and runs == []
    clock.t += 30
    assert bridge.run_reconciles() == [REPO]
    assert runs == [REPO]
    clock.t += 60
    assert bridge.run_reconciles() == []        # once, not once per edit


def test_check_creates_missing_issues_and_pushes_a_plane_close(env):
    bridge, world, clock = env
    bridge.debounce.clock = clock
    world.add_item(1)
    bridge.handle_plane(created())
    world.add_item(2)                           # added to the module without a webhook
    world.items["item-1"]["state"] = "st-done"  # done in Plane, still open on GitHub
    world.items["item-1"]["updated_at"] = "2026-11-19T11:00:00Z"
    bridge.handle_plane(module_updated("m1"))
    clock.t += 31
    bridge.run_reconciles()
    assert world.issues[1]["state"] == "closed" and world.issues[1]["state_reason"] == "completed"
    assert [i["title"] for i in world.issues.values()] == ["Item 1", "Item 2"]


def test_repo_rename_event_triggers_a_check(env):
    bridge, world, clock = env
    bridge.debounce.clock = clock
    event = {"action": "renamed", "repository": {"full_name": REPO},
             "changes": {"repository": {"name": {"from": "old-api"}}}}
    assert bridge.handle_github("repository", event, "r1") == f"check of {REPO} scheduled"
    assert bridge.debounce.pending() == 2       # the new name and the old one
    clock.t += 31
    assert bridge.run_reconciles() == [REPO]
    assert bridge.handle_github("repository", {**event, "action": "deleted"}, "r2").startswith("ignored")


def test_unlinked_issues_are_one_repo_problem_that_clears(env):
    bridge, world, clock = env
    world.issues[1] = {"number": 1, "title": "Made on GitHub", "state": "open", "labels": [],
                       "html_url": "u", "updated_at": "2026-11-19T10:00:00Z"}
    world.issues[2] = dict(world.issues[1], number=2)
    bridge.run_org_scan()
    (problem,) = bridge.diag.open_problems(kind="unlinked_issues")
    assert problem.repo == REPO and problem.issue_number is None
    assert not world.issues[1]["labels"]       # repo-level: no issue gets a label
    world.issues.clear()
    bridge.run_org_scan()
    assert bridge.diag.open_problems(kind="unlinked_issues") == []


def test_title_conflict_is_reported_not_guessed(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.handle_plane(created())
    world.items["item-1"].update(name="Plane title", updated_at="2026-11-19T12:00:00Z")
    world.issues[1].update(title="GitHub title", updated_at="2026-11-19T13:00:00Z")
    bridge.run_org_scan()
    assert world.items["item-1"]["name"] == "Plane title"      # nothing overwritten
    assert world.issues[1]["title"] == "GitHub title"
    (problem,) = bridge.diag.open_problems(kind="title_differs")
    assert problem.plane_item_id == "item-1"
    assert "plane-sync-problem" in world.issues[1]["labels"]
    world.issues[1]["title"] = "Plane title"                    # someone settles it
    bridge.run_org_scan()
    assert bridge.diag.open_problems(kind="title_differs") == []
    assert "plane-sync-problem" not in world.issues[1]["labels"]


# ---------------------------------------------------------------- scans

def test_org_scan_is_measured_and_classified(env):
    bridge, world, clock = env
    world.add_item(1)
    bridge.scheduler.clock = clock
    summary = bridge.run_org_scan()
    assert "1 pair(s)" in summary and "next as light" in summary
    snap = bridge.scheduler.snapshot()
    assert snap["class"] == "light" and snap["last"]["api_calls"] > 0
    assert bridge.scheduler.next_run == pytest.approx(clock.t + bridge.scheduler.light_every)


def test_org_scan_stopped_by_a_rate_limit_is_not_recorded(env):
    bridge, world, clock = env
    bridge.scheduler.clock = clock
    world.fail[("GET", r"plane:/projects/$")] = [429]
    assert bridge.run_org_scan().startswith("stopped early (rate)")
    assert bridge.scheduler.last_class is None
    assert bridge.scheduler.next_run == clock.t + 15 * 60


def test_membership_pass_queues_transient_failures(env):
    bridge, world, clock = env
    world.add_item(1)
    world.fail[("POST", rf"github:/repos/{REPO}/issues$")] = [503]
    assert "1 item(s) failed" in bridge.sync_linked_modules()
    entry = bridge.queue.get("create_issue", "item-1")
    assert entry is not None and entry.payload["repo"] == REPO
    run_queue_after(bridge, clock, 6)
    assert len(world.issues) == 1 and bridge.queue.stats()["length"] == 0


def test_membership_pass_leaves_waiting_entries_to_the_queue(env):
    bridge, world, clock = env
    world.add_item(1)
    world.fail[("POST", rf"github:/repos/{REPO}/issues$")] = [503, 503]
    bridge.handle_plane(created())
    run_queue_after(bridge, clock, 6)           # fails again: next try in 15 s
    reads = world.calls[("plane", "GET")]
    bridge.sync_linked_modules()
    assert not world.issues                     # the pass didn't jump the queue
    assert world.calls[("plane", "GET")] - reads <= 4


# ---------------------------------------------------------------- /health and admin

def test_health_shows_the_engine(env):
    bridge, world, clock = env
    world.add_item(1)
    world.fail[("POST", rf"github:/repos/{REPO}/issues$")] = [503]
    bridge.handle_plane(created())
    with TestClient(build_app(bridge)) as client:
        body = client.get("/health").json()
    assert body["ok"] is True and "plane_api_ok" in body and "mappings" in body
    assert body["queue"]["length"] == 1
    assert {"source", "limit", "limit_source", "remaining", "reserve_share"} <= set(body["plane_pacer"])
    assert body["github_pacer"]["reserve_share"] == pytest.approx(0.3)
    assert {"class", "next_run", "last_run"} <= set(body["scheduler"])
    assert body["problems"]["open"] == 0
    assert body["reconcile_pending"] == 0


def test_plane_limit_is_inferred_from_the_highest_remaining(env):
    bridge, world, clock = env
    pacer = bridge.plane.pacer
    reset = pacer.wall() + 60
    # Plane's headers: Remaining and Reset (unix seconds), no Limit
    pacer.observe(httpx.Headers({"X-Ratelimit-Remaining": "288", "X-Ratelimit-Reset": str(reset)}))
    pacer.observe(httpx.Headers({"X-Ratelimit-Remaining": "250", "X-Ratelimit-Reset": str(reset)}))
    snap = pacer.snapshot()
    assert snap["limit"] == 289 and snap["limit_source"] == "inferred"


def test_admin_scan_needs_a_workspace_admin(env):
    bridge, world, clock = env
    members = {"admin-key": {"role": 20, "is_bot": False}, "bot-key": {"role": 20, "is_bot": True},
               "member-key": {"role": 15, "is_bot": False}}

    def plane_as_caller(req):
        who = members.get(req.headers.get("x-api-key"))
        if who is None:
            return httpx.Response(401, json={})
        if req.url.path.endswith("/users/me/"):
            return httpx.Response(200, json={"id": "u1", **who})
        return httpx.Response(200, json={"results": [{"member": {"id": "u1", **who}, "role": who["role"]}]})

    from bridge.diagnostics import make_router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(make_router(bridge.diag, "http://plane.local", "acme",
                                   transport=httpx.MockTransport(plane_as_caller),
                                   on_scan=bridge.scheduler.force))
    bridge.scheduler.next_run = clock.t + 9999
    bridge.scheduler.clock = clock
    with TestClient(app) as client:
        assert client.post("/admin/scan", headers={"x-api-key": "member-key"}).status_code == 403
        assert client.post("/admin/scan", headers={"x-api-key": "bot-key"}).status_code == 403
        assert not bridge.scheduler.due()
        r = client.post("/admin/scan", headers={"x-api-key": "admin-key"})
        assert r.status_code == 202 and r.json() == {"scheduled": True}
    assert bridge.scheduler.due()


def test_rate_limited_checks_are_postponed_not_dropped(env):
    bridge, world, clock = env
    bridge.debounce.clock = clock
    bridge.handle_github("repository", {"action": "edited", "repository": {"full_name": REPO}}, "r1")
    bridge.handle_plane(module_updated("m1"))
    clock.t += 31
    world.fail[("GET", r"plane:/projects/")] = [429]
    assert bridge.run_reconciles() == []
    assert bridge.debounce.pending() == 2
    clock.t += 31
    assert bridge.run_reconciles() == [REPO, REPO]
