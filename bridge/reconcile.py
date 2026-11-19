"""Check a linked module against its repo and decide what to fix.

The engine only reads. When a pair needs a change it hands `apply_fix(kind, key,
payload)` a "sync_item" action (or "create_issue" / "add_plane_link"); that action
re-reads both sides when it runs and decides again with `decide()`, so a queued fix
never applies a stale decision. Problems go to `report_problem(dict)`, and problems
that are gone to `resolve_problem(kind, item_id)`.

Snapshots. After every successful sync of a pair the caller records both sides as
they actually are (title and open/closed on each side, plus each side's updated_at
right after the bridge's own write). A side whose current value differs from its
own snapshot value changed since then:

▸ One side changed                → push that side's value to the other.
▸ Both changed and now differ     → open/closed: the side whose change time is
                                    known and later wins (GitHub closed_at, Plane
                                    completed_at); titles, or no reliable time:
                                    reported, nothing written.
▸ Neither changed, sides differ   → a divergence already known (and reported if it
                                    needed reporting): left alone.
▸ No snapshot yet (first contact) → both sides become the baseline; differences are
                                    reported (state_differs, title_differs), never
                                    written. With first_contact_fix on, open/closed
                                    is fixed where the change times are reliable.

Whole-item updated_at is never used to pick a winner: labels, comments and
assignees bump it too.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import listing

log = logging.getLogger("bridge")

CLOSED_GROUPS = {"completed", "cancelled"}


# ---------------------------------------------------------------- snapshots

@dataclass
class Snapshot:
    plane_item_id: str
    repo: str
    issue_number: int
    p_title: str
    g_title: str
    p_closed: bool
    g_closed: bool
    p_stamp: str        # Plane's updated_at right after the bridge's last write there
    g_stamp: str        # GitHub's updated_at right after the bridge's last write there
    synced_at: float


def ensure_schema(store) -> None:
    with store._lock:
        # sync_snapshot held one value for both sides; it never shipped, so it's dropped.
        store._db.execute("DROP TABLE IF EXISTS sync_snapshot")
        store._db.execute(
            """CREATE TABLE IF NOT EXISTS pair_snapshot (
                   plane_item_id TEXT PRIMARY KEY,
                   repo          TEXT NOT NULL,
                   issue_number  INTEGER NOT NULL,
                   p_title       TEXT NOT NULL,
                   g_title       TEXT NOT NULL,
                   p_closed      INTEGER NOT NULL,
                   g_closed      INTEGER NOT NULL,
                   p_stamp       TEXT NOT NULL DEFAULT '',
                   g_stamp       TEXT NOT NULL DEFAULT '',
                   synced_at     REAL NOT NULL)""")
        store._db.execute(
            """CREATE TABLE IF NOT EXISTS repo_checks (
                   repo       TEXT PRIMARY KEY,
                   checked_at REAL NOT NULL)""")
        store._db.commit()


def record_snapshot(store, plane_item_id: str, repo: str, issue_number: int, *,
                    p_title: str, g_title: str, p_closed: bool, g_closed: bool,
                    p_stamp: str | None = None, g_stamp: str | None = None,
                    synced_at: float | None = None) -> None:
    """Remember both sides as they are right after a sync of the pair. A stamp left
    as None keeps the one already recorded."""
    old = get_snapshot(store, plane_item_id)
    if p_stamp is None:
        p_stamp = old.p_stamp if old else ""
    if g_stamp is None:
        g_stamp = old.g_stamp if old else ""
    with store._lock:
        store._db.execute(
            "INSERT OR REPLACE INTO pair_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plane_item_id, repo, issue_number, p_title or "", g_title or "", int(bool(p_closed)),
             int(bool(g_closed)), p_stamp or "", g_stamp or "",
             time.time() if synced_at is None else synced_at))
        store._db.commit()


def get_snapshot(store, plane_item_id: str) -> Snapshot | None:
    with store._lock:
        row = store._db.execute(
            "SELECT plane_item_id, repo, issue_number, p_title, g_title, p_closed, g_closed, "
            "p_stamp, g_stamp, synced_at FROM pair_snapshot WHERE plane_item_id = ?",
            (plane_item_id,)).fetchone()
    if not row:
        return None
    return Snapshot(row[0], row[1], row[2], row[3], row[4], bool(row[5]), bool(row[6]),
                    row[7], row[8], row[9])


def last_checked(store, repo: str) -> float | None:
    with store._lock:
        row = store._db.execute("SELECT checked_at FROM repo_checks WHERE repo = ?",
                                (repo.lower(),)).fetchone()
    return row[0] if row else None


def set_checked(store, repo: str, at: float) -> None:
    with store._lock:
        store._db.execute("INSERT OR REPLACE INTO repo_checks VALUES (?, ?)", (repo.lower(), at))
        store._db.commit()


# ---------------------------------------------------------------- the decision

def _ts(value) -> float | None:
    """ISO timestamp (Plane or GitHub) to epoch seconds, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Side:
    title: str
    closed: bool
    since: float | None     # when the current open/closed state began, if known reliably
    state_reason: str | None = None


