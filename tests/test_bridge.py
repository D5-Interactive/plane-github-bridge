import hashlib
import hmac
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import build_app
from bridge.clients import GitHubClient, PlaneClient
from bridge.config import Mapping, Settings
from bridge.render import (closing_refs, normalize_plane_event, plane_html_to_markdown,
                           verify_github_signature, verify_plane_signature)
from bridge.store import Store
from bridge.sync import Bridge

PROJ, ITEM, MOD = "proj-1", "item-1", "mod-auth"
STATES = [
    {"id": "s-backlog", "name": "Backlog", "group": "backlog"},
    {"id": "s-todo", "name": "Todo", "group": "unstarted"},
    {"id": "s-prog", "name": "In Progress", "group": "started"},
    {"id": "s-review", "name": "In Review", "group": "started"},
    {"id": "s-done", "name": "Done", "group": "completed"},
    {"id": "s-cancel", "name": "Cancelled", "group": "cancelled"},
]
DESC = ('<p>Add a scan command.</p><h3>Acceptance criteria</h3>'
        '<ul data-type="taskList"><li data-checked="false" data-type="taskItem"><p>Scans all maps</p></li>'
        '<li data-checked="true" data-type="taskItem"><p>Supports ?? wildcards</p></li></ul>')


# ---------------------------------------------------------------- fakes

class FakePlane:
    def __init__(self):
        self.item = {"id": ITEM, "name": "Scan command", "sequence_id": 7, "state": "s-todo",
                     "description_html": DESC, "project": PROJ}
        self.module_members = {ITEM}
        self.module_desc = ""
        self.module_name = "Auth"
        self.module_patches = []
        self.reject_rename = False
        self.project_desc = ""
        self.links, self.patches, self.comments = [], [], []

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["X-API-Key"] == "plane-key"
        p = req.url.path.split("/workspaces/acme", 1)[1]
        if req.method == "GET" and p == f"/projects/{PROJ}/":
            return httpx.Response(200, json={"id": PROJ, "identifier": "API", "description": self.project_desc})
        if req.method == "GET" and p == f"/projects/{PROJ}/issues/{ITEM}/":
            return httpx.Response(200, json=self.item)
        if req.method == "GET" and p == f"/projects/{PROJ}/states/":
            return httpx.Response(200, json={"results": STATES, "next_page_results": False})
        if req.method == "GET" and p == f"/projects/{PROJ}/modules/":
            return httpx.Response(200, json={"results": [{"id": MOD, "name": self.module_name,
                                                          "description_html": self.module_desc}]})
        if req.method == "GET" and p == f"/projects/{PROJ}/modules/{MOD}/module-issues/":
            return httpx.Response(200, json={"results": [{"id": i} for i in self.module_members]})
        if req.method == "PATCH" and p == f"/projects/{PROJ}/modules/{MOD}/":
            body = json.loads(req.content)
            if "name" in body and self.reject_rename:
                return httpx.Response(400, json={"error": "module with this name already exists"})
            self.module_patches.append(body)
            self.module_desc = body.get("description_html", self.module_desc)
            self.module_name = body.get("name", self.module_name)
            return httpx.Response(200, json={})
        if req.method == "PATCH" and p == f"/projects/{PROJ}/issues/{ITEM}/":
            body = json.loads(req.content)
            self.patches.append(body)
            self.item["state"] = body["state"]
            return httpx.Response(200, json=self.item)
        if req.method == "POST" and p == f"/projects/{PROJ}/issues/{ITEM}/comments/":
            self.comments.append(json.loads(req.content)["comment_html"])
            return httpx.Response(201, json={})
        if req.method == "POST" and p == f"/projects/{PROJ}/issues/{ITEM}/links/":
            self.links.append(json.loads(req.content))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"path": p})


class FakeGitHub:
    def __init__(self):
        self.created, self.updated = [], []
        self.repos = {"acme/from-plane": {"full_name": "acme/from-plane", "name": "from-plane",
                                          "html_url": "https://github.com/acme/from-plane",
                                          "description": "Tools <for> testing"}}

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer gh-token"
        repo_get = re.fullmatch(r"/repos/([^/]+/[^/]+)", req.url.path)
        if req.method == "GET" and repo_get:
            meta = self.repos.get(repo_get.group(1))
            return httpx.Response(200, json=meta) if meta else httpx.Response(404, json={})
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues(?:/(\d+))?", req.url.path)
        assert m, req.url.path
        body = json.loads(req.content)
        if req.method == "POST":
            n = 40 + len(self.created) + 2
            self.created.append((m.group(1), body))
            return httpx.Response(201, json={"number": n, "html_url": f"https://github.com/{m.group(1)}/issues/{n}"})
        self.updated.append((m.group(1), int(m.group(2)), body))
        return httpx.Response(200, json={})


