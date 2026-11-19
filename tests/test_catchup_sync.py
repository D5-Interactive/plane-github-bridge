"""Catch-up sync under failure: per-item errors, rate limits, lost links, call counts."""
import json
import re
import sqlite3
from collections import Counter

import httpx
import pytest

from bridge.clients import GitHubClient, PlaneClient
from bridge.config import Settings
from bridge.store import Link, Store
from bridge.sync import PERMANENT_RETRY_SECONDS, Bridge

PROJ = "proj-1"
RATE_LIMITED_BODY = {"error_code": 5900, "error_message": "RATE_LIMIT_EXCEEDED"}


class Plane:
    """A Plane with several linked modules, counting every API call."""

    def __init__(self, modules: dict[str, tuple[str, list[str]]]):
        # module id -> (repo, [item ids])
        self.modules = modules
        self.calls: Counter = Counter()
        self.links: list[tuple[str, str]] = []
        self.fail_link_for: set[str] = set()     # add_link answers 500 for these items
        self.gone: set[str] = set()              # deleted: Plane answers 404 (JSON) for the item and its links
        self.proxy_404: set[str] = set()         # a proxy answers 404 (HTML page) for these, as while Plane restarts
        self.already_linked: set[str] = set()    # add_link answers Plane's duplicate-URL 400
        self.module_patch_status: int | None = None
        self.rate_limited_after: int | None = None  # answer 429 once this many calls were made
        self.projects_status: int | None = None     # answer this status for the project list
        self.total = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path.split("/workspaces/acme", 1)[1]
        self.total += 1
        if self.rate_limited_after is not None and self.total > self.rate_limited_after:
            return httpx.Response(429, json={"error_code": 5900, "error_message": "RATE_LIMIT_EXCEEDED"})
        key = re.sub(r"/[0-9a-z-]+-\d+/|/item-[^/]+/|/mod-[^/]+/", "/*/", p)
        self.calls[(req.method, key)] += 1
        if req.method == "GET" and p == "/projects/" and self.projects_status:
            return httpx.Response(self.projects_status, json={"detail": "nope"})
        if req.method == "GET" and p == "/projects/":
            return httpx.Response(200, json={"results": [{"id": PROJ, "identifier": "API"}]})
        if req.method == "GET" and p == f"/projects/{PROJ}/":
            return httpx.Response(200, json={"id": PROJ, "identifier": "API"})
        if req.method == "GET" and p == f"/projects/{PROJ}/modules/":
            return httpx.Response(200, json={"results": [
                {"id": mid, "name": repo.split("/")[1], "description_html": f"https://github.com/{repo}"}
                for mid, (repo, _) in self.modules.items()]})
        m = re.fullmatch(rf"/projects/{PROJ}/modules/([^/]+)/module-issues/", p)
        if m:
            return httpx.Response(200, json={"results": [{"id": i} for i in self.modules[m.group(1)][1]]})
        m = re.fullmatch(rf"/projects/{PROJ}/modules/([^/]+)/module-links/", p)
        if m:
            return httpx.Response(404, json={})
        if req.method == "PATCH" and re.fullmatch(rf"/projects/{PROJ}/modules/[^/]+/", p):
            if self.module_patch_status:
                return httpx.Response(self.module_patch_status, json=RATE_LIMITED_BODY)
            return httpx.Response(200, json={})
        m = re.fullmatch(rf"/projects/{PROJ}/issues/([^/]+)/", p)
        if m and req.method == "GET":
            if m.group(1) in self.gone:
                return httpx.Response(404, json={"error": "Issue does not exist"})
            if m.group(1) in self.proxy_404:
                return httpx.Response(404, text="<html><body>404 Not Found</body></html>")
            n = int(m.group(1).rsplit("-", 1)[1])
            return httpx.Response(200, json={"id": m.group(1), "name": f"Item {n}", "sequence_id": n,
                                             "description_html": "<p>x</p>"})
        m = re.fullmatch(rf"/projects/{PROJ}/issues/([^/]+)/links/", p)
        if m and req.method == "POST":
            if m.group(1) in self.fail_link_for:
                return httpx.Response(500, json={"error": "boom"})
            if m.group(1) in self.gone:
                return httpx.Response(404, json={"error": "Issue does not exist"})
            if m.group(1) in self.proxy_404:
                return httpx.Response(404, text="<html><body>404 Not Found</body></html>")
            if m.group(1) in self.already_linked:
                return httpx.Response(400, json={"error": "URL already exists for this Issue"})
            self.links.append((m.group(1), json.loads(req.content)["url"]))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"path": p})


