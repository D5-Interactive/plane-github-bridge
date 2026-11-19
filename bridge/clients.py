"""Thin REST clients for Plane (v1 API) and GitHub."""
from __future__ import annotations

import contextvars
import logging
import math
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger("bridge")

# Longest single wait for a rate limit, and the wait when the server gives no hint.
MAX_WAIT = 60.0
DEFAULT_WAIT = 10.0


def server_wait(r: httpx.Response, now: float | None = None) -> float:
    """Seconds the server asks us to wait before retrying, uncapped (never negative).

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
    return max(0.0, wait)


def retry_after(r: httpx.Response, now: float | None = None) -> float:
    """server_wait, capped at MAX_WAIT."""
    return min(MAX_WAIT, server_wait(r, now))


LANE_INTERACTIVE = "interactive"
LANE_BACKGROUND = "background"
_LANE: contextvars.ContextVar[str] = contextvars.ContextVar("bridge_pacer_lane", default=LANE_INTERACTIVE)


@contextmanager
def lane(name: str):
    """Run the calls inside this block in a pacing lane.

    "interactive" (the default) is for webhook work that someone is waiting on.
    "background" is for scans and catch-up passes: they only use part of the
    remaining budget and step aside whenever an interactive call is waiting.
    """
    if name not in (LANE_INTERACTIVE, LANE_BACKGROUND):
        raise ValueError(f"unknown pacing lane {name!r}")
    token = _LANE.set(name)
    try:
        yield
    finally:
        _LANE.reset(token)


def current_lane() -> str:
    return _LANE.get()


def _header(headers, *names: str) -> str:
    for n in names:
        v = headers.get(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return ""


class Pacer:
    """Spaces requests so a client stays under its rate limit.

    Two sources for the pace:
    ▸ configured: per_minute requests a minute, evenly spaced (0 = no pacing);
    ▸ learned: once a response carries rate-limit headers (Plane's
      X-RateLimit-Remaining/-Reset, GitHub's x-ratelimit-*), the remaining budget
      is spread over the time left until the window resets. Stale headers (the
      window has reset) fall back to the configured pace.

    Two lanes (see lane()): background calls may only use (1 - reserve_share) of
    the remaining budget and yield to waiting interactive calls, so scans can't
    starve webhook work.

    Thread-safe: callers reserve a slot under a lock and sleep outside it.
    """

    def __init__(self, per_minute: float, clock=time.monotonic, sleep=time.sleep,
                 wall=time.time, reserve_share: float = 0.3, floor: float = 0.05,
                 burst: int = 4, resource: str = "core"):
        """per_minute: configured pace, used until headers are seen (0 = none).
        reserve_share: share of the learned budget kept for interactive calls.
        floor: shortest spacing ever used when pacing from headers.
        burst: interactive calls that may go back to back when the learned budget
        is well ahead of schedule (the spacing still holds over time).
        resource: GitHub's x-ratelimit-resource this pacer tracks ("core");
        headers for other resources are recorded for /health but don't pace.
        """
        self.interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.reserve_share = min(max(reserve_share, 0.0), 0.95)
        self.floor = max(floor, 0.0)
        self.burst = max(int(burst), 0)
        self.resource = resource
        self._lock = threading.Lock()
        self._next = 0.0            # theoretical time of the next slot
        self._hold_until = 0.0      # after a 429: nobody sends before this
        # Learned from headers
        self._limit: int | None = None
        self._remaining: int | None = None
        self._reset_at: float | None = None      # on self.clock's scale
        self._others: dict[str, dict] = {}       # other GitHub resources, for /health
        self._waiting = {LANE_INTERACTIVE: 0, LANE_BACKGROUND: 0}

    # ------------------------------------------------------------ learning

    def observe(self, headers) -> None:
        """Learn the budget from a response's rate-limit headers (if it has any)."""
        remaining = _header(headers, "X-RateLimit-Remaining", "x-ratelimit-remaining")
        reset = _header(headers, "X-RateLimit-Reset", "x-ratelimit-reset")
        if not remaining or not reset:
            return
        try:
            remaining_n = int(float(remaining))
            reset_n = float(reset)
        except ValueError:
            return
        limit = _header(headers, "X-RateLimit-Limit", "x-ratelimit-limit")
        try:
            limit_n = int(float(limit)) if limit else None
        except ValueError:
            limit_n = None
        resource = _header(headers, "x-ratelimit-resource") or self.resource
        with self._lock:
            now = self.clock()
            # Epoch seconds, or (if small) seconds until the reset.
            reset_at = now + (reset_n - self.wall() if reset_n > 1_000_000_000 else reset_n)
            if resource != self.resource:
                self._others[resource] = {"limit": limit_n, "remaining": remaining_n,
                                          "reset_in": round(max(0.0, reset_at - now), 1)}
                return
            if reset_at <= now:
                return          # already stale
            same_window = (self._reset_at is not None and abs(reset_at - self._reset_at) < 2.0
                           and self._reset_at > now)
            if same_window and self._remaining is not None:
                # Concurrent responses arrive out of order: within one window the
                # budget only goes down.
                remaining_n = min(remaining_n, self._remaining)
                limit_n = limit_n or self._limit
            self._reset_at = reset_at
            self._remaining = max(remaining_n, 0)
            seen = self._limit if same_window else None
            self._limit = limit_n or max(seen or 0, remaining_n + 1)

    def _fresh(self, now: float) -> bool:
        if self._reset_at is not None and now >= self._reset_at:
            # The window reset: what we learned no longer applies.
            self._remaining = self._reset_at = None
            self._limit = None
        return self._reset_at is not None

    def _reserve(self) -> int:
        return int(math.ceil(self.reserve_share * (self._limit or self._remaining or 0)))

    def _plan(self, now: float, which: str) -> tuple[float, float, float]:
        """(interval, earliest start from the budget, burst tolerance) for a lane."""
        if not self._fresh(now):
            return self.interval, now, 0.0
        left = self._reset_at - now
        budget = self._remaining if which == LANE_INTERACTIVE else self._remaining - self._reserve()
        if budget <= 0:
            # Nothing left for this lane: wait for the window to reset.
            return max(self.floor, self.interval), self._reset_at, 0.0
        interval = max(self.floor, left / budget)
        # GCRA: a tolerance of (burst - 1) slots lets `burst` calls go back to back.
        tolerance = interval * max(self.burst - 1, 0) if which == LANE_INTERACTIVE else 0.0
        return interval, now, tolerance

    def _hold_start(self, now: float, honor_hold: bool, own_hold: float | None) -> float:
        if not honor_hold:
            return now
        if own_hold is not None and self._hold_until <= own_hold:
            return now
        return max(now, self._hold_until)

    def _take(self, now: float, start: float, interval: float) -> None:
        if interval:
            self._next = max(self._next, start) + interval
        if self._remaining is not None and self._fresh(now):
            self._remaining = max(self._remaining - 1, 0)

    # ------------------------------------------------------------ holds and waits

    def hold(self, seconds: float) -> float:
        """After a 429, make every caller wait until the server's window has passed,
        not just the thread that got it. Returns the end of this hold."""
        with self._lock:
            until = self.clock() + seconds
            self._hold_until = max(self._hold_until, until)
            return until

    def wait(self, honor_hold: bool = True, own_hold: float | None = None) -> float:
        """Wait for this caller's slot, in the current lane. Returns seconds waited.

        own_hold: the end of a hold this caller set and has already slept through.
        It still waits for any longer hold another thread set since.
        """
        if current_lane() == LANE_BACKGROUND:
            return self._wait_background(honor_hold, own_hold)
        with self._lock:
            now = self.clock()
            interval, budget_start, tolerance = self._plan(now, LANE_INTERACTIVE)
            start = max(self._hold_start(now, honor_hold, own_hold), budget_start)
            if interval:
                start = max(start, self._next - tolerance)
            self._take(now, start, interval)
            delay = start - now
            if delay > 0:
                self._waiting[LANE_INTERACTIVE] += 1
        if delay > 0:
            try:
                self.sleep(delay)
            finally:
                with self._lock:
                    self._waiting[LANE_INTERACTIVE] -= 1
        return max(delay, 0.0)

    def _wait_background(self, honor_hold: bool, own_hold: float | None) -> float:
        """Background calls don't reserve future slots: they wait until a slot is
        free now and no interactive call is waiting, then take it."""
        waited = 0.0
        with self._lock:
            self._waiting[LANE_BACKGROUND] += 1
        try:
            for _ in range(10_000):
                with self._lock:
                    now = self.clock()
                    interval, budget_start, _tol = self._plan(now, LANE_BACKGROUND)
                    start = max(self._hold_start(now, honor_hold, own_hold), budget_start)
                    if interval:
                        start = max(start, self._next)
                    if start <= now and self._waiting[LANE_INTERACTIVE] == 0:
                        self._take(now, now, interval)
                        return waited
                    delay = start - now if start > now else max(self.floor, interval, 0.01)
                self.sleep(delay)
                waited += delay
            # Safety valve (a frozen test clock): go anyway rather than spin forever.
            with self._lock:
                self._take(self.clock(), self.clock(), self.interval)
            return waited
        finally:
            with self._lock:
                self._waiting[LANE_BACKGROUND] -= 1

    # ------------------------------------------------------------ reporting

    def snapshot(self) -> dict:
        """The pacer's current view, for /health."""
        with self._lock:
            now = self.clock()
            fresh = self._fresh(now)
            interval, _s, _t = self._plan(now, LANE_INTERACTIVE)
            bg_interval, bg_start, _t = self._plan(now, LANE_BACKGROUND)
            out = {
                "source": "headers" if fresh else ("configured" if self.interval else "off"),
                "limit": self._limit if fresh else None,
                "remaining": self._remaining if fresh else None,
                "reset_in": round(self._reset_at - now, 1) if fresh else None,
                "interval": round(interval, 3),
                "background_interval": round(bg_interval, 3),
                "background_blocked_for": round(max(0.0, bg_start - now), 1),
                "reserve_share": self.reserve_share,
                "hold_in": round(max(0.0, self._hold_until - now), 1),
                "waiting": dict(self._waiting),
            }
            if self._others:
                out["other_resources"] = {k: dict(v) for k, v in self._others.items()}
            return out


def _check(r: httpx.Response) -> None:
    """raise_for_status, logging only the status, method and path.

    Error bodies can echo work item content, so they're logged at DEBUG only and
    kept out of the exception message (which tracebacks print).
    """
    if r.is_error:
        log.error("%s %s -> %s", r.request.method, r.request.url.path, r.status_code)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("error body for %s %s: %s", r.request.method, r.request.url.path,
                      r.text.strip().replace("\n", " ")[:500] or "(no body)")
        raise httpx.HTTPStatusError(f"{r.status_code} from {r.request.method} {r.request.url.path}",
                                    request=r.request, response=r)


def plane_not_found(r: httpx.Response) -> bool:
    """A 404 that came from Plane's API (a JSON error body), not from a proxy in front of it."""
    if r.status_code != 404:
        return False
    try:
        return isinstance(r.json(), dict)
    except ValueError:
        return False


def _results(data):
    """Plane list endpoints return either a list or {"results": [...]}."""
    if isinstance(data, list):
        return data
    return data.get("results", [])


class PlaneClient:
    def __init__(self, base_url: str, workspace: str, api_key: str,
                 transport: httpx.BaseTransport | None = None,
                 rate_limit: float = 0, max_retries: int = 3, sleep=time.sleep,
                 clock=time.monotonic, wall=time.time, reserve_share: float = 0.3):
        """rate_limit: requests per minute to stay under until Plane's own rate-limit
        headers have been seen (0 = no pacing until then); after that the pace comes
        from the headers.
        max_retries: how often a request answered 429 is retried before giving up.
        reserve_share: share of the learned budget kept for interactive (webhook) calls."""
        self.ws = workspace
        self.http = httpx.Client(
            base_url=f"{base_url}/api/v1/workspaces/{workspace}",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=20, transport=transport)
        self.pacer = Pacer(rate_limit, clock=clock, sleep=sleep, wall=wall,
                           reserve_share=reserve_share)
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
        own_hold = None
        for attempt in range(self.max_retries + 1):
            self.pacer.wait(own_hold=own_hold)
            r = self.http.request(method, path, **kwargs)
            self.pacer.observe(r.headers)
            if r.status_code != 429:
                return r
            if attempt == self.max_retries:
                # Giving up, but the window is still closed: hold the others too.
                self.pacer.hold(retry_after(r))
                return r
            wait = server_wait(r)
            if wait > MAX_WAIT:
                # Longer than we'd ever sleep: give up now rather than sleep and fail anyway.
                log.warning("Plane rate limit: %s %s answered 429 and asks for %.0fs; not waiting",
                            method, r.request.url.path, wait)
                self.pacer.hold(MAX_WAIT)
                return r
            log.warning("Plane rate limit: %s %s answered 429; waiting %.0fs (retry %d of %d)",
                        method, r.request.url.path, wait, attempt + 1, self.max_retries)
            own_hold = self.pacer.hold(wait)   # other threads wait out the same window
            self.sleep(wait)                   # this thread waits it out here, once
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

    def workspace_status(self) -> tuple[str | None, bool]:
        """Check, once, that the API key can see the workspace: (problem, setup_problem).

        problem is None when all is well. setup_problem is True for mistakes in the
        configuration (wrong key or slug), which a retry won't fix, and False for
        temporary trouble (rate limit, Plane down or unreachable). One attempt only:
        this runs at startup, so it must not sit out rate-limit retries.
        """
        try:
            self.pacer.wait()
            r = self.http.request("GET", "/projects/", params={"per_page": 1})
            self.pacer.observe(r.headers)
        except httpx.HTTPError as e:
            return (f"can't reach Plane's API ({type(e).__name__}); check PLANE_BASE_URL "
                    f"if this lasts", False)
        if r.status_code == 401:
            return "Plane rejected PLANE_API_KEY (401); check the token", True
        if r.status_code in (403, 404):
            return (f"Plane refused workspace '{self.ws}' ({r.status_code}). PLANE_WORKSPACE must match "
                    f"the slug in your Plane URL: https://<plane>/<slug>/projects/...", True)
        if r.status_code == 429:
            self.pacer.hold(retry_after(r))
            return "Plane is rate-limiting this API key (429)", False
        if r.status_code >= 500:
            return f"Plane's API answered {r.status_code}", False
        if r.is_error:
            return f"Plane's API answered {r.status_code} for workspace '{self.ws}'", True
        return None, False

    def check_workspace(self) -> str | None:
        """None if the API key can see the workspace; otherwise a message saying why not."""
        return self.workspace_status()[0]

    def item_gone(self, project_id: str, item_id: str) -> bool:
        """True only if Plane itself says the work item doesn't exist.

        A 404 from a proxy (an HTML page while Plane restarts) doesn't count; nor
        does any other answer. Rate limits and other errors raise as usual.
        """
        r = self._request("GET", f"/projects/{project_id}/issues/{item_id}/")
        if plane_not_found(r):
            return True
        if r.status_code == 404:
            return False
        _check(r)
        return False

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
                 sleep=time.sleep, clock=time.monotonic, wall=time.time,
                 reserve_share: float = 0.3):
        self.http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=20, transport=transport)
        self.sleep = sleep
        # No configured pace: GitHub's x-ratelimit-* headers (core resource) set it.
        self.pacer = Pacer(0, clock=clock, sleep=sleep, wall=wall, reserve_share=reserve_share)

    def _send(self, method: str, path: str, own_hold: float | None = None, **kwargs) -> httpx.Response:
        self.pacer.wait(own_hold=own_hold)
        r = self.http.request(method, path, **kwargs)
        self.pacer.observe(r.headers)
        return r

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send a request, paced by GitHub's rate-limit headers; on a rate limit, wait
        as GitHub says (capped) and retry once. Other threads wait out the same window."""
        r = self._send(method, path, **kwargs)
        if _github_rate_limited(r):
            wait = server_wait(r)
            if wait > MAX_WAIT:     # e.g. the hourly primary limit: don't sleep, fail now
                log.warning("GitHub rate limit: %s %s answered %s and asks for %.0fs; not waiting",
                            method, r.request.url.path, r.status_code, wait)
                self.pacer.hold(MAX_WAIT)
                return r
            log.warning("GitHub rate limit: %s %s answered %s; waiting %.0fs and retrying once",
                        method, r.request.url.path, r.status_code, wait)
            own_hold = self.pacer.hold(wait)
            self.sleep(wait)
            r = self._send(method, path, own_hold=own_hold, **kwargs)
        return r

    def get_repo(self, repo: str) -> dict | None:
        """Repo metadata (name, description, html_url), or None if the token can't see it."""
        r = self._request("GET", f"/repos/{repo}")
        if _github_rate_limited(r):
            _check(r)           # a rate limit isn't "can't see the repo": raise, so the pass stops
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