@pytest.fixture
def env(tmp_path):
    s = Settings(plane_base_url="http://plane.local", plane_public_url="https://plane.example.com",
                 plane_workspace="acme", plane_api_key="plane-key", plane_webhook_secret="plane_wh_secret",
                 github_token="gh-token", github_webhook_secret="gh-secret",
                 mappings=[Mapping(project="API", module="Auth", repo="acme/auth-service")])
    fp, fg = FakePlane(), FakeGitHub()
    bridge = Bridge(s, Store(str(tmp_path / "b.db")),
                    PlaneClient(s.plane_base_url, "acme", "plane-key", transport=httpx.MockTransport(fp.handler)),
                    GitHubClient("gh-token", transport=httpx.MockTransport(fg.handler)))
    return bridge, fp, fg


def plane_event(event="workitem.created", eid="e1", prev=None):
    return {"version": "v2", "event": event, "event_id": eid, "entity_type": "issue",
            "entity_id": ITEM, "data": {"id": ITEM, "project_id": PROJ, "name": "Scan command"},
            "previous_attributes": prev or {}}


# ---------------------------------------------------------------- pure helpers

def test_signatures():
    body = b'{"a":1}'
    sig = hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert verify_plane_signature("k", body, sig)
    assert not verify_plane_signature("k", body + b" ", sig)
    assert not verify_plane_signature("k", body, None)
    assert verify_github_signature("k", body, "sha256=" + sig)
    assert not verify_github_signature("k", body, sig)


@pytest.mark.parametrize("payload,expected", [
    ({"event": "workitem.created"}, ("workitem", "created")),
    ({"event": "workitem.updated"}, ("workitem", "updated")),
    ({"event": "issue", "action": "create"}, ("workitem", "created")),
    ({"event": "issue", "action": "updated"}, ("workitem", "updated")),
    ({"event": "WebhookScope.ScopeChoices.WORK_ITEM_UPDATED"}, ("workitem", "updated")),
    ({"event": "workitem.comment.created"}, (None, None)),
    ({"event": "cycle.created"}, (None, None)),
])
def test_normalize(payload, expected):
    assert normalize_plane_event(payload) == expected


def test_markdown_keeps_checklists():
    md = plane_html_to_markdown(DESC)
    assert "Add a scan command." in md
    assert "### Acceptance criteria" in md
    assert "- [ ] Scans all maps" in md
    assert "- [x] Supports ?? wildcards" in md


def test_closing_refs():
    text = "Closes #12, fixes acme/other#3 and resolves: #12. Mentions #99."
    assert closing_refs(text, "acme/auth-service") == [
        ("acme/auth-service", 12), ("acme/other", 3)]


# ---------------------------------------------------------------- flows

def test_created_item_in_module_makes_issue_once(env):
    bridge, fp, fg = env
    assert bridge.handle_plane(plane_event()).startswith("created acme/auth-service#42")
    repo, body = fg.created[0]
    assert repo == "acme/auth-service" and body["title"] == "Scan command"
    assert "- [ ] Scans all maps" in body["body"]
    assert "https://plane.example.com/acme/browse/API-7/" in body["body"]
    assert fp.links[0]["url"] == "https://github.com/acme/auth-service/issues/42"
    # an update afterwards (e.g. our own link being added) must not create a second issue
    bridge.handle_plane(plane_event("workitem.updated", "e2", {"state": "x"}))
    assert len(fg.created) == 1
    # duplicate delivery of the same event id is ignored
    assert bridge.handle_plane(plane_event()) == "ignored: duplicate delivery"


def test_item_added_to_module_later(env):
    bridge, fp, fg = env
    fp.module_members = set()
    assert "kept in Plane" in bridge.handle_plane(plane_event())
    fp.module_members = {ITEM}
    assert bridge.handle_plane(plane_event("workitem.updated", "e2")).startswith("created")


def test_item_in_unlinked_module_stays_in_plane(env):
    bridge, fp, fg = env
    bridge.s.mappings = [Mapping(project="WEB", module="Site", repo="acme/website")]
    assert bridge.handle_plane(plane_event()) == "kept in Plane: item isn't in a module linked to a repo"
    assert not fg.created


