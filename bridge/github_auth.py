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
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

log = logging.getLogger("bridge")

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# The permissions the app asks for. The bridge uses Issues, Pull requests and
# Metadata today. Checks (write) and Contents (read) are reserved: asked for now
# so a later version doesn't need every installation to approve new permissions.
APP_PERMISSIONS = {
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
    "contents": "read",
    "metadata": "read",
}
APP_EVENTS = ["issues", "pull_request", "pull_request_review", "push", "create", "repository"]


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
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Can't read the saved GitHub App in {self.path} ({e}). Fix or delete "
                               f"the file and recreate the container; deleting it brings /setup back") from None
        if not isinstance(raw, dict) or not raw.get("app_id") or not raw.get("private_key"):
            raise RuntimeError(f"The saved GitHub App in {self.path} is missing its ID or private key. "
                               f"Delete the file and recreate the container to run /setup again")
        return AppCredentials(**{k: str(raw.get(k) or "") for k in AppCredentials.__dataclass_fields__})

    def save(self, creds: AppCredentials) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)      # O_CREAT keeps the mode of a leftover temp file
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


class AppError(Exception):
    """The GitHub App couldn't be used: bad key, clock drift, GitHub down. Message has no secrets."""


class NoGitHubCredentials(Exception):
    """No credential can sign this request right now, so it isn't sent at all.

    Raised instead of sending a request unsigned, which GitHub would answer with a
    404 for a private repo (and the catch-up sync would park as permanent). It
    isn't an HTTP error, so the sync treats it as temporary and retries.
    """


def _github_message(r: httpx.Response) -> str:
    """GitHub's own short explanation ("This installation has been suspended"), if any."""
    try:
        msg = r.json().get("message")
    except (ValueError, AttributeError):
        return ""
    return " ".join(str(msg).split())[:200] if msg else ""


