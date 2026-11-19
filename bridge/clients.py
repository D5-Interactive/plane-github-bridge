"""Thin REST clients for Plane (v1 API) and GitHub."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("bridge")


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
                 transport: httpx.BaseTransport | None = None):
        self.ws = workspace
        self.http = httpx.Client(
            base_url=f"{base_url}/api/v1/workspaces/{workspace}",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=20, transport=transport)
        self._projects: dict[str, tuple[float, dict]] = {}
        self._states: dict[str, list[dict]] = {}
        self._modules: dict[str, tuple[float, list[dict]]] = {}
        # Module and project descriptions hold repo links people edit in Plane,
        # so they're only cached briefly.
        self.cache_seconds = 60.0
        # Plane's public API doesn't document module links; set to False the first
        # time the endpoint turns out not to exist, so we stop asking.
        self.module_links_supported: bool | None = None

    def _get(self, path: str, **params):
        r = self.http.get(path, params=params or None)
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
            r = self.http.get("/projects/", params={"per_page": 1})
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
        r = self.http.get(path)
        if r.status_code in (404, 405):
            self.module_links_supported = False
            log.info("this Plane's API has no module links endpoint; skipping module links")
            return "unsupported"
        _check(r)
        self.module_links_supported = True
        wanted = url.rstrip("/").lower()
        if any((l.get("url") or "").rstrip("/").lower() == wanted for l in _results(r.json())):
            return "exists"
        r = self.http.post(path, json={"url": url, "title": title})
        _check(r)
        return "added"

    def update_module(self, project_id: str, module_id: str, **fields) -> None:
        r = self.http.patch(f"/projects/{project_id}/modules/{module_id}/", json=fields)
        _check(r)
        self._modules.pop(project_id, None)

    def set_state(self, project_id: str, item_id: str, state_id: str) -> None:
        r = self.http.patch(f"/projects/{project_id}/issues/{item_id}/", json={"state": state_id})
        _check(r)

    def add_comment(self, project_id: str, item_id: str, html: str) -> None:
        r = self.http.post(f"/projects/{project_id}/issues/{item_id}/comments/",
                           json={"comment_html": html})
        _check(r)

    def add_link(self, project_id: str, item_id: str, url: str, title: str) -> None:
        r = self.http.post(f"/projects/{project_id}/issues/{item_id}/links/",
                           json={"url": url, "title": title})
        if r.status_code == 400 and "already" in r.text.lower():
            return  # Plane rejects duplicate URLs; that's fine
        _check(r)


class GitHubClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=20, transport=transport)

    def get_repo(self, repo: str) -> dict | None:
        """Repo metadata (name, description, html_url), or None if the token can't see it."""
        r = self.http.get(f"/repos/{repo}")
        if r.status_code in (403, 404):
            return None
        _check(r)
        return r.json()

    def create_issue(self, repo: str, title: str, body: str) -> dict:
        r = self.http.post(f"/repos/{repo}/issues", json={"title": title, "body": body})
        _check(r)
        return r.json()

    def update_issue(self, repo: str, number: int, **fields) -> None:
        r = self.http.patch(f"/repos/{repo}/issues/{number}", json=fields)
        _check(r)
