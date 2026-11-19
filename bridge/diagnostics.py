"""Sync problems: recorded in the bridge, flagged with a label on both sides.

A sync problem is anything the bridge couldn't settle on its own: a queued action
that has waited too long, a conflict where both sides changed, an action GitHub or
Plane refused. Each one is stored here with IDs and short values only (never
descriptions or comments). The affected Plane item gets a "sync problem" label and
the GitHub issue a matching label, so people can see something is off without any
comment landing in the work itself. The details are read from the bridge:

  GET /status/PROJECT-N       the caller's own Plane API key; only if they can read the item
  GET /admin/sync-problems    the caller's key must belong to a workspace admin

Flagging is best effort: if a label call fails, the problem stays recorded and
reflag_pending() tries again later. Nothing here ever raises into the caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from .clients import GitHubClient, PlaneClient, _check

log = logging.getLogger("bridge")

PLANE_LABEL_COLOR = "#E5484D"
GITHUB_LABEL_COLOR = "E5484D"
PLANE_ADMIN_ROLE = 20          # Plane workspace roles: 20 admin, 15 member, 5 guest (checked live)
_REF = re.compile(r"^[A-Za-z0-9]{1,12}-\d{1,9}$")


def ensure_schema(store) -> None:
    with store._lock:
        store._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_problems (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint      TEXT NOT NULL UNIQUE,
                kind             TEXT NOT NULL,
                plane_project_id TEXT,
                plane_item_id    TEXT,
                repo             TEXT,
                issue_number     INTEGER,
                detail           TEXT NOT NULL DEFAULT '{}',
                first_seen       REAL NOT NULL,
                last_seen        REAL NOT NULL,
                count            INTEGER NOT NULL DEFAULT 1,
                resolved_at      REAL
            );
            CREATE INDEX IF NOT EXISTS sync_problems_item ON sync_problems (plane_item_id);
            CREATE INDEX IF NOT EXISTS sync_problems_issue ON sync_problems (repo, issue_number);
            -- What the labels should look like: one row per flagged Plane item or GitHub
            -- issue. done = 0 means the last label call failed and reflag_pending retries it.
            CREATE TABLE IF NOT EXISTS sync_flags (
                target     TEXT PRIMARY KEY,   -- "plane:<project>:<item>" or "github:<owner/repo>#<n>"
                want       INTEGER NOT NULL,   -- 1 label on, 0 label off
                done       INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );
            """)
        store._db.commit()


@dataclass
class Problem:
    id: int
    fingerprint: str
    kind: str
    plane_project_id: str | None
    plane_item_id: str | None
    repo: str | None
    issue_number: int | None
    detail: dict
    first_seen: float
    last_seen: float
    count: int
    resolved_at: float | None

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "plane_project_id": self.plane_project_id,
                "plane_item_id": self.plane_item_id, "repo": self.repo,
                "issue_number": self.issue_number, "detail": self.detail,
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "count": self.count, "resolved_at": self.resolved_at}


_COLS = ("id, fingerprint, kind, plane_project_id, plane_item_id, repo, issue_number, "
         "detail, first_seen, last_seen, count, resolved_at")


def _row(r) -> Problem:
    return Problem(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7] or "{}"),
                   r[8], r[9], r[10], r[11])


def fingerprint(kind: str, plane_project_id=None, plane_item_id=None, repo=None,
                issue_number=None, key: str = "") -> str:
    raw = "|".join(str(x or "") for x in (kind, plane_project_id, plane_item_id,
                                           (repo or "").lower(), issue_number, key))
    return hashlib.sha1(raw.encode()).hexdigest()


def _short(value) -> object:
    """Keep detail values short: IDs, names, numbers. Long text is cut."""
    if isinstance(value, str):
        return value if len(value) <= 120 else value[:117] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_short(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k)[:60]: _short(v) for k, v in list(value.items())[:30]}
    return str(value)[:120]