def test_github_repo_in():
    from bridge.render import github_repo_in
    assert github_repo_in('<p>Repo: <a href="https://github.com/acme/auth-service">x</a></p>') == "acme/auth-service"
    assert github_repo_in("https://github.com/acme/my.repo.git") == "acme/my.repo"
    assert github_repo_in("see https://github.com/acme/api/issues/3 for more") == "acme/api"
    assert github_repo_in("https://github.com/acme") is None
    assert github_repo_in(None, "", "gitlab.com/acme/x") is None


def test_module_description_links_repo_without_config(env):
    bridge, fp, fg = env
    bridge.s.mappings = []
    fp.module_desc = '<p>Code: <a href="https://github.com/acme/from-plane">github.com/acme/from-plane</a></p>'
    assert bridge.handle_plane(plane_event()) == "created acme/from-plane#42"


def test_module_link_only_applies_to_items_in_that_module(env):
    bridge, fp, fg = env
    bridge.s.mappings = []
    fp.module_desc = "https://github.com/acme/from-plane"
    fp.module_members = set()
    assert "kept in Plane" in bridge.handle_plane(plane_event())


def test_item_in_no_module_stays_in_plane_even_with_repo_in_project_description(env):
    bridge, fp, fg = env
    bridge.s.mappings = []
    fp.module_members = set()
    fp.project_desc = "All work for https://github.com/acme/monorepo"
    assert "kept in Plane" in bridge.handle_plane(plane_event())
    assert not fg.created


def test_config_mapping_wins_over_description(env):
    bridge, fp, fg = env
    fp.module_desc = "https://github.com/acme/from-plane"
    assert bridge.handle_plane(plane_event()) == "created acme/auth-service#42"


def test_allowed_owners_blocks_other_orgs(env):
    bridge, fp, fg = env
    bridge.s.mappings = []
    bridge.s.allowed_owners = ["acme"]
    fp.module_desc = "https://github.com/someone-else/repo"
    assert "GITHUB_ALLOWED_OWNERS" in bridge.handle_plane(plane_event())
    assert not fg.created


def test_title_and_description_edits_pushed(env):
    bridge, fp, fg = env
    bridge.handle_plane(plane_event())
    fp.item["name"] = "Scan command v2"
    out = bridge.handle_plane(plane_event("workitem.updated", "e3", {"name": "Scan command"}))
    assert out == "updated acme/auth-service#42"
    assert fg.updated[-1][2]["title"] == "Scan command v2"


def gh_pr(action, number=5, body="Closes #42", draft=False, merged=False, state="open"):
    return {"action": action, "repository": {"full_name": "acme/auth-service"},
            "pull_request": {"number": number, "title": "Add scan", "body": body, "draft": draft,
                             "merged": merged, "state": state,
                             "html_url": f"https://github.com/acme/auth-service/pull/{number}"}}


def test_pr_and_issue_lifecycle(env):
    bridge, fp, fg = env
    bridge.handle_plane(plane_event())

    assert bridge.handle_github("pull_request", gh_pr("opened", draft=True)) == "moved to progress"
    assert fp.item["state"] == "s-prog"
    assert bridge.handle_github("pull_request", gh_pr("ready_for_review")) == "moved to review"
    assert fp.item["state"] == "s-review"
    pr_links = [l for l in fp.links if "/pull/" in l["url"]]
    assert len(pr_links) == 1  # PR link added once, not on every event

    assert bridge.handle_github("pull_request", gh_pr("closed", merged=True, state="closed")) == "moved to done"
    issue_closed = {"action": "closed", "repository": {"full_name": "acme/auth-service"},
                    "issue": {"number": 42, "state_reason": "completed"}}
    assert bridge.handle_github("issues", issue_closed) == "unchanged: already done"

    reopened = {**issue_closed, "action": "reopened"}
    assert bridge.handle_github("issues", reopened) == "moved to todo"
    not_planned = {**issue_closed, "issue": {"number": 42, "state_reason": "not_planned"}}
    assert bridge.handle_github("issues", not_planned) == "moved to cancelled"


def test_pr_for_unlinked_issue_ignored(env):
    bridge, fp, fg = env
    assert "does not close a linked issue" in bridge.handle_github("pull_request", gh_pr("opened", body="Closes #1"))


# ---------------------------------------------------------------- HTTP layer