class GitHub:
    def __init__(self):
        self.created: list[tuple[str, str]] = []
        self.refuse: dict[str, int] = {}     # repo -> status for issue creation
        self.refuse_headers: dict[str, str] = {}
        self.refuse_message = "Issues are disabled for this repo"
        self.repo_status: int | None = None  # answer this status for repo metadata
        self.repo_message = "Not Found"

    def handler(self, req: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)", req.url.path)
        if m and req.method == "GET":
            repo = m.group(1)
            if self.repo_status:
                return httpx.Response(self.repo_status, json={"message": self.repo_message})
            return httpx.Response(200, json={"full_name": repo, "name": repo.split("/")[1],
                                             "html_url": f"https://github.com/{repo}",
                                             "description": "", "created_at": "2026-01-01T00:00:00Z"})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues", req.url.path)
        if m and req.method == "POST":
            repo = m.group(1)
            if repo in self.refuse:
                return httpx.Response(self.refuse[repo], headers=self.refuse_headers,
                                      json={"message": self.refuse_message})
            title = json.loads(req.content)["title"]
            self.created.append((repo, title))
            n = len([r for r, _ in self.created if r == repo])
            return httpx.Response(201, json={"number": n, "html_url": f"https://github.com/{repo}/issues/{n}"})
        return httpx.Response(404, json={})


def make(tmp_path, modules, db="b.db"):
    s = Settings(plane_base_url="http://plane.local", plane_public_url="https://plane.example.com",
                 plane_workspace="acme", plane_api_key="k", plane_webhook_secret="",
                 github_token="t", github_webhook_secret="s")
    plane, gh = Plane(modules), GitHub()
    bridge = Bridge(s, Store(str(tmp_path / db)),
                    PlaneClient(s.plane_base_url, "acme", "k", transport=httpx.MockTransport(plane.handler),
                                sleep=lambda _s: None),
                    GitHubClient("t", transport=httpx.MockTransport(gh.handler), sleep=lambda _s: None))
    bridge.plane.cache_seconds = 0          # every pass re-reads, like the 120 s interval does
    return bridge, plane, gh


def items(prefix, n):
    return [f"item-{prefix}-{i}" for i in range(1, n + 1)]


def test_one_failing_item_does_not_stop_the_rest(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 2)),
                                        "mod-b": ("acme/b", items("b", 2))})
    gh.refuse["acme/a"] = 410                 # Issues disabled on repo a
    result = bridge.sync_linked_modules()
    assert "created acme/b#1, acme/b#2" in result and "2 item(s) failed" in result
    assert {r for r, _ in gh.created} == {"acme/b"}


def test_permanent_failure_waits_for_the_cooldown(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    gh.refuse["acme/a"] = 404
    t = [1_800_000_000.0]
    bridge.clock = lambda: t[0]
    assert "1 item(s) failed" in bridge.sync_linked_modules()
    reads = plane.calls[("GET", "/projects/*/issues/*/")]
    assert reads == 1
    assert bridge.sync_linked_modules() == "nothing new"          # not retried, and quiet
    assert plane.calls[("GET", "/projects/*/issues/*/")] == reads
    del gh.refuse["acme/a"]
    t[0] += PERMANENT_RETRY_SECONDS + 1
    assert bridge.sync_linked_modules() == "created acme/a#1"
    assert bridge.store.failure("item-a-1") is None


def test_transient_failure_is_retried_next_pass(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 1))})
    gh.refuse["acme/a"] = 502
    assert "1 item(s) failed" in bridge.sync_linked_modules()
    del gh.refuse["acme/a"]
    assert bridge.sync_linked_modules() == "created acme/a#1"


