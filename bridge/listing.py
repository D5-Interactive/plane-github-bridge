"""Read-only listings the reconcile check needs, built on the existing clients.

Each function returns (data, calls): calls is how many HTTP requests it made, so a
scan can report its cost. Nothing here writes to Plane or GitHub.
"""
from __future__ import annotations

from .clients import GitHubClient, PlaneClient, _check, _results


def module_items(plane: PlaneClient, project_id: str, module_id: str) -> tuple[list[dict], int]:
    """A module's work items with at least id, name, state and updated_at.

    Plane's module-issues endpoint returns full work items on current versions;
    older ones return only the relation (issue id), in which case each item is
    fetched on its own.
    """
    calls = 0
    out, cursor = [], None
    path = f"/projects/{project_id}/modules/{module_id}/module-issues/"
    for _ in range(50):
        params = {"per_page": 100, **({"cursor": cursor} if cursor else {})}
        r = plane._request("GET", path, params=params)
        calls += 1
        _check(r)
        data = r.json()
        out.extend(_results(data))
        if isinstance(data, list) or not data.get("next_page_results"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    items = []
    for entry in out:
        if "name" in entry and ("state" in entry or "state_id" in entry):
            items.append(entry)
            continue
        item_id = entry.get("issue") or entry.get("id")
        if not item_id:
            continue
        items.append(plane.work_item(project_id, item_id))
        calls += 1
    return items, calls


def state_groups(plane: PlaneClient, project_id: str) -> tuple[dict[str, str], int]:
    """state id -> group (backlog, unstarted, started, completed, cancelled)."""
    cached = project_id in plane._states
    states = plane.states(project_id)
    return {s["id"]: s.get("group", "") for s in states}, (0 if cached else 1)


def repo_issues(gh: GitHubClient, repo: str) -> tuple[list[dict], int]:
    """Every issue in a repo, open and closed, pull requests left out."""
    calls, out = 0, []
    for page in range(1, 51):
        r = gh._request("GET", f"/repos/{repo}/issues",
                        params={"state": "all", "per_page": 100, "page": page})
        calls += 1
        _check(r)
        batch = r.json()
        out.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            break
    return out, calls


def issue(gh: GitHubClient, repo: str, number: int) -> tuple[dict | None, int]:
    """One issue, or None if it no longer exists in that repo (deleted or transferred)."""
    r = gh._request("GET", f"/repos/{repo}/issues/{number}")
    if r.status_code in (301, 404, 410):   # 301: transferred to another repo
        return None, 1
    _check(r)
    return r.json(), 1
