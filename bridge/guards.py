"""Checks run before a queued action runs again, so nothing is done or recorded twice.

An action can fail after it took effect on the other side: GitHub created the issue but
the response was lost, Plane added the link but the request timed out. Before retrying,
the bridge reads the current state and asks these helpers whether the work is already
done. They are pure: callers pass in what they read.
"""
from __future__ import annotations

import re

BRIDGE_MARKER = "Synced from Plane work item"


def issue_already_created(store, item_id: str):
    """The stored link if the bridge already recorded an issue for this item."""
    return store.by_plane_item(item_id)


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def link_already_present(existing_link_urls, url: str) -> bool:
    """True if the Plane item already has this link (case and trailing slash ignored)."""
    want = _norm_url(url)
    return any(_norm_url(u) == want for u in existing_link_urls or [])


def state_already_set(current_state_id: str | None, target_state_id: str | None) -> bool:
    return bool(target_state_id) and current_state_id == target_state_id


def comment_already_posted(store, item_id: str, key: str) -> bool:
    """True if this activity was already posted to the item. Reads only: unlike
    store.post_is_new it records nothing."""
    with store._lock:
        row = store._db.execute(
            "SELECT 1 FROM posted WHERE plane_item_id = ? AND key = ?", (item_id, key)).fetchone()
    return row is not None


def find_similar_issue(open_issues: list[dict], title: str, item_ref: str) -> dict | None:
    """An issue that an earlier attempt probably created for this item.

    First choice: an issue whose body links the same Plane item reference, the way the
    bridge writes it ("[PROJ-12](...)"). Second: an exact title match on an issue the
    bridge created (its body carries the bridge's marker). Pull requests are ignored.
    """
    issues = [i for i in open_issues or [] if "pull_request" not in i]
    if item_ref:
        pat = re.compile(r"\[" + re.escape(item_ref) + r"\]\(")
        for i in issues:
            if pat.search(i.get("body") or ""):
                return i
    want = (title or "").strip()
    if want:
        for i in issues:
            if (i.get("title") or "").strip() == want and BRIDGE_MARKER in (i.get("body") or ""):
                return i
    return None
