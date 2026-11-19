"""HTTP entry point: webhook endpoints, a health check, the GitHub App setup page,
the sync-problems pages and the background threads (catch-up sync, retry queue,
checks, GitHub App upkeep)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .clients import LANE_BACKGROUND, GitHubClient, PlaneClient, lane
from .config import Settings, load_settings
from .diagnostics import make_router
from .github_auth import (AppCredentials, AppTokens, CredentialFile, GitHubAuth, NoGitHubCredentials,
                          load_private_key)
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

QUEUE_TICK = 2          # seconds between retry-queue rounds
CHECK_TICK = 5          # seconds between rounds of module/repo checks
REFLAG_EVERY = 300      # seconds between attempts to add sync-problem labels that failed


_SETUP_SEGMENT = re.compile(r"(^|/)setup(/|$)")


class _HideSetupQuery(logging.Filter):
    """Keep /setup's query strings (the setup token, GitHub's one-time code) out of
    uvicorn's access log. uvicorn's access records carry the path as args[2]."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            # Any "/setup" path segment, so a root_path prefix ("/bridge/setup") is covered too.
            if sep and _SETUP_SEGMENT.search(path):
                record.args = args[:2] + (path + "?...",) + args[3:]
        return True


logging.getLogger("uvicorn.access").addFilter(_HideSetupQuery())


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
                                    rate_limit=s.plane_rate_limit, reserve_share=s.rate_reserve_share),
                        GitHubClient(github_auth_for(s), reserve_share=s.rate_reserve_share))
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

    def first_app_check(app_tokens: AppTokens) -> None:
        """Check the app's key and look it up on the allowed accounts, logging the result."""
        if app_tokens.check() is not None:        # problems are logged by check()
            return
        log.info("acting on GitHub as the app %s", app_tokens.creds.slug or app_tokens.creds.app_id)
        if settings.allowed_owners:
            _installed, missing = app_tokens.refresh_coverage(settings.allowed_owners)
            if missing:
                log.warning("GitHub App is not installed on: %s", ", ".join(missing))

    def app_upkeep() -> None:
        """The app's startup check, then: retry a failed check and refresh install
        coverage that has expired. In its own thread, so GitHub being slow or down
        never holds up startup (webhooks are accepted meanwhile). Runs whether or
        not the catch-up sync does, and picks up an app made by /setup."""
        while True:
            if auth.app:
                try:
                    if auth.app.checked_at is None:
                        first_app_check(auth.app)
                    else:
                        auth.app.maybe_recheck(settings.allowed_owners)
                except Exception:
                    log.exception("GitHub App upkeep failed")
            if stop.wait(APP_UPKEEP_SECONDS):
                return

    def sync_loop() -> None:
        while True:
            try:
                with lane(LANE_BACKGROUND):         # leaves room for webhooks and retries
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

    def queue_loop() -> None:
        """Retry queued actions as soon as they're due (interactive lane: they were
        webhooks). A rate limit ends the round; the pacer decides when to go on."""
        while not stop.wait(QUEUE_TICK):
            try:
                bridge.run_queue()
            except Exception:
                log.exception("retry queue failed")

    def check_loop() -> None:
        """Debounced module/repo checks, the org scan when it's due, and re-flagging."""
        last_reflag = 0.0
        while not stop.wait(CHECK_TICK):
            try:
                # Item checks (an expected catch-up that may not have happened) always
                # run; module and repo checks only with BRIDGE_RECONCILE on.
                bridge.run_reconciles(pairs=settings.reconcile)
                if settings.reconcile and settings.org_scan and bridge.scheduler.due():
                    if bridge.scheduler.defer():
                        log.info("org scan postponed 15 minutes: the host is busy")
                    else:
                        bridge.run_org_scan(stop=stop.is_set)
                if time.monotonic() - last_reflag >= REFLAG_EVERY:
                    last_reflag = time.monotonic()
                    bridge.diag.reflag_pending()
                    bridge.store.prune_catch_up(bridge.clock())
            except Exception:
                log.exception("checks failed")

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
        if not auth.app:
            setup.announce()
        # The app's startup check runs in the upkeep thread, not here: startup
        # doesn't wait for GitHub at all.
        threading.Thread(target=app_upkeep, name="github-app-upkeep", daemon=True).start()
        prepare_store()
        if not (problem and setup_problem):
            # BRIDGE_MEMBERSHIP_INTERVAL=0 turns off only the membership pass; the retry
            # queue always runs, and checks unless BRIDGE_RECONCILE=0.
            if settings.sync_interval > 0:
                threading.Thread(target=sync_loop, name="module-sync", daemon=True).start()
                log.info("catch-up sync of linked modules every %ss", settings.sync_interval)
            threading.Thread(target=queue_loop, name="retry-queue", daemon=True).start()
            threading.Thread(target=check_loop, name="checks", daemon=True).start()
            if not settings.reconcile:
                log.info("BRIDGE_RECONCILE is off: no module/repo checks or org scan")
        yield
        stop.set()

    app = FastAPI(title="Plane-GitHub bridge", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    def clear_parked(owner: str) -> None:
        cleared = bridge.store.clear_failures_for_owner(owner)
        if cleared:
            log.info("sync: GitHub App installation changed on %s; cleared %d parked item(s)", owner, cleared)

    def run(name: str, fn, *args):
        try:
            log.info("%s: %s", name, fn(*args))
        except NoGitHubCredentials as e:   # expected while the app is failing: one line
            log.warning("%s: skipped, %s", name, e)
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
        problem = auth.app.problem_now() if auth.app else None
        if problem:                               # live: why the app can't be used right now
            out["github_problem"] = problem
        out["queue"] = bridge.queue.stats()
        out["plane_pacer"] = bridge.plane.pacer.snapshot()
        out["github_pacer"] = bridge.gh.pacer.snapshot()
        out["github_pacers"] = bridge.gh.pacer_snapshots()
        out["scheduler"] = (bridge.scheduler.snapshot() if settings.reconcile and settings.org_scan
                            else {"enabled": False})
        out["problems"] = bridge.diag.counts()
        out["reconcile_pending"] = bridge.debounce.pending()
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
        tasks.add_task(run, "plane", bridge.handle_plane, payload)
        return {"accepted": True}

    @app.post("/github/webhook", status_code=202)
    async def github_webhook(request: Request, tasks: BackgroundTasks,
                             x_hub_signature_256: str | None = Header(default=None),
                             x_github_event: str = Header(default=""),
                             x_github_delivery: str = Header(default="")):
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
                # Cheap: forgets cached answers and makes its queued actions due now;
                # never waits on network calls.
                bridge.github_account_changed(account["login"])
                # Repos the app now covers may have parked items; try them again.
                tasks.add_task(clear_parked, account["login"])
            if settings.allowed_owners:             # network calls: off the event loop
                tasks.add_task(auth.app.refresh_coverage, settings.allowed_owners)
            return {"accepted": True}
        tasks.add_task(run, f"github:{x_github_event}", bridge.handle_github,
                       x_github_event, json.loads(body or b"{}"), x_github_delivery)
        return {"accepted": True}

    app.include_router(make_router(bridge.diag, settings.plane_base_url, settings.plane_workspace,
                                   on_scan=bridge.scheduler.force,
                                   trust_proxy_headers=getattr(settings, "trust_proxy_headers", False)))
    return app


def create_app() -> FastAPI:  # uvicorn --factory entry point
    return build_app()
