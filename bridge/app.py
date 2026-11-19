"""HTTP entry point: two webhook endpoints and a health check."""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .clients import GitHubClient, PlaneClient
from .config import load_settings
from .render import verify_github_signature, verify_plane_signature
from .store import Store
from .sync import Bridge

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")


def build_app(bridge: Bridge | None = None) -> FastAPI:
    if bridge is None:
        s = load_settings()
        bridge = Bridge(s, Store(s.db_path),
                        PlaneClient(s.plane_base_url, s.plane_workspace, s.plane_api_key),
                        GitHubClient(s.github_token))
    settings = bridge.s
    state = {"plane_problem": None}

    stop = threading.Event()

    def sync_loop() -> None:
        while True:
            try:
                result = bridge.sync_linked_modules()
                if result != "nothing new":
                    log.info("sync: %s", result)
            except Exception:
                log.exception("sync failed")
            if stop.wait(settings.sync_interval):
                return

    @asynccontextmanager
    async def lifespan(_app):
        state["plane_problem"] = bridge.plane.check_workspace()
        if state["plane_problem"]:
            log.error("PLANE SETUP PROBLEM: %s", state["plane_problem"])
        else:
            log.info("Plane workspace '%s' is reachable", settings.plane_workspace)
        if settings.sync_interval > 0 and not state["plane_problem"]:
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

    @app.get("/health")
    def health():
        out = {"ok": True, "mappings": len(settings.mappings),
               "plane_webhook_ready": bool(settings.plane_webhook_secret),
               "plane_api_ok": state["plane_problem"] is None}
        if state["plane_problem"]:
            out["plane_problem"] = state["plane_problem"]
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
        body = await request.body()
        if not verify_github_signature(settings.github_webhook_secret, body, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
        if x_github_event == "ping":
            return {"pong": True}
        tasks.add_task(run, f"github:{x_github_event}", bridge.handle_github,
                       x_github_event, json.loads(body or b"{}"))
        return {"accepted": True}

    return app


def create_app() -> FastAPI:  # uvicorn --factory entry point
    return build_app()
