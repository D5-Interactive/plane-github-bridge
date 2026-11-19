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
    github_token: str
    github_webhook_secret: str
    db_path: str = "/data/bridge.db"
    mappings: list[Mapping] = field(default_factory=list)
    # GitHub owners the bridge may create issues for. Empty = any the token can reach.
    allowed_owners: list[str] = field(default_factory=list)
    # Seconds between catch-up syncs of linked modules (0 = off). Plane sends no
    # webhook when an existing work item is added to a module, so this is how
    # those items reach GitHub.
    sync_interval: int = 120
    # One-off: on startup, re-check that every issue link is on its Plane item
    # (BRIDGE_RECHECK_LINKS=1). Remove the setting again afterwards.
    recheck_links: bool = False
    # Requests per minute the bridge allows itself against Plane's API (0 = no
    # pacing). Plane limits each API key per minute and answers 429 above it.
    plane_rate_limit: float = 50
    # Share of the learned rate budget kept for webhooks and retries, so background
    # scans can't slow instant sync down (BRIDGE_RATE_RESERVE_SHARE).
    rate_reserve_share: float = 0.3
    # The bridge's own public address, used in the sync-problem label descriptions.
    public_url: str = ""
    # Seconds to wait after the last edit of a module or repo before checking it,
    # so a burst of edits is checked once (BRIDGE_RECONCILE_DELAY).
    reconcile_delay: float = 30
    # A queued action older than this (seconds) is flagged as a sync problem.
    queue_max_age: float = 300
    # Org-wide check of every linked module and repo, scheduled by its own cost.
    org_scan: bool = True
    timezone: str = "UTC"
    quiet_hours: tuple[int, int] = (1, 5)

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


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required environment variable {name}")
    return v


def _hours(value: str) -> tuple[int, int]:
    """"1-5" -> (1, 5): the local hours the quiet window starts and ends."""
    try:
        a, b = (int(x) for x in value.strip().split("-", 1))
    except ValueError:
        raise RuntimeError(f"BRIDGE_QUIET_HOURS must look like 1-5, not {value!r}") from None
    if not (0 <= a <= 23 and 0 <= b <= 23):
        raise RuntimeError(f"BRIDGE_QUIET_HOURS hours must be 0-23, not {value!r}")
    return a, b


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
        github_token=_need("GITHUB_TOKEN"),
        github_webhook_secret=_need("GITHUB_WEBHOOK_SECRET"),
        db_path=os.environ.get("BRIDGE_DB", "/data/bridge.db"),
        mappings=mappings,
        allowed_owners=[o.strip() for o in os.environ.get("GITHUB_ALLOWED_OWNERS", "").split(",") if o.strip()],
        # BRIDGE_MEMBERSHIP_INTERVAL: the light pass for items added to linked modules.
        # BRIDGE_SYNC_INTERVAL is its older name and still works.
        sync_interval=int(os.environ.get("BRIDGE_MEMBERSHIP_INTERVAL")
                          or os.environ.get("BRIDGE_SYNC_INTERVAL", "120") or 0),
        recheck_links=os.environ.get("BRIDGE_RECHECK_LINKS", "").strip().lower() in ("1", "true", "yes"),
        plane_rate_limit=float(os.environ.get("PLANE_RATE_LIMIT", "50") or 0),
        rate_reserve_share=min(max(float(os.environ.get("BRIDGE_RATE_RESERVE_SHARE") or "0.3"), 0.0), 0.9),
        public_url=os.environ.get("BRIDGE_PUBLIC_URL", "").strip().rstrip("/"),
        reconcile_delay=float(os.environ.get("BRIDGE_RECONCILE_DELAY", "30") or 0),
        queue_max_age=float(os.environ.get("BRIDGE_QUEUE_MAX_AGE", "300") or 300),
        org_scan=os.environ.get("BRIDGE_ORG_SCAN", "1").strip().lower() not in ("0", "false", "no", "off"),
        timezone=os.environ.get("BRIDGE_TIMEZONE", "UTC").strip() or "UTC",
        quiet_hours=_hours(os.environ.get("BRIDGE_QUIET_HOURS", "1-5")),
    )