def test_http_rejects_bad_signatures_and_accepts_good(env):
    bridge, fp, fg = env
    client = TestClient(build_app(bridge))
    body = json.dumps(plane_event()).encode()
    assert client.post("/plane/webhook", content=body, headers={"X-Plane-Signature": "nope"}).status_code == 401
    sig = hmac.new(b"plane_wh_secret", body, hashlib.sha256).hexdigest()
    r = client.post("/plane/webhook", content=body, headers={"X-Plane-Signature": sig})
    assert r.status_code == 202 and len(fg.created) == 1  # background task ran

    gbody = json.dumps(gh_pr("opened")).encode()
    gsig = "sha256=" + hmac.new(b"gh-secret", gbody, hashlib.sha256).hexdigest()
    assert client.post("/github/webhook", content=gbody,
                       headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "pull_request"}).status_code == 401
    r = client.post("/github/webhook", content=gbody,
                    headers={"X-Hub-Signature-256": gsig, "X-GitHub-Event": "pull_request"})
    assert r.status_code == 202 and fp.item["state"] == "s-review"
    assert client.get("/health").json()["ok"] is True


def test_missing_plane_secret_refuses_plane_webhooks(env):
    bridge, fp, fg = env
    bridge.s.plane_webhook_secret = ""
    client = TestClient(build_app(bridge))
    body = json.dumps(plane_event()).encode()
    forged = hmac.new(b"", body, hashlib.sha256).hexdigest()  # signed with the empty key
    assert client.post("/plane/webhook", content=body, headers={"X-Plane-Signature": forged}).status_code == 503
    assert not verify_plane_signature("", body, forged)
    assert client.get("/health").json()["plane_webhook_ready"] is False
    assert not fg.created


# ---------------------------------------------------------------- GitHub activity -> Plane comments

def test_mention_and_branch_helpers():
    from bridge.render import branch_issue_number, mention_refs
    assert mention_refs("Fix scan (#42), see acme/other#3; not a#9", "acme/x") == [("acme/x", 42), ("acme/other", 3)]
    assert branch_issue_number("42-scan-command") == 42
    assert branch_issue_number("feature/42_scan") == 42
    assert branch_issue_number("main") is None


def push_event(branch="42-scan-command", commits=None):
    commits = commits or [{"id": "abc1234def", "message": "Add scanner\n\nbody", "url": "https://github.com/c/abc"}]
    return {"ref": f"refs/heads/{branch}", "repository": {"full_name": "acme/auth-service"},
            "pusher": {"name": "zep"}, "commits": commits}


def test_push_to_issue_branch_posts_commits_once(env):
    bridge, fp, fg = env
    bridge.handle_plane(plane_event())
    assert bridge.handle_github("push", push_event()) == "commits posted to 1 item(s)"
    assert "1 commit pushed to <code>42-scan-command</code>" in fp.comments[-1]
    assert "<code>abc1234</code></a> Add scanner" in fp.comments[-1]
    # the same commit pushed again (e.g. to another branch) isn't posted twice
    assert bridge.handle_github("push", push_event()) == "commits posted to 0 item(s)"
    assert len(fp.comments) == 1


def test_push_with_mention_on_other_branch(env):
    bridge, fp, fg = env
    bridge.handle_plane(plane_event())
    ev = push_event("main", [{"id": "1111111aaa", "message": "Tweak <b>html</b> for #42", "url": "u"},
                             {"id": "2222222bbb", "message": "Unrelated", "url": "u"}])
    assert bridge.handle_github("push", ev) == "commits posted to 1 item(s)"
    assert "&lt;b&gt;html&lt;/b&gt;" in fp.comments[-1]       # commit text is escaped
    assert "Unrelated" not in fp.comments[-1]
    assert "ignored" in bridge.handle_github("push", push_event("main", [{"id": "3", "message": "nope", "url": "u"}]))


def test_branch_creation_posts_comment_and_link(env):
    bridge, fp, fg = env
    bridge.handle_plane(plane_event())
    ev = {"ref": "42-scan-command", "ref_type": "branch", "repository": {"full_name": "acme/auth-service"},
          "sender": {"login": "zep"}}
    bridge.handle_github("create", ev)
    assert "branch" in fp.comments[-1] and "42-scan-command" in fp.comments[-1]
    assert fp.links[-1]["url"] == "https://github.com/acme/auth-service/tree/42-scan-command"
    bridge.handle_github("create", ev)
    assert len(fp.comments) == 1


