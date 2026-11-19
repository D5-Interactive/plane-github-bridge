"""One-click GitHub App setup through GitHub's manifest flow.

1. The bridge logs a one-time link: https://<bridge>/setup?token=...
2. That page has a button that sends GitHub a description of the app (name,
   permissions, events, webhook URL). GitHub shows a confirmation page.
3. After you confirm, GitHub sends you back to /setup/callback with a code. The
   bridge trades the code for the app's ID, private key and webhook secret,
   stores them next to its database, starts using them, and sends you on to
   install the app on your org.

The setup link only works while no app is configured, and only with the token
from the log, so nobody else can attach their own app to your bridge.
"""
from __future__ import annotations

import html
import json
import logging
import re
import secrets
import threading
import time
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings
from .github_auth import AppTokens, CredentialFile, GitHubAuth, convert_manifest, manifest

log = logging.getLogger("bridge")

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
STATE_TTL = 3600.0        # GitHub's codes are valid for an hour too


# The setup pages' URLs carry the setup token or GitHub's one-time code: never
# send them to another site in a Referer header.
NO_REFERRER = {"Referrer-Policy": "no-referrer"}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status, headers=NO_REFERRER, content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)}</title>
<style>
 body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#222}}
 h1{{font-size:1.4rem}} code{{background:#f2f2f2;padding:0 .25rem;border-radius:3px}}
 label{{display:block;margin:.75rem 0 .25rem;font-weight:600}}
 input[type=text]{{width:100%;padding:.4rem;font:inherit;box-sizing:border-box}}
 button{{font:inherit;padding:.5rem 1rem;margin-top:1rem;cursor:pointer}}
 .muted{{color:#666;font-size:.9rem}} table{{border-collapse:collapse}} td{{padding:.1rem .75rem .1rem 0}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>""")


class SetupFlow:
    def __init__(self, settings: Settings, auth: GitHubAuth,
                 transport: httpx.BaseTransport | None = None, clock=time.time):
        self.s, self.auth, self.transport, self.clock = settings, auth, transport, clock
        self.token = None if auth.app else secrets.token_urlsafe(24)
        self._states: dict[str, float] = {}
        self._states_lock = threading.Lock()   # setup pages are served from several threads
        self._callback_lock = threading.Lock()

    # ------------------------------------------------------------ helpers

    def announce(self) -> None:
        if not self.token:
            return
        where = self.s.public_url or "https://<the bridge's public address>"
        log.warning("GitHub App not set up%s. To create one in a click, open: %s/setup?token=%s",
                "" if self.auth.mode == "none" else " (using GITHUB_TOKEN for now)", where, self.token)

    def _base_url(self, request: Request) -> str:
        return self.s.public_url or str(request.base_url).rstrip("/")

    @staticmethod
    def _placeholder(base: str) -> bool:
        host = (urlparse(base).hostname or "").lower()
        return host == "example.com" or host.endswith(".example.com")

    def _check_token(self, token: str | None) -> None:
        if self.auth.app or not self.token:
            raise HTTPException(404, "A GitHub App is already set up for this bridge")
        if not token or not secrets.compare_digest(token, self.token):
            raise HTTPException(403, "Open the setup link from the bridge's log (it includes ?token=...)")

    def _new_state(self) -> str:
        now = self.clock()
        state = secrets.token_urlsafe(24)
        with self._states_lock:
            self._states = {k: v for k, v in self._states.items() if v > now}
            self._states[state] = now + STATE_TTL
        return state

    def _take_state(self, state: str | None) -> bool:
        with self._states_lock:
            expires = self._states.pop(state or "", 0)
        return expires > self.clock()

    def _clear_states(self) -> None:
        with self._states_lock:
            self._states.clear()

    # ------------------------------------------------------------ routes

    def mount(self, app: FastAPI) -> None:
        @app.get("/setup", response_class=HTMLResponse)
        def setup_page(request: Request, token: str | None = None, owner: str | None = None,
                       name: str | None = None):
            self._check_token(token)
            if owner is None:
                owner = self.s.allowed_owners[0] if self.s.allowed_owners else ""
            owner = owner.strip()
            if owner and not _OWNER.match(owner):
                raise HTTPException(400, "That isn't a GitHub organization name")
            name = (name or f"{owner or 'Plane'} Plane bridge").strip()[:34]
            base = self._base_url(request)
            if self._placeholder(base):
                raise HTTPException(400, f"The bridge's public address is {base}, the example value. "
                                         f"Set BRIDGE_PUBLIC_URL in .env to your real address (or leave "
                                         f"it empty) and recreate the container")
            state = self._new_state()
            target = (f"https://github.com/organizations/{owner}/settings/apps/new" if owner
                      else "https://github.com/settings/apps/new")
            esc = html.escape
            body = f"""
<p>This creates a GitHub App for the bridge. Activity then shows as something like
<code>{esc(re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-'))}[bot]</code>, the app belongs to the
{'organization' if owner else 'account'} instead of one person, and its tokens expire after an hour.</p>

<form method="get" action="/setup">
 <input type="hidden" name="token" value="{esc(token or '')}">
 <label for="owner">Organization</label>
 <input type="text" id="owner" name="owner" value="{esc(owner)}" placeholder="your-org">
 <div class="muted">Leave empty to create the app on your personal account.
 Creating it on an organization needs an org owner (or an app manager).</div>
 <label for="name">App name</label>
 <input type="text" id="name" name="name" value="{esc(name)}" maxlength="34">
 <div class="muted">Must be unique across GitHub, 34 characters at most.</div>
 <button type="submit">Update</button>
</form>

<h2>What GitHub will be asked for</h2>
<table>
 <tr><td>Webhook</td><td><code>{esc(base)}/github/webhook</code></td></tr>
 <tr><td>Read and write</td><td>Issues, Pull requests, Checks</td></tr>
 <tr><td>Read only</td><td>Contents, Metadata</td></tr>
 <tr><td>Events</td><td>Issues, Pull request, Pull request review, Push, Create</td></tr>
</table>
<p class="muted">If the webhook address is wrong, set <code>BRIDGE_PUBLIC_URL</code> in
<code>.env</code> and recreate the container.</p>

<form method="post" action="{esc(target)}?state={esc(state)}">
 <input type="hidden" name="manifest" value="{esc(json.dumps(manifest(name, base)))}">
 <button type="submit">Create the app on GitHub {'(' + esc(owner) + ')' if owner else ''}</button>
</form>"""
            return _page("Set up the GitHub App", body)

        @app.get("/setup/callback", response_class=HTMLResponse)
        def setup_callback(code: str | None = None, state: str | None = None):
            # One callback at a time, so a double submit or a second tab can't
            # save a second app over the first.
            with self._callback_lock:
                return self._finish(code, state)

    def _finish(self, code: str | None, state: str | None):
            if self.auth.app:
                raise HTTPException(404, "A GitHub App is already set up for this bridge")
            if not code or not self._take_state(state):
                raise HTTPException(403, "This setup link is unknown or expired. Start again from /setup")
            self._clear_states()               # every other open setup page is now stale
            try:
                creds = convert_manifest(code, transport=self.transport)
            except (RuntimeError, httpx.HTTPError, KeyError) as e:
                log.error("GitHub App setup failed: %s", e)
                return _page("Setup failed", f"<p>{html.escape(str(e))}</p>", status=502)
            CredentialFile(self.s.data_dir).save(creds)
            tokens = AppTokens(creds, transport=self.transport)
            self.auth.use_app(tokens)
            self.token = None
            tokens.check()                     # a problem shows in /health and the log
            if self.s.allowed_owners:
                tokens.refresh_coverage(self.s.allowed_owners)
            log.info("GitHub App %s (id %s) created and saved; install it on your org next",
                     creds.slug, creds.app_id)
            if creds.html_url:
                return RedirectResponse(f"{creds.html_url}/installations/new", status_code=303,
                                        headers=NO_REFERRER)
            return _page("App created", "<p>Install it from the app's page on GitHub.</p>")
