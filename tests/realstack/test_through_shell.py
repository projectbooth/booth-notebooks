"""booth-notebooks through the REAL shell and REAL booth-core (ADR 0069), end to end.

Unlike tests/integration (a stand-in core), everything here is the real thing: booth-design's nginx
routes /iframe/ and the cookie-keyed follow-ups, booth-core mints the iframe URL and session cookie,
signs X-Booth-Identity on every proxied request, provisions this module's database and minting Secret,
and mints the kernel's workload token; Keycloak is the IdP. Only the *browser* is simulated, and only
where a browser is mechanical: after fetching the iframe URL with the user's bearer token (what the
shell's JS does), every request carries cookies only — exactly what an iframe can send.

Skipped unless BOOTH_REAL_SHELL_URL is set. hack/real-stack-e2e.md describes the stack it expects.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

SHELL = os.environ.get("BOOTH_REAL_SHELL_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not SHELL, reason="set BOOTH_REAL_SHELL_URL (see hack/real-stack-e2e.md)")

KEYCLOAK = os.environ.get("BOOTH_REAL_KEYCLOAK_URL", "http://127.0.0.1:8080")
# Keycloak dev mode stamps `iss` from the Host header; it must be the issuer core trusts.
KEYCLOAK_HOST = os.environ.get("BOOTH_REAL_KEYCLOAK_HOST", "keycloak.keycloak.svc:8080")
USERNAME = os.environ.get("BOOTH_REAL_USERNAME", "owner-user")
PASSWORD = os.environ.get("BOOTH_REAL_PASSWORD", "")
WORKSPACE = os.environ.get("BOOTH_REAL_WORKSPACE", "acme-analytics")
CONTEXT = os.environ.get("BOOTH_REAL_KUBE_CONTEXT", "")
NS = os.environ.get("BOOTH_REAL_NAMESPACE", "booth-notebooks")
PROXY_DIRECT = os.environ.get("BOOTH_REAL_PROXY_DIRECT_URL", "")  # optional: the notebooks proxy, bypassing core
CORE_WORKLOAD_ISSUER = os.environ.get("BOOTH_REAL_CORE_WORKLOAD_ISSUER", "http://booth-core.booth-system.svc:8080")
CORE_IFRAME_ISSUER = os.environ.get("BOOTH_REAL_CORE_IFRAME_ISSUER", "http://booth-core.booth-system.svc.cluster.local:8080/iframe-identity")


def kubectl(*args: str) -> str:
    base = ["kubectl"] + (["--context", CONTEXT] if CONTEXT else [])
    out = subprocess.run(base + list(args), capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return out.stdout


class Browser:
    """The parts of a browser this flow depends on: a path-scoped cookie jar that sends Secure cookies
    to http://localhost (browsers treat localhost as a secure context), same-origin redirects, and the
    ``Sec-Fetch-Dest`` header browsers attach to every request — which the shell and core now route on
    (ADR 0069 implementation note): ``iframe`` for a navigation inside the notebooks pane, ``empty`` for
    JupyterLab's own fetch/XHR calls, ``document`` for a top-level page load."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.jar: dict[tuple[str, str], str] = {}  # (name, path) -> value
        self.http = httpx.Client(timeout=60, follow_redirects=False)

    def _store(self, resp: httpx.Response) -> None:
        for raw in resp.headers.get_list("set-cookie"):
            c = SimpleCookie()
            c.load(raw)
            for name, m in c.items():
                path = m["path"] or "/"
                expired = m["max-age"] == "0" or (m["expires"] and parsedate_to_datetime(m["expires"]) < datetime.now(UTC))
                if expired or m.value == "":
                    self.jar.pop((name, path), None)
                else:
                    self.jar[(name, path)] = m.value

    def cookie(self, name: str, path: str) -> str:
        best = [(p, v) for (n, p), v in self.jar.items() if n == name and path.startswith(p)]
        return max(best)[1] if best else ""

    def cookie_header(self, path: str) -> str:
        matching = sorted(((len(p), n, v) for (n, p), v in self.jar.items() if path.startswith(p)), reverse=True)
        return "; ".join(f"{n}={v}" for _, n, v in matching)

    def request(self, method: str, url: str, headers: dict | None = None, follow: bool = True, dest: str = "empty", **kw) -> httpx.Response:
        url = urljoin(self.origin + "/", url)
        for _ in range(25):
            path = urlsplit(url).path
            h = {**(headers or {}), "Cookie": self.cookie_header(path), "Sec-Fetch-Dest": dest}
            r = self.http.request(method, url, headers=h, **kw)
            self._store(r)
            if not (follow and r.is_redirect):
                return r
            nxt = urljoin(url, r.headers["location"])
            assert nxt.startswith(self.origin), f"redirected off the shell's origin: {nxt}"
            url, method, kw = nxt, "GET", {}
        raise AssertionError("too many redirects")