def test_review_and_pr_activity_comments(env):
    bridge, fp, fg = env
    bridge.handle_plane(plane_event())
    bridge.handle_github("pull_request", gh_pr("opened"))
    assert "opened by" in fp.comments[-1]
    review = {"action": "submitted", "repository": {"full_name": "acme/auth-service"},
              "pull_request": gh_pr("opened")["pull_request"],
              "review": {"id": 9, "state": "APPROVED", "body": "", "user": {"login": "lead"},
                         "html_url": "https://github.com/r/9"}}
    assert bridge.handle_github("pull_request_review", review) == "review posted to 1 item(s)"
    assert fp.comments[-1].startswith("<p>◆ <strong>GitHub</strong> · <code>lead</code> ✓ approved")
    merged = gh_pr("closed", merged=True, state="closed")
    merged["pull_request"]["base"] = {"ref": "main"}
    bridge.handle_github("pull_request", merged)
    assert "merged into <code>main</code>" in fp.comments[-1]
    assert fp.item["state"] == "s-done"


def test_pr_linked_by_branch_name_without_closes(env):
    bridge, fp, fg = env
    bridge.handle_plane(plane_event())
    ev = gh_pr("opened", body="no keywords here")
    ev["pull_request"]["head"] = {"ref": "42-scan-command"}
    assert bridge.handle_github("pull_request", ev) == "moved to review"


# ---------------------------------------------------------------- module description tidy-up

def module_event(desc, eid="m1", name="Auth"):
    return {"event": "module.updated", "event_id": eid, "entity_type": "module", "entity_id": MOD,
            "data": {"id": MOD, "project_id": PROJ, "name": name, "description_html": desc}}


def test_module_link_anywhere_is_rewritten_to_link_plus_description(env):
    bridge, fp, fg = env
    out = bridge.handle_plane(module_event("<p>We keep the code over at "
                                           "https://github.com/acme/from-plane, ping Zep with questions</p>"))
    assert out == "module tidied for acme/from-plane"
    assert fp.module_patches[-1]["name"] == "from-plane"
    html = fp.module_patches[-1]["description_html"]
    assert html == ('<p><a href="https://github.com/acme/from-plane">https://github.com/acme/from-plane</a></p>'
                    "<p>Tools &lt;for&gt; testing</p>")
    assert fp.module_patches[-1]["description"] == "https://github.com/acme/from-plane Tools <for> testing"


def test_tidy_is_idempotent_so_our_own_edit_does_not_loop(env):
    bridge, fp, fg = env
    bridge.handle_plane(module_event("https://github.com/acme/from-plane"))
    # Plane fires module.updated for our own edit; it must be a no-op
    assert bridge.handle_plane(module_event(fp.module_desc, "m2", fp.module_name)) == "unchanged: module already tidy"
    assert len(fp.module_patches) == 1


def test_tidy_leaves_module_alone_when_token_cannot_see_repo(env):
    bridge, fp, fg = env
    assert bridge.handle_plane(module_event("https://github.com/acme/secret")) == "ignored: token can't see acme/secret"
    assert not fp.module_patches


def test_tidy_respects_allowed_owners(env):
    bridge, fp, fg = env
    bridge.s.allowed_owners = ["other-org"]
    assert "GITHUB_ALLOWED_OWNERS" in bridge.handle_plane(module_event("https://github.com/acme/from-plane"))
    assert not fp.module_patches


def test_new_work_item_also_tidies_its_module(env):
    bridge, fp, fg = env
    bridge.s.mappings = []
    fp.module_desc = "repo: https://github.com/acme/from-plane"
    assert bridge.handle_plane(plane_event()) == "created acme/from-plane#42"
    assert fp.module_patches  # description was rewritten on the way


def test_normalize_module_events():
    assert normalize_plane_event({"event": "module.updated"}) == ("module", "updated")
    assert normalize_plane_event({"event": "module", "action": "update"}) == ("module", "updated")
    assert normalize_plane_event({"event": "WebhookScope.ScopeChoices.MODULE_CREATED"}) == ("module", "created")


def test_rename_conflict_still_tidies_description(env):
    bridge, fp, fg = env
    fp.reject_rename = True
    assert bridge.handle_plane(module_event("https://github.com/acme/from-plane")) == "module tidied for acme/from-plane"
    assert "name" not in fp.module_patches[-1] and fp.module_name == "Auth"


def test_config_mapping_requires_module(tmp_path, monkeypatch):
    from bridge.config import load_settings
    cfg = tmp_path / "c.yaml"
    cfg.write_text("mappings:\n  - project: API\n    repo: acme/x\n")
    for k in ("PLANE_BASE_URL", "PLANE_PUBLIC_URL", "PLANE_WORKSPACE", "PLANE_API_KEY",
              "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET"):
        monkeypatch.setenv(k, "x")
    with pytest.raises(RuntimeError, match="module"):
        load_settings(str(cfg))
