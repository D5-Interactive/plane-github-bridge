"""Thin REST clients for Plane (v1 API) and GitHub."""
from __future__ import annotations

import logging
import threading
import time
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger("bridge")

# Longest single wait for a rate limit, and the wait when the server gives no hint.
MAX_WAIT = 60.0
DEFAULT_WAIT = 10.0


def retry_after(r: httpx.Response, now: float | None = None) -> float:
    """Seconds to wait before retrying a rate-limited request, capped at MAX_WAIT.

    Reads Retry-After (seconds or an HTTP date), then X-RateLimit-Reset (an epoch
    time, or a number of seconds if it's small), else DEFAULT_WAIT.
    """
    now = time.time() if now is None else now
    wait = None
    value = (r.headers.get("Retry-After") or "").strip()
    if value:
        try:
            wait = float(value)
        except ValueError:
            try:
                wait = parsedate_to_datetime(value).timestamp() - now
            except (TypeError, ValueError, IndexError):
                wait = None
    if wait is None:
        reset = (r.headers.get("X-RateLimit-Reset") or "").strip()
        try:
            n = float(reset)
            wait = n - now if n > 1_000_000_000 else n
        except ValueError:
            wait = None
    if wait is None:
        wait = DEFAULT_WAIT
    return max(0.0, min(MAX_WAIT, wait))


