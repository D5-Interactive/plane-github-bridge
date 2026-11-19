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
import re
import threading
import time
from collections import defaultdict

import httpx

from .clients import GitHubClient, PlaneClient, _github_rate_limited, plane_not_found
from .config import Settings
from .github_auth import NoGitHubCredentials
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

# An item GitHub refused for a lasting reason (repo gone, Issues disabled, no access)
# is retried by the catch-up sync only after this long. Editing the item retries it
# at once, through the webhook.
PERMANENT_RETRY_SECONDS = 6 * 3600
# Missing Plane links repaired per catch-up pass, so a backlog can't eat the rate limit.
LINK_REPAIRS_PER_PASS = 10


# GET .../issues/<id>/ : reading one work item (not its links or comments).
_WORK_ITEM_PATH = re.compile(r"/issues/[^/]+/$")


class _StopPass(Exception):
    """End this catch-up pass and let the next one resume.

    kind is "rate" (a rate limit) or "auth" (a key or token was rejected)."""

    def __init__(self, reason: str, kind: str = "rate"):
        super().__init__(reason)
        self.kind = kind


def _failure_kind(e: Exception) -> str:
    """How the catch-up sync treats an error:

    "rate"      a rate limit: stop the pass, the next one resumes
    "auth"      a key or token was rejected (401): stop the pass, log once
    "permanent" GitHub refused this item for a lasting reason: park it
    "gone"      Plane itself says the work item doesn't exist: park it
    "no_credentials" nothing can sign GitHub calls for that repo (no app
                installation, no token): skip the repo for the rest of the pass,
                don't park (it must sync as soon as credentials arrive)
    "transient" anything else: retry next pass
    """
    if isinstance(e, NoGitHubCredentials):
        return "no_credentials"
    if isinstance(e, httpx.HTTPStatusError):
        status, host = e.response.status_code, e.request.url.host
        if status == 429:
            return "rate"
        if status == 401:
            return "auth"
        if (not host.endswith("github.com") and e.request.method == "GET"
                and _WORK_ITEM_PATH.search(e.request.url.path) and plane_not_found(e.response)):
            return "gone"
        if host.endswith("github.com"):
            if _github_rate_limited(e.response):
                return "rate"
            if status in (403, 404, 410, 422):
                return "permanent"
    return "transient"


def _stop_if_pass_ending(e: Exception) -> None:
    """Raise _StopPass for errors that should end the whole pass (rate limit, rejected key)."""
    kind = _failure_kind(e)
    if kind in ("rate", "auth"):
        raise _StopPass(_short_reason(e), kind) from None


def _short_reason(e: Exception) -> str:
    """A log-safe reason: status and endpoint, never response bodies or item content."""
    if isinstance(e, httpx.HTTPStatusError):
        where = "GitHub" if e.request.url.host.endswith("github.com") else "Plane"
        return f"{where} answered {e.response.status_code} for {e.request.method} {e.request.url.path}"
    return type(e).__name__


