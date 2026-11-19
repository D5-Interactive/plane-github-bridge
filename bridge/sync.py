"""The sync rules. Plane owns the task; GitHub owns the code work.

Plane -> GitHub
  * A work item in a mapped project (and module, if the mapping names one)
    gets a GitHub issue, once. The issue link is added to the work item.
  * Later edits to the item's title or description are copied to the issue.

GitHub -> Plane (state only)
  * PR that says "Closes #N" opened / ready for review  -> In Review
  * PR is a draft, converted to draft, or closed unmerged -> In Progress
  * PR merged, or issue closed as completed              -> Done
  * Issue closed as not planned                         -> Cancelled
  * Issue reopened                                      -> Todo
  The PR link is also added to the work item the first time it is seen.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict

from .clients import GitHubClient, PlaneClient
from .config import Settings
from .render import closing_refs, issue_body, normalize_plane_event, plane_html_to_markdown
from .store import Link, Store

log = logging.getLogger("bridge")

# target -> (preferred state names, fallback state group)
TARGETS = {
    "todo": (("todo", "to do"), "unstarted"),
    "progress": (("in progress",), "started"),
    "review": (("in review", "review"), "started"),
    "done": (("done",), "completed"),
    "cancelled": (("cancelled", "canceled"), "cancelled"),
}


class Bridge:
    def __init__(self, settings: Settings, store: Store, plane: PlaneClient, github: GitHubClient):
        self.s, self.store, self.plane, self.gh = settings, store, plane, github
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[key]

    # ------------------------------------------------------------ Plane -> GitHub

    def handle_plane(self, payload: dict) -> str:
        kind, action = normalize_plane_event(payload)
        if kind != "workitem" or action == "deleted":
            return "ignored: not a work item create/update"
        if not self.store.first_time(payload.get("event_id", "")):
            return "ignored: duplicate delivery"

        data = payload.get("data") or {}
        item_id = data.get("id") or payload.get("entity_id")
        project_id = data.get("project") or data.get("project_id")
        if not item_id or not project_id:
            return "ignored: payload has no work item or project id"

        with self._lock(item_id):
            link = self.store.by_plane_item(item_id)
            if link:
                return self._push_edits(link, payload)
            return self._maybe_create_issue(project_id, item_id)

    def _changed_fields(self, payload: dict) -> set[str]:
        prev = payload.get("previous_attributes")
        if isinstance(prev, dict) and prev:
            return set(prev)
        activity = payload.get("activity") or {}
        return {activity["field"]} if activity.get("field") else set()

    def _push_edits(self, link: Link, payload: dict) -> str:
        changed = self._changed_fields(payload) & {"name", "description_html", "description"}
        if not changed:
            return "ignored: linked item, no title/description change"
        item = self.plane.work_item(link.plane_project_id, link.plane_item_id)
        ref, url = self._item_ref(link.plane_project_id, item)
        self.gh.update_issue(link.repo, link.issue_number, title=item["name"],
                             body=issue_body(plane_html_to_markdown(item.get("description_html")), ref, url))
        return f"updated {link.repo}#{link.issue_number}"

    def _item_ref(self, project_id: str, item: dict) -> tuple[str, str]:
        ident = self.plane.project(project_id)["identifier"]
        ref = f"{ident}-{item['sequence_id']}"
        return ref, f"{self.s.plane_public_url}/{self.s.plane_workspace}/browse/{ref}/"

    def _repo_for(self, project_id: str, item: dict) -> str | None:
        ident = self.plane.project(project_id)["identifier"]
        mappings = self.s.mappings_for(ident)
        # A module-specific mapping wins over a project-wide one.
        for m in (m for m in mappings if m.module):
            mid = self.plane.module_id(project_id, m.module)
            if mid and self.plane.item_in_module(project_id, mid, item):
                return m.repo
        for m in (m for m in mappings if not m.module):
            return m.repo
        return None

    def _maybe_create_issue(self, project_id: str, item_id: str) -> str:
        ident = self.plane.project(project_id)["identifier"]
        if not self.s.mappings_for(ident):
            return f"ignored: project {ident} has no mapping"
        item = self.plane.work_item(project_id, item_id)
        repo = self._repo_for(project_id, item)
        if not repo:
            return "ignored: item is not in a mapped module (yet)"

        ref, url = self._item_ref(project_id, item)
        body = issue_body(plane_html_to_markdown(item.get("description_html")), ref, url)
        issue = self.gh.create_issue(repo, item["name"], body)
        self.store.save(Link(item_id, project_id, repo, issue["number"], issue["html_url"]))
        self.plane.add_link(project_id, item_id, issue["html_url"], f"GitHub {repo}#{issue['number']}")
        log.info("created %s#%s for %s", repo, issue["number"], ref)
        return f"created {repo}#{issue['number']}"

    # ------------------------------------------------------------ GitHub -> Plane

    def handle_github(self, event: str, payload: dict) -> str:
        repo = (payload.get("repository") or {}).get("full_name", "")
        if event == "issues":
            return self._on_issue(repo, payload)
        if event == "pull_request":
            return self._on_pull_request(repo, payload)
        return f"ignored: {event} event"

    def _on_issue(self, repo: str, p: dict) -> str:
        issue, action = p.get("issue") or {}, p.get("action")
        link = self.store.by_issue(repo, issue.get("number", -1))
        if not link:
            return "ignored: issue not linked to Plane"
        if action == "closed":
            target = "cancelled" if issue.get("state_reason") == "not_planned" else "done"
        elif action == "reopened":
            target = "todo"
        else:
            return f"ignored: issue {action}"
        return self._move(link, target)

    def _on_pull_request(self, repo: str, p: dict) -> str:
        pr, action = p.get("pull_request") or {}, p.get("action")
        refs = closing_refs(f"{pr.get('title', '')}\n{pr.get('body') or ''}", repo)
        links = [l for l in (self.store.by_issue(r, n) for r, n in refs) if l]
        if not links:
            return "ignored: PR does not close a linked issue"

        if action == "closed":
            target = "done" if pr.get("merged") else "progress"
        elif action in ("opened", "reopened", "ready_for_review", "edited", "synchronize"):
            if pr.get("state") != "open":
                return "ignored: PR not open"
            target = "progress" if pr.get("draft") else "review"
        elif action == "converted_to_draft":
            target = "progress"
        else:
            return f"ignored: pull_request {action}"

        results = []
        for link in links:
            if pr.get("html_url") and self.store.pr_link_is_new(link.plane_item_id, pr["html_url"]):
                self.plane.add_link(link.plane_project_id, link.plane_item_id, pr["html_url"],
                                    f"PR {repo}#{pr.get('number')}: {pr.get('title', '')}"[:255])
            results.append(self._move(link, target))
        return "; ".join(results)

    def _state_id(self, project_id: str, target: str) -> str | None:
        names, group = TARGETS[target]
        states = self.plane.states(project_id)
        for st in states:
            if st.get("name", "").strip().lower() in names:
                return st["id"]
        for st in states:
            if st.get("group") == group:
                return st["id"]
        return None

    def _move(self, link: Link, target: str) -> str:
        state_id = self._state_id(link.plane_project_id, target)
        if not state_id:
            return f"skipped: project has no state for '{target}'"
        item = self.plane.work_item(link.plane_project_id, link.plane_item_id)
        current = item.get("state") or item.get("state_id")
        if current == state_id:
            return f"unchanged: already {target}"
        self.plane.set_state(link.plane_project_id, link.plane_item_id, state_id)
        log.info("moved %s#%s's work item to %s", link.repo, link.issue_number, target)
        return f"moved to {target}"