class AppTokens:
    """Installation tokens for one GitHub App, one per account (org or user).

    Locking: a short-lived guard protects the caches and is never held across a
    network call. Minting for one account is serialized by that account's own
    lock, so concurrent callers share one mint; other accounts, forget() and the
    webhook handler never wait on it.
    """

    # Refresh this many seconds before GitHub's expiry, so a token never runs out mid-request.
    margin = 300.0
    # How long "the app isn't installed on this account" is believed before asking again.
    negative_ttl = 600.0
    # Right after an installation event GitHub can still answer 404 briefly; don't
    # let that stick for the full negative_ttl.
    settle_seconds = 120.0
    settle_ttl = 30.0
    # After the app fails as a whole (key can't sign, GitHub down or erroring), use
    # the personal token for this long before calling GitHub as the app again, so
    # every call doesn't pay for the failure. Still-valid cached tokens keep working.
    cooldown = 45.0
    # After GitHub refuses the app for one account (installation suspended, 401,
    # 403 or 422 on its lookup or token), skip that account for this long. Other
    # accounts are unaffected. An installation webhook for the account clears it.
    account_failure_ttl = 300.0
    # How often a failed or never-run check() is retried in the background.
    recheck_every = 300.0

    def __init__(self, creds: AppCredentials, transport: httpx.BaseTransport | None = None,
                 clock=time.time):
        self.creds = creds
        self.clock = clock
        self.http = httpx.Client(base_url=API, headers=HEADERS, timeout=20, transport=transport)
        self._guard = threading.Lock()
        self._owner_locks: dict[str, threading.Lock] = {}
        # owner -> (installation id or None = not installed, believed until; inf for positives)
        self._installations: dict[str, tuple[int | None, float]] = {}
        self._tokens: dict[str, tuple[str, float]] = {}     # owner -> (token, expires_at)
        self._forgotten_at: dict[str, float] = {}
        # Bumped by forget(), so an answer that was in flight when the account was
        # forgotten isn't cached over the reset.
        self._generation: dict[str, int] = {}
        # owner -> (why GitHub refused the app for that account, skipped until)
        self._account_failures: dict[str, tuple[str, float]] = {}
        # The latest reason the app as a whole couldn't be used; None when it works.
        # /health shows problem_now(), which adds per-account refusals.
        self.problem: str | None = None
        # Until when the app is skipped after a failure (see cooldown).
        self.cooling_until = 0.0
        # When check() last ran, and whether it passed (for maybe_recheck).
        self.checked_at: float | None = None
        self.check_ok = False

    def jwt(self) -> str:
        return app_jwt(self.creds.app_id, self.creds.private_key, self.clock())

    def _app_request(self, method: str, path: str) -> httpx.Response:
        try:
            signed = self.jwt()
        except (jwt.PyJWTError, ValueError, TypeError, UnsupportedAlgorithm) as e:
            # A malformed, encrypted or truncated key: the app can't sign anything.
            raise self._fail(f"the app's private key can't be used to sign requests "
                             f"({type(e).__name__}); replace it with the app's .pem file") from None
        try:
            return self.http.request(method, path, headers={"Authorization": f"Bearer {signed}"})
        except httpx.HTTPError as e:
            raise self._fail(f"can't reach GitHub's API ({type(e).__name__})") from None

    def cooling(self) -> bool:
        """True while the app as a whole is skipped after a failure."""
        return self.clock() < self.cooling_until

    def _fail(self, message: str) -> AppError:
        """Record why the app as a whole can't be used (pauses it for every account),
        logging only when the reason changes."""
        if message != self.problem:
            log.error("GITHUB APP PROBLEM: %s", message)
        self.problem = message
        self.cooling_until = self.clock() + self.cooldown
        return AppError(message)

    def _fail_account(self, key: str, message: str) -> AppError:
        """Record that GitHub refused the app for one account; other accounts carry on."""
        with self._guard:
            before = self._account_failures.get(key)
            self._account_failures[key] = (message, self.clock() + self.account_failure_ttl)
        if not before or before[0] != message:
            log.error("GITHUB APP PROBLEM: %s", message)
        return AppError(message)

    def _ok(self, key: str | None = None) -> None:
        """A call as the app worked: the app as a whole (and that account) is fine."""
        if self.problem:
            log.info("GitHub App working again")
        self.problem = None
        self.cooling_until = 0.0
        if key:
            with self._guard:
                dropped = self._account_failures.pop(key, None)
            if dropped:
                log.info("GitHub App working again for %s", key)

    def _account_failure(self, key: str) -> str | None:
        """Why the app is skipped for this account right now, or None."""
        with self._guard:
            hit = self._account_failures.get(key)
        return hit[0] if hit and hit[1] > self.clock() else None

    def _precheck(self, key: str) -> None:
        """Raise instead of calling GitHub while the app (or this account) is skipped.

        Called right before each network call, inside the account's lock, so
        callers queued behind a failing call don't each repeat it.
        """
        if self.cooling():
            raise AppError(self.problem or "the GitHub App is paused after a failure")
        why = self._account_failure(key)
        if why:
            raise AppError(why)

    def problem_now(self) -> str | None:
        """What /health shows: the app-wide problem, else refusals for single accounts."""
        if self.problem:
            return self.problem
        now = self.clock()
        with self._guard:
            current = sorted(m for m, until in self._account_failures.values() if until > now)
        return "; ".join(current) or None

    def _describe(self, r: httpx.Response, what: str) -> str:
        said = _github_message(r)
        said = f' ("{said}")' if said else ""
        if r.status_code == 401:
            return (f"GitHub rejected the app's private key (401{said}) while {what}. Check that it "
                    f"belongs to app {self.creds.app_id} and that the server's clock is right")
        return f"GitHub answered {r.status_code}{said} while {what}"

    def _refused(self, r: httpx.Response, key: str, what: str) -> AppError:
        """An error answer on an account's lookup or token (JWT-signed calls).

        A rate limit pauses the whole app for as long as GitHub asks (capped). A 401
        means the JWT itself was refused (wrong key, clock skew): app-wide. A 403 or
        422 is GitHub refusing the app on this one account (e.g. suspended).
        Anything else (5xx and the like) is app-wide.
        """
        from .clients import MAX_WAIT, _github_rate_limited, server_wait   # clients imports us
        if _github_rate_limited(r):
            wait = min(server_wait(r, now=self.clock()), MAX_WAIT)
            err = self._fail(f"GitHub's rate limit ({r.status_code}) while {what}; "
                             f"pausing the app for {wait:.0f}s")
            self.cooling_until = self.clock() + wait
            return err
        message = self._describe(r, what)
        if r.status_code in (403, 422):
            return self._fail_account(key, message)
        return self._fail(message)

    def _json(self, r: httpx.Response, what: str) -> dict:
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise self._fail(f"GitHub answered {r.status_code} with something that isn't JSON while {what}")
        return data

    def check(self) -> str | None:
        """None if GitHub accepts the app's key; otherwise a message saying why not."""
        self.checked_at, self.check_ok = self.clock(), False
        what = f"checking app {self.creds.app_id}"
        try:
            r = self._app_request("GET", "/app")
            if r.is_error:
                raise self._fail(self._describe(r, what))
            meta = self._json(r, what)
        except AppError as e:
            return str(e)
        self.check_ok = True
        self.creds.slug = self.creds.slug or str(meta.get("slug") or "")
        self.creds.html_url = self.creds.html_url or str(meta.get("html_url") or "")
        self._ok()
        return None

    def _owner_lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._owner_locks.setdefault(key, threading.Lock())

    def _lookup_installation(self, owner: str) -> int | None:
        """The app's installation on an account (org first, then user), or None. Network call."""
        key = owner.lower()
        what = f"looking up the app's installation on {owner}"
        for kind in ("orgs", "users"):
            self._precheck(key)
            r = self._app_request("GET", f"/{kind}/{owner}/installation")
            if r.status_code == 404:
                continue
            if r.is_error:
                raise self._refused(r, key, what)
            inst = self._json(r, what).get("id")
            if not isinstance(inst, int):
                raise self._fail(f"GitHub's answer has no installation ID while {what}")
            self._ok(key)
            return inst
        self._ok(key)                         # GitHub answered: the app itself works
        return None

    def installation_id(self, owner: str) -> int | None:
        key = owner.lower()
        now = self.clock()
        with self._guard:
            hit = self._installations.get(key)
            generation = self._generation.get(key, 0)
        if hit and hit[1] > now:
            return hit[0]
        inst = self._lookup_installation(owner)
        if inst is None:
            with self._guard:
                settling = now - self._forgotten_at.get(key, float("-inf")) < self.settle_seconds
            if not hit or hit[0] is not None:     # log once per change, not on every re-check
                log.warning("GitHub App isn't installed on %s; install it, or that account falls back "
                            "to GITHUB_TOKEN if set", owner)
            until = now + (self.settle_ttl if settling else self.negative_ttl)
        else:
            until = float("inf")
        with self._guard:
            if self._generation.get(key, 0) == generation:    # not forgotten meanwhile
                self._installations[key] = (inst, until)
        return inst

    def token_for(self, owner: str, repo: str | None = None) -> str | None:
        """An installation token for owner's repos, or None if the app isn't installed there.

        Raises AppError when the app can't be used for this account right now (bad
        key, GitHub down, installation suspended). A still-valid cached token is
        returned even while the app is paused after a failure.
        """
        key = owner.lower()
        with self._owner_lock(key):           # one mint per account at a time
            with self._guard:
                hit = self._tokens.get(key)
                generation = self._generation.get(key, 0)
            now = self.clock()
            if hit and hit[1] - self.margin > now:
                return hit[0]
            self._precheck(key)               # a caller ahead of us may just have failed
            inst = self.installation_id(owner)
            if inst is None:
                return None
            what = f"minting an installation token for {owner}"
            self._precheck(key)
            r = self._app_request("POST", f"/app/installations/{inst}/access_tokens")
            if r.status_code == 404:          # app was uninstalled since we looked
                with self._guard:
                    self._installations.pop(key, None)
                return None
            if r.is_error:
                raise self._refused(r, key, what)
            data = self._json(r, what)
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise self._fail(f"GitHub's answer has no token while {what}")
            with self._guard:
                if self._generation.get(key, 0) == generation:    # not forgotten meanwhile
                    self._tokens[key] = (token, _expiry(data.get("expires_at"), now))
            self._ok(key)
            log.debug("minted installation token for %s", owner)
            return token

    def cached_state(self, owner: str) -> str:
        """What the app knows about an account, without any network call:
        "token" (a valid cached token), "installed", "missing" (not installed, fresh
        answer), "refused" (GitHub refused the app there, or the app is paused with
        no cached token) or "unknown" (not looked up, or the answer expired)."""
        key = owner.lower()
        now = self.clock()
        with self._guard:
            tok = self._tokens.get(key)
            inst = self._installations.get(key)
            failure = self._account_failures.get(key)
        if tok and tok[1] - self.margin > now:
            return "token"
        if (failure and failure[1] > now) or self.cooling():
            return "refused"
        if inst and inst[1] > now:
            return "installed" if inst[0] is not None else "missing"
        return "unknown"

    def forget(self, owner: str) -> None:
        """Drop what's cached for an account, including a refusal (an installation
        event may mean it was unsuspended). Never waits on network calls."""
        key = owner.lower()
        with self._guard:
            self._tokens.pop(key, None)
            self._installations.pop(key, None)
            self._account_failures.pop(key, None)
            self._generation[key] = self._generation.get(key, 0) + 1
            self._forgotten_at[key] = self.clock()

    def refresh_coverage(self, owners: list[str]) -> tuple[list[str], list[str]]:
        """Look the app up on these accounts (network calls; run off the event loop).

        Returns (installed on, missing on). An account whose lookup failed is in
        neither list: coverage_now() reports it as unknown.
        """
        installed, missing = [], []
        for owner in owners:
            try:
                (installed if self.installation_id(owner) is not None else missing).append(owner)
            except AppError:
                pass
        return installed, missing

    def coverage_now(self, owners: list[str]) -> dict[str, list[str]]:
        """Where the app is installed, from the live cache; no network calls.

        "unknown" means not looked up yet, the last answer has expired, or the
        lookup failed; only a fresh "not installed" answer counts as missing.
        """
        now = self.clock()
        out: dict[str, list[str]] = {"installed": [], "missing": [], "unknown": []}
        with self._guard:
            cached = dict(self._installations)
        for owner in owners:
            hit = cached.get(owner.lower())
            if not hit or hit[1] <= now:
                out["unknown"].append(owner)
            else:
                out["installed" if hit[0] is not None else "missing"].append(owner)
        return out

    def maybe_recheck(self, owners: list[str]) -> None:
        """Background upkeep: retry a failed check() and refresh accounts whose answer
        expired or is missing. Network calls; run off the event loop."""
        now = self.clock()
        if not self.check_ok and (self.checked_at is None or now - self.checked_at >= self.recheck_every):
            self.check()
        if not self.check_ok:
            return
        stale = self.coverage_now(owners)["unknown"]
        if stale:
            self.refresh_coverage(stale)


