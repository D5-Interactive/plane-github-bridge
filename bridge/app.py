"""HTTP entry point: webhook endpoints, a health check, the sync-problems pages and the
background threads (catch-up sync, retry queue, checks)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import anyio

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .clients import LANE_BACKGROUND, GitHubClient, PlaneClient, lane
from .config import load_settings
from .diagnostics import make_router
from .render import verify_github_signature, verify_plane_signature
from .store import Store
from .sync import LINK_REPAIRS_PER_PASS, Bridge

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

QUEUE_TICK = 2          # seconds between retry-queue rounds
CHECK_TICK = 5          # seconds between rounds of module/repo checks
REFLAG_EVERY = 300      # seconds between attempts to add sync-problem labels that failed


def build_app(bridge: Bridge | None = None) -> FastAPI:
    if bridge is None:
        s = load_settings()
        bridge = Bridge(s, Store(s.db_path),
                        PlaneClient(s.plane_base_url, s.plane_workspace, s.plane_api_key,
                                    rate_limit=s.plane_rate_limit, reserve_share=s.rate_reserve_share),
                        GitHubClient(s.github_token, reserve_share=s.rate_reserve_share))
    settings = bridge.s
    # plane_problem: why Plane's API isn't usable, or None. temporary: a rate limit or
    # outage (the sync runs anyway and clears it), as opposed to a setup mistake.
    state = {"plane_problem": None, "temporary": False}

    stop = threading.Event()

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
                bridge.run_reconciles()
                if settings.org_scan and bridge.scheduler.due():
                    if bridge.scheduler.defer():
                        log.info("org scan postponed 15 minutes: the host is busy")
                    else:
                        bridge.run_org_scan()
                if time.monotonic() - last_reflag >= REFLAG_EVERY:
                    last_reflag = time.monotonic()
                    bridge.diag.reflag_pending()
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
        prepare_store()
        if settings.sync_interval > 0 and not (problem and setup_problem):
            threading.Thread(target=sync_loop, name="module-sync", daemon=True).start()
            log.info("catch-up sync of linked modules every %ss", settings.sync_interval)
        if settings.sync_interval > 0:
            threading.Thread(target=queue_loop, name="retry-queue", daemon=True).start()
            threading.Thread(target=check_loop, name="checks", daemon=True).start()
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

    @app.get("/health")
    def health():
        out = {"ok": True, "mappings": len(settings.mappings),
               "plane_webhook_ready": bool(settings.plane_webhook_secret),
               "plane_api_ok": state["plane_problem"] is None}
        if state["plane_problem"]:
            out["plane_problem"] = state["plane_problem"]
            if state["temporary"]:
                out["plane_problem_temporary"] = True
        out["queue"] = bridge.queue.stats()
        out["plane_pacer"] = bridge.plane.pacer.snapshot()
        out["github_pacer"] = bridge.gh.pacer.snapshot()
        out["scheduler"] = bridge.scheduler.snapshot() if settings.org_scan else {"enabled": False}
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
        body = await request.body()
        if not verify_github_signature(settings.github_webhook_secret, body, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
        if x_github_event == "ping":
            return {"pong": True}
        tasks.add_task(run, f"github:{x_github_event}", bridge.handle_github,
                       x_github_event, json.loads(body or b"{}"), x_github_delivery)
        return {"accepted": True}

    app.include_router(make_router(bridge.diag, settings.plane_base_url, settings.plane_workspace,
                                   on_scan=bridge.scheduler.force))
    return app


def create_app() -> FastAPI:  # uvicorn --factory entry point
    return build_app()
