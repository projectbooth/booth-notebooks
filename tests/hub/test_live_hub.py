"""A real JupyterHub process running this module's configuration, driven over HTTP.

Unit tests pin decisions; these pin that JupyterHub actually *calls* them the way the design assumes:
that ``auto_login`` reaches ``authenticate`` with the request's headers, that ``refresh_user`` really
runs on every cookie request (so a stale cookie alone gets nothing), that API-token requests bypass
it, and that the platform-token endpoint is reachable only with a user's API token. The hub's real
OIDC discovery + JWKS verification path runs against ``fakecore``.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from booth_notebooks.identity import hub_username

from .fakecore import CREDENTIAL, FakeCore

HERE = Path(__file__).parent
SERVICE_TOKEN = secrets.token_hex(16)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def core():
    c = FakeCore()
    yield c
    c.close()


@pytest.fixture(scope="module")
def hub(core, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hub")
    mint = tmp / "mint"
    mint.mkdir()
    (mint / "credential").write_text(CREDENTIAL)
    (mint / "url").write_text(f"{core.url}/api/internal/workload-tokens")
    port = _free_port()
    env = {
        **os.environ,
        "TEST_HUB_PORT": str(port),
        "TEST_HUB_DB": str(tmp / "hub.sqlite").replace("\\", "/"),
        "TEST_SERVICE_TOKEN": SERVICE_TOKEN,
        "JUPYTERHUB_CRYPT_KEY": secrets.token_hex(32),
        "BOOTH_NOTEBOOKS_DEV_SQLITE": "true",
        "BOOTH_IDENTITY_ISSUER_URL": core.issuer,
        "BOOTH_CORE_URL": core.url,
        "BOOTH_WORKLOAD_MINT_DIR": str(mint),
        "BOOTH_NOTEBOOKS_CULL_IDLE_SECONDS": "0",
    }
    log = open(tmp / "hub.log", "wb")  # noqa: SIM115 - closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "jupyterhub", "-f", str(HERE / "hub_test_config.py")],
        cwd=tmp,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            if httpx.get(f"{base}/hub/booth/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    else:
        proc.kill()
    if proc.poll() is not None:
        log.close()
        pytest.fail("hub did not start:\n" + (tmp / "hub.log").read_text(errors="replace")[-4000:])
    yield base
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def browser_headers(core, workspace="acme", **kw) -> dict:
    return {"X-Booth-Identity": core.assertion(**kw), "X-Booth-Workspace": workspace}


def login(hub, core, **kw) -> httpx.Client:
    client = httpx.Client(base_url=hub, follow_redirects=False)
    r = client.get("/hub/login", headers=browser_headers(core, **kw))
    assert r.status_code == 302, r.text
    assert any(k.startswith("jupyterhub-") for k in client.cookies), dict(client.cookies)
    return client


def user_token(hub, name) -> str:
    r = httpx.post(f"{hub}/hub/api/users/{name}/tokens", headers={"Authorization": f"token {SERVICE_TOKEN}"}, json={"note": "test"})
    assert r.status_code in (200, 201), r.text
    return r.json()["token"]


def test_health_reports_ok_and_platform_access(hub):
    r = httpx.get(f"{hub}/hub/booth/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "module": "notebooks", "version": "0.1.0", "platformAccess": True}


def test_no_identity_no_login(hub):
    r = httpx.get(f"{hub}/hub/login", headers={"X-Booth-Workspace": "acme", "X-Booth-Role": "owner"})
    assert r.status_code == 403


def test_a_login_creates_the_per_workspace_hub_user(hub, core):
    c = login(hub, core, sub="alice")
    r = c.get("/hub/api/user", headers=browser_headers(core, sub="alice"))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == hub_username("acme", "alice")
    assert r.json()["admin"] is False


def test_the_hub_cookie_alone_is_worth_nothing(hub, core):
    """The core of ADR 0041 applied to a session: without the current request's identity the cookie
    doesn't authenticate, so a leaked/stale cookie, or a request that bypassed core, gets nothing."""
    c = login(hub, core, sub="bob")
    assert c.get("/hub/api/user").status_code == 403
    # ...and a fresh identity for the same person and workspace restores it.
    assert c.get("/hub/api/user", headers=browser_headers(core, sub="bob")).status_code == 200


def test_losing_membership_ends_the_session_immediately(hub, core):
    c = login(hub, core, sub="carol")
    h = browser_headers(core, sub="carol", groups=("/workspaces/other/owner",))
    assert c.get("/hub/api/user", headers=h).status_code == 403


def test_switching_workspace_logs_in_as_the_other_hub_user(hub, core):
    groups = ("/workspaces/acme/editor", "/workspaces/beta/viewer")
    c = login(hub, core, sub="dave", groups=groups)
    beta = browser_headers(core, workspace="beta", sub="dave", groups=groups)
    assert c.get("/hub/api/user", headers=beta).status_code == 403  # the acme session doesn't carry over
    r = c.get("/hub/login", headers=beta)
    assert r.status_code == 302
    who = c.get("/hub/api/user", headers=beta).json()
    assert who["name"] == hub_username("beta", "dave")


def test_a_pods_api_token_gets_a_platform_token_for_exactly_its_identity(hub, core):
    login(hub, core, sub="erin")
    name = hub_username("acme", "erin")
    tok = user_token(hub, name)
    before = len(core.mints)
    # The pod names nothing: workspace, owner and ceiling all come from the hub's verified record.
    r = httpx.post(f"{hub}/hub/api/booth/platform-token", headers={"Authorization": f"token {tok}"}, json={"workspace": "evil", "owner": "x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("wl-") and body["workspace"] == "acme" and body["gatewayUrl"] == f"{core.url}/modules"
    assert r.headers["Cache-Control"] == "no-store"
    assert core.mints[before:] == [{"workspace": "acme", "subject": f"notebook:{name}", "roleCeiling": "editor", "owner": "erin"}]


def test_a_browser_session_cannot_mint(hub, core):
    c = login(hub, core, sub="frank")
    xsrf = c.cookies.get("_xsrf", "")
    r = c.post("/hub/api/booth/platform-token", headers={**browser_headers(core, sub="frank"), "X-XSRFToken": xsrf})
    assert r.status_code == 403


def test_a_service_token_is_not_a_user_and_cannot_mint(hub):
    r = httpx.post(f"{hub}/hub/api/booth/platform-token", headers={"Authorization": f"token {SERVICE_TOKEN}"})
    assert r.status_code == 403


def test_a_core_refusal_is_relayed_as_403_with_the_reason(hub, core):
    login(hub, core, sub="gina")
    tok = user_token(hub, hub_username("acme", "gina"))
    core.refuse_mint = True
    try:
        r = httpx.post(f"{hub}/hub/api/booth/platform-token", headers={"Authorization": f"token {tok}"})
    finally:
        core.refuse_mint = False
    assert r.status_code == 403
    assert "signed in" in r.text
