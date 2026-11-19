"""Sync problems: recording, labels on both sides, and the /status and /admin views."""
import json
import logging
import re

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bridge.clients import GitHubClient, PlaneClient
from bridge.diagnostics import Diagnostics, fingerprint, make_router
from bridge.store import Store

PROJ, ITEM, OTHER_LABEL = "proj-1", "item-1", "lbl-existing"


class FakePlane:
    def __init__(self):
        self.labels: list[dict] = []
        self.item_labels = {ITEM: [OTHER_LABEL], "item-2": []}
        self.fail = False
        self.calls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path.split("/workspaces/acme", 1)[1]
        self.calls.append((req.method, p))
        if self.fail:
            return httpx.Response(500, json={"detail": "boom"})
        if p == f"/projects/{PROJ}/labels/" and req.method == "GET":
            return httpx.Response(200, json={"results": self.labels})
        if p == f"/projects/{PROJ}/labels/" and req.method == "POST":
            body = json.loads(req.content)
            label = {"id": f"lbl-{len(self.labels) + 1}", **body}
            self.labels.append(label)
            return httpx.Response(201, json=label)
        m = re.fullmatch(rf"/projects/{PROJ}/issues/([^/]+)/", p)
        if m and req.method == "GET":
            return httpx.Response(200, json={"id": m.group(1), "labels": list(self.item_labels[m.group(1)])})
        if m and req.method == "PATCH":
            self.item_labels[m.group(1)] = json.loads(req.content)["labels"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "not found"})


class FakeGitHub:
    def __init__(self):
        self.repo_labels: set[str] = set()
        self.issue_labels: dict[int, set[str]] = {}
        self.calls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        self.calls.append((req.method, p))
        if p == "/repos/acme/app/labels/plane-sync-problem" and req.method == "GET":
            return httpx.Response(200 if "plane-sync-problem" in self.repo_labels else 404, json={})
        if p == "/repos/acme/app/labels" and req.method == "POST":
            body = json.loads(req.content)
            assert len(body["description"]) <= 100
            self.repo_labels.add(body["name"])
            return httpx.Response(201, json=body)
        m = re.fullmatch(r"/repos/acme/app/issues/(\d+)/labels", p)
        if m and req.method == "POST":
            self.issue_labels.setdefault(int(m.group(1)), set()).update(json.loads(req.content)["labels"])
            return httpx.Response(200, json=[])
        m = re.fullmatch(r"/repos/acme/app/issues/(\d+)/labels/(.+)", p)
        if m and req.method == "DELETE":
            labels = self.issue_labels.get(int(m.group(1)), set())
            if m.group(2) not in labels:
                return httpx.Response(404, json={})
            labels.discard(m.group(2))
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})


def make(tmp_path, t=None):
    plane, gh = FakePlane(), FakeGitHub()
    t = t or [1000.0]
    diag = Diagnostics(Store(str(tmp_path / "b.db")),
                       PlaneClient("http://plane", "acme", "k", transport=httpx.MockTransport(plane.handler)),
                       GitHubClient("t", transport=httpx.MockTransport(gh.handler)),
                       public_url="https://hooks.example.test", clock=lambda: t[0])
    return diag, plane, gh, t


def test_report_dedupes_and_counts(tmp_path):
    diag, *_ = make(tmp_path)
    a = diag.report("conflict", plane_project_id=PROJ, plane_item_id=ITEM, key="title")
    b = diag.report("conflict", plane_project_id=PROJ, plane_item_id=ITEM, key="title")
    c = diag.report("conflict", plane_project_id=PROJ, plane_item_id=ITEM, key="state")
    assert a == b != c
    [p] = [p for p in diag.open_problems() if p.id == a]
    assert p.count == 2
    assert diag.counts()["open"] == 2


def test_detail_values_are_kept_short(tmp_path):
    diag, *_ = make(tmp_path)
    diag.report("refused", repo="acme/app", detail={"status": 403, "why": "x" * 500})
    [p] = diag.open_problems()
    assert len(p.detail["why"]) <= 120


def test_plane_label_is_created_and_merged_without_dropping_others(tmp_path):
    diag, plane, *_ = make(tmp_path)
    diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM)
    [label] = plane.labels
    assert label["name"] == "sync problem" and "hooks.example.test/status/" in label["description"]
    assert plane.item_labels[ITEM] == [OTHER_LABEL, label["id"]]
    # A second problem on the same item doesn't add the label twice, nor create another label.
    diag.report("conflict", plane_project_id=PROJ, plane_item_id=ITEM)
    assert plane.item_labels[ITEM] == [OTHER_LABEL, label["id"]] and len(plane.labels) == 1


def test_existing_plane_label_is_reused(tmp_path):
    diag, plane, *_ = make(tmp_path)
    plane.labels.append({"id": "lbl-old", "name": "Sync Problem"})
    diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM)
    assert plane.item_labels[ITEM] == [OTHER_LABEL, "lbl-old"]
    assert not any(m == "POST" and p.endswith("/labels/") for m, p in plane.calls)


def test_github_label_is_created_and_added(tmp_path):
    diag, _, gh, _ = make(tmp_path)
    diag.report("conflict", repo="acme/app", issue_number=7)
    assert "plane-sync-problem" in gh.repo_labels
    assert gh.issue_labels[7] == {"plane-sync-problem"}


