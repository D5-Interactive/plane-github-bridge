"""How the bridge signs in to GitHub: as a GitHub App (preferred) or with a personal access token.

A GitHub App has an ID and a private key. With them the bridge signs a short JWT,
asks GitHub for an *installation token* for the account that owns a repo, and
uses that token (valid for an hour) for API calls on that account's repos.
Tokens are cached per account until a few minutes before they expire.

A personal access token is the fallback: used when no app is configured, or for
an account the app isn't installed on.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime

import httpx
import jwt

log = logging.getLogger("bridge")

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# What the app needs and nothing more. Checks are for check runs on commits and
# PRs; Contents read lets it see commits and branches.
APP_PERMISSIONS = {
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
    "contents": "read",
    "metadata": "read",
}
APP_EVENTS = ["issues", "pull_request", "pull_request_review", "push", "create"]


@dataclass
class AppCredentials:
    app_id: str
    private_key: str           # PEM text
    webhook_secret: str = ""
    slug: str = ""             # the app's URL name; its bot user is "<slug>[bot]"
    html_url: str = ""         # https://github.com/apps/<slug>
    source: str = "env"        # "env" (bring your own) or "setup" (made by /setup)


class CredentialFile:
    """The app made by /setup, kept next to the database so it survives restarts."""

    def __init__(self, directory: str):
        self.path = os.path.join(directory, "github-app.json")

    def load(self) -> AppCredentials | None:
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return None
        return AppCredentials(**{k: raw.get(k, "") for k in AppCredentials.__dataclass_fields__})

    def save(self, creds: AppCredentials) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(creds), f)
        os.replace(tmp, self.path)


def app_jwt(app_id: str, private_key: str, now: float | None = None) -> str:
    """The short-lived JWT that proves the bridge holds the app's private key.

    Backdated a minute for clock drift; GitHub refuses lifetimes over 10 minutes.
    """
    now = int(now if now is not None else time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                      private_key, algorithm="RS256")


def _expiry(stamp: str | None, now: float) -> float:
    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return now + 3600   # GitHub's documented lifetime


class AppTokens:
    """Installation tokens for one GitHub App, one per account (org or user)."""

    # Refresh this many seconds before GitHub's expiry, so a token never runs out mid-request.
    margin = 300.0

    def __init__(self, creds: AppCredentials, transport: httpx.BaseTransport | None = None,
                 clock=time.time):
        self.creds = creds
        self.clock = clock
        self.http = httpx.Client(base_url=API, headers=HEADERS, timeout=20, transport=transport)
        self._lock = threading.Lock()
        self._installations: dict[str, int | None] = {}     # owner -> installation id (None = not installed)
        self._tokens: dict[str, tuple[str, float]] = {}     # owner -> (token, expires_at)

    def jwt(self) -> str:
        return app_jwt(self.creds.app_id, self.creds.private_key, self.clock())

    def _app_get(self, path: str) -> httpx.Response:
        return self.http.get(path, headers={"Authorization": f"Bearer {self.jwt()}"})

    def check(self) -> str | None:
        """None if GitHub accepts the app's key; otherwise a message saying why not."""
        try:
            r = self._app_get("/app")
        except httpx.HTTPError as e:
            return f"can't reach GitHub's API ({e})"
        if r.status_code == 401:
            return (f"GitHub rejected the app's private key (401). Check that it belongs to app "
                    f"{self.creds.app_id} and that the server's clock is right")
        if r.is_error:
            return f"GitHub answered {r.status_code} for app {self.creds.app_id}"
        meta = r.json()
        self.creds.slug = self.creds.slug or meta.get("slug", "")
        self.creds.html_url = self.creds.html_url or meta.get("html_url", "")
        return None

    def _installation_id(self, owner: str, repo: str) -> int | None:
        key = owner.lower()
        if key not in self._installations:
            r = self._app_get(f"/repos/{owner}/{repo}/installation")
            if r.status_code == 404:
                log.warning("GitHub App isn't installed on %s; install it, or that account falls back "
                            "to GITHUB_TOKEN if set", owner)
                self._installations[key] = None
            elif r.is_error:
                raise httpx.HTTPStatusError(f"{r.status_code} looking up the app's installation on {owner}",
                                            request=r.request, response=r)
            else:
                self._installations[key] = r.json()["id"]
        return self._installations[key]

    def token_for(self, owner: str, repo: str) -> str | None:
        """An installation token for owner's repos, or None if the app isn't installed there."""
        key = owner.lower()
        with self._lock:
            hit = self._tokens.get(key)
            now = self.clock()
            if hit and hit[1] - self.margin > now:
                return hit[0]
            inst = self._installation_id(owner, repo)
            if inst is None:
                return None
            r = self.http.post(f"/app/installations/{inst}/access_tokens",
                               headers={"Authorization": f"Bearer {self.jwt()}"})
            if r.status_code == 404:          # app was uninstalled since we looked
                self._installations.pop(key, None)
                return None
            if r.is_error:
                raise httpx.HTTPStatusError(f"{r.status_code} minting an installation token for {owner}",
                                            request=r.request, response=r)
            data = r.json()
            self._tokens[key] = (data["token"], _expiry(data.get("expires_at"), now))
            log.debug("minted installation token for %s", owner)
            return data["token"]

    def forget(self, owner: str) -> None:
        with self._lock:
            self._tokens.pop(owner.lower(), None)
            self._installations.pop(owner.lower(), None)