def keycloak_token(username: str) -> str:
    r = httpx.post(
        f"{KEYCLOAK}/realms/booth/protocol/openid-connect/token",
        headers={"Host": KEYCLOAK_HOST},
        data={"grant_type": "password", "client_id": "booth-design", "username": username, "password": PASSWORD, "scope": "openid"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def claims_of(token: str) -> dict:
    p = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


class State:
    token = ""
    browser: Browser | None = None
    name = ""


S = State()


def wait_for(fn, timeout: float, what: str):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what} ({last!r})")


# ---- the shell and core, as the SPA uses them (bearer token) -------------------------------------


def test_the_shell_knows_the_user_and_the_notebooks_module():
    S.token = keycloak_token(USERNAME)
    auth = {"Authorization": f"Bearer {S.token}"}
    me = httpx.get(f"{SHELL}/api/me", headers=auth).json()
    assert any(m["workspace"] == WORKSPACE for m in me["memberships"]), me
    mods = httpx.get(f"{SHELL}/api/modules", headers={**auth, "X-Workspace": WORKSPACE}).json()
    mods = mods.get("modules", mods) if isinstance(mods, dict) else mods
    (nb,) = [m for m in mods if m["id"] == "notebooks"]
    assert nb["uiIntegrationMode"] == "iframe-proxy" and nb["navGroup"] == "build"


def test_core_issues_an_iframe_url_on_the_shells_origin():
    r = httpx.get(f"{SHELL}/api/modules/notebooks/iframe-url", headers={"Authorization": f"Bearer {S.token}", "X-Workspace": WORKSPACE})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    # Relative since core fa11da0: the shell routes /iframe/ itself, so the URL works on any shell origin.
    assert url.startswith("/iframe/notebooks/?booth_iframe_token="), url
    S.browser = Browser(SHELL)
    S.iframe_url = url


# ---- from here on: the iframe. Cookies only, no bearer token, exactly like a browser iframe --------


def test_the_iframe_navigation_logs_in_and_spawns_through_core():
    b = S.browser
    r = b.request("GET", S.iframe_url, dest="iframe")
    assert b.cookie("booth_iframe_session", "/"), "core did not set its iframe session cookie"
    assert r.status_code == 200, (r.status_code, r.text[:300])
    who = b.request("GET", "/hub/api/user")
    assert who.status_code == 200, who.text[:300]
    S.name = who.json()["name"]
    assert S.name.startswith(f"{WORKSPACE}."), S.name  # one hub user per (person, workspace)

    def ready():
        u = b.request("GET", f"/hub/api/users/{S.name}").json()
        return (u.get("servers", {}).get("") or {}).get("ready")

    wait_for(ready, 300, "the notebook server to become ready")


def test_the_real_core_assertion_was_what_logged_in():
    """The hub only ever logs anyone in from a verified X-Booth-Identity: show the login came from
    core's iframe issuer, for this person, with the role core's token grants."""
    state = kubectl("-n", NS, "logs", "deploy/notebooks-booth-notebooks-hub", "-c", "hub", "--tail=2000")
    sub = claims_of(S.token)["sub"]
    assert f"as {S.name} in {WORKSPACE}" in state
    assert sub  # the hub username is derived from this sub (hashed), so it can only be this person
    from booth_notebooks.identity import hub_username

    assert hub_username(WORKSPACE, sub) == S.name


def test_the_pod_is_the_persons_isolated_pod_with_no_module_credentials():
    pods = json.loads(kubectl("-n", NS, "get", "pods", "-l", "component=singleuser-server", "-o", "json"))["items"]
    (pod,) = [p for p in pods if p["metadata"]["annotations"].get("hub.jupyter.org/username") == S.name]
    spec = pod["spec"]
    assert pod["metadata"]["labels"]["booth.projectbooth.io/workspace"] == WORKSPACE
    assert spec["automountServiceAccountToken"] is False
    assert not [v for v in spec.get("volumes", []) if "secret" in v or "projected" in v]


def test_jupyterlab_loads_through_the_shell():
    b = S.browser
    r = b.request("GET", f"/user/{S.name}/lab", dest="iframe")
    assert r.status_code == 200 and "JupyterLab" in r.text, r.text[:300]
    specs = b.request("GET", f"/user/{S.name}/api/kernelspecs").json()["kernelspecs"]
    assert set(specs) == {"python3"}


def run_in_kernel(code: str) -> str:
    """Start a kernel and execute code over its websocket — through the shell's nginx, core's
    cookie-keyed fallback, the notebooks proxy and into the pod, as JupyterLab does."""
    import websocket

    b = S.browser
    base = f"/user/{S.name}"
    xsrf = b.cookie("_xsrf", f"{base}/")
    r = b.request("POST", f"{base}/api/kernels", headers={"X-XSRFToken": xsrf}, json={"name": "python3"})
    assert r.status_code == 201, r.text[:300]
    kid = r.json()["id"]
    ws_url = SHELL.replace("http", "ws", 1) + f"{base}/api/kernels/{kid}/channels"
    ws = websocket.create_connection(ws_url, header=[f"Cookie: {b.cookie_header(base + '/api/kernels')}"], origin=SHELL, timeout=120)
    try:
        msg_id = uuid.uuid4().hex
        ws.send(json.dumps({
            "header": {"msg_id": msg_id, "msg_type": "execute_request", "username": "", "session": uuid.uuid4().hex, "version": "5.3", "date": ""},
            "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
            "content": {"code": code, "silent": False, "store_history": False, "user_expressions": {}, "allow_stdin": False, "stop_on_error": True},
        }))
        out, deadline = [], time.time() + 120
        while time.time() < deadline:
            m = json.loads(ws.recv())
            if m.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            t = m["header"]["msg_type"]
            if t == "stream":
                out.append(m["content"]["text"])
            elif t == "error":
                out.append("ERROR " + m["content"]["ename"] + ": " + m["content"]["evalue"])
            elif t == "status" and m["content"]["execution_state"] == "idle":
                return "".join(out)
        raise AssertionError("kernel did not finish")
    finally:
        ws.close()
        b.request("DELETE", f"{base}/api/kernels/{kid}", headers={"X-XSRFToken": xsrf})


def test_a_kernel_gets_a_real_core_workload_token_for_its_own_identity():
    code = (
        "import booth, json, base64\n"
        "t = booth._default._http.token()\n"
        "p = t.split('.')[1]; p += '=' * (-len(p) % 4)\n"
        "print('CLAIMS ' + json.dumps(json.loads(base64.urlsafe_b64decode(p))))\n"
        "try:\n"
        "    booth.catalog.datasets()\n"
        "    print('CATALOG ok')\n"
        "except booth.BoothError as e:\n"
        "    print('CATALOG %s %s' % (e.status, e))\n"
    )
    out = run_in_kernel(code)
    line = next(ln for ln in out.splitlines() if ln.startswith("CLAIMS "))
    c = json.loads(line[len("CLAIMS "):])
    assert c["iss"] == CORE_WORKLOAD_ISSUER  # minted by the real booth-core, ADR 0056/0058
    assert c["sub"] == f"notebook:{S.name}"  # names the server, never the person
    assert c["groups"] == [f"/workspaces/{WORKSPACE}/editor"]  # owner capped at the editor ceiling
    assert c.get("booth_module") == "notebooks"
    # booth-catalog may not be installed in the stack under test; what matters here is that core's
    # gateway ACCEPTED the notebook's workload token (ADR 0059): anything but 401. With the catalog
    # installed this is "CATALOG ok".
    catalog = next(ln for ln in out.splitlines() if ln.startswith("CATALOG"))
    print(catalog)
    assert catalog == "CATALOG ok" or not catalog.startswith("CATALOG 401"), catalog


def test_a_forged_identity_header_from_the_page_is_replaced_by_core():
    """Page JS (or anyone) setting X-Booth-Identity/X-Booth-Role gets them stripped and re-minted by core."""
    b = S.browser
    r = b.request("GET", "/hub/api/user", headers={"X-Booth-Identity": "forged.token.here", "X-Booth-Role": "owner"})
    assert r.status_code == 200 and r.json()["name"] == S.name


@pytest.mark.skipif(not PROXY_DIRECT, reason="set BOOTH_REAL_PROXY_DIRECT_URL to test bypassing core")
def test_bypassing_core_with_a_self_signed_assertion_gets_nothing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode({"iss": CORE_IFRAME_ISSUER, "sub": claims_of(S.token)["sub"], "aud": "notebooks", "iat": now, "exp": now + 60,
                         "groups": [f"/workspaces/{WORKSPACE}/owner"]}, key, algorithm="RS256", headers={"kid": "forged"})
    r = httpx.get(f"{PROXY_DIRECT}/hub/login", headers={"X-Booth-Identity": forged, "X-Booth-Workspace": WORKSPACE})
    assert r.status_code == 403


@pytest.mark.parametrize("path", ["/", "/storage", "/notebooks", "/catalog/datasets"])
def test_shell_pages_still_load_while_an_iframe_session_exists(path):
    """A top-level page load (a refresh, a link opened in a new tab) with the iframe cookie live must get
    the shell, not be proxied into the notebooks module. Was broken (docs/decisions/0004 finding 1);
    fixed by core fa11da0 + design af1fc39 gating the cookie fallback on Sec-Fetch-Dest."""
    r = S.browser.request("GET", path, follow=False, dest="document")
    assert r.status_code == 200 and '<div id="root"' in r.text, (r.status_code, r.headers.get("location"), r.text[:200])


def test_the_same_paths_inside_the_pane_still_reach_jupyterhub():
    """The other half of the gate: a navigation inside the iframe (and JupyterLab's own calls) must keep
    reaching the module — the fix must not have simply switched the fallback off."""
    assert S.browser.request("GET", f"/user/{S.name}/lab", dest="iframe").status_code == 200
    assert S.browser.request("GET", "/hub/api/user", dest="empty").json()["name"] == S.name


@pytest.mark.skipif(os.environ.get("BOOTH_REAL_WITH_DATA") != "1", reason="needs real booth-storage + booth-catalog (hack/real-stack-data.sh)")
def test_a_kernel_reads_a_registered_dataset_and_registers_its_output():
    """The DoD's "access to registered storage/catalog entries", with every hop real: the person catalogs a
    file through the shell; the kernel reads it by name via core's gateway with a core-minted workload token
    (real booth-catalog + booth-storage verifying core as a second issuer), then writes and registers a
    result — attributed to the notebook's run identity, never the person (ADR 0056)."""
    run = uuid.uuid4().hex[:6]
    gw = {"Authorization": f"Bearer {S.token}", "X-Workspace": WORKSPACE}
    backend, src, out = f"nb-{run}", f"nb-sales-{run}", f"nb-total-{run}"
    r = httpx.post(f"{SHELL}/modules/storage/api/admin/backends", headers=gw,
                   json={"id": backend, "displayName": backend, "kind": "filesystem", "config": {"rootPath": f"/data/{WORKSPACE}"}})
    assert r.status_code in (200, 201), r.text
    r = httpx.put(f"{SHELL}/modules/storage/api/backends/{backend}/objects/sales.csv", headers=gw, content=b"day,amount\n1,10\n2,20\n")
    assert r.status_code in (200, 201), r.text
    r = httpx.post(f"{SHELL}/modules/catalog/api/datasets", headers=gw,
                   json={"name": src, "description": "tests/realstack", "location": {"backendId": backend, "path": "sales.csv"}})
    assert r.status_code in (200, 201), r.text

    code = (
        "import booth, json\n"
        f"df = booth.read_dataset({src!r})\n"
        "total = int(df['amount'].sum())\n"
        f"booth.storage.write({backend!r}, 'out/total.csv', 'total\\n%d\\n' % total, 'text/csv')\n"
        f"ds = booth.catalog.register_dataset({out!r}, {backend!r}, 'out/total.csv', description='written by a notebook')\n"
        "print('RESULT ' + json.dumps({'type': type(df).__name__, 'total': total, 'id': ds['id']}))\n"
    )
    output = run_in_kernel(code)
    line = next((ln for ln in output.splitlines() if ln.startswith("RESULT ")), None)
    assert line, output
    res = json.loads(line[len("RESULT "):])
    assert res["type"] == "DataFrame" and res["total"] == 30

    got = httpx.get(f"{SHELL}/modules/catalog/api/datasets/{res['id']}", headers=gw).json()
    assert got["location"] == {"backendId": backend, "path": "out/total.csv"}
    assert got["createdBy"] == f"notebook:{S.name}", got  # the run, not the person
    body = httpx.get(f"{SHELL}/modules/storage/api/backends/{backend}/objects/out/total.csv", headers=gw)
    assert body.status_code == 200 and body.text == "total\n30\n"


def test_stopping_the_server_deletes_the_pod():
    b = S.browser
    b.request("GET", "/hub/home")
    r = b.request("DELETE", f"/hub/api/users/{S.name}/server", headers={"X-XSRFToken": b.cookie("_xsrf", "/hub/")})
    assert r.status_code in (202, 204), r.text[:300]

    def gone():
        pods = json.loads(kubectl("-n", NS, "get", "pods", "-l", "component=singleuser-server", "-o", "json"))["items"]
        return not [p for p in pods if p["metadata"]["annotations"].get("hub.jupyter.org/username") == S.name]

    wait_for(gone, 120, "the notebook pod to be deleted")
