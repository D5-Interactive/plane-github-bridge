"""The sync rules. Plane owns the task; GitHub owns the code work.

Plane -> GitHub
  * Only work items in a *linked module* go to GitHub. A module is linked by putting
    a github.com repo URL anywhere in its description (or, for admins, by a mapping
    in config.yaml, which wins). Items in unlinked modules, and items in no module at
    all, stay in Plane: that's where admin and organisational work lives.
  * A linked module is tidied: renamed to the repo's name, its description set to
    the repo link plus the repo's GitHub description, its start date set to the
    day the repo was created, and the repo added to its Links section (where this
    Plane's API supports module links).
  * A synced work item gets a GitHub issue, once, and the issue link is added to it.
  * Later edits to the item's title or description are copied to the issue.

GitHub -> Plane: state
  * PR that says "Closes #N" opened / ready for review  -> In Review
  * PR is a draft, converted to draft, or closed unmerged -> In Progress
  * PR merged, or issue closed as completed              -> Done
  * Issue closed as not planned                         -> Cancelled
  * Issue reopened                                      -> Todo

GitHub -> Plane: activity (posted as comments on the work item, each once)
  * PR opened, ready for review, merged, closed, reopened (+ the PR link)
  * PR reviews: approved, changes requested, commented
  * Branches created for the issue (named like "12-scan-command") (+ the branch link)
  * Commits pushed to the issue's branch, or whose message mentions #N
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict

from .clients import GitHubClient, PlaneClient
from .config import Settings
from .render import (branch_issue_number, closing_refs, esc, github_repo_in, issue_body,
                     mention_refs, normalize_plane_event, plain_text, plane_html_to_markdown,
                     repo_description_html)
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
        if kind is None or action == "deleted":
            return "ignored: not a work item or module create/update"
        if not self.store.first_time(payload.get("event_id", "")):
            return "ignored: duplicate delivery"

        data = payload.get("data") or {}
        if kind == "module":
            project_id = data.get("project") or data.get("project_id")
            if not project_id or not data.get("id"):
                return "ignored: payload has no module or project id"
            with self._lock(f"module:{data['id']}"):
                return self.tidy_module_description(project_id, data)

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

    @staticmethod
    def _described_repo(entity: dict) -> str | None:
        return github_repo_in(entity.get("description_html"), entity.get("description"),
                              entity.get("description_text"))

    def _repo_for(self, project_id: str, item: dict) -> str | None:
        """The repo for an item's linked module, or None: the item stays in Plane."""
        project = self.plane.project(project_id)
        # 1. config.yaml module mappings (admin overrides).
        for m in self.s.mappings_for(project["identifier"]):
            mid = self.plane.module_id(project_id, m.module)
            if mid and self.plane.item_in_module(project_id, mid, item):
                return m.repo
        # 2. A github.com URL in the description of one of the item's modules.
        for mod in self.plane.modules(project_id):
            repo = self._described_repo(mod)
            if repo and self.plane.item_in_module(project_id, mod["id"], item):
                try:
                    self.tidy_module_description(project_id, mod)
                except Exception:
                    log.exception("couldn't tidy the description of module %s", mod.get("id"))
                return repo
        # Unlinked module, or no module at all: keep it off GitHub.
        return None

    def tidy_module_description(self, project_id: str, module: dict) -> str:
        """If a module's description mentions a repo, rename the module after the repo,
        add the repo to its Links, and rewrite its description to:

            https://github.com/owner/repo
            <the repo's GitHub description>
        """
        repo = self._described_repo(module)
        if not repo:
            return "ignored: module description has no GitHub repo link"
        if not self.s.owner_allowed(repo):
            return f"ignored: {repo} is not in GITHUB_ALLOWED_OWNERS"
        meta = self.gh.get_repo(repo)
        if meta is None:
            log.warning("module links %s, but the GitHub token can't see that repo", repo)
            return f"ignored: token can't see {repo}"
        url = meta.get("html_url") or f"https://github.com/{repo}"
        wanted = repo_description_html(url, meta.get("description"))
        current = module.get("description_html") or module.get("description") or ""
        changes: dict = {}
        if plain_text(current) != plain_text(wanted):
            changes.update(description_html=wanted, description=plain_text(wanted))
        name = meta.get("name") or repo.split("/", 1)[1]
        if name and (module.get("name") or "") != name:
            changes["name"] = name
        created = (meta.get("created_at") or "")[:10]          # "2026-10-12T..." -> "2026-10-12"
        if created and (module.get("start_date") or "")[:10] != created:
            changes["start_date"] = created

        try:
            link = self.plane.ensure_module_link(project_id, module["id"], url,
                                                 f"GitHub: {meta.get('full_name', repo)}")
        except Exception:
            log.exception("couldn't add the repo link to module %s", module.get("id"))
            link = "failed"

        if not changes:
            return "unchanged: module already tidy" + (" (repo link added)" if link == "added" else "")
        # Plane can refuse a name (already taken in the project) or a start date
        # (after the module's target date). If the combined update fails, send the
        # description on its own and then try each of those separately.
        try:
            self.plane.update_module(project_id, module["id"], **changes)
        except Exception:
            optional = {f: changes.pop(f) for f in ("name", "start_date") if f in changes}
            if not optional:
                raise
            if changes:
                self.plane.update_module(project_id, module["id"], **changes)
            for field, value in optional.items():
                try:
                    self.plane.update_module(project_id, module["id"], **{field: value})
                    changes[field] = value
                except Exception:
                    log.warning("Plane refused module %r's %s=%r; left as is",
                                module.get("name"), field, value)
            if not changes:
                return "unchanged: Plane refused the module update"
        log.info("tidied module %r for %s (%s; link %s)", module.get("name", module["id"]), repo,
                 ", ".join(changes), link)
        return f"module tidied for {meta.get('full_name', repo)}"

    def _maybe_create_issue(self, project_id: str, item_id: str) -> str:
        item = self.plane.work_item(project_id, item_id)
        repo = self._repo_for(project_id, item)
        if not repo:
            return "kept in Plane: item isn't in a module linked to a repo"
        if not self.s.owner_allowed(repo):
            log.warning("refusing %s: owner not in GITHUB_ALLOWED_OWNERS", repo)
            return f"ignored: {repo} is not in GITHUB_ALLOWED_OWNERS"

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
        if event == "pull_request_review":
            return self._on_review(repo, payload)
        if event == "push":
            return self._on_push(repo, payload)
        if event == "create":
            return self._on_create(repo, payload)
        return f"ignored: {event} event"

    # -------- activity comments

    def _comment(self, link: Link, key: str, html: str) -> bool:
        """Post an activity comment on the linked work item, once per key."""
        if not self.store.post_is_new(link.plane_item_id, key):
            return False
        self.plane.add_comment(link.plane_project_id, link.plane_item_id,
                               f"<p>◆ <strong>GitHub</strong> · {html}</p>")
        return True

    def _pr_html(self, repo: str, pr: dict) -> str:
        return (f'PR <a href="{esc(pr.get("html_url"))}">{esc(repo)}#{esc(pr.get("number"))}</a> '
                f'“{esc(pr.get("title"))}”')

    def _on_review(self, repo: str, p: dict) -> str:
        if p.get("action") != "submitted":
            return f"ignored: review {p.get('action')}"
        pr, review = p.get("pull_request") or {}, p.get("review") or {}
        state = (review.get("state") or "").lower()
        verb = {"approved": "✓ approved", "changes_requested": "✕ requested changes on",
                "commented": "commented on"}.get(state)
        if not verb or (state == "commented" and not (review.get("body") or "").strip()):
            return "ignored: empty or unknown review"
        links = self._links_for_pr(repo, pr)
        user = (review.get("user") or {}).get("login", "someone")
        posted = 0
        for link in links:
            posted += self._comment(link, f"review:{review.get('id')}",
                                    f'<code>{esc(user)}</code> {verb} '
                                    f'<a href="{esc(review.get("html_url"))}">{self._pr_html(repo, pr)}</a>')
        return f"review posted to {posted} item(s)" if links else "ignored: PR does not close a linked issue"

    def _on_create(self, repo: str, p: dict) -> str:
        if p.get("ref_type") != "branch":
            return "ignored: not a branch"
        branch = p.get("ref", "")
        num = branch_issue_number(branch)
        link = self.store.by_issue(repo, num) if num else None
        if not link:
            return "ignored: branch not named after a linked issue"
        url = f"https://github.com/{repo}/tree/{branch}"
        user = (p.get("sender") or {}).get("login", "someone")
        if self._comment(link, f"branch:{branch}",
                         f'branch <a href="{esc(url)}"><code>{esc(branch)}</code></a> created by '
                         f'<code>{esc(user)}</code>'):
            self.plane.add_link(link.plane_project_id, link.plane_item_id, url, f"Branch {branch}")
        return f"branch {branch} posted"

    def _on_push(self, repo: str, p: dict) -> str:
        ref = p.get("ref", "")
        if not ref.startswith("refs/heads/") or p.get("deleted"):
            return "ignored: not a branch push"
        branch = ref[len("refs/heads/"):]
        commits = [c for c in p.get("commits") or [] if c.get("distinct", True)]
        if not commits:
            return "ignored: no new commits"

        branch_num = branch_issue_number(branch)
        per_item: dict[str, tuple[Link, list[dict]]] = {}
        for c in commits:
            refs = mention_refs(c.get("message"), repo)
            if branch_num:
                refs.append((repo, branch_num))
            for r, n in refs:
                link = self.store.by_issue(r, n)
                if link:
                    entry = per_item.setdefault(link.plane_item_id, (link, []))
                    if c not in entry[1]:
                        entry[1].append(c)
        if not per_item:
            return "ignored: commits don't touch a linked issue"

        pusher = (p.get("pusher") or {}).get("name") or (p.get("sender") or {}).get("login", "someone")
        posted = 0
        for link, cs in per_item.values():
            new = [c for c in cs if self.store.post_is_new(link.plane_item_id, f"commit:{c.get('id')}")]
            if not new:
                continue
            rows = "".join(
                f'<li><a href="{esc(c.get("url"))}"><code>{esc((c.get("id") or "")[:7])}</code></a> '
                f'{esc((c.get("message") or "").splitlines()[0][:120])}</li>' for c in new[:10])
            more = f"<li>…and {len(new) - 10} more</li>" if len(new) > 10 else ""
            noun = "commit" if len(new) == 1 else "commits"
            self.plane.add_comment(
                link.plane_project_id, link.plane_item_id,
                f"<p>◆ <strong>GitHub</strong> · {len(new)} {noun} pushed to "
                f"<code>{esc(branch)}</code> by <code>{esc(pusher)}</code></p><ul>{rows}{more}</ul>")
            posted += 1
        return f"commits posted to {posted} item(s)"

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

    def _links_for_pr(self, repo: str, pr: dict) -> list[Link]:
        refs = closing_refs(f"{pr.get('title', '')}\n{pr.get('body') or ''}", repo)
        num = branch_issue_number((pr.get("head") or {}).get("ref"))
        if num:
            refs.append((repo, num))
        out: list[Link] = []
        for r, n in refs:
            link = self.store.by_issue(r, n)
            if link and link.plane_item_id not in {l.plane_item_id for l in out}:
                out.append(link)
        return out

    def _on_pull_request(self, repo: str, p: dict) -> str:
        pr, action = p.get("pull_request") or {}, p.get("action")
        links = self._links_for_pr(repo, pr)
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

        user = (p.get("sender") or {}).get("login", "someone")
        what = {
            "opened": "opened as a draft" if pr.get("draft") else "opened",
            "reopened": "reopened",
            "ready_for_review": "marked ready for review",
            "converted_to_draft": "converted to a draft",
            "closed": (f"● merged into <code>{esc((pr.get('base') or {}).get('ref'))}</code>"
                       if pr.get("merged") else "closed without merging"),
        }.get(action)

        results = []
        for link in links:
            if pr.get("html_url") and self.store.pr_link_is_new(link.plane_item_id, pr["html_url"]):
                self.plane.add_link(link.plane_project_id, link.plane_item_id, pr["html_url"],
                                    f"PR {repo}#{pr.get('number')}: {pr.get('title', '')}"[:255])
            if what:
                self._comment(link, f"pr:{pr.get('number')}:{action}:{pr.get('updated_at', '')}",
                              f"{self._pr_html(repo, pr)} {what} by <code>{esc(user)}</code>")
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
