"""Layer 3 (testing-strategy.md): the chart on a real kind cluster, spawning and tearing down real
KubeSpawner pods — the part the brief calls out as most likely to pass a unit test and break in a cluster.

Deployed: this chart (real hub + configurable-http-proxy images), a throwaway PostgreSQL standing in for
the core-provisioned database (ADR 0053), and ``fixtures/fakecore_server.py`` standing in for booth-core's
identity issuer, minting endpoint and gateway. The test plays booth-core's iframe proxy: every request it
sends through the notebooks proxy carries a freshly signed identity assertion plus X-Booth-Workspace.

Run by ``hack/kind-integration.sh`` (locally) and ``.github/workflows/integration.yml``; skipped unless
BOOTH_KIND_CLUSTER names a cluster with the images already loaded.

What this does NOT prove: that booth-core itself reconciles the BoothModule, mints, or proxies iframes
(booth-e2e's job). NetworkPolicy egress is enforced because the cluster runs Calico
(tests/integration/cni/; kind's default kindnet enforces ingress only), and the egress test proves it on
every run: each denial is reachable with the policy deleted, blocked with it applied, reachable again.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

from booth_notebooks.identity import hub_username

CLUSTER = os.environ.get("BOOTH_KIND_CLUSTER", "")
pytestmark = pytest.mark.skipif(not CLUSTER, reason="set BOOTH_KIND_CLUSTER (hack/kind-integration.sh does)")

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures"
NS, CORE_NS = "booth-notebooks", "booth-system"
ISSUER = f"http://booth-core.{CORE_NS}.svc:8080/iframe-identity"
CREDENTIAL = "bwmc.notebooks.kind-standin"
HUB_IMAGE = os.environ.get("BOOTH_HUB_IMAGE", "booth-notebooks-hub:ci")
SINGLEUSER_IMAGE = os.environ.get("BOOTH_SINGLEUSER_IMAGE", "booth-notebooks-singleuser:ci")
SUB = "3f1c0a52-9b7e-4c1f-8d2a-kind-user"


def sh(*args: str, input: str | None = None, check: bool = True) -> str:
    out = subprocess.run(list(args), capture_output=True, text=True, input=input, check=False)
    if check and out.returncode != 0:
        raise AssertionError(f"{' '.join(args)}\n{out.stdout}\n{out.stderr}")
    return out.stdout


def kubectl(*args: str, **kw) -> str:
    return sh("kubectl", "--context", f"kind-{CLUSTER}", *args, **kw)


def wait_for(fn, timeout: float, what: str, interval: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except pytest.fail.Exception:
            raise
        except Exception as e:  # noqa: BLE001 - keep polling, report the last error
            last = e
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what} (last error: {last!r})")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Core:
    """Plays booth-core: signs identity assertions with the key whose JWKS the in-cluster stand-in serves."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # A fresh kid per run, as real key rotation does: the hub caches JWKS by kid, so re-running
        # against the same cluster with a new key under an old kid would (correctly) be rejected.
        self.kid = f"kind-{int(time.time())}"

    def jwks(self) -> str:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return json.dumps({"keys": [jwk]})

    def headers(self, workspace: str, groups: tuple[str, ...]) -> dict:
        now = int(time.time())
        tok = jwt.encode(
            {"iss": ISSUER, "sub": SUB, "aud": "notebooks", "iat": now, "exp": now + 120, "groups": list(groups), "preferred_username": "kind-user"},
            self.key, algorithm="RS256", headers={"kid": self.kid},
        )
        return {"X-Booth-Identity": tok, "X-Booth-Workspace": workspace}

    def browser(self, base: str, workspace: str, groups=("/workspaces/acme/editor", "/workspaces/beta/viewer")) -> httpx.Client:
        """A browser session as seen through core: every request gets a fresh assertion added."""

        def add_identity(request: httpx.Request) -> None:
            request.headers.update(self.headers(workspace, groups))

        return httpx.Client(base_url=base, follow_redirects=True, timeout=30, event_hooks={"request": [add_identity]})