_REPO_PATH = re.compile(r"^/repos/([^/]+)/([^/]+)")


_INTEGRATION_403 = "resource not accessible by integration"


class GitHubAuth(httpx.Auth):
    """Puts the right token on every GitHub API request.

    Repo calls (/repos/owner/name/...) use the app's installation token for that
    owner when an app is set up and installed there, and the personal access token
    otherwise: also when the app can't be used right now, or the installation can't
    reach that one repo. The app can be switched on at runtime (after /setup).

    A request is never sent unsigned: with no usable token it raises
    NoGitHubCredentials instead.
    """

    # The 403 fallback below reads the response's message.
    requires_response_body = True

    # How long "this repo needs the personal token" (the installation can't reach it)
    # is remembered, so such a repo doesn't cost two requests on every call.
    needs_pat_ttl = 600.0

    def __init__(self, pat: str = "", app: AppTokens | None = None, clock=time.time):
        self.pat = pat
        self.app = app
        self.clock = clock
        self._needs_pat: dict[str, float] = {}      # "owner/repo" -> remembered until
        self._needs_pat_lock = threading.Lock()
        # Called with (credential key, response) for every response, including a
        # re-mint or PAT-fallback resend inside one request, so the right pacer
        # learns from it. Set by GitHubClient.
        self.observer = None

    # -------- which credential a request uses (for pacing), without network calls

    @staticmethod
    def cred_key(owner: str | None) -> str:
        """The pacing key of a credential: each installation token and the personal
        token have their own rate limits."""
        return f"app:{owner.lower()}" if owner else "pat"

    def _pat_remembered(self, repo_full: str) -> bool:
        key, now = repo_full.lower(), self.clock()
        with self._needs_pat_lock:
            until = self._needs_pat.get(key)
            if until is not None and until <= now:
                del self._needs_pat[key]      # expired: forget it
                until = None
        return until is not None

    def _remember_pat(self, repo_full: str) -> None:
        now = self.clock()
        with self._needs_pat_lock:
            self._needs_pat[repo_full.lower()] = now + self.needs_pat_ttl
            if len(self._needs_pat) > 1000:   # drop expired entries now and then
                for k in [k for k, until in self._needs_pat.items() if until <= now]:
                    del self._needs_pat[k]

    def predict_key(self, path: str) -> str:
        """The credential a request to `path` will most likely use."""
        m = _REPO_PATH.match(path)
        if not (self.app and m):
            return "pat"
        owner = m.group(1)
        if self.pat and self._pat_remembered(f"{owner}/{m.group(2)}"):
            return "pat"
        state = self.app.cached_state(owner)
        if state in ("missing", "refused") and self.pat:
            return "pat"
        return self.cred_key(owner)

    def can_sign(self, owner: str) -> bool:
        """Cheap check (no network): could anything sign a request for owner's repos?
        False only when there's no personal token and the app is known to be
        unusable there (not installed, refused, or paused with no cached token)."""
        if self.pat:
            return True
        if not self.app:
            return False
        return self.app.cached_state(owner) not in ("missing", "refused")

    def forget(self, owner: str) -> None:
        """An installation changed on an account: drop cached answers for it."""
        prefix = owner.lower() + "/"
        with self._needs_pat_lock:
            for k in [k for k in self._needs_pat if k.startswith(prefix)]:
                del self._needs_pat[k]
        if self.app:
            self.app.forget(owner)

    @property
    def mode(self) -> str:
        return "app" if self.app else ("token" if self.pat else "none")

    def use_app(self, app: AppTokens) -> None:
        self.app = app

    def _token(self, path: str) -> tuple[str, str | None]:
        """(token, owner it was minted for, or None when it's the personal token).

        Raises NoGitHubCredentials rather than returning no token at all.
        """
        m = _REPO_PATH.match(path)
        why = ""
        if self.app and m and self.pat and self._pat_remembered(f"{m.group(1)}/{m.group(2)}"):
            return self.pat, None             # the installation can't reach this repo (recent answer)
        if self.app and m:
            try:
                token = self.app.token_for(m.group(1), m.group(2))
            except AppError as e:
                token, why = None, str(e)     # already logged and shown in /health
            if token:
                return token, m.group(1)
            why = why or f"the GitHub App isn't installed on {m.group(1)}"
        if self.pat:
            return self.pat, None
        if self.app:
            raise NoGitHubCredentials(f"not sending {path}: {why or 'the GitHub App only signs repo requests'}, "
                                      f"and GITHUB_TOKEN isn't set as a fallback")
        raise NoGitHubCredentials(f"not sending {path}: no GitHub App or GITHUB_TOKEN is set up yet")

    def _set(self, request: httpx.Request, token: str) -> None:
        request.headers["Authorization"] = f"Bearer {token}"

    def _sent(self, request: httpx.Request, owner: str | None, response: httpx.Response) -> None:
        key = self.cred_key(owner)
        request.extensions["bridge_cred"] = key
        if self.observer is not None:
            self.observer(key, response)

    def auth_flow(self, request: httpx.Request):
        token, owner = self._token(request.url.path)
        self._set(request, token)
        response = yield request
        self._sent(request, owner, response)
        if owner and self.app and response.status_code == 401:
            # A revoked or rotated installation token: mint a fresh one and try once more.
            self.app.forget(owner)
            token, owner = self._token(request.url.path)
            self._set(request, token)
            response = yield request
            self._sent(request, owner, response)
        if not (owner and self.app and self.pat):
            return
        if response.status_code == 404 or (response.status_code == 403
                                           and _INTEGRATION_403 in response.text.lower()):
            # 404: the installation can't see this repo (not in a "select
            # repositories" install). 403 "not accessible by integration": a public
            # repo outside the install, where reads work but writes are refused.
            # Either way the personal token may be allowed.
            self._set(request, self.pat)
            response = yield request
            self._sent(request, None, response)
            m = _REPO_PATH.match(request.url.path)
            if m and response.is_success:
                # The token reached what the installation couldn't: remember for a
                # while (not on a plain 404 both answer, such as a deleted issue).
                self._remember_pat(f"{m.group(1)}/{m.group(2)}")