def test_rate_limit_stops_the_pass_and_the_next_pass_resumes(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 6))})
    plane.rate_limited_after = 12
    first = bridge.sync_linked_modules()
    assert "stopped early: rate limited" in first
    made = len(gh.created)
    assert 0 < made < 6
    calls_when_stopped = plane.total
    # The client retries the rate-limited call 3 times (no real sleeping here),
    # then the pass stops: one call plus its retries, then nothing more.
    assert calls_when_stopped == 13 + 3
    plane.rate_limited_after = None
    second = bridge.sync_linked_modules()
    assert "stopped" not in second
    assert len(gh.created) == 6 and len(set(gh.created)) == 6     # resumed, no duplicates


def test_lost_plane_link_is_repaired_on_the_next_pass(tmp_path):
    bridge, plane, gh = make(tmp_path, {"mod-a": ("acme/a", items("a", 2))})
    plane.fail_link_for = {"item-a-1"}
    result = bridge.sync_linked_modules()
    assert "created acme/a#2" in result and "1 item(s) failed" in result
    link = bridge.store.by_plane_item("item-a-1")
    assert link and link.plane_linked is False                     # issue exists, link missing
    plane.fail_link_for = set()
    assert bridge.sync_linked_modules() == "repaired 1 Plane link(s)"
    assert ("item-a-1", "https://github.com/acme/a/issues/1") in plane.links
    assert bridge.store.by_plane_item("item-a-1").plane_linked is True
    assert len(gh.created) == 2                                    # never a second issue
    assert bridge.sync_linked_modules() == "nothing new"


def test_calls_per_pass_do_not_scale_with_modules_times_items(tmp_path):
    mods = {f"mod-{k}": (f"acme/r{k}", items(str(k), 5)) for k in range(8)}
    bridge, plane, gh = make(tmp_path, mods)
    assert bridge.sync_linked_modules().startswith("created")
    assert len(gh.created) == 40
    listings = sum(v for (m, k), v in plane.calls.items() if k.endswith("/module-issues/"))
    assert listings == 8                                           # once per module, not per item
    plane.calls.clear()
    assert bridge.sync_linked_modules() == "nothing new"
    assert sum(plane.calls.values()) == 1 + 1 + 8                  # projects, modules, one listing each


def test_projects_without_linked_modules_list_no_items(tmp_path):
    bridge, plane, gh = make(tmp_path, {})
    assert bridge.sync_linked_modules() == "nothing new"
    assert not any(k.endswith("/module-issues/") for _, k in plane.calls)


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE links (plane_item_id TEXT PRIMARY KEY, plane_project_id TEXT NOT NULL,
            repo TEXT NOT NULL, issue_number INTEGER NOT NULL, issue_url TEXT NOT NULL,
            UNIQUE (repo, issue_number));
        INSERT INTO links VALUES ('item-1', 'p', 'acme/a', 3, 'https://github.com/acme/a/issues/3');
    """)
    db.commit()
    db.close()
    store = Store(str(path))
    link = store.by_plane_item("item-1")
    assert link.issue_number == 3 and link.plane_linked is True    # old rows were linked in Plane
    assert store.missing_plane_links(10) == []
    store.save(Link("item-2", "p", "acme/a", 4, "u", plane_linked=False))
    assert [l.plane_item_id for l in store.missing_plane_links(10)] == ["item-2"]
    Store(str(path))                                               # opening again is harmless