class Pacer:
    """Spaces requests evenly so a client stays under a per-minute limit.

    Thread-safe: each caller reserves the next free slot under a lock, then
    sleeps outside it. per_minute <= 0 turns pacing off.
    """

    def __init__(self, per_minute: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> float:
        if not self.interval:
            return 0.0
        with self._lock:
            now = self.clock()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            self.sleep(delay)
        return delay


def _check(r: httpx.Response) -> None:
    """raise_for_status, but put the server's own error message in the exception and the log."""
    if r.is_error:
        detail = r.text.strip().replace("\n", " ")[:500]
        log.error("%s %s -> %s: %s", r.request.method, r.request.url, r.status_code, detail or "(no body)")
        raise httpx.HTTPStatusError(f"{r.status_code} from {r.request.method} {r.request.url.path}: {detail}",
                                    request=r.request, response=r)


def _results(data):
    """Plane list endpoints return either a list or {"results": [...]}."""
    if isinstance(data, list):
        return data
    return data.get("results", [])


class PlaneClient:
    def __init__(self, base_url: str, workspace: str, api_key: str,
                 transport: httpx.BaseTransport | None = None,
                 rate_limit: float = 0, max_retries: int = 3, sleep=time.sleep,
                 clock=time.monotonic):
        """rate_limit: requests per minute to stay under (0 = no pacing).
        max_retries: how often a request answered 429 is retried before giving up."""
        self.ws = workspace
        self.http = httpx.Client(
            base_url=f"{base_url}/api/v1/workspaces/{workspace}",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=20, transport=transport)
        self.pacer = Pacer(rate_limit, clock=clock, sleep=sleep)
        self.max_retries = max_retries
        self.sleep = sleep
        self._projects: dict[str, tuple[float, dict]] = {}
        self._states: dict[str, list[dict]] = {}
        self._modules: dict[str, tuple[float, list[dict]]] = {}
        # Module and project descriptions hold repo links people edit in Plane,
        # so they're only cached briefly.
        self.cache_seconds = 60.0
        # Plane's public API doesn't document module links; set to False the first
        # time the endpoint turns out not to exist, so we stop asking.
        self.module_links_supported: bool | None = None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send a request, paced, retrying on 429 as Plane's headers say.

        After max_retries the 429 response is returned, so _check raises as usual
        and the caller (for example the catch-up sync) can stop for now. Retrying a
        write is safe: Plane rejected it without doing anything.
        """
        for attempt in range(self.max_retries + 1):
            self.pacer.wait()
            r = self.http.request(method, path, **kwargs)
            if r.status_code != 429 or attempt == self.max_retries:
                return r
            wait = retry_after(r)
            log.warning("Plane rate limit: %s %s answered 429; waiting %.0fs (retry %d of %d)",
                        method, r.request.url.path, wait, attempt + 1, self.max_retries)
            self.sleep(wait)
        return r  # not reached

    def _get(self, path: str, **params):
        r = self._request("GET", path, params=params or None)
        _check(r)
        return r.json()

    def _paged(self, path: str) -> list[dict]:
        out, cursor = [], None
        for _ in range(50):  # hard stop; 50 pages is far more than we expect
            data = self._get(path, per_page=100, **({"cursor": cursor} if cursor else {}))
            out.extend(_results(data))
            if isinstance(data, list) or not data.get("next_page_results"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return out

    def check_workspace(self) -> str | None:
        """None if the API key can see the workspace; otherwise a message saying why not."""
        try:
            r = self._request("GET", "/projects/", params={"per_page": 1})
        except httpx.HTTPError as e:
            return f"can't reach Plane's API ({e}); check PLANE_BASE_URL"
        if r.status_code in (401,):
            return "Plane rejected PLANE_API_KEY (401); check the token"
        if r.status_code in (403, 404):
            return (f"Plane refused workspace '{self.ws}' ({r.status_code}). PLANE_WORKSPACE must match "
                    f"the slug in your Plane URL: https://<plane>/<slug>/projects/...")
        if r.is_error:
            return f"Plane's API answered {r.status_code} for workspace '{self.ws}'"
        return None

    def projects(self) -> list[dict]:
        return self._paged("/projects/")

    def module_item_ids(self, project_id: str, module_id: str) -> list[str]:
        return [m.get("id") or m.get("issue")
                for m in self._paged(f"/projects/{project_id}/modules/{module_id}/module-issues/")]

    def project(self, project_id: str) -> dict:
        hit = self._projects.get(project_id)
        if not hit or time.monotonic() - hit[0] > self.cache_seconds:
            hit = (time.monotonic(), self._get(f"/projects/{project_id}/"))
            self._projects[project_id] = hit
        return hit[1]

    def modules(self, project_id: str) -> list[dict]:
        hit = self._modules.get(project_id)
        if not hit or time.monotonic() - hit[0] > self.cache_seconds:
            hit = (time.monotonic(), self._paged(f"/projects/{project_id}/modules/"))
            self._modules[project_id] = hit
        return hit[1]

    def work_item(self, project_id: str, item_id: str) -> dict:
        return self._get(f"/projects/{project_id}/issues/{item_id}/")

    def states(self, project_id: str) -> list[dict]:
        if project_id not in self._states:
            self._states[project_id] = self._paged(f"/projects/{project_id}/states/")
        return self._states[project_id]

    def module_id(self, project_id: str, module_name: str) -> str | None:
        for m in self.modules(project_id):
            if m.get("name", "").strip().lower() == module_name.strip().lower():
                return m["id"]
        # The module may have been created after we cached the list.
        self._modules.pop(project_id, None)
        return None

    def item_in_module(self, project_id: str, module_id: str, item: dict) -> bool:
        ids = item.get("module_ids") or item.get("modules")
        if isinstance(ids, list) and ids and isinstance(ids[0], str):
            return module_id in ids
        members = self._paged(f"/projects/{project_id}/modules/{module_id}/module-issues/")
        return any((m.get("id") or m.get("issue")) == item["id"] for m in members)

    def ensure_module_link(self, project_id: str, module_id: str, url: str, title: str) -> str:
        """Add url to the module's Links section if it isn't there.

        Returns "added", "exists" or "unsupported" (this Plane's API has no module links).
        """
        if self.module_links_supported is False:
            return "unsupported"
        path = f"/projects/{project_id}/modules/{module_id}/module-links/"
        r = self._request("GET", path)
        if r.status_code in (404, 405):
            self.module_links_supported = False
            log.info("this Plane's API has no module links endpoint; skipping module links")
            return "unsupported"
        _check(r)
        self.module_links_supported = True
        wanted = url.rstrip("/").lower()
        if any((l.get("url") or "").rstrip("/").lower() == wanted for l in _results(r.json())):
            return "exists"
        r = self._request("POST", path, json={"url": url, "title": title})
        _check(r)
        return "added"

    def update_module(self, project_id: str, module_id: str, **fields) -> None:
        r = self._request("PATCH", f"/projects/{project_id}/modules/{module_id}/", json=fields)
        _check(r)
        self._modules.pop(project_id, None)

    def set_state(self, project_id: str, item_id: str, state_id: str) -> None:
        r = self._request("PATCH", f"/projects/{project_id}/issues/{item_id}/", json={"state": state_id})
        _check(r)

    def add_comment(self, project_id: str, item_id: str, html: str) -> None:
        r = self._request("POST", f"/projects/{project_id}/issues/{item_id}/comments/",
                           json={"comment_html": html})
        _check(r)

    def add_link(self, project_id: str, item_id: str, url: str, title: str) -> None:
        r = self._request("POST", f"/projects/{project_id}/issues/{item_id}/links/",
                           json={"url": url, "title": title})
        if r.status_code == 400 and "already" in r.text.lower():
            return  # Plane rejects duplicate URLs; that's fine
        _check(r)


def _github_rate_limited(r: httpx.Response) -> bool:
    """A 429, or a 403 that is GitHub's (secondary) rate limit rather than a permission error."""
    if r.status_code == 429:
        return True
    if r.status_code != 403:
        return False
    if r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Remaining") == "0":
        return True
    return "rate limit" in r.text.lower()


class GitHubClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=20, transport=transport)
        self.sleep = sleep

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send a request; on a rate limit, wait as GitHub says (capped) and retry once."""
        r = self.http.request(method, path, **kwargs)
        if _github_rate_limited(r):
            wait = retry_after(r)
            log.warning("GitHub rate limit: %s %s answered %s; waiting %.0fs and retrying once",
                        method, r.request.url.path, r.status_code, wait)
            self.sleep(wait)
            r = self.http.request(method, path, **kwargs)
        return r

    def get_repo(self, repo: str) -> dict | None:
        """Repo metadata (name, description, html_url), or None if the token can't see it."""
        r = self._request("GET", f"/repos/{repo}")
        if r.status_code in (403, 404):
            return None
        _check(r)
        return r.json()

    def create_issue(self, repo: str, title: str, body: str) -> dict:
        r = self._request("POST", f"/repos/{repo}/issues", json={"title": title, "body": body})
        _check(r)
        return r.json()

    def update_issue(self, repo: str, number: int, **fields) -> None:
        r = self._request("PATCH", f"/repos/{repo}/issues/{number}", json=fields)
        _check(r)