@pytest.fixture(scope="module")
def core() -> Core:
    return Core()


@pytest.fixture(scope="module")
def base(core, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("kind")
    for ns in (NS, CORE_NS):
        kubectl("create", "namespace", ns, check=False)
    kubectl("apply", "-f", str(FIX / "boothmodule-crd.yaml"))

    # booth-core stand-in, labelled like the real one so the chart's selectors match it.
    (tmp / "jwks.json").write_text(core.jwks())
    cm = kubectl("create", "configmap", "fakecore", "-n", CORE_NS, f"--from-file=server.py={FIX / 'fakecore_server.py'}",
                 f"--from-file=jwks.json={tmp / 'jwks.json'}", "--dry-run=client", "-o", "yaml")
    kubectl("apply", "-f", "-", input=cm)
    kubectl("apply", "-n", CORE_NS, "-f", "-", input=FAKE_CORE_MANIFEST)

    # The database and credentials booth-core would provision (ADR 0053 / ADR 0056).
    kubectl("apply", "-n", NS, "-f", str(FIX / "postgres.yaml"))
    kubectl("-n", NS, "create", "secret", "generic", "booth-database-credentials",
            "--from-literal=dsn=postgresql://booth:booth-test@postgres:5432/booth_notebooks", check=False)
    kubectl("-n", NS, "create", "secret", "generic", "booth-workload-minting-credentials",
            f"--from-literal=credential={CREDENTIAL}",
            f"--from-literal=url=http://booth-core.{CORE_NS}.svc:8080/api/internal/workload-tokens",
            f"--from-literal=issuer=http://booth-core.{CORE_NS}.svc:8080", check=False)
    kubectl("-n", CORE_NS, "rollout", "restart", "deployment/booth-core")  # pick up this run's script/JWKS
    kubectl("-n", CORE_NS, "rollout", "status", "deployment/booth-core", "--timeout=180s")
    # Old pods may still answer behind the Service for a moment after a restart: wait until what the
    # Service serves is this run's key, or the first login races it.
    wait_for(lambda: core.kid in kubectl("-n", CORE_NS, "run", f"jwks-{core.kid}-{int(time.time())}", "--rm", "-i", "--restart=Never",
                                         "--image=python:3.13-slim", "--", "python", "-c",
                                         f"import urllib.request;print(urllib.request.urlopen('{ISSUER}/jwks.json').read().decode())",
                                         check=False), 120, "the stand-in core to serve this run's signing key", interval=3)
    kubectl("-n", NS, "rollout", "status", "deployment/postgres", "--timeout=180s")

    hub_repo, hub_tag = HUB_IMAGE.rsplit(":", 1)
    su_repo, su_tag = SINGLEUSER_IMAGE.rsplit(":", 1)
    sh("helm", "--kube-context", f"kind-{CLUSTER}", "upgrade", "--install", "notebooks", str(ROOT / "charts" / "booth-notebooks"),
       "--namespace", NS,
       "--set", f"hub.image.repository={hub_repo}", "--set", f"hub.image.tag={hub_tag}", "--set", "hub.image.pullPolicy=Never",
       "--set", f"singleuser.image.repository={su_repo}", "--set", f"singleuser.image.tag={su_tag}",
       "--set", "singleuser.image.pullPolicy=Never",
       "--set", f"identity.issuerUrl={ISSUER}", "--set", f"core.url=http://booth-core.{CORE_NS}.svc:8080",
       "--set", "singleuser.storage.capacity=1Gi", "--set", "singleuser.cpu.guarantee=0.05",
       "--wait", "--timeout", "6m")

    port = free_port()
    pf = subprocess.Popen(["kubectl", "--context", f"kind-{CLUSTER}", "-n", NS, "port-forward", "svc/notebooks-booth-notebooks-proxy-public", f"{port}:8000"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    wait_for(lambda: httpx.get(f"{url}/hub/booth/healthz", timeout=3).status_code == 200, 60, "the proxy port-forward")
    yield url
    pf.terminate()


def core_records() -> dict:
    out = kubectl("-n", CORE_NS, "exec", "deploy/booth-core", "--", "python", "-c",
                  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/_records').read().decode())")
    return json.loads(out)


def notebook_pod(username: str) -> dict | None:
    pods = json.loads(kubectl("-n", NS, "get", "pods", "-l", "component=singleuser-server", "-o", "json"))["items"]
    mine = [p for p in pods if p["metadata"].get("annotations", {}).get("hub.jupyter.org/username") == username]
    return mine[0] if mine else None


def server_ready(client: httpx.Client, name: str) -> bool:
    # Fail fast, with the container's own output, if the notebook container is crash-looping:
    # KubeSpawner deletes the pod on spawn timeout, taking the only evidence with it.
    pod = notebook_pod(name)
    statuses = [cs for cs in (pod or {}).get("status", {}).get("containerStatuses") or [] if cs["name"] == "notebook"]
    if statuses and statuses[0].get("restartCount", 0) > 0:
        logs = kubectl("-n", NS, "logs", pod["metadata"]["name"], "--previous", check=False)
        pytest.fail(f"notebook container for {name} is crash-looping:\n{logs[-4000:]}")
    r = client.get(f"/hub/api/users/{name}")
    r.raise_for_status()
    srv = r.json().get("servers", {}).get("")
    return bool(srv and srv.get("ready"))


def stop_server(client: httpx.Client, name: str) -> None:
    xsrf = next((c.value for c in client.cookies.jar if c.name == "_xsrf"), "")
    r = client.delete(f"/hub/api/users/{name}/server", headers={"X-XSRFToken": xsrf})
    assert r.status_code in (202, 204), r.text


# ---- the lifecycle ------------------------------------------------------------------------------

ACME = hub_username("acme", SUB)
BETA = hub_username("beta", SUB)


def test_the_boothmodule_registers_with_the_fields_core_reads(base):
    m = json.loads(kubectl("-n", NS, "get", "boothmodules.booth.projectbooth.io", "notebooks", "-o", "json"))["spec"]
    # Checked on the LIVE resource: a CRD that doesn't know a field prunes it silently.
    assert (m["uiIntegrationMode"], m["navGroup"], m["navPath"]) == ("iframe-proxy", "build", "/notebooks")
    assert m["database"] == {"enabled": True} and m["workloadIdentity"] == {"mint": True}


def test_health_through_the_proxy(base):
    r = httpx.get(f"{base}/hub/booth/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["platformAccess"] is True


def test_no_identity_no_hub(base):
    assert httpx.get(f"{base}/hub/login", headers={"X-Booth-Workspace": "acme"}).status_code == 403


def test_spawn_creates_an_isolated_pod_for_the_person_in_the_workspace(base, core):
    c = core.browser(base, "acme")
    r = c.get("/hub/spawn")
    assert r.status_code == 200, f"login/spawn refused ({r.status_code}): {r.text[:300]}"
    wait_for(lambda: server_ready(c, ACME), 300, "the acme notebook server")
    pod = notebook_pod(ACME)
    assert pod is not None
    spec = pod["spec"]
    assert pod["metadata"]["labels"]["booth.projectbooth.io/workspace"] == "acme"
    assert spec["automountServiceAccountToken"] is False
    assert not [v for v in spec.get("volumes", []) if "secret" in v or "projected" in v]
    env = {e["name"] for e in spec["containers"][0].get("env", [])}
    assert not {n for n in env if any(f in n for f in ("DSN", "DATABASE", "MINT", "CRYPT"))}
    sc = spec["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False and sc["runAsNonRoot"] is True
    # its home volume exists
    kubectl("-n", NS, "get", "pvc", spec["volumes"][0]["persistentVolumeClaim"]["claimName"])


def test_the_pod_has_no_route_to_the_kubernetes_api_credentials(base):
    pod = notebook_pod(ACME)["metadata"]["name"]
    out = subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", "-n", NS, "exec", pod, "--", "ls", "/var/run/secrets/kubernetes.io"],
                         capture_output=True, text=True)
    assert out.returncode != 0


def test_jupyterlab_is_served_through_the_proxy_with_only_the_default_python_kernel(base, core):
    c = core.browser(base, "acme")
    # As a browser does: navigate to the page first (API paths never start the OAuth dance, they 403),
    # which follows the single-user server's OAuth flow through the hub and lands on JupyterLab.
    page = c.get(f"/user/{ACME}/lab")
    assert page.status_code == 200 and "JupyterLab" in page.text, page.text[:300]
    r = c.get(f"/user/{ACME}/api/kernelspecs")
    assert r.status_code == 200, r.text[:500]
    specs = r.json()["kernelspecs"]
    assert set(specs) == {"python3"}  # docs/decisions/0001: v0 ships Python only


def test_a_kernel_reaches_the_catalog_as_its_own_short_lived_identity(base):
    """kernel -> hub (its per-user API token) -> core mints -> kernel -> core gateway, end to end."""
    pod = notebook_pod(ACME)["metadata"]["name"]
    out = kubectl("-n", NS, "exec", pod, "--", "python", "-c", "import booth, json; print(json.dumps(booth.catalog.datasets()))")
    assert json.loads(out.strip().splitlines()[-1])[0]["name"] == "from-fake-catalog"
    rec = core_records()
    assert {"workspace": "acme", "subject": f"notebook:{ACME}", "roleCeiling": "editor", "owner": SUB} in rec["mints"]
    call = rec["gateway"][-1]
    assert call["auth"].startswith("Bearer wl-") and call["workspace"] == "acme"


# ---- notebook-pod egress, on an enforcing CNI (ARCHITECTURE item 60) -----------------------------
# The chart's only egress policy is the notebook pods' (<release>-singleuser); the hub's and the proxy's
# are ingress-only. Integration runs Calico (tests/integration/cni/), because kind's default kindnet
# enforces ingress but not egress. Every denial below is shown on the same address and port, from the same
# live notebook pod: reachable with the policy deleted, blocked with it applied, reachable once it is
# deleted again. A timeout (Calico drops) and a refusal (kube-router rejects, on k3s) both count as
# "blocked", but only inside that sequence: on its own a refusal only says nothing accepted.

NETPROBE = (FIX / "netprobe.py").read_text(encoding="utf-8")
BLOCKED = ("closed:TimeoutError", "closed:ConnectionRefusedError")
LAN = dict(line.split("=", 1) for line in (FIX.parent / "cni" / "lan-targets.env").read_text(encoding="utf-8").splitlines()
           if line and not line.startswith("#"))
# CI makes every LAN address answer on the kind host (cni/lan-targets.sh, needs sudo); a local run may not.
LAN_REQUIRED = os.environ.get("BOOTH_LAN_TARGETS") == "1"
SINGLEUSER_POLICY = "notebooks-booth-notebooks-singleuser"
CLUSTER_DNS = "kubernetes.default.svc.cluster.local"


def probe(pod: str, targets: dict, dns: str | None = CLUSTER_DNS) -> dict:
    """TCP connects from INSIDE the notebook container, where user code runs (fixtures/netprobe.py)."""
    spec = json.dumps({"targets": targets, "dns": dns, "timeout": 3})
    out = kubectl("-n", NS, "exec", "-i", pod, "-c", "notebook", "--", "python", "-", spec, input=NETPROBE)
    return json.loads(out.strip().splitlines()[-1])


def settle(pod: str, targets: dict, want: tuple[str, ...], what: str, timeout: float = 60) -> dict:
    """Probe until every target's result is in ``want`` (the CNI programs a changed policy within
    seconds), then return the results; fail with the last results otherwise."""
    deadline = time.time() + timeout
    while True:
        r = probe(pod, targets)
        if all(r[name] in want for name in targets) or time.time() > deadline:
            break
        time.sleep(2)
    print(f"{what}: {json.dumps(r)}")
    bad = {name: r[name] for name in targets if r[name] not in want}
    assert not bad, f"{what}: want {want} for every target, got {bad} (all: {r})"
    return r


def pod_ip(ns: str, selector: str) -> str:
    pods = json.loads(kubectl("-n", ns, "get", "pods", "-l", selector, "-o", "json"))["items"]
    ips = [p["status"].get("podIP") for p in pods
           if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]
    assert ips and ips[0], f"no running pod for {selector} in {ns}"
    return ips[0]


def live_policy() -> str:
    """The installed policy, as an object that can be created again (server-set fields dropped)."""
    np = json.loads(kubectl("-n", NS, "get", "networkpolicy", SINGLEUSER_POLICY, "-o", "json"))
    md = np["metadata"]
    np["metadata"] = {k: md[k] for k in ("name", "namespace", "labels", "annotations") if k in md}
    np.pop("status", None)
    return json.dumps(np)


def closed_policy(tmp: Path) -> str:
    """The chart's own render of the policy with allowInternet=false, for this release's values."""
    values = tmp / "values.json"
    values.write_text(sh("helm", "--kube-context", f"kind-{CLUSTER}", "get", "values", "notebooks", "-n", NS, "-o", "json"))
    out = sh("helm", "template", "notebooks", str(ROOT / "charts" / "booth-notebooks"), "--namespace", NS, "-f", str(values),
             "--set", "singleuser.networkPolicy.egress.allowInternet=false", "--show-only", "templates/networkpolicies.yaml")
    (np,) = [d for d in yaml.safe_load_all(out) if d and d["metadata"]["name"] == SINGLEUSER_POLICY]
    np["metadata"]["namespace"] = NS
    assert "ipBlock" not in json.dumps(np), "the allowInternet=false render still has an ipBlock rule"
    return json.dumps(np)


def put_policy(doc: str | None) -> None:
    """Replace the singleuser policy with ``doc``; None deletes it."""
    kubectl("-n", NS, "delete", "networkpolicy", SINGLEUSER_POLICY, "--ignore-not-found", "--wait")
    if doc:
        kubectl("-n", NS, "create", "-f", "-", input=doc)


def test_notebook_pod_egress_is_enforced_reachable_blocked_reachable(base, tmp_path):
    """The shipped egress policy (default values: allowInternet on, no opt-ins), tested as the CNI enforces
    it. Denied: the hub's own database, booth-database before opting in, the Kubernetes API and the
    kubelet on the node, and one LAN host in each range the internet rule excludes (10/8, 172.16/12,
    192.168/16, 100.64/10, metadata 169.254.169.254). With allowInternet=false (the chart's own render),
    the internet too. Allowed while applied: DNS, the hub API, booth-core, the internet, and a host outside
    the excluded ranges reached through the same listener as the LAN hosts."""
    notebook = notebook_pod(ACME)["metadata"]["name"]
    own_db = pod_ip(NS, "app=postgres")
    kubectl("create", "namespace", "booth-database", check=False)
    kubectl("apply", "-n", "booth-database", "-f", str(FIX / "booth-database-postgres.yaml"))
    kubectl("-n", "booth-database", "rollout", "status", "deployment/booth-database-postgres", "--timeout=180s")
    booth_db = pod_ip("booth-database", "app.kubernetes.io/name=booth-database,app.kubernetes.io/component=postgres")
    node = kubectl("get", "nodes", "-o", "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}").strip()
    hub = pod_ip(NS, "app.kubernetes.io/component=hub")
    core = pod_ip(CORE_NS, "app.kubernetes.io/name=booth-core")
    print(f"targets: own db {own_db}, booth-database {booth_db}, node {node}, hub {hub}, core {core}")

    denied = {
        "the hub's own database :5432": [own_db, 5432],
        "booth-database, not opted in :5432": [booth_db, 5432],
        "the Kubernetes API on the node :6443": [node, 6443],
        "the kubelet on the node :10250": [node, 10250],
    }
    port = int(LAN["LAN_PORT"])
    lan = {f"LAN {cidr} ({LAN[k]}) :{port}": [LAN[k], port] for k, cidr in
           (("LAN_10", "10.0.0.0/8"), ("LAN_172", "172.16.0.0/12"), ("LAN_192", "192.168.0.0/16"),
            ("LAN_CGNAT", "100.64.0.0/10"), ("LAN_METADATA", "169.254.0.0/16"))}
    lan_control = {f"outside the excluded ranges ({LAN['LAN_CONTROL']}) :{port}": [LAN["LAN_CONTROL"], port]}
    internet = {"the internet (1.1.1.1:443)": ["1.1.1.1", 443]}
    allowed = {"the hub API :8081": [hub, 8081], "booth-core :8080": [core, 8080]}

    original = live_policy()
    try:
        # 1. Policy deleted: every target answers this pod, so a later block is the policy, not the target.
        put_policy(None)
        if not LAN_REQUIRED and any(v != "open" for v in probe(notebook, {**lan, **lan_control}, None).values()):
            print("LAN targets do not answer (cni/lan-targets.sh not run, BOOTH_LAN_TARGETS unset): LAN probes skipped")
            lan, lan_control = {}, {}
        everything = {**denied, **lan, **lan_control, **internet, **allowed}
        settle(notebook, everything, ("open",), "1. policy deleted: all reachable")

        # 2. The shipped policy applied: the denied set is blocked, the allowed set still connects.
        put_policy(original)
        settle(notebook, {**denied, **lan}, BLOCKED, "2. policy applied: denied blocked")
        r = settle(notebook, {**allowed, **internet, **lan_control}, ("open",), "2. policy applied: allowed open")
        assert r["dns"] == kubectl("get", "svc", "kubernetes", "-o", "jsonpath={.spec.clusterIP}").strip(), r

        # 2b. allowInternet=false, the chart's own render: the internet is blocked too.
        put_policy(closed_policy(tmp_path))
        settle(notebook, {**denied, **lan, **internet, **lan_control}, BLOCKED, "2b. allowInternet=false: internet blocked too")
        r = settle(notebook, allowed, ("open",), "2b. allowInternet=false: allowed open")
        assert not r["dns"].startswith("failed"), r

        # 3. Policy deleted again: the same targets answer again.
        put_policy(None)
        settle(notebook, everything, ("open",), "3. policy deleted again: all reachable")
    finally:
        put_policy(original)
    settle(notebook, {**denied, **lan}, BLOCKED, "restored: denied blocked")


def test_booth_database_is_reachable_only_after_opting_in_and_the_hubs_own_database_never(base):
    """ADR 0092 (as amended): setting boothDatabase.url adds the rule to booth-database's Postgres, and the
    hub's own database stays blocked. Both addresses, on this port, were shown reachable / blocked /
    reachable by the test above. Mirrors booth-pipeline's step for its runner (eb67c2b)."""
    notebook = notebook_pod(ACME)["metadata"]["name"]
    own_db = pod_ip(NS, "app=postgres")
    booth_db = pod_ip("booth-database", "app.kubernetes.io/name=booth-database,app.kubernetes.io/component=postgres")
    settle(notebook, {"booth-database :5432": [booth_db, 5432], "the hub's own database :5432": [own_db, 5432]},
           BLOCKED, "before opting in: both blocked")
    bdb = "booth-database-postgres.booth-database.svc.cluster.local"

    sh("helm", "--kube-context", f"kind-{CLUSTER}", "upgrade", "notebooks", str(ROOT / "charts" / "booth-notebooks"),
       "--namespace", NS, "--reuse-values", "--set", f"boothDatabase.url={bdb}:5432",
       # ADR 0095 third amendment: s3 mode too, checked at the next spawn (the beta test below).
       "--set", "boothStorage.url=minio.storage.svc:9000",
       "--set-json", 'singleuser.networkPolicy.egress.boothStorage.namespaceSelector={"kubernetes.io/metadata.name":"storage"}',
       "--set-json", 'singleuser.networkPolicy.egress.boothStorage.podSelector={"app":"minio"}',
       "--wait", "--timeout", "5m")
    settle(notebook, {"booth-database :5432": [booth_db, 5432]}, ("open",), "after opting in: booth-database reachable")
    settle(notebook, {"the hub's own database :5432": [own_db, 5432]}, BLOCKED, "after opting in: the hub's own database still blocked")


def test_servers_survive_a_hub_restart(base, core):
    before = notebook_pod(ACME)["metadata"]["uid"]
    kubectl("-n", NS, "delete", "pod", "-l", "app.kubernetes.io/component=hub", "--wait=true")
    kubectl("-n", NS, "rollout", "status", "deployment/notebooks-booth-notebooks-hub", "--timeout=180s")
    c = core.browser(base, "acme")
    wait_for(lambda: c.get("/hub/login").status_code == 200, 120, "the restarted hub to accept a login")
    wait_for(lambda: server_ready(c, ACME), 120, "the hub to re-adopt the running server")
    assert notebook_pod(ACME)["metadata"]["uid"] == before  # the same pod, never restarted
    assert c.get(f"/user/{ACME}/lab").status_code == 200  # and still routed through the proxy


def test_the_same_person_in_another_workspace_gets_another_pod(base, core):
    # The stand-in S3 backend the stand-in broker leases (s3 kind only), labelled to match boothStorage's selectors.
    kubectl("create", "namespace", "storage", check=False)
    kubectl("apply", "-n", "storage", "-f", str(FIX / "minio.yaml"))
    kubectl("-n", "storage", "rollout", "status", "deployment/minio", "--timeout=180s")
    kubectl("-n", "storage", "exec", "deploy/minio", "--", "sh", "-c",
            "mc alias set l http://127.0.0.1:9000 kindtest kindtest-secret >/dev/null && mc mb -p l/lake")
    c = core.browser(base, "beta")
    r = c.get("/hub/spawn")
    assert r.status_code == 200, f"login/spawn refused ({r.status_code}): {r.text[:300]}"
    wait_for(lambda: server_ready(c, BETA), 300, "the beta notebook server")
    a, b = notebook_pod(ACME), notebook_pod(BETA)
    # This pod was spawned after the ADR 0092 step set boothDatabase.url, so it carries the ADR 0095
    # credential sidecar — and this suite's stand-in core has no credential broker, so the sidecar can never
    # get a lease. The notebook must start anyway: database access is optional, never a precondition
    # (BoothSpawner.is_pod_running). This test is the regression guard for that.
    assert {"booth-token", "credential-sidecar"} <= {c["name"] for c in b["spec"]["containers"]}
    # boothStorage.url is set too, and the stand-in lakehouse has a warehouse for beta: the hub looked it up
    # as this notebook (one call, a minted workload token) and scoped an s3 sidecar to it. Same "never
    # blocks" guarantee: no broker, so that sidecar can't get a lease either.
    s3 = {c["name"]: c for c in b["spec"]["containers"]}["credential-sidecar-s3"]
    scope = next(x.split("=", 1)[1] for x in s3["args"] if x.startswith("--scope="))
    assert json.loads(scope) == {"backendId": "lake", "path": "warehouses/beta"}
    lookups = [x for x in core_records()["lakehouse"] if x["workspace"] == "beta"]
    assert lookups and all(x["auth"].startswith("Bearer wl-") for x in lookups)
    nb_env = {e["name"]: e.get("value") for e in {c["name"]: c for c in b["spec"]["containers"]}["notebook"]["env"]}
    assert nb_env["AWS_SHARED_CREDENTIALS_FILE"] == "/var/run/booth-s3/credentials"
    assert nb_env["AWS_CONFIG_FILE"] == "/var/run/booth-s3/credentials.config"
    # The sidecar's directory is mounted in the live notebook, read-only to user code.
    out = subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", "-n", NS, "exec", b["metadata"]["name"], "-c", "notebook", "--",
                          "sh", "-c", "test -d /var/run/booth-s3 && ! touch /var/run/booth-s3/x 2>/dev/null"], capture_output=True, text=True)
    assert out.returncode == 0, f"the s3 credentials directory is missing or writable from the notebook: {out.stderr}"
    # End to end through the pinned sidecar image: it leases from the stand-in broker and writes both files,
    # and booth.s3 hands DuckDB the location (URL_STYLE 'path' from the file's addressing_style, ADR 0095
    # fourth amendment), so a kernel writes and reads the in-cluster MinIO through the boothStorage egress rule.
    def nb(code: str) -> subprocess.CompletedProcess:
        return subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", "-n", NS, "exec", b["metadata"]["name"], "-c", "notebook", "--",
                               "python", "-c", code], capture_output=True, text=True, timeout=300)

    wait_for(lambda: nb("import os,sys;sys.exit(0 if os.path.exists('/var/run/booth-s3/credentials.config') else 1)").returncode == 0,
             120, "the s3 sidecar's first lease (credentials + .config)")
    out = nb(
        "import duckdb, booth.s3\n"
        "con = duckdb.connect(); booth.s3.duckdb_secret(con)\n"
        "s = con.execute(\"SELECT secret_string FROM duckdb_secrets() WHERE name='booth_s3'\").fetchone()[0]\n"
        "print([p for p in s.split(';') if p.startswith(('url_style=', 'endpoint='))])\n"
        "con.execute(\"COPY (SELECT 42 AS y) TO 's3://lake/warehouses/beta/kind.parquet'\")\n"
        "print('READ', con.execute(\"SELECT y FROM 's3://lake/warehouses/beta/kind.parquet'\").fetchone()[0])\n"
    )
    assert out.returncode == 0, f"DuckDB through booth.s3 failed:\n{out.stdout}\n{out.stderr[-1500:]}"
    assert "'url_style=path'" in out.stdout and "'endpoint=minio.storage.svc.cluster.local:9000'" in out.stdout, out.stdout
    assert "READ 42" in out.stdout, out.stdout
    lease = [x for x in core_records()["credentials"] if x["kind"] == "s3"]
    assert lease and lease[0]["scope"] == {"backendId": "lake", "path": "warehouses/beta"} and lease[0]["workspace"] == "beta"
    assert lease[0]["access"] == "read"  # beta is this person's viewer workspace
    assert "credential-sidecar-s3" not in {c["name"] for c in a["spec"]["containers"]}  # spawned before opting in
    assert a["metadata"]["name"] != b["metadata"]["name"]
    assert b["metadata"]["labels"]["booth.projectbooth.io/workspace"] == "beta"
    assert a["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] != b["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
    # the acme session does not open the beta server, and vice versa
    assert c.get(f"/hub/api/users/{ACME}").status_code in (403, 404)


def test_stopping_a_server_deletes_its_pod_and_keeps_its_home_volume(base, core):
    for ws, name in (("acme", ACME), ("beta", BETA)):
        c = core.browser(base, ws)
        claim = notebook_pod(name)["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        c.get("/hub/home")  # renders a page, which sets the _xsrf cookie the API needs
        stop_server(c, name)
        wait_for(lambda n=name: notebook_pod(n) is None, 120, f"{name}'s pod to be deleted")
        kubectl("-n", NS, "get", "pvc", claim)  # notebooks outlive the server


FAKE_CORE_MANIFEST = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: booth-core
  labels: {{app.kubernetes.io/name: booth-core}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app.kubernetes.io/name: booth-core}}}}
  template:
    metadata:
      labels: {{app.kubernetes.io/name: booth-core}}
    spec:
      containers:
        - name: fakecore
          image: python:3.13-slim
          command: ["python", "/config/server.py"]
          env:
            - {{name: ISSUER, value: "{ISSUER}"}}
            - {{name: CREDENTIAL, value: "{CREDENTIAL}"}}
          ports: [{{containerPort: 8080}}]
          readinessProbe: {{httpGet: {{path: /healthz, port: 8080}}}}
          volumeMounts: [{{name: config, mountPath: /config}}]
      volumes:
        - {{name: config, configMap: {{name: fakecore}}}}
---
apiVersion: v1
kind: Service
metadata:
  name: booth-core
spec:
  selector: {{app.kubernetes.io/name: booth-core}}
  ports: [{{port: 8080, targetPort: 8080}}]
"""
