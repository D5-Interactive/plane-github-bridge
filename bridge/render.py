"""Pure helpers: signatures, Plane HTML -> GitHub markdown, closing-reference parsing."""
from __future__ import annotations

import hashlib
import hmac
import html as _html
import re

from markdownify import markdownify


# ---------- webhook signatures ----------

def verify_plane_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Plane: X-Plane-Signature = hex HMAC-SHA256 of the raw body."""
    if not secret or not header:  # an empty secret would make signatures forgeable
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    """GitHub: X-Hub-Signature-256 = 'sha256=' + hex HMAC-SHA256 of the raw body."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


# ---------- Plane webhook payloads ----------

_KINDS = {"issue": "workitem", "work_item": "workitem", "workitem": "workitem", "module": "module"}


def normalize_plane_event(payload: dict) -> tuple[str | None, str | None]:
    """Return (kind, action) for a Plane webhook, or (None, None) if it isn't one we handle.

    kind is "workitem" or "module"; action is "created" | "updated" | "deleted".
    Handles the v2 format ("workitem.created", "module.updated") and the older one
    ({"event": "issue", "action": "create"}).
    """
    event = str(payload.get("event", "")).lower()
    action = str(payload.get("action", "")).lower()

    if event in _KINDS:
        kind = _KINDS[event]
    elif "." in event and event.split(".")[0] in _KINDS and "scopechoices" not in event:
        parts = event.split(".")
        if len(parts) != 2:          # workitem.comment.created etc. are not ours
            return None, None
        kind, action = _KINDS[parts[0]], parts[1]
    elif "work_item_" in event or "module_" in event:   # "webhookscope.scopechoices.work_item_updated"
        marker = "work_item_" if "work_item_" in event else "module_"
        tail = event.rsplit(marker, 1)[1]
        if tail not in ("created", "updated", "deleted"):
            return None, None
        kind, action = ("workitem" if marker == "work_item_" else "module"), tail
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


_MENTION = re.compile(r"(?<![\w/])(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>\d+)\b")
_BRANCH = re.compile(r"^(?:[\w.-]+/)?(?P<num>\d+)[-_]")


def mention_refs(text: str | None, default_repo: str) -> list[tuple[str, int]]:
    """Every issue a commit message mentions: '#12', 'see your-org/repo#3', 'Closes #4'."""
    if not text:
        return []
    out: list[tuple[str, int]] = []
    for m in _MENTION.finditer(text):
        ref = (m.group("repo") or default_repo, int(m.group("num")))
        if ref not in out:
            out.append(ref)
    return out


def branch_issue_number(branch: str | None) -> int | None:
    """Issue number from a branch named the way GitHub's 'Create a branch' names it:
    '12-scan-command' (also 'feature/12-scan', '12_scan')."""
    m = _BRANCH.match(branch or "")
    return int(m.group("num")) if m else None


_GITHUB_REPO = re.compile(
    r"https?://(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?=[/?#\s\"'<)\],]|$)")


def github_repo_in(*texts) -> str | None:
    """The first github.com/owner/repo URL in any of the given texts, as 'owner/repo'."""
    for t in texts:
        if isinstance(t, str):
            m = _GITHUB_REPO.search(t)
            if m:
                return f"{m.group('owner')}/{m.group('repo')}"
    return None


def esc(text) -> str:
    return _html.escape(str(text or ""), quote=True)


def plain_text(html: str | None) -> str:
    """Visible text of an HTML fragment, whitespace collapsed."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def repo_description_html(repo_url: str, description: str | None) -> str:
    """The module description the bridge writes: the repo link, then the repo's description."""
    out = f'<p><a href="{esc(repo_url)}">{esc(repo_url)}</a></p>'
    if description and description.strip():
        out += f"<p>{esc(description.strip())}</p>"
    return out


def closing_refs(text: str | None, default_repo: str) -> list[tuple[str, int]]:
    """Issues a PR says it closes: 'Closes #12', 'fixes your-org/repo#3', ..."""
    if not text:
        return []
    out: list[tuple[str, int]] = []
    for m in _CLOSING.finditer(text):
        ref = (m.group("repo") or default_repo, int(m.group("num")))
        if ref not in out:
            out.append(ref)
    return out