_REPO_PATH = re.compile(r"^/repos/([^/]+)/([^/]+)")


class GitHubAuth(httpx.Auth):
    """Puts the right token on every GitHub API request.

    Repo calls (/repos/owner/name/...) use the app's installation token for that
    owner when an app is set up and installed there, and the personal access token
    otherwise. The app can be switched on at runtime (after /setup) without a restart.
    """

    def __init__(self, pat: str = "", app: AppTokens | None = None):
        self.pat = pat
        self.app = app

    @property
    def mode(self) -> str:
        return "app" if self.app else ("token" if self.pat else "none")

    def use_app(self, app: AppTokens) -> None:
        self.app = app

    def _token(self, path: str) -> tuple[str | None, str | None]:
        """(token, owner it was minted for, or None for the PAT)."""
        m = _REPO_PATH.match(path)
        if self.app and m:
            token = self.app.token_for(m.group(1), m.group(2))
            if token:
                return token, m.group(1)
        return (self.pat or None), None

    def auth_flow(self, request: httpx.Request):
        token, owner = self._token(request.url.path)
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        # A revoked or rotated installation token: mint a fresh one and try once more.
        if response.status_code == 401 and owner and self.app:
            self.app.forget(owner)
            token, owner = self._token(request.url.path)
            if token:
                request.headers["Authorization"] = f"Bearer {token}"
                yield request


def load_private_key(path: str) -> str:
    try:
        with open(path) as f:
            key = f.read()
    except OSError as e:
        raise RuntimeError(f"Can't read GITHUB_APP_PRIVATE_KEY_PATH {path!r}: {e.strerror}") from None
    if "PRIVATE KEY" not in key:
        raise RuntimeError(f"GITHUB_APP_PRIVATE_KEY_PATH {path!r} doesn't look like a PEM private key")
    return key


def convert_manifest(code: str, transport: httpx.BaseTransport | None = None) -> AppCredentials:
    """Finish GitHub's manifest flow: trade the one-time code for the new app's credentials."""
    with httpx.Client(base_url=API, headers=HEADERS, timeout=20, transport=transport) as http:
        r = http.post(f"/app-manifests/{code}/conversions")
    if r.is_error:
        raise RuntimeError(f"GitHub refused the setup code ({r.status_code}); it's single-use and "
                           f"expires after an hour. Start again from /setup")
    d = r.json()
    return AppCredentials(app_id=str(d["id"]), private_key=d["pem"],
                          webhook_secret=d.get("webhook_secret") or "", slug=d.get("slug", ""),
                          html_url=d.get("html_url", ""), source="setup")


def manifest(name: str, public_url: str) -> dict:
    """The app GitHub creates when someone clicks the button on /setup."""
    return {
        "name": name,
        "url": public_url,
        "description": "Syncs Plane work items with GitHub issues, pull requests and commits.",
        "hook_attributes": {"url": f"{public_url}/github/webhook", "active": True},
        "redirect_url": f"{public_url}/setup/callback",
        "public": False,
        "default_permissions": APP_PERMISSIONS,
        "default_events": APP_EVENTS,
    }
