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
import os
import re
import threading
import time
from collections import defaultdict

import httpx

from . import guards
from .clients import (LANE_BACKGROUND, GitHubClient, PlaneClient, _github_rate_limited, lane,
                      plane_not_found)
from .config import Settings
from .diagnostics import Diagnostics, fingerprint
from .queue import RetryQueue, run_due
from .reconcile import CLOSED_GROUPS, Reconciler, get_snapshot, record_snapshot
from .scheduler import Debouncer, ScanScheduler
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
    "transient" anything else: retry next pass
    """
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
        now = lambda: self.clock()          # noqa: E731  (tests swap self.clock)
        # Failed actions wait here and are retried within minutes (see run_queue).
        self.queue = RetryQueue(store, clock=now, max_age=getattr(settings, "queue_max_age", 300),
                                on_stale=self._on_stale)
        # Sync problems: recorded here, flagged with a label on both sides.
        self.diag = Diagnostics(store, plane, github, public_url=getattr(settings, "public_url", ""),
                                clock=now)
        # Module/repo checks: one per burst of edits, plus the self-scheduling org scan.
        self.reconciler = Reconciler(plane, github, store, apply_fix=self._apply_fix,
                                     report_problem=self._report_problem, clock=now)
        self.debounce = Debouncer(getattr(settings, "reconcile_delay", 30), clock=now)
        self.scheduler = ScanScheduler(clock=now, tz=getattr(settings, "timezone", "UTC"),
                                       quiet_hours=getattr(settings, "quiet_hours", (1, 5)))
        self.handlers = {
            "create_issue": self._act_create_issue,
            "push_edit": self._act_push_edit,
            "move": self._act_move,
            "comment": self._act_comment,
            "add_link": self._act_add_link,
            "tidy_module": self._act_tidy_module,
            # reconcile fixes
            "add_plane_link": self._act_add_plane_link,
            "push_title_to_github": self._act_title_to_github,
            "push_title_to_plane": self._act_title_to_plane,
            "push_state_to_github": self._act_state_to_github,
            "push_state_to_plane": self._act_state_to_plane,
        }

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[key]

    # ------------------------------------------------------------ Plane -> GitHub

    def handle_plane(self, payload: dict) -> str:
        kind, action = normalize_plane_event(payload)
        if kind is None or action == "deleted":
            return "ignored: not a work item or module create/update"
        event_id = payload.get("event_id", "")
        if self.store.event_seen(event_id):
            return "ignored: duplicate delivery"
        result = self._handle_plane(kind, payload)
        # Marked seen only now: its actions have either run or been queued.
        self.store.mark_seen(event_id)
        return result

    def _handle_plane(self, kind: str, payload: dict) -> str:
        data = payload.get("data") or {}
        if kind == "module":
            project_id = data.get("project") or data.get("project_id")
            if not project_id or not data.get("id"):
                return "ignored: payload has no module or project id"
            # Editing a module also checks the module against its repo (debounced).
            self.debounce.touch(f"module:{project_id}:{data['id']}")
            with self._lock(f"module:{data['id']}"):
                return self._do("tidy_module", f"module:{data['id']}",
                                {"project_id": project_id, "module": data})

        item_id = data.get("id") or payload.get("entity_id")
        project_id = data.get("project") or data.get("project_id")
        if not item_id or not project_id:
            return "ignored: payload has no work item or project id"

        with self._lock(item_id):
            link = self.store.by_plane_item(item_id)
            if link:
                changed = self._changed_fields(payload) & {"name", "description_html", "description"}
                if not changed:
                    return "ignored: linked item, no title/description change"
                return self._do("push_edit", item_id, {"project_id": project_id, "item_id": item_id,
                                                        "repo": link.repo, "issue_number": link.issue_number,
                                                        "only_name": changed == {"name"}})
            return self._do("create_issue", item_id, {"project_id": project_id, "item_id": item_id})

    def _changed_fields(self, payload: dict) -> set[str]:
        prev = payload.get("previous_attributes")
        if isinstance(prev, dict) and prev:
            return set(prev)
        activity = payload.get("activity") or {}
        return {activity["field"]} if activity.get("field") else set()

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
                c, f = self._sync_project(pid, ident, linked, logged_repos)
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
                      logged_repos: set[str] | None = None) -> tuple[list[str], int]:
        logged_repos = set() if logged_repos is None else logged_repos
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
            queued = self.queue.get("create_issue", item_id)
            if queued and queued.next_at > now:
                continue        # waiting in the retry queue, which will run it when it's due
            past = self.store.failure(item_id)
            if past and past[0].lower() == repo.lower() and now - past[2] < PERMANENT_RETRY_SECONDS:
                continue        # parked for this repo; a different repo means the module was relinked
            if not pinned and mod["id"] not in tidied:
                tidied.add(mod["id"])
                try:
                    self.tidy_module_description(pid, mod)
                except Exception as e:
                    _stop_if_pass_ending(e)
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
                if kind == "gone":
                    # Listed in the module but deleted (or unreadable) in Plane: don't
                    # read it every pass. Editing it, or a restart, tries again.
                    self.store.record_failure(item_id, repo, reason, now)
                    log.warning("sync: %s item %s is listed in a module but Plane says it doesn't "
                                "exist; parked for %dh", ident, item_id, PERMANENT_RETRY_SECONDS // 3600)
                elif kind == "permanent":
                    self.store.record_failure(item_id, repo, reason, now)
                    self.diag.report("refused", plane_project_id=pid, plane_item_id=item_id, repo=repo,
                                     detail={"action": "create_issue", "reason": reason},
                                     key="create_issue")
                    if repo.lower() not in logged_repos:     # one line per repo per pass
                        logged_repos.add(repo.lower())
                        log.warning("sync: %s refused items from %s (%s); parked for %dh, or until "
                                    "the item is edited or the bridge restarts",
                                    repo, ident, reason, PERMANENT_RETRY_SECONDS // 3600)
                else:
                    self.queue.put("create_issue", item_id,
                                   {"project_id": pid, "item_id": item_id, "repo": repo,
                                    "_retry": True, "_key": item_id}, reason)
                    log.warning("sync: %s item %s failed, queued for retry (%s)", ident, item_id, reason)
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

    def _maybe_create_issue(self, project_id: str, item_id: str, repo: str | None = None,
                            check_similar: bool = False) -> str:
        """Create the item's issue. repo: already known (catch-up sync), so skip the module lookup.

        check_similar: this is a retry, and an earlier attempt may have created the issue
        before its response was lost; look for it among the repo's newest issues first.
        """
        item = self.plane.work_item(project_id, item_id)
        if repo is None:
            repo = self._repo_for(project_id, item)
        if not repo:
            return "kept in Plane: item isn't in a module linked to a repo"
        if not self.s.owner_allowed(repo):
            log.warning("refusing %s: owner not in GITHUB_ALLOWED_OWNERS", repo)
            return f"ignored: {repo} is not in GITHUB_ALLOWED_OWNERS"

        ref, url = self._item_ref(project_id, item)
        issue, adopted = None, False
        if check_similar:
            found = guards.find_similar_issue(self.gh.recent_issues(repo), item.get("name", ""), ref)
            if found and not self.store.by_issue(repo, found["number"]):
                issue, adopted = found, True
                log.info("found %s#%s from an earlier attempt for %s; linking it instead of "
                         "creating another", repo, found["number"], ref)
        if issue is None:
            body = issue_body(plane_html_to_markdown(item.get("description_html")), ref, url)
            issue = self.gh.create_issue(repo, item["name"], body)
        # Saved before the Plane link: if adding the link fails, the issue isn't made
        # twice, and the link is added later (retry queue or catch-up repair).
        link = Link(item_id, project_id, repo, issue["number"], issue["html_url"], plane_linked=False)
        self.store.save(link)
        self.store.clear_failure(item_id)
        self.diag.resolve(kind="refused", plane_item_id=item_id)
        record_snapshot(self.store, item_id, repo, issue["number"], item.get("name", ""),
                        self._item_closed(project_id, item), synced_at=self.clock())
        if not adopted:
            log.info("created %s#%s for %s", repo, issue["number"], ref)
        self._add_issue_link(link)
        return f"{'linked' if adopted else 'created'} {repo}#{issue['number']}"

    def _add_issue_link(self, link: Link) -> None:
        self.plane.add_link(link.plane_project_id, link.plane_item_id, link.issue_url,
                            f"GitHub {link.repo}#{link.issue_number}")
        self.store.mark_plane_linked(link.plane_item_id)

    def _item_closed(self, project_id: str, item: dict) -> bool:
        state = item.get("state") or item.get("state_id")
        if isinstance(state, dict):
            return state.get("group") in CLOSED_GROUPS
        try:
            groups = {st["id"]: st.get("group") for st in self.plane.states(project_id)}
        except Exception as e:
            _stop_if_pass_ending(e)
            return False
        return groups.get(state) in CLOSED_GROUPS

    def _snap(self, link: Link, *, title: str | None = None, closed: bool | None = None) -> None:
        """Remember what was last synced for a pair, so a later check can tell which side
        changed. A pair with no snapshot yet only gets one when both values are known."""
        snap = get_snapshot(self.store, link.plane_item_id)
        if snap is None and (title is None or closed is None):
            return
        record_snapshot(self.store, link.plane_item_id, link.repo, link.issue_number,
                        title if title is not None else snap.title,
                        closed if closed is not None else snap.closed,
                        snap.state_group if snap else "", synced_at=self.clock())

    # ------------------------------------------------------------ actions
    #
    # Every write to Plane or GitHub is an action: it runs at once, and if it fails for
    # a reason that may pass (rate limit, outage, timeout) it goes into the retry queue
    # instead of being lost. A refusal that won't pass (GitHub 403/404/410/422, an item
    # Plane says is gone) is recorded as a sync problem instead. Handlers don't take
    # locks themselves: callers hold the lock from _lock_key.

    @staticmethod
    def _lock_key(kind: str, payload: dict) -> str:
        item = payload.get("item_id", "")
        if kind == "comment":
            return f"comment:{item}"
        if kind == "add_link":
            return f"link:{item}"
        if kind == "tidy_module":
            return f"module:{(payload.get('module') or {}).get('id', '')}"
        return item

    def _do(self, kind: str, key: str, payload: dict) -> str:
        try:
            return self.handlers[kind](payload)
        except Exception as e:
            return self._action_failed(kind, key, payload, e)

    def _action_failed(self, kind: str, key: str, payload: dict, e: Exception) -> str:
        fk, reason = _failure_kind(e), _short_reason(e)
        if fk in ("permanent", "gone"):
            self._refused(kind, payload, fk, reason)
            return f"refused: {reason}"
        self.queue.put(kind, key, {**payload, "_retry": True, "_key": key}, reason)
        log.warning("%s for %s failed (%s); queued for retry", kind, key, reason)
        return f"queued: {reason}"

    def _refused(self, kind: str, payload: dict, fk: str, reason: str) -> None:
        item_id, repo = payload.get("item_id"), payload.get("repo")
        if kind == "create_issue" and item_id:
            # Parked: the catch-up sync won't try it again for a while (see PERMANENT_RETRY_SECONDS).
            self.store.record_failure(item_id, repo or "", reason, self.clock())
        if fk == "permanent":
            self.diag.report("refused", plane_project_id=payload.get("project_id"),
                             plane_item_id=item_id, repo=repo, issue_number=payload.get("issue_number"),
                             detail={"action": kind, "reason": reason}, key=kind)
        log.warning("%s for %s was refused (%s); not retrying", kind, item_id or payload.get("_key"), reason)

    @staticmethod
    def _stuck_where(kind: str, key: str, p: dict) -> dict:
        return dict(plane_project_id=p.get("project_id"), plane_item_id=p.get("item_id"),
                    repo=p.get("repo"), issue_number=p.get("issue_number"), key=f"{kind}:{key}")

    def _stuck_fp(self, kind: str, key: str, p: dict) -> str:
        w = self._stuck_where(kind, key, p)
        return fingerprint("stuck", w["plane_project_id"], w["plane_item_id"], w["repo"],
                           w["issue_number"], w["key"])

    def _on_stale(self, entry) -> None:
        """A queued action has waited longer than BRIDGE_QUEUE_MAX_AGE: flag it."""
        self.diag.report("stuck", **self._stuck_where(entry.kind, entry.key, entry.payload),
                         detail={"action": entry.kind, "attempts": entry.attempts, "reason": entry.reason})

    def run_queue(self, limit: int = 20) -> dict:
        """Retry due queued actions. Each handler re-reads the current state first (guards),
        so an action that took effect before its response was lost isn't done twice."""
        def wrap(kind):
            def run(payload: dict) -> None:
                with self._lock(self._lock_key(kind, payload)):
                    try:
                        self.handlers[kind](payload)
                    except Exception as e:
                        fk = _failure_kind(e)
                        if fk not in ("permanent", "gone"):
                            raise
                        self._refused(kind, payload, fk, _short_reason(e))
                self.diag.resolve(self._stuck_fp(kind, payload.get("_key", ""), payload))
                log.info("retried %s: done", kind)
            return run
        return run_due(self.queue, {k: wrap(k) for k in self.handlers}, limit=limit,
                       stop=lambda e: _failure_kind(e) in ("rate", "auth"))

    # -------- action handlers (payload -> result)

    def _act_create_issue(self, p: dict) -> str:
        link = guards.issue_already_created(self.store, p["item_id"])
        if link:
            if not link.plane_linked:
                self._add_issue_link(link)
                return f"linked {link.repo}#{link.issue_number}"
            return f"unchanged: already {link.repo}#{link.issue_number}"
        return self._maybe_create_issue(p["project_id"], p["item_id"], repo=p.get("repo"),
                                        check_similar=bool(p.get("_retry")))

    def _act_push_edit(self, p: dict) -> str:
        link = self.store.by_plane_item(p["item_id"])
        if not link:
            return "ignored: item has no issue"
        item = self.plane.work_item(link.plane_project_id, link.plane_item_id)
        snap = get_snapshot(self.store, link.plane_item_id)
        if p.get("only_name") and snap and snap.title == item.get("name"):
            return "unchanged: title already synced"     # our own push coming back
        ref, url = self._item_ref(link.plane_project_id, item)
        self.gh.update_issue(link.repo, link.issue_number, title=item["name"],
                             body=issue_body(plane_html_to_markdown(item.get("description_html")), ref, url))
        self._snap(link, title=item["name"])
        self.diag.resolve(kind="title_differs", plane_item_id=link.plane_item_id)
        return f"updated {link.repo}#{link.issue_number}"

    def _act_move(self, p: dict) -> str:
        link = self.store.by_plane_item(p["item_id"])
        if not link:
            return "ignored: item has no issue"
        result = self._move(link, p["target"])
        if not result.startswith("skipped"):
            self._snap(link, closed=TARGETS[p["target"]][1] in CLOSED_GROUPS)
        return result

    def _act_comment(self, p: dict) -> str:
        keys = p.get("keys") or []
        if keys and all(guards.comment_already_posted(self.store, p["item_id"], k) for k in keys):
            return "unchanged: already posted"
        self.plane.add_comment(p["project_id"], p["item_id"], p["html"])
        for k in keys:
            self.store.post_is_new(p["item_id"], k)      # recorded only once it's posted
        return "posted"

    def _act_add_link(self, p: dict) -> str:
        pid, item_id = p["project_id"], p["item_id"]
        if p.get("_retry") and guards.link_already_present(self.plane.item_link_urls(pid, item_id), p["url"]):
            result = "unchanged: link already there"
        else:
            self.plane.add_link(pid, item_id, p["url"], p["title"])
            result = "linked"
        if p.get("pr_url"):
            self.store.pr_link_is_new(item_id, p["pr_url"])
        return result

    def _act_tidy_module(self, p: dict) -> str:
        module = p["module"]
        if p.get("_retry"):         # the module may have been edited since: use the current one
            module = next((m for m in self.plane.modules(p["project_id"]) if m["id"] == module["id"]), None)
            if module is None:
                return "ignored: module is gone"
        return self.tidy_module_description(p["project_id"], module)

    # -------- reconcile fixes (see reconcile.py)

    def _linked(self, p: dict) -> Link | None:
        return self.store.by_plane_item(p["item_id"])

    def _act_add_plane_link(self, p: dict) -> str:
        link = self._linked(p)
        if not link:
            return "ignored: item has no issue"
        if link.plane_linked:
            return "unchanged: link already there"
        self._add_issue_link(link)
        return f"linked {link.repo}#{link.issue_number}"

    def _act_title_to_github(self, p: dict) -> str:
        link = self._linked(p)
        if not link:
            return "ignored: item has no issue"
        self.gh.update_issue(link.repo, link.issue_number, title=p["title"])
        self._snap(link, title=p["title"])
        self.diag.resolve(kind="title_differs", plane_item_id=link.plane_item_id)
        return f"title pushed to {link.repo}#{link.issue_number}"

    def _act_title_to_plane(self, p: dict) -> str:
        link = self._linked(p)
        if not link:
            return "ignored: item has no issue"
        self.plane.update_work_item(link.plane_project_id, link.plane_item_id, name=p["title"])
        self._snap(link, title=p["title"])
        self.diag.resolve(kind="title_differs", plane_item_id=link.plane_item_id)
        return "title pushed to Plane"

    def _act_state_to_github(self, p: dict) -> str:
        link = self._linked(p)
        if not link:
            return "ignored: item has no issue"
        fields: dict = {"state": "closed" if p["closed"] else "open"}
        if p["closed"]:
            # Closed as cancelled in Plane: say so, so GitHub's webhook doesn't move it to Done.
            item = self.plane.work_item(link.plane_project_id, link.plane_item_id)
            state = item.get("state") or item.get("state_id")
            groups = {st["id"]: st.get("group") for st in self.plane.states(link.plane_project_id)}
            fields["state_reason"] = "not_planned" if groups.get(state) == "cancelled" else "completed"
        self.gh.update_issue(link.repo, link.issue_number, **fields)
        self._snap(link, closed=p["closed"])
        return f"{fields['state']} {link.repo}#{link.issue_number}"

    def _act_state_to_plane(self, p: dict) -> str:
        if p["closed"]:
            target = "cancelled" if p.get("state_reason") == "not_planned" else "done"
        else:
            target = "todo"
        return self._act_move({**p, "target": target})

    # ------------------------------------------------------------ checks (reconcile)

    def _apply_fix(self, kind: str, key: str, payload: dict) -> None:
        with self._lock(self._lock_key(kind, payload)):
            result = self._do(kind, key, payload)
        log.info("reconcile fix %s for %s: %s", kind, key, result)

    def _report_problem(self, problem: dict) -> None:
        kind = problem.get("kind", "problem")
        where = dict(plane_project_id=problem.get("project_id"), plane_item_id=problem.get("item_id"),
                     repo=problem.get("repo"), issue_number=problem.get("issue_number"))
        if kind == "conflict":
            # Settled by "most recent change wins": kept for /status, no label.
            field = problem.get("field", "")
            detail = {"field": field, "winner": problem.get("winner"),
                      "plane_updated": problem.get("plane_updated"),
                      "github_updated": problem.get("github_updated")}
            if field == "closed":       # booleans, not work item text
                detail.update(plane_value=problem.get("plane_value"), github_value=problem.get("github_value"))
            self.diag.note("conflict", **where, detail=detail, key=field)
        else:
            self.diag.report(kind, **where, detail={"field": problem.get("field", "")} if problem.get("field") else {})

    def _note_unlinked(self, repo: str, issues: list[dict]) -> None:
        """Issues in a linked repo with no Plane item: one repo-level problem, no label."""
        if issues:
            self.diag.report("unlinked_issues", repo=repo,
                             detail={"count": len(issues), "numbers": [i["issue_number"] for i in issues[:20]]})
        else:
            self.diag.resolve(kind="unlinked_issues", repo=repo)

    def _reconcile(self, project_id: str, module: dict, repo: str):
        report = self.reconciler.reconcile_pair(project_id, module, repo)
        self._note_unlinked(repo, report.unlinked_issues)
        if report.fixed or report.conflicts:
            log.info("checked %s: %d item(s), %d fix(es), %d conflict(s)", repo, report.checked,
                     len(report.fixed), len(report.conflicts))
        return report

    def run_reconciles(self) -> list[str]:
        """Check the modules and repos that were edited (once the burst of edits is over)."""
        done = []
        keys = self.debounce.due()
        for i, key in enumerate(keys):
            try:
                with lane(LANE_BACKGROUND):
                    done += self._reconcile_key(key)
            except Exception as e:
                if _failure_kind(e) in ("rate", "auth"):
                    for later in keys[i:]:          # this one and the rest: after the delay
                        self.debounce.touch(later)
                    log.warning("checks postponed (%s)", _short_reason(e))
                    break
                log.warning("check of %s failed (%s)", key.split(":", 1)[0], _short_reason(e))
        return done

    def _reconcile_key(self, key: str) -> list[str]:
        out = []
        if key.startswith("module:"):
            _, pid, mid = key.split(":", 2)
            project = self.plane.project(pid)
            mod = next((m for m in self.plane.modules(pid) if m["id"] == mid), None)
            if mod is not None:
                for m, repo, _ in self._linked_modules(project.get("identifier", ""), [mod]):
                    self._reconcile(pid, m, repo)
                    out.append(repo)
        elif key.startswith("repo:"):
            want = key[len("repo:"):].lower()
            for project in self.plane.projects():
                try:
                    modules = self.plane.modules(project["id"])
                except Exception as e:
                    _stop_if_pass_ending(e)
                    continue
                for m, repo, _ in self._linked_modules(project.get("identifier", ""), modules):
                    if repo.lower() == want:
                        self._reconcile(project["id"], m, repo)
                        out.append(repo)
        return out

    def run_org_scan(self) -> str:
        """Check every linked module against its repo, measure the cost, and let the
        scheduler pick when to run next (light: often; heavy: set times; slow: at night)."""
        t0, cpu0 = time.monotonic(), time.process_time()
        calls = pairs = checked = fixed = conflicts = 0
        stopped = ""
        with lane(LANE_BACKGROUND):
            try:
                try:
                    projects = self.plane.projects()
                except Exception as e:
                    _stop_if_pass_ending(e)
                    raise
                calls += 1
                for project in projects:
                    pid = project["id"]
                    try:
                        modules = self.plane.modules(pid)
                    except Exception as e:
                        _stop_if_pass_ending(e)
                        continue
                    calls += 1
                    for mod, repo, _ in self._linked_modules(project.get("identifier", ""), modules):
                        try:
                            report = self._reconcile(pid, mod, repo)
                        except Exception as e:
                            _stop_if_pass_ending(e)
                            log.warning("scan: couldn't check %s (%s)", repo, _short_reason(e))
                            continue
                        pairs += 1
                        calls += report.calls
                        checked += report.checked
                        fixed += len(report.fixed)
                        conflicts += len(report.conflicts)
            except _StopPass as e:
                stopped = e.kind
            except Exception as e:
                stopped = "error"
                log.warning("scan: couldn't list Plane projects (%s)", _short_reason(e))
        duration, cpu = time.monotonic() - t0, time.process_time() - cpu0
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = 0.0
        if stopped:
            self.scheduler.next_run = self.clock() + 15 * 60     # try again soon
            summary = f"stopped early ({stopped}) after {pairs} pair(s)"
        else:
            self.scheduler.record(duration, calls, cpu, load)
            summary = (f"{pairs} pair(s), {checked} item(s), {fixed} fix(es), {conflicts} conflict(s); "
                       f"{duration:.0f}s, {calls} calls; next as {self.scheduler.last_class}")
        log.info("org scan: %s", summary)
        return summary

    # ------------------------------------------------------------ GitHub -> Plane

    def handle_github(self, event: str, payload: dict, delivery: str = "") -> str:
        seen_id = f"github:{delivery}" if delivery else ""
        if self.store.event_seen(seen_id):
            return "ignored: duplicate delivery"
        result = self._handle_github(event, payload)
        # Marked seen only now: its actions have either run or been queued.
        self.store.mark_seen(seen_id)
        return result

    def _handle_github(self, event: str, payload: dict) -> str:
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
        if event == "repository":
            return self._on_repository(payload)
        return f"ignored: {event} event"

    def _on_repository(self, p: dict) -> str:
        """A repo was edited, renamed or transferred: check it against its module(s)
        once the edits settle. A rename is checked under both names."""
        if p.get("action") not in ("edited", "renamed", "transferred"):
            return f"ignored: repository {p.get('action')}"
        repo = (p.get("repository") or {}).get("full_name", "")
        if not repo:
            return "ignored: no repository name"
        keys = {repo}
        old_name = (((p.get("changes") or {}).get("repository") or {}).get("name") or {}).get("from")
        if old_name and "/" in repo:
            keys.add(f"{repo.split('/', 1)[0]}/{old_name}")
        for k in keys:
            self.debounce.touch(f"repo:{k}")
        return f"check of {repo} scheduled"

    # -------- activity comments

    def _post(self, link: Link, keys: list[str], html: str) -> str | None:
        """Post an activity comment on the linked work item, once per key. Returns the
        action's result, or None if it was already posted."""
        payload = {"project_id": link.plane_project_id, "item_id": link.plane_item_id,
                   "repo": link.repo, "issue_number": link.issue_number, "keys": keys, "html": html}
        with self._lock(self._lock_key("comment", payload)):
            if all(guards.comment_already_posted(self.store, link.plane_item_id, k) for k in keys):
                return None
            return self._do("comment", f"{link.plane_item_id}:{keys[0]}", payload)

    def _comment(self, link: Link, key: str, html: str) -> bool:
        return self._post(link, [key], f"<p>◆ <strong>GitHub</strong> · {html}</p>") is not None

    def _link(self, link: Link, url: str, title: str, pr_url: str = "") -> str:
        payload = {"project_id": link.plane_project_id, "item_id": link.plane_item_id,
                   "repo": link.repo, "issue_number": link.issue_number,
                   "url": url, "title": title, "pr_url": pr_url}
        with self._lock(self._lock_key("add_link", payload)):
            return self._do("add_link", f"{link.plane_item_id}:{url}", payload)

    def _move_item(self, link: Link, target: str) -> str:
        with self._lock(link.plane_item_id):
            return self._do("move", link.plane_item_id,
                            {"project_id": link.plane_project_id, "item_id": link.plane_item_id,
                             "repo": link.repo, "issue_number": link.issue_number, "target": target})

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
            self._link(link, url, f"Branch {branch}")
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
            new = [c for c in cs if not guards.comment_already_posted(
                self.store, link.plane_item_id, f"commit:{c.get('id')}")]
            if not new:
                continue
            rows = "".join(
                f'<li><a href="{esc(c.get("url"))}"><code>{esc((c.get("id") or "")[:7])}</code></a> '
                f'{esc((c.get("message") or "").splitlines()[0][:120])}</li>' for c in new[:10])
            more = f"<li>…and {len(new) - 10} more</li>" if len(new) > 10 else ""
            noun = "commit" if len(new) == 1 else "commits"
            if self._post(link, [f"commit:{c.get('id')}" for c in new],
                          f"<p>◆ <strong>GitHub</strong> · {len(new)} {noun} pushed to "
                          f"<code>{esc(branch)}</code> by <code>{esc(pusher)}</code></p>"
                          f"<ul>{rows}{more}</ul>") is not None:
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
        return self._move_item(link, target)

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
            if pr.get("html_url") and not self.store.pr_link_known(link.plane_item_id, pr["html_url"]):
                self._link(link, pr["html_url"],
                           f"PR {repo}#{pr.get('number')}: {pr.get('title', '')}"[:255], pr_url=pr["html_url"])
            if what:
                self._comment(link, f"pr:{pr.get('number')}:{action}:{pr.get('updated_at', '')}",
                              f"{self._pr_html(repo, pr)} {what} by <code>{esc(user)}</code>")
            results.append(self._move_item(link, target))
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