def load_private_key(path: str) -> str:
    try:
        with open(path) as f:
            key = f.read()
    except OSError as e:
        raise RuntimeError(f"Can't read GITHUB_APP_PRIVATE_KEY_PATH {path!r}: {e.strerror}") from None
    if "PRIVATE KEY" not in key:
        raise RuntimeError(f"GITHUB_APP_PRIVATE_KEY_PATH {path!r} doesn't look like a PEM private key")
    try:
        parsed = serialization.load_pem_private_key(key.encode(), password=None)
    except TypeError:
        raise RuntimeError(f"GITHUB_APP_PRIVATE_KEY_PATH {path!r} is encrypted with a passphrase; "
                           f"use the unencrypted .pem GitHub generated for the app") from None
    except (ValueError, UnsupportedAlgorithm):
        raise RuntimeError(f"GITHUB_APP_PRIVATE_KEY_PATH {path!r} can't be read as a private key "
                           f"(damaged or truncated?); download a new one from the app's settings") from None
    if not isinstance(parsed, rsa.RSAPrivateKey):
        raise RuntimeError(f"GITHUB_APP_PRIVATE_KEY_PATH {path!r} isn't an RSA key; GitHub Apps use RSA keys")
    return key


def convert_manifest(code: str, transport: httpx.BaseTransport | None = None) -> AppCredentials:
    """Finish GitHub's manifest flow: trade the one-time code for the new app's credentials."""
    with httpx.Client(base_url=API, headers=HEADERS, timeout=20, transport=transport) as http:
        r = http.post(f"/app-manifests/{code}/conversions")
    if r.is_error:
        raise RuntimeError(f"GitHub refused the setup code ({r.status_code}); it's single-use and "
                           f"expires after an hour. Start again from /setup")
    d = r.json()
    if not d.get("webhook_secret"):
        log.warning("GitHub returned no webhook secret for the new app; GitHub webhooks are refused "
                    "until GITHUB_WEBHOOK_SECRET is set to the app's secret")
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
