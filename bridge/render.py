"""Pure helpers: signatures, Plane HTML -> GitHub markdown, closing-reference parsing."""
from __future__ import annotations

import hashlib
import hmac
import re

from markdownify import markdownify


# ---------- webhook signatures ----------

def verify_plane_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Plane: X-Plane-Signature = hex HMAC-SHA256 of the raw body."""
    if not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub: X-Hub-Signature-256 = 'sha256=' + hex HMAC-SHA256 of the raw body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


# ---------- Plane webhook payloads ----------

def normalize_plane_event(payload: dict) -> tuple[str | None, str | None]:
    """Return (kind, action) for a Plane webhook, or (None, None) if it is not a work item event.

    kind is always "workitem"; action is "created" | "updated" | "deleted".
    Handles the v2 format ("workitem.created") and the older one
    ({"event": "issue", "action": "create"}).
    """
    event = str(payload.get("event", "")).lower()
    action = str(payload.get("action", "")).lower()

    if event in ("issue", "work_item", "workitem"):
        kind = "workitem"
    elif event.startswith(("workitem.", "work_item.", "issue.")):
        parts = event.split(".")
        if len(parts) != 2:          # workitem.comment.created etc. are not ours
            return None, None
        kind, action = "workitem", parts[1]
    elif "work_item_" in event:      # e.g. "webhookscope.scopechoices.work_item_updated"
        tail = event.rsplit("work_item_", 1)[1]
        if tail not in ("created", "updated", "deleted"):
            return None, None
        kind, action = "workitem", tail
    else:
        return None, None

    action = {"create": "created", "update": "updated", "delete": "deleted"}.get(action, action)
    if action not in ("created", "updated", "deleted"):
        return None, None
    return kind, action


# ---------- description rendering ----------

_TASK_ITEM = re.compile(r'<li[^>]*data-checked="(true|false)"[^>]*>', re.I)


def plane_html_to_markdown(html: str | None) -> str:
    """Convert a Plane description to GitHub markdown, keeping checklists as - [ ] / - [x]."""
    if not html or html.strip() in ("<p></p>", ""):
        return ""
    # Plane task lists: <ul data-type="taskList"><li data-checked="false">…</li></ul>
    html = _TASK_ITEM.sub(lambda m: "<li>" + ("[x] " if m.group(1).lower() == "true" else "[ ] "), html)
    md = markdownify(html, heading_style="ATX", bullets="-")
    md = re.sub(r"^(\s*)- \\\[( |x)\\\] ", r"\1- [\2] ", md, flags=re.M)  # un-escape brackets
    md = re.sub(r"(-\s\[[ x]\])\s+", r"\1 ", md)                             # one space after the box
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def issue_body(description_md: str, item_ref: str, item_url: str) -> str:
    parts = []
    if description_md:
        parts.append(description_md)
    parts.append("---")
    parts.append(f"Synced from Plane work item [{item_ref}]({item_url}). "
                 "Plan, estimate and edit the task in Plane; do the code work here.")
    return "\n\n".join(parts)


# ---------- GitHub closing references ----------

_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>\d+)",
    re.I,
)


def closing_refs(text: str | None, default_repo: str) -> list[tuple[str, int]]:
    """Issues a PR says it closes: 'Closes #12', 'fixes D5-Interactive/DBGC#3', ..."""
    if not text:
        return []
    out: list[tuple[str, int]] = []
    for m in _CLOSING.finditer(text):
        ref = (m.group("repo") or default_repo, int(m.group("num")))
        if ref not in out:
            out.append(ref)
    return out
