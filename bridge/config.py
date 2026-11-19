"""Settings (from environment) and repo mappings (from config.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


@dataclass
class Mapping:
    project: str               # Plane project identifier, e.g. "API"
    repo: str                  # GitHub "owner/name", e.g. "your-org/auth-service"
    module: str                # Plane module name


@dataclass
class Settings:
    plane_base_url: str        # how the bridge reaches Plane's API, e.g. http://localhost:80
    plane_public_url: str      # what humans click, e.g. https://plane.example.com
    plane_workspace: str       # workspace slug, e.g. "your-workspace"
    plane_api_key: str
    plane_webhook_secret: str
    github_token: str = ""         # personal access token; fallback when no GitHub App is set up
    github_webhook_secret: str = ""
    # Bring your own GitHub App. Without these, /setup can create one.
    github_app_id: str = ""
    github_app_private_key_path: str = ""
    # The bridge's own public address (https://hooks.example.com). Optional: /setup
    # works it out from the request when this is empty.
    public_url: str = ""
    db_path: str = "/data/bridge.db"
    mappings: list[Mapping] = field(default_factory=list)
    # GitHub owners the bridge may create issues for. Empty = any the token can reach.
    allowed_owners: list[str] = field(default_factory=list)
    # Seconds between catch-up syncs of linked modules (0 = off). Plane sends no
    # webhook when an existing work item is added to a module, so this is how
    # those items reach GitHub.
    sync_interval: int = 120

    @property
    def data_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.db_path))

    def owner_allowed(self, repo: str) -> bool:
        if not self.allowed_owners:
            return True
        return repo.split("/", 1)[0].lower() in {o.lower() for o in self.allowed_owners}

    def mappings_for(self, project_identifier: str) -> list[Mapping]:
        p = project_identifier.upper()
        return [m for m in self.mappings if m.project.upper() == p]

    def mapping_for_repo(self, repo: str) -> list[Mapping]:
        r = repo.lower()
        return [m for m in self.mappings if m.repo.lower() == r]


DEFAULT_KEY_PATH = "/app/secrets/github-app.pem"


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required environment variable {name}")
    return v


def load_settings(config_path: str | None = None) -> Settings:
    config_path = config_path or os.environ.get("BRIDGE_CONFIG", "config.yaml")
    raw = {}
    if os.path.isfile(config_path):          # config.yaml is optional
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

    mappings = []
    for m in raw.get("mappings", []):
        if not m.get("project") or not m.get("repo") or not m.get("module"):
            raise RuntimeError(f"Each mapping needs 'project', 'module' and 'repo': {m!r}")
        if m["repo"].count("/") != 1:
            raise RuntimeError(f"repo must look like owner/name: {m['repo']!r}")
        mappings.append(Mapping(project=m["project"], repo=m["repo"], module=m.get("module")))

    return Settings(
        plane_base_url=_need("PLANE_BASE_URL").rstrip("/"),
        plane_public_url=_need("PLANE_PUBLIC_URL").rstrip("/"),
        plane_workspace=_need("PLANE_WORKSPACE"),
        plane_api_key=_need("PLANE_API_KEY"),
        # Optional at startup: Plane only shows this secret after the webhook exists,
        # and the webhook needs the bridge running. Plane webhooks are refused until set.
        plane_webhook_secret=os.environ.get("PLANE_WEBHOOK_SECRET", "").strip(),
        # All optional: a GitHub App made through /setup brings its own key and
        # webhook secret. Without an app, GITHUB_TOKEN and GITHUB_WEBHOOK_SECRET are used.
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip(),
        github_app_id=os.environ.get("GITHUB_APP_ID", "").strip(),
        # Only read when GITHUB_APP_ID is set. The default matches docker-compose.yml.
        github_app_private_key_path=(os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
                                     or DEFAULT_KEY_PATH),
        public_url=os.environ.get("BRIDGE_PUBLIC_URL", "").strip().rstrip("/"),
        db_path=os.environ.get("BRIDGE_DB", "/data/bridge.db"),
        mappings=mappings,
        allowed_owners=[o.strip() for o in os.environ.get("GITHUB_ALLOWED_OWNERS", "").split(",") if o.strip()],
        sync_interval=int(os.environ.get("BRIDGE_SYNC_INTERVAL", "120") or 0),
    )
