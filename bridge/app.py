"""HTTP entry point: two webhook endpoints, a health check and the GitHub App setup page."""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .clients import GitHubClient, PlaneClient
from .config import Settings, load_settings
from .github_auth import AppCredentials, AppTokens, CredentialFile, GitHubAuth, load_private_key
from .setup import SetupFlow
from .render import verify_github_signature, verify_plane_signature
from .store import Store
from .sync import LINK_REPAIRS_PER_PASS, Bridge

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")
# httpx logs every request URL at INFO, which would include /setup's one-time
# manifest code. Keep it to warnings unless debugging.
if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
    for _noisy in ("httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


APP_UPKEEP_SECONDS = 60.0


def github_auth_for(s: Settings, transport: httpx.BaseTransport | None = None) -> GitHubAuth:
    """App from .env (bring your own) wins, then the app /setup made, then the token alone."""
    auth = GitHubAuth(pat=s.github_token)
    if s.github_app_id:
        creds = AppCredentials(app_id=s.github_app_id,
                               private_key=load_private_key(s.github_app_private_key_path),
                               webhook_secret=s.github_webhook_secret, source="env")
    else:
        creds = CredentialFile(s.data_dir).load()
    if creds:
        auth.use_app(AppTokens(creds, transport=transport))
    return auth


def build_app(bridge: Bridge | None = None,
              github_transport: httpx.BaseTransport | None = None) -> FastAPI:
    if bridge is None:
        s = load_settings()
        bridge = Bridge(s, Store(s.db_path),
                        PlaneClient(s.plane_base_url, s.plane_workspace, s.plane_api_key,
                                    rate_limit=s.plane_rate_limit),
                        GitHubClient(github_auth_for(s)))
    settings = bridge.s
    auth = bridge.gh.auth
    # plane_problem: why Plane's API isn't usable, or None. temporary: a rate limit or
    # outage (the sync runs anyway and clears it), as opposed to a setup mistake.
    state = {"plane_problem": None, "temporary": False}
    setup = SetupFlow(settings, auth, transport=github_transport)

    def github_secrets() -> list[str]:
        """Webhook secrets GitHub may sign with: the app's, and the one in .env.
        Both are accepted so an org webhook keeps working while you move to the app."""
        out = [auth.app.creds.webhook_secret if auth.app else "", settings.github_webhook_secret]
        return [x for i, x in enumerate(out) if x and x not in out[:i]]

    stop = threading.Event()

    def app_upkeep() -> None:
        """Retry a failed GitHub App check and refresh install coverage that has expired.
        Runs whether or not the catch-up sync does, and picks up an app made by /setup."""
        while not stop.wait(APP_UPKEEP_SECONDS):
            if auth.app:
                try:
                    auth.app.maybe_recheck(settings.allowed_owners)
                except Exception:
                    log.exception("GitHub App upkeep failed")

    def sync_loop() -> None:
        while True:
            try:
                result = bridge.sync_linked_modules()
                if result != "nothing new":
                    log.info("sync: %s", result)
                if state["temporary"] and not result.startswith("stopped"):
                    log.info("Plane's API is answering again")
                    state["plane_problem"], state["temporary"] = None, False
            except Exception:
                log.exception("sync failed")
            if stop.wait(settings.sync_interval):
                return

    def prepare_store() -> None:
        """Startup housekeeping for the catch-up sync."""
        cleared = bridge.store.clear_failures()
        if cleared:
            # A restart often follows a fix (token, repo access), so give parked items a fresh try.
            log.info("sync: cleared %d parked item(s); they'll be tried again", cleared)
        if settings.recheck_links:
            n = bridge.store.recheck_all_links()
            log.warning("BRIDGE_RECHECK_LINKS is set: re-checking %d issue link(s) in Plane, %d per pass. "
                        "Remove the setting once the log shows no more repairs", n, LINK_REPAIRS_PER_PASS)

    @asynccontextmanager
    async def lifespan(_app):
        # One attempt, in a worker thread: a rate-limited Plane must not hold up startup.
        problem, setup_problem = await anyio.to_thread.run_sync(bridge.plane.workspace_status)
        state["temporary"] = bool(problem) and not setup_problem
        state["plane_problem"] = f"temporary: {problem}" if state["temporary"] else problem
        if problem and setup_problem:
            log.error("PLANE SETUP PROBLEM: %s", problem)
        elif problem:
            log.warning("Plane isn't answering normally at startup (%s); starting the catch-up "
                        "sync anyway, it will retry", problem)
        else:
            log.info("Plane workspace '%s' is reachable", settings.plane_workspace)
        if auth.app:
            # In a worker thread too: GitHub being slow must not hold up startup.
            if await anyio.to_thread.run_sync(auth.app.check) is None:   # problems are logged by check()
                log.info("acting on GitHub as the app %s", auth.app.creds.slug or auth.app.creds.app_id)
                if settings.allowed_owners:
                    installed, missing = await anyio.to_thread.run_sync(
                        auth.app.refresh_coverage, settings.allowed_owners)
                    if missing:
                        log.warning("GitHub App is not installed on: %s", ", ".join(missing))
        else:
            setup.announce()
        threading.Thread(target=app_upkeep, name="github-app-upkeep", daemon=True).start()
        prepare_store()
        if settings.sync_interval > 0 and not (problem and setup_problem):
            threading.Thread(target=sync_loop, name="module-sync", daemon=True).start()
            log.info("catch-up sync of linked modules every %ss", settings.sync_interval)
        yield
        stop.set()

    app = FastAPI(title="Plane-GitHub bridge", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    def run(name: str, fn, *args):
        try:
            log.info("%s: %s", name, fn(*args))
        except Exception:  # never let one bad event kill the worker
            log.exception("%s failed", name)

    if not settings.plane_webhook_secret:
        log.warning("PLANE_WEBHOOK_SECRET is not set: Plane webhooks will be refused until it is")

    setup.mount(app)
    app.state.setup = setup

    @app.get("/health")
    def health():
        out = {"ok": True, "mappings": len(settings.mappings),
               "plane_webhook_ready": bool(settings.plane_webhook_secret),
               "plane_api_ok": state["plane_problem"] is None,
               "github_auth": auth.mode,
               "github_webhook_ready": bool(github_secrets())}
        if auth.app:
            if auth.app.creds.slug:
                out["github_app"] = auth.app.creds.slug
            if settings.allowed_owners:
                # From the live cache: an answer that has expired or failed is "unknown".
                cov = auth.app.coverage_now(settings.allowed_owners)
                out["github_app_installed_on"] = cov["installed"]
                out["github_app_missing_on"] = cov["missing"]
                out["github_app_unknown_on"] = cov["unknown"]
        if state["plane_problem"]:
            out["plane_problem"] = state["plane_problem"]
            if state["temporary"]:
                out["plane_problem_temporary"] = True
        if auth.app and auth.app.problem:        # live: the latest reason the app can't be used
            out["github_problem"] = auth.app.problem
        return out

    @app.post("/plane/webhook", status_code=202)
    async def plane_webhook(request: Request, tasks: BackgroundTasks,
                            x_plane_signature: str | None = Header(default=None)):
        if not settings.plane_webhook_secret:
            raise HTTPException(503, "PLANE_WEBHOOK_SECRET is not set on the bridge")
        body = await request.body()
        if not verify_plane_signature(settings.plane_webhook_secret, body, x_plane_signature):
            raise HTTPException(401, "bad signature")
        payload = json.loads(body or b"{}")
        log.debug("plane payload: %s", body[:2000])
        tasks.add_task(run, "plane", bridge.handle_plane, payload)
        return {"accepted": True}

    @app.post("/github/webhook", status_code=202)
    async def github_webhook(request: Request, tasks: BackgroundTasks,
                             x_hub_signature_256: str | None = Header(default=None),
                             x_github_event: str = Header(default="")):
        secrets = github_secrets()
        if not secrets:
            raise HTTPException(503, "no GitHub webhook secret yet: set up the GitHub App or GITHUB_WEBHOOK_SECRET")
        body = await request.body()
        if not any(verify_github_signature(k, body, x_hub_signature_256) for k in secrets):
            raise HTTPException(401, "bad signature")
        if x_github_event == "ping":
            return {"pong": True}
        if x_github_event in ("installation", "installation_repositories") and auth.app:
            account = ((json.loads(body or b"{}").get("installation") or {}).get("account") or {})
            if account.get("login"):
                auth.app.forget(account["login"])   # cheap: never waits on network calls
            if settings.allowed_owners:             # network calls: off the event loop
                tasks.add_task(auth.app.refresh_coverage, settings.allowed_owners)
            return {"accepted": True}
        tasks.add_task(run, f"github:{x_github_event}", bridge.handle_github,
                       x_github_event, json.loads(body or b"{}"))
        return {"accepted": True}

    return app


def create_app() -> FastAPI:  # uvicorn --factory entry point
    return build_app()