class Bridge:
    def __init__(self, settings: Settings, store: Store, plane: PlaneClient, github: GitHubClient):
        self.s, self.store, self.plane, self.gh = settings, store, plane, github
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()
        self.clock = time.time

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
                except Exception as e:
                    if _failure_kind(e) in ("rate", "auth"):
                        raise
                    log.warning("couldn't tidy module %s (%s)", mod.get("id"), _short_reason(e))
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
        except Exception as e:
            if _failure_kind(e) in ("rate", "auth"):
                raise       # don't pile more requests onto a rate limit
            log.warning("couldn't add the repo link to module %s (%s)", module.get("id"), _short_reason(e))
            link = "failed"

        if not changes:
            return "unchanged: module already tidy" + (" (repo link added)" if link == "added" else "")
        # Plane can refuse a name (already taken in the project) or a start date
        # (after the module's target date). If the combined update fails, send the
        # description on its own and then try each of those separately.
        try:
            self.plane.update_module(project_id, module["id"], **changes)
        except Exception as e:
            if _failure_kind(e) in ("rate", "auth"):
                raise       # the fallbacks below would only hit the same limit
            optional = {f: changes.pop(f) for f in ("name", "start_date") if f in changes}
            if not optional:
                raise
            if changes:
                self.plane.update_module(project_id, module["id"], **changes)
            for field, value in optional.items():
                try:
                    self.plane.update_module(project_id, module["id"], **{field: value})
                    changes[field] = value
                except Exception as e:
                    if _failure_kind(e) in ("rate", "auth"):
                        raise
                    log.warning("Plane refused module %r's %s=%r; left as is",
                                module.get("name"), field, value)
            if not changes:
                return "unchanged: Plane refused the module update"
        log.info("tidied module %r for %s (%s; link %s)", module.get("name", module["id"]), repo,
                 ", ".join(changes), link)
        return f"module tidied for {meta.get('full_name', repo)}"

    def sync_linked_modules(self) -> str:
        """Catch-up pass: give every item in a linked module an issue if it has none.

        Plane sends no webhook when an existing work item is added to a module, so
        without this, items dragged into a linked module would never reach GitHub.

        Each module's items are listed once per pass and an item's repo is taken from
        that list, so the pass costs about one Plane call per linked module plus a few
        per new item. One item failing doesn't stop the others; a rate limit (429)
        ends the pass early and the next pass carries on.
        """
        created: list[str] = []
        failed = repaired = skipped_projects = 0
        stopped = ""
        logged_repos: set[str] = set()      # parked repos already logged in this pass
        no_credentials: set[str] = set()    # repos nothing can sign for: skipped for this pass
        try:
            repaired = self._repair_plane_links()
            try:
                projects = self.plane.projects()
            except Exception as e:
                _stop_if_pass_ending(e)
                log.warning("sync: couldn't list Plane projects (%s)", _short_reason(e))
                projects = []
            for project in projects:
                pid, ident = project["id"], project.get("identifier", "")
                try:
                    modules = self.plane.modules(pid)
                except Exception as e:
                    _stop_if_pass_ending(e)
                    skipped_projects += 1  # usually: the token's user isn't a member of this project
                    continue
                linked = self._linked_modules(ident, modules)
                if not linked:
                    continue                # nothing linked here: no item listings at all
                c, f = self._sync_project(pid, ident, linked, logged_repos, no_credentials)
                created += c
                failed += f
        except _StopPass as e:
            stopped = e.kind
            if e.kind == "auth":
                log.error("sync: stopped, a key or token was rejected (%s); check GITHUB_TOKEN "
                          "and PLANE_API_KEY. Will try again next pass", e)
            else:
                log.warning("sync: stopped early, will resume next pass (%s)", e)

        parts = []
        if created:
            parts.append(f"created {', '.join(created)}")
        if repaired:
            parts.append(f"repaired {repaired} Plane link(s)")
        if failed:
            parts.append(f"{failed} item(s) failed")
        if stopped == "rate":
            parts.append("stopped early: rate limited")
        elif stopped == "auth":
            parts.append("stopped: credentials rejected")
        if not parts:
            return "nothing new"    # quiet: the caller logs only when something happened
        if skipped_projects:
            parts.append(f"skipped {skipped_projects} project(s) the token can't read")
        return "; ".join(parts)

    def _linked_modules(self, ident: str, modules: list[dict]) -> list[tuple[dict, str, bool]]:
        """(module, repo, pinned) for each linked module; config.yaml pins first, as in _repo_for."""
        pinned = {m.module.strip().lower(): m.repo for m in self.s.mappings_for(ident)}
        out = []
        for mod in modules:
            repo = pinned.get((mod.get("name") or "").strip().lower())
            if repo:
                out.append((mod, repo, True))
        for mod in modules:
            if (mod.get("name") or "").strip().lower() in pinned:
                continue
            repo = self._described_repo(mod)
            if repo:
                out.append((mod, repo, False))
        # A repo outside GITHUB_ALLOWED_OWNERS never gets issues: don't list its items.
        return [t for t in out if self.s.owner_allowed(t[1])]

    def _sync_project(self, pid: str, ident: str, linked: list[tuple[dict, str, bool]],
                      logged_repos: set[str] | None = None,
                      no_credentials: set[str] | None = None) -> tuple[list[str], int]:
        logged_repos = set() if logged_repos is None else logged_repos
        no_credentials = set() if no_credentials is None else no_credentials

        def skip_repo(repo: str, e: Exception) -> None:
            """No credential can reach this repo: skip it for the rest of the pass, one log line."""
            no_credentials.add(repo.lower())
            log.warning("sync: skipping %s for this pass: %s", repo, e)
        # item id -> (module, repo, pinned): the first linked module listing it wins.
        targets: dict[str, tuple[dict, str, bool]] = {}
        for mod, repo, pinned in linked:
            try:
                ids = self.plane.module_item_ids(pid, mod["id"])
            except Exception as e:
                _stop_if_pass_ending(e)
                log.warning("sync: couldn't list module %s in %s (%s)", mod.get("id"), ident, _short_reason(e))
                continue
            for item_id in ids:
                if item_id and item_id not in targets:
                    targets[item_id] = (mod, repo, pinned)

        created, failed, tidied = [], 0, set()
        now = self.clock()
        for item_id, (mod, repo, pinned) in targets.items():
            if self.store.by_plane_item(item_id):
                continue
            past = self.store.failure(item_id)
            if past and past[0].lower() == repo.lower() and now - past[2] < PERMANENT_RETRY_SECONDS:
                continue        # parked for this repo; a different repo means the module was relinked
            if repo.lower() in no_credentials:
                continue        # checked before reading the item: no Plane calls for it this pass
            if not pinned and mod["id"] not in tidied:
                tidied.add(mod["id"])
                try:
                    self.tidy_module_description(pid, mod)
                except Exception as e:
                    _stop_if_pass_ending(e)
                    if _failure_kind(e) == "no_credentials":
                        skip_repo(repo, e)
                        continue
                    log.warning("sync: couldn't tidy module %s (%s)", mod.get("id"), _short_reason(e))
            try:
                with self._lock(item_id):
                    if self.store.by_plane_item(item_id):
                        continue
                    result = self._maybe_create_issue(pid, item_id, repo=repo)
            except Exception as e:
                _stop_if_pass_ending(e)
                kind = _failure_kind(e)
                failed += 1
                reason = _short_reason(e)
                if kind == "no_credentials":
                    skip_repo(repo, e)          # not parked: it syncs once credentials arrive
                elif kind == "gone":
                    # Listed in the module but deleted (or unreadable) in Plane: don't
                    # read it every pass. Editing it, or a restart, tries again.
                    self.store.record_failure(item_id, repo, reason, now)
                    log.warning("sync: %s item %s is listed in a module but Plane says it doesn't "
                                "exist; parked for %dh", ident, item_id, PERMANENT_RETRY_SECONDS // 3600)
                elif kind == "permanent":
                    self.store.record_failure(item_id, repo, reason, now)
                    if repo.lower() not in logged_repos:     # one line per repo per pass
                        logged_repos.add(repo.lower())
                        log.warning("sync: %s refused items from %s (%s); parked for %dh, or until "
                                    "the item is edited or the bridge restarts",
                                    repo, ident, reason, PERMANENT_RETRY_SECONDS // 3600)
                else:
                    log.warning("sync: %s item %s failed, retrying next pass (%s)", ident, item_id, reason)
                continue
            if result.startswith("created"):
                created.append(result.removeprefix("created "))
        return created, failed

    def _repair_plane_links(self) -> int:
        """Add issue links that never reached Plane (the pass died between the two calls)."""
        repaired = 0
        for link in self.store.missing_plane_links(LINK_REPAIRS_PER_PASS):
            self.store.link_attempted(link.plane_item_id, self.clock())
            try:
                self.plane.add_link(link.plane_project_id, link.plane_item_id, link.issue_url,
                                    f"GitHub {link.repo}#{link.issue_number}")
            except Exception as e:
                _stop_if_pass_ending(e)
                status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                if status == 404 and self._confirm_gone(link):
                    # Plane says the work item doesn't exist: stop trying instead of
                    # spending a call on it every pass. A 404 from anything else (a proxy
                    # page while Plane restarts) is temporary, and the row stays queued.
                    self.store.mark_plane_linked(link.plane_item_id)
                    log.warning("sync: work item %s is gone from Plane; not adding the link for %s#%s",
                                link.plane_item_id, link.repo, link.issue_number)
                else:
                    log.warning("sync: couldn't add the link for %s#%s yet (%s)",
                                link.repo, link.issue_number, _short_reason(e))
                continue
            self.store.mark_plane_linked(link.plane_item_id)
            repaired += 1
        return repaired

    def _confirm_gone(self, link: Link) -> bool:
        """After a 404 adding a link: is the work item really deleted? Asks Plane directly."""
        try:
            return self.plane.item_gone(link.plane_project_id, link.plane_item_id)
        except Exception as e:
            _stop_if_pass_ending(e)
            return False

    def _maybe_create_issue(self, project_id: str, item_id: str, repo: str | None = None) -> str:
        """Create the item's issue. repo: already known (catch-up sync), so skip the module lookup."""
        item = self.plane.work_item(project_id, item_id)
        if repo is None:
            repo = self._repo_for(project_id, item)
        if not repo:
            return "kept in Plane: item isn't in a module linked to a repo"
        if not self.s.owner_allowed(repo):
            log.warning("refusing %s: owner not in GITHUB_ALLOWED_OWNERS", repo)
            return f"ignored: {repo} is not in GITHUB_ALLOWED_OWNERS"

        ref, url = self._item_ref(project_id, item)
        body = issue_body(plane_html_to_markdown(item.get("description_html")), ref, url)
        issue = self.gh.create_issue(repo, item["name"], body)
        # Saved before the Plane link: if adding the link fails, the issue isn't made
        # twice, and the catch-up sync adds the missing link later.
        link = Link(item_id, project_id, repo, issue["number"], issue["html_url"], plane_linked=False)
        self.store.save(link)
        self.store.clear_failure(item_id)
        log.info("created %s#%s for %s", repo, issue["number"], ref)
        self.plane.add_link(project_id, item_id, issue["html_url"], f"GitHub {repo}#{issue['number']}")
        self.store.mark_plane_linked(item_id)
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