def plane_side(item: dict, groups: dict) -> Side:
    group = item_group(item, groups)
    closed = group in CLOSED_GROUPS
    # Plane sets completed_at when an item reaches a completed state. There's no
    # reliable time for cancelled items or for reopening, so those stay unknown.
    since = _ts(item.get("completed_at")) if group == "completed" else None
    return Side(item.get("name", ""), closed, since)


def github_side(issue: dict) -> Side:
    closed = issue.get("state") == "closed"
    if closed:
        since = _ts(issue.get("closed_at"))
    elif not issue.get("closed_at"):
        since = _ts(issue.get("created_at"))     # never closed: open since it was made
    else:
        since = None                             # reopened at some unknown time
    return Side(issue.get("title", ""), closed, since, issue.get("state_reason"))


def item_group(item: dict, groups: dict) -> str:
    state = item.get("state") or item.get("state_id")
    if isinstance(state, dict):
        return state.get("group") or groups.get(state.get("id"), "")
    return groups.get(state, "")


@dataclass
class Decision:
    title_to: str | None = None      # "github" | "plane"
    closed_to: str | None = None     # "github" | "plane"
    problems: list[dict] = field(default_factory=list)   # {"kind", "field", ...}
    notes: list[dict] = field(default_factory=list)      # settled conflicts
    resolved: list[str] = field(default_factory=list)    # problem kinds that are gone

    @property
    def writes(self) -> bool:
        return bool(self.title_to or self.closed_to)


def decide(snap: Snapshot | None, p: Side, g: Side, *, prefer: dict | None = None,
           first_contact_fix: bool = False) -> Decision:
    """What to do with one pair.

    prefer: {"title"|"closed": "plane"|"github"} for a field a webhook just reported
    changing on that side: the event itself is the most recent change.
    """
    prefer = prefer or {}
    d = Decision()
    for fld, pv, gv, sp, sg in (
            ("title", p.title, g.title, snap.p_title if snap else None, snap.g_title if snap else None),
            ("closed", p.closed, g.closed, snap.p_closed if snap else None, snap.g_closed if snap else None)):
        kind = "title_differs" if fld == "title" else "state_differs"
        if pv == gv:
            d.resolved.append(kind)
            continue
        if fld in prefer:
            winner = prefer[fld]
        elif snap is None:
            winner = _trusted_winner(p, g) if (fld == "closed" and first_contact_fix) else None
            if winner is None:
                d.problems.append({"kind": kind, "field": fld, "first_contact": True})
                continue
        else:
            p_changed, g_changed = pv != sp, gv != sg
            if p_changed and not g_changed:
                winner = "plane"
            elif g_changed and not p_changed:
                winner = "github"
            elif not p_changed and not g_changed:
                continue            # a known divergence: reported when it was found
            else:
                winner = _trusted_winner(p, g) if fld == "closed" else None
                if winner is None:
                    d.problems.append({"kind": kind, "field": fld})
                    continue
                d.notes.append({"kind": "conflict", "field": fld, "winner": winner})
        target = "github" if winner == "plane" else "plane"
        if fld == "title":
            d.title_to = target
        else:
            d.closed_to = target
    return d


def _trusted_winner(p: Side, g: Side) -> str | None:
    if p.since is None or g.since is None or p.since == g.since:
        return None
    return "plane" if p.since > g.since else "github"


# ---------------------------------------------------------------- engine

@dataclass
class Report:
    checked: int = 0
    fixed: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    unlinked_issues: list[dict] = field(default_factory=list)
    calls: int = 0
    complete: bool = True       # every issue in the repo was listed (unlinked_issues is whole)


