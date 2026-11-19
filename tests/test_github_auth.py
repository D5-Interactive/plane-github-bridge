import hashlib
import hmac
import json
import re

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from bridge.app import build_app, github_auth_for
from bridge.clients import GitHubClient, PlaneClient
from bridge.config import Settings
from bridge.github_auth import (APP_EVENTS, APP_PERMISSIONS, AppCredentials, AppTokens, CredentialFile,
                                GitHubAuth, app_jwt, manifest)
from bridge.store import Store
from bridge.sync import Bridge


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def iso(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeAppGitHub:
    """GitHub as a GitHub App sees it: installations, token minting, and repo calls."""

    def __init__(self, public_key, clock, installed=("acme",), users=()):
        self.public_key, self.clock = public_key, clock
        self.installed = {o: 900 + i for i, o in enumerate(installed)}
        self.users = set(users)                  # accounts that are users, not orgs
        self.minted, self.lookups, self.calls = 0, 0, []
        self.revoked: set[str] = set()
        self.app_status = 200                    # what installation lookups and mints answer
        self.hidden: set[str] = set()            # repos the installation can't see

    def _check_jwt(self, req):
        token = req.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, self.public_key, algorithms=["RS256"],
                            options={"verify_exp": False, "verify_iat": False})
        assert claims["iss"] == "123"
        assert claims["exp"] - claims["iat"] <= 600
        return claims

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/app":
            self._check_jwt(req)
            return httpx.Response(200, json={"slug": "acme-plane-bridge",
                                             "html_url": "https://github.com/apps/acme-plane-bridge"})
        m = re.fullmatch(r"/(orgs|users)/([^/]+)/installation", p)
        if m:
            self._check_jwt(req)
            if self.app_status != 200:
                return httpx.Response(self.app_status, json={})
            kind, owner = m.groups()
            if (kind == "users") != (owner in self.users):
                return httpx.Response(404, json={})   # /orgs/ for a user and vice versa
            self.lookups += 1
            inst = self.installed.get(owner)
            return httpx.Response(200, json={"id": inst}) if inst else httpx.Response(404, json={})
        m = re.fullmatch(r"/app/installations/(\d+)/access_tokens", p)
        if m:
            self._check_jwt(req)
            if self.app_status != 200:
                return httpx.Response(self.app_status, json={})
            self.minted += 1
            return httpx.Response(201, json={"token": f"ghs_{m.group(1)}_{self.minted}",
                                             "expires_at": iso(self.clock() + 3600)})
        auth = req.headers.get("Authorization", "")
        self.calls.append((req.method, p, auth))
        if auth.removeprefix("Bearer ") in self.revoked:
            return httpx.Response(401, json={"message": "Bad credentials"})
        repo = "/".join(p.split("/")[2:4])
        if repo in self.hidden and auth.startswith("Bearer ghs_"):
            return httpx.Response(404, json={})
        if req.method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+", p):
            return httpx.Response(200, json={"name": p.rsplit("/", 1)[1]})
        if req.method == "POST" and p.endswith("/issues"):
            return httpx.Response(201, json={"number": 1, "html_url": "https://github.com/x/y/issues/1"})
        return httpx.Response(404, json={})


def creds(pem):
    return AppCredentials(app_id="123", private_key=pem, webhook_secret="app-secret")


def app_client(pem, fake, clock, pat=""):
    tokens = AppTokens(creds(pem), transport=httpx.MockTransport(fake.handler), clock=clock)
    return GitHubClient(GitHubAuth(pat=pat, app=tokens), transport=httpx.MockTransport(fake.handler))


# ---------------------------------------------------------------- minting and caching

def test_app_jwt_is_short_lived_and_signed(keypair):
    pem, public = keypair
    claims = jwt.decode(app_jwt("123", pem, now=1_800_000_000), public, algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iss": "123", "iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540}