class Diagnostics:
    def __init__(self, store, plane: PlaneClient | None, gh: GitHubClient | None, *,
                 public_url: str = "", plane_label: str = "sync problem",
                 github_label: str = "plane-sync-problem", clock=time.time):
        self.store, self.plane, self.gh = store, plane, gh
        self.public_url = public_url.rstrip("/")
        self.plane_label, self.github_label = plane_label, github_label
        self.clock = clock
        self._plane_label_ids: dict[str, str] = {}     # project id -> label id
        self._gh_labels: set[str] = set()              # repos known to have the label
        ensure_schema(store)

    # ------------------------------------------------------------ recording

    def report(self, kind: str, *, plane_project_id: str | None = None,
               plane_item_id: str | None = None, repo: str | None = None,
               issue_number: int | None = None, detail: dict | None = None,
               key: str = "") -> int:
        """Record a problem (or bump its count if already open) and flag the item and issue.

        detail holds IDs and short values only; key separates distinct problems of the
        same kind on the same item (for example the field in conflict). Returns the id.
        """
        now = self.clock()
        fp = fingerprint(kind, plane_project_id, plane_item_id, repo, issue_number, key)
        det = json.dumps(_short(detail or {}), sort_keys=True)
        db, lock = self.store._db, self.store._lock
        with lock:
            row = db.execute("SELECT id FROM sync_problems WHERE fingerprint = ?", (fp,)).fetchone()
            if row:
                db.execute("UPDATE sync_problems SET last_seen = ?, count = count + 1, detail = ?, "
                           "resolved_at = NULL WHERE id = ?", (now, det, row[0]))
                pid = row[0]
            else:
                cur = db.execute(
                    "INSERT INTO sync_problems (fingerprint, kind, plane_project_id, plane_item_id, "
                    "repo, issue_number, detail, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                    (fp, kind, plane_project_id, plane_item_id, repo, issue_number, det, now, now))
                pid = cur.lastrowid
            db.commit()
        log.warning("sync problem %s: %s", kind,
                    " ".join(f"{k}={v}" for k, v in (("item", plane_item_id),
                             ("issue", f"{repo}#{issue_number}" if repo and issue_number else repo))
                             if v))
        self._want_flags(plane_project_id, plane_item_id, repo, issue_number)
        return pid

    def note(self, kind: str, *, plane_project_id: str | None = None,
             plane_item_id: str | None = None, repo: str | None = None,
             issue_number: int | None = None, detail: dict | None = None, key: str = "") -> int:
        """Record something the bridge settled on its own (for example a conflict where the
        most recent change won). Kept for /status and the admin view as already resolved;
        no label is added, so nothing flickers on the item."""
        now = self.clock()
        fp = fingerprint(kind, plane_project_id, plane_item_id, repo, issue_number, key)
        det = json.dumps(_short(detail or {}), sort_keys=True)
        db, lock = self.store._db, self.store._lock
        with lock:
            row = db.execute("SELECT id FROM sync_problems WHERE fingerprint = ?", (fp,)).fetchone()
            if row:
                db.execute("UPDATE sync_problems SET last_seen = ?, count = count + 1, detail = ?, "
                           "resolved_at = ? WHERE id = ?", (now, det, now, row[0]))
                pid = row[0]
            else:
                cur = db.execute(
                    "INSERT INTO sync_problems (fingerprint, kind, plane_project_id, plane_item_id, "
                    "repo, issue_number, detail, first_seen, last_seen, resolved_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (fp, kind, plane_project_id, plane_item_id, repo, issue_number, det, now, now, now))
                pid = cur.lastrowid
            db.commit()
        log.info("sync note %s: %s", kind,
                 " ".join(f"{k}={v}" for k, v in (("item", plane_item_id),
                          ("issue", f"{repo}#{issue_number}" if repo and issue_number else repo)) if v))
        return pid

    def resolve(self, fp: str | None = None, *, kind: str | None = None,
                plane_item_id: str | None = None, repo: str | None = None,
                issue_number: int | None = None) -> int:
        """Mark matching open problems resolved; drop labels where nothing else is open.

        Pass a fingerprint, or any of kind / plane_item_id / repo+issue_number to match.
        Returns how many problems were resolved.
        """
        clauses, args = ["resolved_at IS NULL"], []
        if fp:
            clauses.append("fingerprint = ?"); args.append(fp)
        if kind:
            clauses.append("kind = ?"); args.append(kind)
        if plane_item_id:
            clauses.append("plane_item_id = ?"); args.append(plane_item_id)
        if repo:
            clauses.append("lower(repo) = lower(?)"); args.append(repo)
        if issue_number is not None:
            clauses.append("issue_number = ?"); args.append(issue_number)
        if len(clauses) == 1:
            raise ValueError("resolve() needs a fingerprint or something to match")
        where = " AND ".join(clauses)
        db, lock = self.store._db, self.store._lock
        with lock:
            rows = db.execute(f"SELECT {_COLS} FROM sync_problems WHERE {where}", args).fetchall()
            db.execute(f"UPDATE sync_problems SET resolved_at = ? WHERE {where}", [self.clock(), *args])
            db.commit()
        for p in map(_row, rows):
            self._want_flags(p.plane_project_id, p.plane_item_id, p.repo, p.issue_number)
        return len(rows)

    def open_problems(self, *, repo: str | None = None, project: str | None = None,
                      kind: str | None = None, plane_item_id: str | None = None,
                      include_resolved_since: float | None = None, limit: int = 500) -> list[Problem]:
        clauses, args = [], []
        if include_resolved_since is None:
            clauses.append("resolved_at IS NULL")
        else:
            clauses.append("(resolved_at IS NULL OR resolved_at >= ?)"); args.append(include_resolved_since)
        if repo:
            clauses.append("lower(repo) = lower(?)"); args.append(repo)
        if project:
            clauses.append("plane_project_id = ?"); args.append(project)
        if kind:
            clauses.append("kind = ?"); args.append(kind)
        if plane_item_id:
            clauses.append("plane_item_id = ?"); args.append(plane_item_id)
        with self.store._lock:
            rows = self.store._db.execute(
                f"SELECT {_COLS} FROM sync_problems WHERE {' AND '.join(clauses)} "
                "ORDER BY last_seen DESC LIMIT ?", [*args, limit]).fetchall()
        return [_row(r) for r in rows]

    def counts(self) -> dict:
        """For /health: open problems by kind, and label updates still pending."""
        with self.store._lock:
            by_kind = dict(self.store._db.execute(
                "SELECT kind, count(*) FROM sync_problems WHERE resolved_at IS NULL GROUP BY kind").fetchall())
            pending = self.store._db.execute("SELECT count(*) FROM sync_flags WHERE done = 0").fetchone()[0]
        return {"open": sum(by_kind.values()), "by_kind": by_kind, "flags_pending": pending}

    # ------------------------------------------------------------ flags

    def _open_on_item(self, item_id: str) -> bool:
        with self.store._lock:
            return bool(self.store._db.execute(
                "SELECT 1 FROM sync_problems WHERE plane_item_id = ? AND resolved_at IS NULL LIMIT 1",
                (item_id,)).fetchone())

    def _open_on_issue(self, repo: str, number: int) -> bool:
        with self.store._lock:
            return bool(self.store._db.execute(
                "SELECT 1 FROM sync_problems WHERE lower(repo) = lower(?) AND issue_number = ? "
                "AND resolved_at IS NULL LIMIT 1", (repo, number)).fetchone())

    def _set_flag(self, target: str, want: bool, done: bool) -> None:
        with self.store._lock:
            self.store._db.execute("INSERT OR REPLACE INTO sync_flags VALUES (?, ?, ?, ?)",
                                   (target, int(want), int(done), self.clock()))
            self.store._db.commit()

    def _want_flags(self, project_id, item_id, repo, number) -> None:
        if project_id and item_id:
            self._apply(f"plane:{project_id}:{item_id}", self._open_on_item(item_id))
        if repo and number:
            self._apply(f"github:{repo}#{number}", self._open_on_issue(repo, number))

    def _github_can_sign(self, repo: str) -> bool:
        auth = getattr(self.gh, "auth", None)
        can = getattr(auth, "can_sign", None)
        return can is None or can(repo.split("/", 1)[0])

    def _apply(self, target: str, want: bool) -> bool:
        if target.startswith("github:") and not \
                self._github_can_sign(target.removeprefix("github:").rsplit("#", 1)[0]):
            # Nothing can sign GitHub calls for that account right now: leave the label
            # change pending without calling GitHub (or logging) until it can.
            self._set_flag(target, want, False)
            return False
        try:
            if target.startswith("plane:"):
                _, project_id, item_id = target.split(":", 2)
                self._plane_flag(project_id, item_id, want)
            else:
                repo, number = target.removeprefix("github:").rsplit("#", 1)
                self._github_flag(repo, int(number), want)
        except Exception as e:   # never raise into the caller; retried by reflag_pending
            log.warning("couldn't %s the sync-problem label on %s (%s); will retry",
                        "add" if want else "remove", target, _reason(e))
            self._set_flag(target, want, False)
            return False
        self._set_flag(target, want, True)
        return True

    def reflag_pending(self, limit: int = 20) -> int:
        """Retry label updates that failed. Returns how many succeeded."""
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT target FROM sync_flags WHERE done = 0 ORDER BY updated_at LIMIT ?",
                (limit,)).fetchall()
        ok = 0
        for (target,) in rows:
            if target.startswith("plane:"):
                want = self._open_on_item(target.split(":", 2)[2])
            else:
                repo, number = target.removeprefix("github:").rsplit("#", 1)
                want = self._open_on_issue(repo, int(number))
            ok += self._apply(target, want)
        return ok

    # Plane: labels live per project; a work item's labels are a list of label IDs,
    # and PATCH replaces the list, so we always send the existing IDs plus or minus ours.

    def _plane_label_id(self, project_id: str) -> str:
        if project_id in self._plane_label_ids:
            return self._plane_label_ids[project_id]
        for label in self.plane._paged(f"/projects/{project_id}/labels/"):
            if (label.get("name") or "").strip().lower() == self.plane_label.lower():
                self._plane_label_ids[project_id] = label["id"]
                return label["id"]
        where = f"{self.public_url}/status/<PROJECT-N>" if self.public_url else "the bridge's /status/<PROJECT-N>"
        r = self.plane._request("POST", f"/projects/{project_id}/labels/", json={
            "name": self.plane_label, "color": PLANE_LABEL_COLOR,
            "description": f"Set by the Plane-GitHub bridge: this item has a sync problem. Details: {where}"})
        _check(r)
        self._plane_label_ids[project_id] = r.json()["id"]
        return self._plane_label_ids[project_id]

    def _plane_flag(self, project_id: str, item_id: str, want: bool) -> None:
        label_id = self._plane_label_id(project_id)
        r = self.plane._request("GET", f"/projects/{project_id}/issues/{item_id}/")
        _check(r)
        current = [lid if isinstance(lid, str) else lid.get("id")
                   for lid in (r.json().get("labels") or [])]
        current = [x for x in current if x]
        if want and label_id not in current:
            new = current + [label_id]
        elif not want and label_id in current:
            new = [x for x in current if x != label_id]
        else:
            return
        r = self.plane._request("PATCH", f"/projects/{project_id}/issues/{item_id}/", json={"labels": new})
        _check(r)

    def _github_label(self, repo: str) -> None:
        if repo.lower() in self._gh_labels:
            return
        r = self.gh._request("GET", f"/repos/{repo}/labels/{self.github_label}")
        if r.status_code == 404:
            where = f"{self.public_url}/status" if self.public_url else "the Plane bridge"
            r = self.gh._request("POST", f"/repos/{repo}/labels", json={
                "name": self.github_label, "color": GITHUB_LABEL_COLOR,
                "description": f"Plane bridge sync problem. Details: {where}"[:100]})
            if r.status_code != 422:          # 422: someone created it meanwhile
                _check(r)
        else:
            _check(r)
        self._gh_labels.add(repo.lower())

    def _github_flag(self, repo: str, number: int, want: bool) -> None:
        if want:
            self._github_label(repo)
            r = self.gh._request("POST", f"/repos/{repo}/issues/{number}/labels",
                                 json={"labels": [self.github_label]})
            _check(r)
        else:
            r = self.gh._request("DELETE", f"/repos/{repo}/issues/{number}/labels/{self.github_label}")
            if r.status_code != 404:          # 404: the label wasn't on the issue
                _check(r)


def _reason(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"{e.response.status_code} from {e.request.method} {e.request.url.path}"
    return type(e).__name__


# ---------------------------------------------------------------- HTTP views

def make_router(diag: Diagnostics, plane_base_url: str, workspace: str,
                transport: httpx.BaseTransport | None = None,
                recent_seconds: float = 7 * 86400, on_scan=None,
                per_minute: int = 30, clock=time.monotonic, trust_proxy_headers: bool = False,
                max_clients: int = 10_000) -> APIRouter:
    """/status/PROJECT-N and /admin/sync-problems, checked against Plane with the caller's key.

    The caller's key is only used for those checks: it's never stored or logged. Each
    client address may make `per_minute` requests a minute (each one costs a Plane
    call). The address is the socket's, or, with trust_proxy_headers (only behind a
    tunnel or proxy that sets them), CF-Connecting-IP or the first X-Forwarded-For
    entry. At most max_clients addresses are remembered; the least recent go first.
    """
    hits: dict[str, list[float]] = {}
    hits_lock = threading.Lock()

    def throttle(request: Request) -> None:
        ip = ""
        if trust_proxy_headers:
            ip = (request.headers.get("cf-connecting-ip")
                  or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip())
        ip = ip or (request.client.host if request.client else "unknown")
        now = clock()
        with hits_lock:
            recent = [t for t in hits.get(ip, []) if now - t < 60]
            if len(recent) >= per_minute:
                hits[ip] = recent
                raise HTTPException(429, "too many requests; try again in a minute")
            recent.append(now)
            hits[ip] = recent
            if len(hits) > max_clients:
                for k in [k for k, v in hits.items() if not v or now - v[-1] >= 60]:
                    del hits[k]             # idle addresses first
                if len(hits) > max_clients:  # then the least recently seen
                    for k in sorted(hits, key=lambda k: hits[k][-1])[:len(hits) - max_clients]:
                        del hits[k]

    router = APIRouter(dependencies=[Depends(throttle)])
    api = f"{plane_base_url.rstrip('/')}/api/v1"

    def as_caller(key: str) -> httpx.Client:
        return httpx.Client(base_url=api, timeout=10, transport=transport,
                            headers={"X-API-Key": key, "Accept": "application/json"})

    @router.get("/status/{ref}")
    def status(ref: str, x_api_key: str | None = Header(default=None)):
        if not x_api_key or not _REF.match(ref):
            raise HTTPException(404, "not found")
        try:
            with as_caller(x_api_key) as c:
                r = c.get(f"/workspaces/{workspace}/issues/{ref.upper()}/")
        except httpx.HTTPError:
            raise HTTPException(503, "Plane is unreachable") from None
        if r.status_code != 200:
            raise HTTPException(404, "not found")       # unknown, or the caller can't see it
        item = r.json()
        problems = diag.open_problems(plane_item_id=item.get("id"),
                                      include_resolved_since=diag.clock() - recent_seconds)
        return {"ref": ref.upper(), "plane_item_id": item.get("id"),
                "open": [p.as_dict() for p in problems if p.resolved_at is None],
                "recently_resolved": [p.as_dict() for p in problems if p.resolved_at is not None]}

    @router.get("/admin/sync-problems")
    def admin(x_api_key: str | None = Header(default=None), repo: str | None = None,
              project: str | None = None, kind: str | None = None):
        if not x_api_key or not _is_admin(as_caller, x_api_key, workspace):
            raise HTTPException(403, "workspace admins only")
        problems = diag.open_problems(repo=repo, project=project, kind=kind)
        return {"counts": diag.counts(), "open": [p.as_dict() for p in problems]}

    if on_scan is not None:
        @router.post("/admin/scan", status_code=202)
        def scan(x_api_key: str | None = Header(default=None)):
            """Run the org-wide check at the next opportunity (workspace admins only)."""
            if not x_api_key or not _is_admin(as_caller, x_api_key, workspace):
                raise HTTPException(403, "workspace admins only")
            on_scan()
            return {"scheduled": True}

    return router


def _is_admin(as_caller, key: str, workspace: str) -> bool:
    """True if the key's owner is an admin of the workspace (checked with Plane, fail closed)."""
    try:
        with as_caller(key) as c:
            me = c.get("/users/me/")
            if me.status_code != 200:
                return False
            my_id = me.json().get("id")
            r = c.get(f"/workspaces/{workspace}/members/")
            if r.status_code != 200:
                return False
            data = r.json()
    except httpx.HTTPError:
        return False
    for m in (data if isinstance(data, list) else data.get("results", [])):
        member = m.get("member")
        mid = member.get("id") if isinstance(member, dict) else (member or m.get("id"))
        is_bot = m.get("is_bot") or (member.get("is_bot") if isinstance(member, dict) else False)
        if mid == my_id:
            if is_bot:       # Plane's own bot user is role 20 too; it never counts as an admin
                return False
            try:
                return int(m.get("role") or 0) >= PLANE_ADMIN_ROLE
            except (TypeError, ValueError):
                return False
    return False