class Reconciler:
    def __init__(self, plane, gh, store, *,
                 apply_fix: Callable[[str, str, dict], None],
                 report_problem: Callable[[dict], None],
                 resolve_problem: Callable[[str, str], None] = lambda kind, item_id: None,
                 in_progress: Callable[[str], bool] = lambda item_id: False,
                 first_contact_fix: bool = False,
                 clock=time.time):
        self.plane, self.gh, self.store = plane, gh, store
        self.apply_fix, self.report_problem = apply_fix, report_problem
        self.resolve_problem, self.in_progress = resolve_problem, in_progress
        self.first_contact_fix = first_contact_fix
        self.clock = clock
        ensure_schema(store)

    def _fix(self, report: Report, kind: str, key: str, payload: dict) -> None:
        self.apply_fix(kind, key, payload)
        report.fixed.append({"kind": kind, "key": key, **payload})

    def reconcile_pair(self, project_id: str, module: dict, repo: str,
                       since: float | None = None) -> Report:
        """Check one module against one repo.

        since: list only issues updated since then (a debounced check); issues not
        listed are unchanged on GitHub and are only read when their Plane side
        changed. None: list every issue (the org scan).
        """
        report = Report()
        started = self.clock()
        items, calls = listing.module_items(self.plane, project_id, module["id"])
        report.calls += calls
        groups, calls = listing.state_groups(self.plane, project_id)
        report.calls += calls
        issues, calls, truncated = listing.repo_issues(
            self.gh, repo, since=iso(since) if since is not None else None)
        report.calls += calls
        report.complete = since is None and not truncated
        by_number = {i["number"]: i for i in issues}
        linked_numbers: set[int] = set()

        for item in items:
            report.checked += 1
            item_id = item["id"]
            link = self.store.by_plane_item(item_id)
            base = {"project_id": project_id, "item_id": item_id, "repo": repo}
            if link is None:
                if not self.in_progress(item_id):
                    self._fix(report, "create_issue", item_id, base)
                continue
            if link.repo.lower() != repo.lower():
                continue  # linked to another repo (moved between modules): not this pair
            linked_numbers.add(link.issue_number)
            base["issue_number"] = link.issue_number
            if not link.plane_linked:
                self._fix(report, "add_plane_link", item_id, {**base, "issue_url": link.issue_url})
            snap = get_snapshot(self.store, item_id)
            issue = by_number.get(link.issue_number)
            if issue is None and not report.complete:
                p = plane_side(item, groups)
                if since is not None and not truncated and snap is not None and \
                        p.title == snap.p_title and p.closed == snap.p_closed:
                    continue    # unchanged on both sides since the last check
                issue, calls = listing.issue(self.gh, repo, link.issue_number)
                report.calls += calls
            if issue is None:
                self.report_problem({"kind": "issue_missing", **base})
                continue
            self.resolve_problem("issue_missing", item_id)
            self._compare(report, item, issue, groups, base, snap)

        if report.complete:
            # Issues in the repo that aren't linked to any Plane item: report only.
            for number, issue in sorted(by_number.items()):
                if number in linked_numbers or self.store.by_issue(repo, number):
                    continue
                report.unlinked_issues.append({"repo": repo, "issue_number": number})
        set_checked(self.store, repo, started)
        return report

    def _compare(self, report: Report, item: dict, issue: dict, groups: dict, base: dict,
                 snap: Snapshot | None) -> None:
        p, g = plane_side(item, groups), github_side(issue)
        d = decide(snap, p, g, first_contact_fix=self.first_contact_fix)
        for kind in d.resolved:
            self.resolve_problem(kind, item["id"])
        for problem in d.problems:
            self.report_problem({**problem, **base})
        if d.writes:
            # The action re-reads both sides and decides again when it runs.
            self._fix(report, "sync_item", item["id"], {**base, "reconcile": True})
            for note in d.notes:
                report.conflicts.append({**note, **base})
            return
        if snap is None or (p.title, g.title, p.closed, g.closed) != (
                snap.p_title, snap.g_title, snap.p_closed, snap.g_closed):
            record_snapshot(self.store, item["id"], base["repo"], base["issue_number"],
                            p_title=p.title, g_title=g.title, p_closed=p.closed, g_closed=g.closed,
                            synced_at=self.clock())