def test_installation_token_is_minted_once_and_cached(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    gh = app_client(pem, fake, clock)
    gh.get_repo("acme/one")
    gh.create_issue("acme/two", "t", "b")        # same account: same token
    assert fake.minted == 1 and fake.lookups == 1
    assert {c[2] for c in fake.calls} == {"Bearer ghs_900_1"}


def test_token_is_refreshed_shortly_before_it_expires(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    gh = app_client(pem, fake, clock)
    gh.get_repo("acme/one")
    clock.t += 3600 - 400                        # still more than 5 minutes left
    gh.get_repo("acme/one")
    assert fake.minted == 1
    clock.t += 200                               # now inside the 5-minute margin
    gh.get_repo("acme/one")
    assert fake.minted == 2 and fake.calls[-1][2] == "Bearer ghs_900_2"
    assert fake.lookups == 1                     # installation id is cached separately


def test_one_token_per_account(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock, installed=("acme", "other"))
    gh = app_client(pem, fake, clock)
    gh.get_repo("acme/one")
    gh.get_repo("other/thing")
    assert [c[2] for c in fake.calls] == ["Bearer ghs_900_1", "Bearer ghs_901_2"]


def test_revoked_token_is_replaced_and_the_call_retried(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    gh = app_client(pem, fake, clock)
    gh.get_repo("acme/one")
    fake.revoked.add("ghs_900_1")
    assert gh.get_repo("acme/one") == {"name": "one"}
    assert fake.calls[-1][2] == "Bearer ghs_900_2"


# ---------------------------------------------------------------- fallback

def test_personal_token_alone_still_works():
    seen = []
    gh = GitHubClient("ghp_pat", transport=httpx.MockTransport(
        lambda r: seen.append(r.headers["Authorization"]) or httpx.Response(200, json={})))
    gh.get_repo("acme/one")
    assert gh.auth.mode == "token" and seen == ["Bearer ghp_pat"]


def test_account_without_the_app_falls_back_to_the_personal_token(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock, installed=("acme",))
    gh = app_client(pem, fake, clock, pat="ghp_pat")
    gh.get_repo("acme/one")
    gh.get_repo("elsewhere/repo")
    assert [c[2] for c in fake.calls] == ["Bearer ghs_900_1", "Bearer ghp_pat"]
    gh.get_repo("elsewhere/repo")
    assert fake.lookups == 2                     # "not installed" is remembered for a while too


def test_app_from_env_beats_saved_app_beats_token(keypair, tmp_path):
    pem, _ = keypair
    key = tmp_path / "app.pem"
    key.write_text(pem)
    base = dict(plane_base_url="http://p", plane_public_url="https://p", plane_workspace="w",
                plane_api_key="k", plane_webhook_secret="", db_path=str(tmp_path / "b.db"))
    assert github_auth_for(Settings(**base, github_token="ghp")).mode == "token"
    assert github_auth_for(Settings(**base)).mode == "none"
    CredentialFile(str(tmp_path)).save(AppCredentials(app_id="999", private_key=pem, source="setup"))
    assert github_auth_for(Settings(**base, github_token="ghp")).app.creds.app_id == "999"
    env = github_auth_for(Settings(**base, github_app_id="123", github_app_private_key_path=str(key),
                                   github_webhook_secret="s"))
    assert env.app.creds.app_id == "123" and env.app.creds.source == "env"


def test_config_github_is_optional_and_key_path_has_a_default(monkeypatch, tmp_path):
    from bridge.config import load_settings
    for k, v in {"PLANE_BASE_URL": "http://p", "PLANE_PUBLIC_URL": "https://p",
                 "PLANE_WORKSPACE": "w", "PLANE_API_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert load_settings(str(tmp_path / "none.yaml")).github_token == ""   # nothing GitHub is required
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    s = load_settings(str(tmp_path / "none.yaml"))
    assert s.github_app_private_key_path == "/app/secrets/github-app.pem"
    with pytest.raises(RuntimeError, match="Can't read GITHUB_APP_PRIVATE_KEY_PATH"):
        github_auth_for(s)


def test_saved_credentials_are_private(keypair, tmp_path):
    pem, _ = keypair
    f = CredentialFile(str(tmp_path))
    f.save(AppCredentials(app_id="1", private_key=pem, webhook_secret="w", slug="s"))
    assert (tmp_path / "github-app.json").stat().st_mode & 0o777 == 0o600
    assert f.load().webhook_secret == "w"


def test_manifest_asks_for_least_privilege():
    m = manifest("acme Plane bridge", "https://hooks.example.com")
    assert m["default_permissions"] == APP_PERMISSIONS
    assert "administration" not in m["default_permissions"]
    assert set(m["default_events"]) == set(APP_EVENTS)
    assert m["hook_attributes"]["url"] == "https://hooks.example.com/github/webhook"
    assert m["redirect_url"] == "https://hooks.example.com/setup/callback"
    assert m["public"] is False


# ---------------------------------------------------------------- /setup and webhooks

@pytest.fixture
def bare(tmp_path):
    """A bridge with no GitHub credentials at all, as on first start."""
    s = Settings(plane_base_url="http://plane.local", plane_public_url="https://plane.example.com",
                 plane_workspace="acme", plane_api_key="plane-key", plane_webhook_secret="",
                 db_path=str(tmp_path / "b.db"), allowed_owners=["acme"],
                 public_url="https://hooks.example.com")
    plane = PlaneClient(s.plane_base_url, "acme", "plane-key",
                        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []})))
    return Bridge(s, Store(s.db_path), plane, GitHubClient(GitHubAuth()))


def test_setup_creates_app_and_switches_to_it(bare, keypair, tmp_path):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)

    def handler(req):
        if req.url.path == "/app-manifests/the-code/conversions":
            return httpx.Response(201, json={"id": 123, "slug": "acme-plane-bridge", "pem": pem,
                                             "webhook_secret": "app-secret",
                                             "html_url": "https://github.com/apps/acme-plane-bridge"})
        return fake.handler(req)

    app = build_app(bare, github_transport=httpx.MockTransport(handler))
    client = TestClient(app, follow_redirects=False)
    assert client.get("/health").json()["github_auth"] == "none"

    assert client.get("/setup").status_code == 403                      # token from the log required
    assert client.get("/setup?token=wrong").status_code == 403
    token = app.state.setup.token          # printed in the log at startup
    page = client.get(f"/setup?token={token}")
    assert page.status_code == 200
    assert "https://github.com/organizations/acme/settings/apps/new?state=" in page.text
    assert "https://hooks.example.com/github/webhook" in page.text
    state = re.search(r"state=([\w-]+)", page.text).group(1)

    assert client.get("/setup/callback?code=the-code&state=forged").status_code == 403
    r = client.get(f"/setup/callback?code=the-code&state={state}")
    assert r.status_code == 303
    assert r.headers["location"] == "https://github.com/apps/acme-plane-bridge/installations/new"
    assert bare.gh.auth.mode == "app"
    assert CredentialFile(str(tmp_path)).load().app_id == "123"

    # The state is single-use, and setup closes once an app exists.
    assert client.get(f"/setup/callback?code=the-code&state={state}").status_code == 404
    assert client.get(f"/setup?token={token}").status_code == 404

    # GitHub's webhooks are now checked against the app's secret.
    body = json.dumps({"action": "opened"}).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    ok = client.post("/github/webhook", content=body,
                     headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"})
    assert ok.status_code == 202


def test_github_webhooks_refused_until_a_secret_exists(bare):
    client = TestClient(build_app(bare))
    body = b"{}"
    forged = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    r = client.post("/github/webhook", content=body,
                    headers={"X-Hub-Signature-256": forged, "X-GitHub-Event": "issues"})
    assert r.status_code == 503
    assert client.get("/health").json()["github_webhook_ready"] is False


def test_setup_closed_when_app_comes_from_env(bare, keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    bare.gh.auth.use_app(AppTokens(creds(pem), transport=httpx.MockTransport(fake.handler), clock=clock))
    with TestClient(build_app(bare, github_transport=httpx.MockTransport(fake.handler))) as client:
        assert client.get("/setup?token=anything").status_code == 404
        health = client.get("/health").json()
    assert health["github_auth"] == "app" and health["github_app"] == "acme-plane-bridge"
    assert "github_problem" not in health


def test_both_secrets_accepted_while_moving_from_org_webhook_to_app(bare, keypair):
    pem, public = keypair
    bare.s.github_webhook_secret = "old-org-secret"
    bare.gh.auth.use_app(AppTokens(creds(pem), transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    client = TestClient(build_app(bare))
    body = json.dumps({"zen": "hi"}).encode()
    for secret in (b"old-org-secret", b"app-secret"):
        sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        assert client.post("/github/webhook", content=body,
                           headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"}).json() == {"pong": True}


def test_bad_private_key_is_reported_in_health(bare, keypair):
    pem, _ = keypair
    bare.gh.auth.use_app(AppTokens(creds(pem), transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"message": "A JSON web token could not be decoded"}))))
    with TestClient(build_app(bare)) as client:
        health = client.get("/health").json()
    assert "rejected the app's private key" in health["github_problem"]


# ---------------------------------------------------------------- per-account lookup (ALL-84)

def test_typo_repo_does_not_cost_the_whole_account_its_app_token(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    gh = app_client(pem, fake, clock, pat="ghp_pat")
    gh.get_repo("acme/typo")                     # a repo that doesn't exist
    gh.get_repo("acme/real")
    assert [c[2] for c in fake.calls] == ["Bearer ghs_900_1", "Bearer ghs_900_1"]


def test_user_accounts_are_found_too(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock, installed=("solo",), users=("solo",))
    gh = app_client(pem, fake, clock)
    gh.get_repo("solo/tool")
    assert fake.calls[-1][2] == "Bearer ghs_900_1"


def test_repo_outside_a_select_repositories_install_retries_with_the_token(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    fake.hidden.add("acme/private-one")
    gh = app_client(pem, fake, clock, pat="ghp_pat")
    assert gh.get_repo("acme/private-one") == {"name": "private-one"}
    assert [c[2] for c in fake.calls] == ["Bearer ghs_900_1", "Bearer ghp_pat"]
    assert gh.get_repo("acme/visible") == {"name": "visible"}        # app token still used elsewhere
    assert fake.calls[-1][2] == "Bearer ghs_900_1"


def test_repo_outside_the_install_without_a_token_is_just_not_found(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    fake.hidden.add("acme/private-one")
    gh = app_client(pem, fake, clock)
    assert gh.get_repo("acme/private-one") is None
    assert len(fake.calls) == 1


def test_not_installed_is_rechecked_after_a_while(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock, installed=())
    gh = app_client(pem, fake, clock, pat="ghp_pat")
    gh.get_repo("acme/one")
    fake.installed["acme"] = 900                 # installed, but no webhook reached us
    gh.get_repo("acme/one")
    assert fake.calls[-1][2] == "Bearer ghp_pat"  # still believed missing
    clock.t += AppTokens.negative_ttl + 1
    gh.get_repo("acme/one")
    assert fake.calls[-1][2] == "Bearer ghs_900_1"


def test_404_right_after_an_installation_event_does_not_stick(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock, installed=())
    tokens = AppTokens(creds(pem), transport=httpx.MockTransport(fake.handler), clock=clock)
    tokens.forget("acme")                        # installation event arrives
    assert tokens.installation_id("acme") is None  # GitHub hasn't caught up yet
    fake.installed["acme"] = 900
    clock.t += AppTokens.settle_ttl + 1          # well short of negative_ttl
    assert tokens.installation_id("acme") == 900


# ---------------------------------------------------------------- broken app falls back (ALL-85)

@pytest.mark.parametrize("status", [401, 500, 503])
def test_broken_app_falls_back_to_the_token(keypair, status, caplog):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    fake.app_status = status
    gh = app_client(pem, fake, clock, pat="ghp_pat")
    assert gh.get_repo("acme/one") == {"name": "one"}               # no exception
    gh.create_issue("acme/one", "t", "b")
    assert [c[2] for c in fake.calls] == ["Bearer ghp_pat", "Bearer ghp_pat"]
    assert str(status) in gh.auth.app.problem
    assert len([r for r in caplog.records if "GITHUB APP PROBLEM" in r.getMessage()]) == 1   # logged once


def test_broken_app_without_a_token_sends_no_auth(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    fake.app_status = 401
    gh = app_client(pem, fake, clock)
    gh.get_repo("acme/one")
    assert fake.calls[-1][2] == ""
    assert "rejected the app's private key" in gh.auth.app.problem


def test_github_unreachable_during_minting_falls_back(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)

    def app_side(req):
        raise httpx.ConnectError("down", request=req)

    tokens = AppTokens(creds(pem), transport=httpx.MockTransport(app_side), clock=clock)
    gh = GitHubClient(GitHubAuth(pat="ghp_pat", app=tokens), transport=httpx.MockTransport(fake.handler))
    assert gh.get_repo("acme/one") == {"name": "one"}
    assert fake.calls[-1][2] == "Bearer ghp_pat"
    assert "can't reach GitHub" in tokens.problem


def test_problem_clears_when_the_app_works_again(keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    fake.app_status = 500
    gh = app_client(pem, fake, clock, pat="ghp_pat")
    gh.get_repo("acme/one")
    assert gh.auth.app.problem
    fake.app_status = 200
    gh.get_repo("acme/one")
    assert gh.auth.app.problem is None and fake.calls[-1][2] == "Bearer ghs_900_1"


def test_health_shows_a_runtime_app_problem_live(bare, keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    bare.gh.auth.pat = "ghp_pat"
    bare.gh.auth.use_app(AppTokens(creds(pem), transport=httpx.MockTransport(fake.handler), clock=clock))
    bare.gh = GitHubClient(bare.gh.auth, transport=httpx.MockTransport(fake.handler))
    with TestClient(build_app(bare)) as client:
        assert "github_problem" not in client.get("/health").json()
        fake.app_status = 503
        bare.gh.get_repo("acme/one")
        assert "503" in client.get("/health").json()["github_problem"]


# ---------------------------------------------------------------- installed on which owners (ALL-86)

def test_health_lists_where_the_app_is_installed(bare, keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock, installed=("acme",))
    bare.s.allowed_owners = ["acme", "other"]
    bare.gh.auth.use_app(AppTokens(creds(pem), transport=httpx.MockTransport(fake.handler), clock=clock))
    with TestClient(build_app(bare)) as client:
        health = client.get("/health").json()
        assert health["github_app_installed_on"] == ["acme"]
        assert health["github_app_missing_on"] == ["other"]

        # Installing on "other" sends an installation event: cache reset, coverage refreshed.
        fake.installed["other"] = 950
        body = json.dumps({"action": "created",
                           "installation": {"id": 950, "account": {"login": "other"}}}).encode()
        sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        r = client.post("/github/webhook", content=body,
                        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "installation"})
        assert r.status_code == 202
        health = client.get("/health").json()
    assert health["github_app_installed_on"] == ["acme", "other"]
    assert health["github_app_missing_on"] == []


def test_installation_event_resets_the_cached_token(bare, keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    tokens = AppTokens(creds(pem), transport=httpx.MockTransport(fake.handler), clock=clock)
    bare.gh.auth.use_app(tokens)
    tokens.token_for("acme")
    client = TestClient(build_app(bare))
    body = json.dumps({"action": "suspend", "installation": {"account": {"login": "ACME"}}}).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    client.post("/github/webhook", content=body,
                headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "installation_repositories"})
    tokens.token_for("acme")
    assert fake.minted == 2 and fake.lookups == 2


# ---------------------------------------------------------------- locking (ALL-88)

def test_concurrent_requests_for_one_account_mint_once(keypair):
    import threading
    import time as _time
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)

    def slow(req):
        if req.url.path.endswith("/access_tokens"):
            _time.sleep(0.2)
        return fake.handler(req)

    tokens = AppTokens(creds(pem), transport=httpx.MockTransport(slow), clock=clock)
    out = []
    threads = [threading.Thread(target=lambda: out.append(tokens.token_for("acme"))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake.minted == 1 and set(out) == {"ghs_900_1"}


def test_forget_does_not_wait_for_a_mint_in_progress(keypair):
    import threading
    import time as _time
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)
    release = threading.Event()

    def blocking(req):
        if req.url.path.endswith("/access_tokens"):
            release.wait(5)
        return fake.handler(req)

    tokens = AppTokens(creds(pem), transport=httpx.MockTransport(blocking), clock=clock)
    minting = threading.Thread(target=tokens.token_for, args=("acme",))
    minting.start()
    _time.sleep(0.1)                             # the mint is now stuck on the network
    started = _time.monotonic()
    tokens.forget("acme")
    tokens.forget("other")
    assert _time.monotonic() - started < 0.5
    release.set()
    minting.join()


# ---------------------------------------------------------------- after /setup (ALL-90)

def test_after_setup_repo_calls_carry_an_installation_token(bare, keypair):
    pem, public = keypair
    clock = Clock()
    fake = FakeAppGitHub(public, clock)

    def handler(req):
        if req.url.path == "/app-manifests/c/conversions":
            return httpx.Response(201, json={"id": 123, "slug": "acme-plane-bridge", "pem": pem,
                                             "webhook_secret": "app-secret",
                                             "html_url": "https://github.com/apps/acme-plane-bridge"})
        return fake.handler(req)

    transport = httpx.MockTransport(handler)
    bare.gh = GitHubClient(bare.gh.auth, transport=transport)
    app = build_app(bare, github_transport=transport)
    client = TestClient(app, follow_redirects=False)
    page = client.get(f"/setup?token={app.state.setup.token}")
    state = re.search(r"state=([\w-]+)", page.text).group(1)
    assert client.get(f"/setup/callback?code=c&state={state}").status_code == 303
    bare.gh.get_repo("acme/one")
    assert fake.calls[-1][2].startswith("Bearer ghs_")
    health = client.get("/health").json()
    assert health["github_app_installed_on"] == ["acme"] and "github_problem" not in health
