"""Settings (from environment) and repo mappings (from config.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


@dataclass
class Mapping:
    project: str               # Plane project identifier, e.g. "SECDEV"
    repo: str                  # GitHub "owner/name", e.g. "D5-Interactive/DBGC"
    module: str | None = None  # Plane module name; None = every work item in the project


@dataclass
class Settings:
    plane_base_url: str        # how the bridge reaches Plane's API, e.g. http://localhost:8090
    plane_public_url: str      # what humans click, e.g. https://plane.d5i.dev
    plane_workspace: str       # workspace slug, e.g. "d5i"
    plane_api_key: str
    plane_webhook_secret: str
    github_token: str
    github_webhook_secret: str
    db_path: str = "/data/bridge.db"
    mappings: list[Mapping] = field(default_factory=list)

    def mappings_for(self, project_identifier: str) -> list[Mapping]:
        p = project_identifier.upper()
        return [m for m in self.mappings if m.project.upper() == p]

    def mapping_for_repo(self, repo: str) -> list[Mapping]:
        r = repo.lower()
        return [m for m in self.mappings if m.repo.lower() == r]


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required environment variable {name}")
    return v


def load_settings(config_path: str | None = None) -> Settings:
    config_path = config_path or os.environ.get("BRIDGE_CONFIG", "config.yaml")
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    mappings = []
    for m in raw.get("mappings", []):
        if not m.get("project") or not m.get("repo"):
            raise RuntimeError(f"Each mapping needs 'project' and 'repo': {m!r}")
        if m["repo"].count("/") != 1:
            raise RuntimeError(f"repo must look like owner/name: {m['repo']!r}")
        mappings.append(Mapping(project=m["project"], repo=m["repo"], module=m.get("module")))

    return Settings(
        plane_base_url=_need("PLANE_BASE_URL").rstrip("/"),
        plane_public_url=_need("PLANE_PUBLIC_URL").rstrip("/"),
        plane_workspace=_need("PLANE_WORKSPACE"),
        plane_api_key=_need("PLANE_API_KEY"),
        plane_webhook_secret=_need("PLANE_WEBHOOK_SECRET"),
        github_token=_need("GITHUB_TOKEN"),
        github_webhook_secret=_need("GITHUB_WEBHOOK_SECRET"),
        db_path=os.environ.get("BRIDGE_DB", "/data/bridge.db"),
        mappings=mappings,
    )
