"""Thin REST clients for Plane (v1 API) and GitHub."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("bridge")


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
        self._projects: dict[str, dict] = {}
        self._states: dict[str, list[dict]] = {}
        self._modules: dict[str, list[dict]] = {}

    def _get(self, path: str, **params):
        r = self.http.get(path, params=params or None)
        r.raise_for_status()
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

    def project(self, project_id: str) -> dict:
        if project_id not in self._projects:
            self._projects[project_id] = self._get(f"/projects/{project_id}/")
        return self._projects[project_id]

    def work_item(self, project_id: str, item_id: str) -> dict:
        return self._get(f"/projects/{project_id}/issues/{item_id}/")

    def states(self, project_id: str) -> list[dict]:
        if project_id not in self._states:
            self._states[project_id] = self._paged(f"/projects/{project_id}/states/")
        return self._states[project_id]

    def module_id(self, project_id: str, module_name: str) -> str | None:
        if project_id not in self._modules:
            self._modules[project_id] = self._paged(f"/projects/{project_id}/modules/")
        for m in self._modules[project_id]:
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

    def set_state(self, project_id: str, item_id: str, state_id: str) -> None:
        r = self.http.patch(f"/projects/{project_id}/issues/{item_id}/", json={"state": state_id})
        r.raise_for_status()

    def add_comment(self, project_id: str, item_id: str, html: str) -> None:
        r = self.http.post(f"/projects/{project_id}/issues/{item_id}/comments/",
                           json={"comment_html": html})
        r.raise_for_status()

    def add_link(self, project_id: str, item_id: str, url: str, title: str) -> None:
        r = self.http.post(f"/projects/{project_id}/issues/{item_id}/links/",
                           json={"url": url, "title": title})
        if r.status_code == 400 and "already" in r.text.lower():
            return  # Plane rejects duplicate URLs; that's fine
        r.raise_for_status()


class GitHubClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=20, transport=transport)

    def create_issue(self, repo: str, title: str, body: str) -> dict:
        r = self.http.post(f"/repos/{repo}/issues", json={"title": title, "body": body})
        r.raise_for_status()
        return r.json()

    def update_issue(self, repo: str, number: int, **fields) -> None:
        r = self.http.patch(f"/repos/{repo}/issues/{number}", json=fields)
        r.raise_for_status()