def test_resolve_removes_labels_only_when_nothing_else_is_open(tmp_path):
    diag, plane, gh, _ = make(tmp_path)
    first = diag.report("conflict", plane_project_id=PROJ, plane_item_id=ITEM, repo="acme/app",
                        issue_number=7, key="title")
    diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM, repo="acme/app", issue_number=7)
    label_id = plane.labels[0]["id"]
    fp = fingerprint("conflict", PROJ, ITEM, "acme/app", 7, "title")
    assert diag.resolve(fp) == 1
    assert label_id in plane.item_labels[ITEM] and gh.issue_labels[7] == {"plane-sync-problem"}
    assert diag.resolve(kind="stuck", plane_item_id=ITEM) == 1
    assert plane.item_labels[ITEM] == [OTHER_LABEL]
    assert gh.issue_labels[7] == set()
    assert diag.counts()["open"] == 0 and first


def test_reported_again_after_resolving_reopens(tmp_path):
    diag, plane, *_ = make(tmp_path)
    pid = diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM)
    diag.resolve(plane_item_id=ITEM)
    assert diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM) == pid
    assert plane.labels[0]["id"] in plane.item_labels[ITEM]


def test_flag_failure_never_raises_and_is_retried(tmp_path, caplog):
    diag, plane, *_ = make(tmp_path)
    plane.fail = True
    with caplog.at_level(logging.WARNING, logger="bridge"):
        diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM)   # doesn't raise
    assert diag.counts() == {"open": 1, "by_kind": {"stuck": 1}, "flags_pending": 1}
    assert "will retry" in caplog.text
    plane.fail = False
    assert diag.reflag_pending() == 1
    assert plane.labels[0]["id"] in plane.item_labels[ITEM]
    assert diag.counts()["flags_pending"] == 0


def test_reflag_follows_the_current_state(tmp_path):
    diag, plane, *_ = make(tmp_path)
    plane.fail = True
    diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM)
    diag.resolve(plane_item_id=ITEM)            # resolved while Plane was failing
    plane.fail = False
    diag.reflag_pending()
    assert plane.item_labels[ITEM] == [OTHER_LABEL]   # the retry removes, it doesn't add


def test_filters(tmp_path):
    diag, *_ = make(tmp_path)
    diag.report("refused", repo="acme/app", issue_number=1)
    diag.report("conflict", plane_project_id=PROJ, plane_item_id="item-2")
    assert [p.kind for p in diag.open_problems(repo="ACME/app")] == ["refused"]
    assert [p.kind for p in diag.open_problems(project=PROJ)] == ["conflict"]
    assert [p.kind for p in diag.open_problems(kind="refused")] == ["refused"]


# ---------------------------------------------------------------- views

KEY = "plane_api_secret_caller_key_123"


def views(tmp_path, *, can_read=True, role=20):
    diag, *_ = make(tmp_path)
    seen = []

    def caller_plane(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("X-API-Key"))
        p = req.url.path
        if p == "/api/v1/workspaces/acme/issues/API-7/":
            return (httpx.Response(200, json={"id": ITEM, "project": PROJ}) if can_read
                    else httpx.Response(404, json={"detail": "no"}))
        if p == "/api/v1/users/me/":
            return httpx.Response(200, json={"id": "user-me"})
        if p == "/api/v1/workspaces/acme/members/":
            return httpx.Response(200, json=[{"id": "user-other", "role": 20},
                                             {"id": "user-me", "role": role}])
        return httpx.Response(404, json={})

    app = FastAPI()
    app.include_router(make_router(diag, "http://plane", "acme", transport=httpx.MockTransport(caller_plane)))
    return diag, TestClient(app), seen


def test_status_returns_the_items_problems_to_a_caller_who_can_read_it(tmp_path, caplog):
    diag, client, seen = views(tmp_path)
    diag.report("conflict", plane_project_id=PROJ, plane_item_id=ITEM, key="title")
    diag.report("stuck", plane_project_id=PROJ, plane_item_id="item-2")
    with caplog.at_level(logging.DEBUG):
        r = client.get("/status/api-7", headers={"X-Api-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["ref"] == "API-7" and [p["kind"] for p in body["open"]] == ["conflict"]
    assert seen == [KEY]                       # checked with the caller's own key
    assert KEY not in caplog.text              # and never logged


def test_status_hides_items_the_caller_cant_read(tmp_path):
    diag, client, _ = views(tmp_path, can_read=False)
    diag.report("conflict", plane_project_id=PROJ, plane_item_id=ITEM)
    assert client.get("/status/API-7", headers={"X-Api-Key": KEY}).status_code == 404
    assert client.get("/status/API-7").status_code == 404                      # no key
    assert client.get("/status/not a ref", headers={"X-Api-Key": KEY}).status_code == 404


def test_status_lists_recently_resolved_separately(tmp_path):
    diag, client, _ = views(tmp_path)
    diag.report("stuck", plane_project_id=PROJ, plane_item_id=ITEM)
    diag.resolve(plane_item_id=ITEM)
    body = client.get("/status/API-7", headers={"X-Api-Key": KEY}).json()
    assert body["open"] == [] and [p["kind"] for p in body["recently_resolved"]] == ["stuck"]


def test_admin_view_for_admins_only(tmp_path):
    diag, client, _ = views(tmp_path, role=20)
    diag.report("refused", repo="acme/app", issue_number=3)
    r = client.get("/admin/sync-problems?repo=acme/app", headers={"X-Api-Key": KEY})
    assert r.status_code == 200 and r.json()["counts"]["open"] == 1
    assert client.get("/admin/sync-problems").status_code == 403

    _, member_client, _ = views(tmp_path, role=15)
    assert member_client.get("/admin/sync-problems", headers={"X-Api-Key": KEY}).status_code == 403
