"""HTTP entry point: two webhook endpoints and a health check."""
from __future__ import annotations

import json
import logging
import os

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
    app = FastAPI(title="Plane-GitHub bridge", docs_url=None, redoc_url=None, openapi_url=None)

    def run(name: str, fn, *args):
        try:
            log.info("%s: %s", name, fn(*args))
        except Exception:  # never let one bad event kill the worker
            log.exception("%s failed", name)

    @app.get("/health")
    def health():
        return {"ok": True, "mappings": len(settings.mappings)}

    @app.post("/plane/webhook", status_code=202)
    async def plane_webhook(request: Request, tasks: BackgroundTasks,
                            x_plane_signature: str | None = Header(default=None)):
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
