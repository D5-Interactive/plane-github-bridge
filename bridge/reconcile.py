"""Check a linked module against its repo and decide what to fix.

The engine only reads. Every change it wants is handed to `apply_fix(kind, key,
payload)`, which the caller routes through the retry queue, and every problem to
`report_problem(dict)`. That keeps it easy to test and keeps all writes in one place.

How it tells which side changed: after each successful sync of a pair, the caller
records a snapshot (title, closed, state group, both sides' updated_at). Here, a
side whose current value differs from the snapshot changed since then.

▸ One side changed      → push that side's value to the other.
▸ Both changed, differ  → the most recent change wins; the conflict is reported.
▸ Both changed, agree   → nothing to push; the snapshot is refreshed.
▸ No snapshot yet       → matching values become the baseline; only a clear
                          mismatch (closed on one side, open on the other) is
                          fixed, most recent wins. A title mismatch is reported.

Fix kinds: create_issue, add_plane_link, push_title_to_github, push_title_to_plane,
push_state_to_github, push_state_to_plane.
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
    title: str
    closed: bool
    state_group: str
    plane_updated: str
    gh_updated: str
    synced_at: float


def ensure_schema(store) -> None:
    with store._lock:
        store._db.execute(
            """CREATE TABLE IF NOT EXISTS sync_snapshot (
                   plane_item_id TEXT PRIMARY KEY,
                   repo          TEXT NOT NULL,
                   issue_number  INTEGER NOT NULL,
                   title         TEXT NOT NULL,
                   closed        INTEGER NOT NULL,
                   state_group   TEXT NOT NULL DEFAULT '',
                   plane_updated TEXT NOT NULL DEFAULT '',
                   gh_updated    TEXT NOT NULL DEFAULT '',
                   synced_at     REAL NOT NULL)""")
        store._db.commit()


def record_snapshot(store, plane_item_id: str, repo: str, issue_number: int, title: str,
                    closed: bool, state_group: str = "", plane_updated: str = "",
                    gh_updated: str = "", synced_at: float | None = None) -> None:
    """Remember what both sides looked like right after a successful sync of the pair."""
    with store._lock:
        store._db.execute(
            "INSERT OR REPLACE INTO sync_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plane_item_id, repo, issue_number, title, int(closed), state_group or "",
             plane_updated or "", gh_updated or "", time.time() if synced_at is None else synced_at))
        store._db.commit()


def get_snapshot(store, plane_item_id: str) -> Snapshot | None:
    with store._lock:
        row = store._db.execute(
            "SELECT * FROM sync_snapshot WHERE plane_item_id = ?", (plane_item_id,)).fetchone()
    if not row:
        return None
    return Snapshot(row[0], row[1], row[2], row[3], bool(row[4]), row[5], row[6], row[7], row[8])


# ---------------------------------------------------------------- engine

@dataclass
class Report:
    checked: int = 0
    fixed: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    unlinked_issues: list[dict] = field(default_factory=list)
    calls: int = 0


def _ts(value) -> float:
    """ISO timestamp (Plane or GitHub) to epoch seconds; missing or bad counts as oldest."""
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class Reconciler:
    def __init__(self, plane, gh, store, *,
                 apply_fix: Callable[[str, str, dict], None],
                 report_problem: Callable[[dict], None],
                 clock=time.time):
        self.plane, self.gh, self.store = plane, gh, store
        self.apply_fix, self.report_problem, self.clock = apply_fix, report_problem, clock
        ensure_schema(store)

    # ----------------------------------------------------------------- helpers

    def _fix(self, report: Report, kind: str, key: str, payload: dict) -> None:
        self.apply_fix(kind, key, payload)
        report.fixed.append({"kind": kind, "key": key, **payload})

    def _conflict(self, report: Report, **info) -> None:
        problem = {"kind": "conflict", **info}
        self.report_problem(problem)
        report.conflicts.append(problem)

    @staticmethod
    def _newer(item: dict, issue: dict) -> str:
        """Which side changed last: "plane" or "github". A tie goes to Plane."""
        return "github" if _ts(issue.get("updated_at")) > _ts(item.get("updated_at")) else "plane"

    # ----------------------------------------------------------------- main

    def reconcile_pair(self, project_id: str, module: dict, repo: str) -> Report:
        report = Report()
        items, calls = listing.module_items(self.plane, project_id, module["id"])
        report.calls += calls
        groups, calls = listing.state_groups(self.plane, project_id)
        report.calls += calls
        issues, calls = listing.repo_issues(self.gh, repo)
        report.calls += calls
        by_number = {i["number"]: i for i in issues}
        linked_numbers: set[int] = set()

        for item in items:
            report.checked += 1
            item_id = item["id"]
            link = self.store.by_plane_item(item_id)
            base = {"project_id": project_id, "item_id": item_id, "repo": repo}
            if link is None:
                self._fix(report, "create_issue", item_id, base)
                continue
            if link.repo.lower() != repo.lower():
                continue  # linked to another repo (moved between modules): not this pair
            linked_numbers.add(link.issue_number)
            base["issue_number"] = link.issue_number
            if not link.plane_linked:
                self._fix(report, "add_plane_link", item_id, {**base, "issue_url": link.issue_url})
            issue = by_number.get(link.issue_number)
            if issue is None:
                self.report_problem({"kind": "issue_missing", **base})
                continue
            self._compare(report, item, issue, groups, base)

        # Issues in the repo that aren't linked to any Plane item: report only.
        # (Turning them into Plane items is a separate feature.)
        for number, issue in sorted(by_number.items()):
            if number in linked_numbers or self.store.by_issue(repo, number):
                continue
            report.unlinked_issues.append({"repo": repo, "issue_number": number,
                                           "title": issue.get("title", "")})
        return report

    def _compare(self, report: Report, item: dict, issue: dict, groups: dict, base: dict) -> None:
        state_id = item.get("state") or item.get("state_id")
        if isinstance(state_id, dict):
            state_id = state_id.get("id")
        group = groups.get(state_id, "")
        p_title, g_title = item.get("name", ""), issue.get("title", "")
        p_closed, g_closed = group in CLOSED_GROUPS, issue.get("state") == "closed"
        snap = get_snapshot(self.store, item["id"])
        newer = self._newer(item, issue)
        stamps = {"plane_updated": item.get("updated_at", ""),
                  "github_updated": issue.get("updated_at", "")}

        def baseline() -> None:
            record_snapshot(self.store, item["id"], base["repo"], base["issue_number"], p_title,
                            p_closed, group, item.get("updated_at", ""), issue.get("updated_at", ""),
                            self.clock())

        if snap is None:
            if p_title == g_title and p_closed == g_closed:
                baseline()
                return
            if p_closed != g_closed:
                self._push_state(report, newer, p_closed, g_closed, issue, base)
            if p_title != g_title:
                self.report_problem({"kind": "title_differs", **base, "plane_value": p_title,
                                     "github_value": g_title, **stamps})
            return

        # Title
        p_changed, g_changed = p_title != snap.title, g_title != snap.title
        if p_changed and not g_changed:
            self._fix(report, "push_title_to_github", item["id"], {**base, "title": p_title})
        elif g_changed and not p_changed:
            self._fix(report, "push_title_to_plane", item["id"], {**base, "title": g_title})
        elif p_changed and g_changed and p_title != g_title:
            winner = newer
            kind = "push_title_to_github" if winner == "plane" else "push_title_to_plane"
            self._fix(report, kind, item["id"], {**base, "title": p_title if winner == "plane" else g_title})
            self._conflict(report, field="title", **base, plane_value=p_title, github_value=g_title,
                           winner=winner, **stamps)

        # Open or closed. (Open/closed has two values, so if both sides changed
        # since the snapshot they agree; the last branch is kept for safety.)
        p_changed, g_changed = p_closed != snap.closed, g_closed != snap.closed
        if p_changed and not g_changed:
            self._fix(report, "push_state_to_github", item["id"], {**base, "closed": p_closed})
        elif g_changed and not p_changed:
            self._fix(report, "push_state_to_plane", item["id"],
                      {**base, "closed": g_closed, "state_reason": issue.get("state_reason")})
        elif p_changed and g_changed and p_closed != g_closed:
            self._push_state(report, newer, p_closed, g_closed, issue, base)
            self._conflict(report, field="closed", **base, plane_value=p_closed, github_value=g_closed,
                           winner=newer, **stamps)

        # Both sides moved to the same values: nothing to push, just remember it.
        if p_title == g_title and p_closed == g_closed and (
                p_title != snap.title or p_closed != snap.closed):
            baseline()

    def _push_state(self, report: Report, winner: str, p_closed: bool, g_closed: bool,
                    issue: dict, base: dict) -> None:
        if winner == "plane":
            self._fix(report, "push_state_to_github", base["item_id"], {**base, "closed": p_closed})
        else:
            self._fix(report, "push_state_to_plane", base["item_id"],
                      {**base, "closed": g_closed, "state_reason": issue.get("state_reason")})
