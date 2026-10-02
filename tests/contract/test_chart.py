"""Contract tests (testing-strategy.md layer 2): the BoothModule manifest against
contracts/module-manifest.md, and the chart's security topology (ADR 0056/0057), from a rendered
``helm template`` — no cluster. Skips locally without helm; CI sets BOOTH_TEST_REQUIRE_EMULATORS=1 so a
missing helm FAILS instead of skipping green.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from traitlets.config import Config

from booth_notebooks.hubconfig import configure

CHART = Path(__file__).resolve().parents[2] / "charts" / "booth-notebooks"
REQUIRED = ["--set", "identity.issuerUrl=http://booth-core.booth-system.svc:8080/iframe-identity"]
RELEASE = "nb"
FULL = f"{RELEASE}-booth-notebooks"


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("helm") is None:
        if os.environ.get("BOOTH_TEST_REQUIRE_EMULATORS") == "1":
            pytest.fail("helm is not installed but BOOTH_TEST_REQUIRE_EMULATORS=1")
        pytest.skip("helm not installed; CI has it")
    return subprocess.run(["helm", *args], capture_output=True, text=True, check=False)


def docs(*extra: str, required: bool = True) -> list[dict]:
    args = ["template", RELEASE, str(CHART), "--namespace", "booth-notebooks", *(REQUIRED if required else []), *extra]
    out = helm(*args)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def one(items: list[dict], kind: str, name: str) -> dict:
    (found,) = [d for d in items if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


@pytest.fixture(scope="module")
def chart() -> list[dict]:
    return docs()


@pytest.fixture(scope="module")
def module(chart) -> dict:
    return one(chart, "BoothModule", "notebooks")


@pytest.fixture(scope="module")
def hub(chart) -> dict:
    return one(chart, "Deployment", f"{FULL}-hub")


@pytest.fixture(scope="module")
def proxy(chart) -> dict:
    return one(chart, "Deployment", f"{FULL}-proxy")


def pod(d: dict) -> dict:
    return d["spec"]["template"]["spec"]


def env(d: dict) -> dict:
    return {e["name"]: e for e in pod(d)["containers"][0].get("env", [])}


def secret_names(d: dict) -> set[str]:
    p = pod(d)
    names = {v["secret"]["secretName"] for v in p.get("volumes", []) if "secret" in v}
    for c in p["containers"]:
        for e in c.get("env", []):
            ref = e.get("valueFrom", {}).get("secretKeyRef")
            if ref:
                names.add(ref["name"])
        for ef in c.get("envFrom", []):
            if "secretRef" in ef:
                names.add(ef["secretRef"]["name"])
    return names


# ---- The manifest (module-manifest.md) ----------------------------------------------------------


def test_manifest_required_fields(module):
    assert (module["apiVersion"], module["kind"]) == ("booth.projectbooth.io/v1alpha1", "BoothModule")  # ADR 0019
    s = module["spec"]
    assert s["id"] == "notebooks" and re.fullmatch(r"[a-z][a-z0-9-]*", s["id"])  # repo name minus booth-
    assert s["displayName"] == "Notebooks"
    assert re.match(r"^\d+\.\d+\.\d+", s["version"]) and re.match(r"^\d+\.\d+\.\d+", s["contractVersion"])
    assert s["healthCheckPath"].startswith("/")


def test_manifest_ui_rules(module):
    s = module["spec"]
    assert s["hasOwnUi"] is True
    assert s["uiIntegrationMode"] == "iframe-proxy"  # ui-integration.md names booth-notebooks explicitly
    assert s["navGroup"] == "build"  # the brief / ADR 0017
    assert s["navPath"] == "/notebooks"
    assert "adminNavPath" not in s


def test_core_fronts_the_proxy_and_polls_health_through_it(module, hub):
    s = module["spec"]
    assert s["serviceRef"] == {"name": f"{FULL}-proxy-public", "port": 8000}
    # The manifest's health path is the hub's readiness path: reached via the proxy's default route.
    assert pod(hub)["containers"][0]["readinessProbe"]["httpGet"]["path"] == s["healthCheckPath"] == "/hub/booth/healthz"


def test_manifest_declares_database_and_workload_identity_and_no_event_bus(module):
    s = module["spec"]
    assert s["database"] == {"enabled": True}  # ADR 0053
    assert s["workloadIdentity"] == {"mint": True}  # ADR 0056
    assert "events" not in s  # ADR 0050: undeclared = no bus credential


def test_an_issuer_is_required():
    out = helm("template", "x", str(CHART), "--set", "identity.issuerUrl=")
    assert out.returncode != 0 and "identity.issuerUrl or oidc.issuerUrl is required" in out.stderr


def test_the_default_issuer_is_booth_cores_iframe_issuer_exactly():
    """ADR 0069. Must be byte-for-byte booth-core chart's default BOOTH_IFRAME_IDENTITY_ISSUER_URL for a
    release `booth-core` in `booth-system` (its deployment.yaml: http://<fullname>.<ns>.svc.cluster.local:
    <port>/iframe-identity). `iss` is compared exactly, so a `.svc:8080` spelling would refuse every login."""
    h = one(docs(required=False), "Deployment", f"{FULL}-hub")
    assert env(h)["BOOTH_IDENTITY_ISSUER_URL"]["value"] == "http://booth-core.booth-system.svc.cluster.local:8080/iframe-identity"
    assert env(h)["BOOTH_IDENTITY_AUDIENCE"]["value"] == "notebooks"  # core sets aud = the module id


def test_helm_lint_is_clean():
    out = helm("lint", str(CHART), *REQUIRED)
    assert out.returncode == 0, out.stdout + out.stderr


# ---- Credential topology (ADR 0057) ------------------------------------------------------------


def test_only_the_hub_holds_module_credentials(hub, proxy):
    assert {"booth-workload-minting-credentials", "booth-database-credentials", f"{FULL}-hub"} <= secret_names(hub)
    # The proxy gets exactly one secret value: the shared route-API token.
    assert secret_names(proxy) == {f"{FULL}-hub"}
    assert set(env(proxy)) == {"CONFIGPROXY_AUTH_TOKEN"}


def test_the_minting_credential_is_mounted_read_only_and_required(hub):
    c = pod(hub)["containers"][0]
    assert {"name": "workload-minting", "mountPath": "/etc/booth/workload", "readOnly": True} in c["volumeMounts"]
    (vol,) = [v for v in pod(hub)["volumes"] if v["name"] == "workload-minting"]
    assert vol["secret"] == {"secretName": "booth-workload-minting-credentials"}  # not optional
    assert env(hub)["BOOTH_WORKLOAD_MINT_DIR"]["value"] == "/etc/booth/workload"


def test_disabling_workload_identity_removes_the_field_and_the_mount():
    items = docs("--set", "workloadIdentity.enabled=false")
    assert "workloadIdentity" not in one(items, "BoothModule", "notebooks")["spec"]
    h = one(items, "Deployment", f"{FULL}-hub")
    assert "booth-workload-minting-credentials" not in secret_names(h)
    assert "BOOTH_WORKLOAD_MINT_DIR" not in env(h)


def test_the_dsn_comes_from_the_core_provisioned_secret(hub):
    assert env(hub)["BOOTH_NOTEBOOKS_DATABASE_DSN"]["valueFrom"]["secretKeyRef"] == {"name": "booth-database-credentials", "key": "dsn"}


def test_the_hub_secret_is_generated_once_and_kept(chart):
    s = one(chart, "Secret", f"{FULL}-hub")
    assert s["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert set(s["data"]) == {"crypt-key", "cookie-secret", "proxy-token"}
    items = docs("--set", "hub.secret.name=mine")
    assert not [d for d in items if d["kind"] == "Secret"]
    assert "mine" in secret_names(one(items, "Deployment", f"{FULL}-hub"))


def test_rbac_is_namespaced_minimal_and_only_for_the_hub(chart):
    assert not [d for d in chart if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    role = one(chart, "Role", f"{FULL}-hub")
    rules = {r["resources"][0]: set(r["verbs"]) for r in role["rules"]}
    assert rules == {
        "pods": {"get", "list", "watch", "create", "delete"},
        "persistentvolumeclaims": {"get", "list", "watch", "create"},
        "events": {"get", "list", "watch"},
    }
    assert all(r["apiGroups"] == [""] for r in role["rules"])  # nothing beyond core resources; no secrets
    (subject,) = one(chart, "RoleBinding", f"{FULL}-hub")["subjects"]
    assert subject["name"] == f"{FULL}-hub"


def test_the_proxy_has_no_kubernetes_identity(chart, proxy):
    assert pod(proxy)["automountServiceAccountToken"] is False
    assert one(chart, "ServiceAccount", f"{FULL}-proxy")["automountServiceAccountToken"] is False


def test_hub_and_proxy_are_locked_down(hub, proxy):
    for d in (hub, proxy):
        p = pod(d)
        c = p["containers"][0]
        assert p["securityContext"]["runAsNonRoot"] is True
        assert c["securityContext"]["readOnlyRootFilesystem"] is True
        assert c["securityContext"]["allowPrivilegeEscalation"] is False
        assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
        assert d["spec"]["replicas"] == 1 and d["spec"]["strategy"]["type"] == "Recreate"
    assert {"name": "tmp", "mountPath": "/tmp"} in pod(hub)["containers"][0]["volumeMounts"]


# ---- Network boundaries -----------------------------------------------------------------------


def test_notebook_pods_accept_only_the_proxy_and_hub(chart):
    np = one(chart, "NetworkPolicy", f"{FULL}-singleuser")
    assert np["spec"]["podSelector"]["matchLabels"] == {"component": "singleuser-server", "app.kubernetes.io/part-of": "booth-notebooks"}
    assert set(np["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    (rule,) = np["spec"]["ingress"]
    assert {f["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for f in rule["from"]} == {"proxy", "hub"}
    assert rule["ports"] == [{"protocol": "TCP", "port": 8888}]


def test_notebook_pod_egress_never_reaches_private_ranges_or_metadata(chart):
    np = one(chart, "NetworkPolicy", f"{FULL}-singleuser")
    blocks = [t["ipBlock"] for r in np["spec"]["egress"] for t in r.get("to", []) if "ipBlock" in t]
    (blk,) = blocks
    assert blk["cidr"] == "0.0.0.0/0"
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"} <= set(blk["except"])
    # the only in-cluster destinations: DNS, the hub API, core's gateway
    in_cluster = [r for r in np["spec"]["egress"] if not any("ipBlock" in t for t in r.get("to", []))]
    assert len(in_cluster) == 3
    assert "postgres" not in yaml.safe_dump(np).lower()


DB_URL = "http://booth-database.booth-database.svc:8080"


def _db_rules(np: dict) -> list[dict]:
    return [r for r in np["spec"]["egress"]
            if any(t.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "booth-database" for t in r.get("to", []))]


def test_no_route_to_booth_database_unless_booth_database_url_is_set(chart):
    """ADR 0092: booth-database is optional, so by default notebook pods stay exactly as closed as before."""
    np = one(chart, "NetworkPolicy", f"{FULL}-singleuser")
    assert _db_rules(np) == []
    assert "5432" not in yaml.safe_dump(np["spec"]["egress"])


def test_booth_database_url_opens_exactly_booth_databases_bundled_postgres_on_5432():
    items = docs("--set", f"boothDatabase.url={DB_URL}")
    np = one(items, "NetworkPolicy", f"{FULL}-singleuser")
    (rule,) = _db_rules(np)
    (dest,) = rule["to"]
    assert dest["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "booth-database"}
    assert dest["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "booth-database", "app.kubernetes.io/component": "postgres"}
    assert rule["ports"] == [{"protocol": "TCP", "port": 5432}]  # the database port only, not the pod wholesale
    # One rule added; nothing else about the policy moves (still no private-range route via the internet rule).
    default = one(docs(), "NetworkPolicy", f"{FULL}-singleuser")
    assert len(np["spec"]["egress"]) == len(default["spec"]["egress"]) + 1
    assert np["spec"]["ingress"] == default["spec"]["ingress"]
    (blk,) = [t["ipBlock"] for r in np["spec"]["egress"] for t in r.get("to", []) if "ipBlock" in t]
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"} <= set(blk["except"])


def test_the_database_rule_follows_the_configured_booth_database_install():
    items = docs("--set", f"boothDatabase.url={DB_URL}",
                 "--set-json", 'singleuser.networkPolicy.egress.boothDatabase.namespaceSelector={"kubernetes.io/metadata.name":"data"}')
    (rule,) = _db_rules(one(items, "NetworkPolicy", f"{FULL}-singleuser"))
    assert rule["to"][0]["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "data"}


def test_booth_database_url_only_adds_the_egress_rule_and_the_sidecar_switch():
    """It gates the network rule (ADR 0092) and the credential sidecar (ADR 0095) and nothing else: the
    proxy and the hub/proxy policies don't move, and the hub's own database wiring (ADR 0053) is unrelated.
    The kernel gets the real host from core's credential broker, never from this value."""
    base, with_db = docs(), docs("--set", f"boothDatabase.url={DB_URL}")
    for kind, name in [("Deployment", f"{FULL}-proxy"), ("NetworkPolicy", f"{FULL}-hub"), ("NetworkPolicy", f"{FULL}-proxy")]:
        a, b = one(base, kind, name), one(with_db, kind, name)
        for d in (a, b):  # the hub's checksum/config annotation hashes all values, so it moves with any change
            d.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}).pop("checksum/config", None)
        assert a == b, f"{kind} {name} changed"
    hub_base, hub_db = env(one(base, "Deployment", f"{FULL}-hub")), env(one(with_db, "Deployment", f"{FULL}-hub"))
    assert hub_db["BOOTH_NOTEBOOKS_DATABASE_DSN"]["valueFrom"]["secretKeyRef"] == {"name": "booth-database-credentials", "key": "dsn"}
    assert set(hub_db) - set(hub_base) == {"BOOTH_NOTEBOOKS_BOOTH_DATABASE_URL", "BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE"}
    assert hub_db["BOOTH_NOTEBOOKS_BOOTH_DATABASE_URL"]["value"] == DB_URL
    assert "BOOTH_NOTEBOOKS_BOOTH_DATABASE_URL" not in hub_base  # unset: no sidecar at all


def test_the_credential_sidecar_image_is_pinned_by_digest_and_a_tag_is_refused():
    with_db = env(one(docs("--set", f"boothDatabase.url={DB_URL}"), "Deployment", f"{FULL}-hub"))
    assert re.fullmatch(r"ghcr\.io/projectbooth/credential-sidecar@sha256:[0-9a-f]{64}", with_db["BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE"]["value"])
    out = helm("template", "x", str(CHART), *REQUIRED, "--set", f"boothDatabase.url={DB_URL}",
               "--set", "credentialSidecar.image=ghcr.io/projectbooth/credential-sidecar:latest")
    assert out.returncode != 0 and "pinned by digest" in out.stderr


def test_the_rendered_sidecar_settings_are_a_valid_hub_configuration():
    """Chart and code agree: the env the chart renders configures the spawner's sidecar."""
    h = one(docs("--set", f"boothDatabase.url={DB_URL}", "--set", "credentialSidecar.renewIntervalSeconds=5"), "Deployment", f"{FULL}-hub")
    c = Config()
    configure(c, _hub_env_as_the_container_sees_it(h))
    assert c.BoothSpawner.booth_database_url == DB_URL
    assert c.BoothSpawner.credential_sidecar_image.startswith("ghcr.io/projectbooth/credential-sidecar@sha256:")
    assert c.BoothSpawner.sidecar_renew_interval_seconds == 5


def test_the_old_database_url_name_fails_loudly_instead_of_silently_rendering_no_rule():
    """ADR 0092 was amended from database.url to boothDatabase.url. Setting the old name must not quietly do
    nothing (leaving kernels cut off from their database) — nor be read as the hub's own database."""
    out = helm("template", "x", str(CHART), *REQUIRED, "--set", f"database.url={DB_URL}")
    assert out.returncode != 0 and "renamed to boothDatabase.url" in out.stderr


def test_the_hubs_own_database_values_are_unaffected_by_the_rename(hub):
    assert env(hub)["BOOTH_NOTEBOOKS_DATABASE_DSN"]["valueFrom"]["secretKeyRef"] == {"name": "booth-database-credentials", "key": "dsn"}


def test_internet_egress_can_be_turned_off():
    np = one(docs("--set", "singleuser.networkPolicy.egress.allowInternet=false"), "NetworkPolicy", f"{FULL}-singleuser")
    assert "0.0.0.0/0" not in yaml.safe_dump(np)


def test_the_proxy_admits_only_booth_core_and_its_api_only_the_hub(chart):
    np = one(chart, "NetworkPolicy", f"{FULL}-proxy")
    public, api = np["spec"]["ingress"]
    assert public["ports"] == [{"protocol": "TCP", "port": 8000}]
    (src,) = public["from"]
    assert src["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "booth-system"}
    assert src["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "booth-core"}
    assert api["ports"] == [{"protocol": "TCP", "port": 8001}]
    assert api["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "hub"


# ---- Chart <-> code agreement -----------------------------------------------------------------


def _hub_env_as_the_container_sees_it(hub: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, e in env(hub).items():
        if "value" in e:
            out[name] = e["value"]
        elif "secretKeyRef" in e.get("valueFrom", {}):
            out[name] = {"dsn": "postgresql://u:p@db/booth_mod_notebooks", "crypt-key": "ab" * 32}.get(e["valueFrom"]["secretKeyRef"]["key"], "x")
        elif "fieldRef" in e.get("valueFrom", {}):
            out[name] = "booth-notebooks"
    return out


def test_the_rendered_hub_environment_is_a_valid_hub_configuration(hub):
    """The chart's env and hubconfig.configure must agree — a renamed variable on either side fails here."""
    c = Config()
    configure(c, _hub_env_as_the_container_sees_it(hub))
    assert c.BoothSpawner.namespace == "booth-notebooks"
    assert c.BoothSpawner.image == "ghcr.io/projectbooth/booth-notebooks-singleuser:0.1.0"
    assert c.BoothSpawner.gateway_url == "http://booth-core.booth-system.svc:8080/modules"
    assert c.BoothAuthenticator.trusted_issuers == [{"url": "http://booth-core.booth-system.svc:8080/iframe-identity", "audience": "notebooks"}]
    assert c.BoothAuthenticator.groups_claim == "groups"
    assert c.JupyterHub.hub_connect_url == f"http://{FULL}-hub:8081"
    assert c.ConfigurableHTTPProxy.api_url == f"http://{FULL}-proxy-api:8001"
    assert c.JupyterHub.db_url.startswith("postgresql+psycopg2://")


def test_profiles_and_idp_settings_round_trip():
    items = docs(
        "--set", "oidc.issuerUrl=https://kc.example.com/realms/booth", "--set", "oidc.clientId=booth-design",
        "--set-json", 'singleuser.profiles=[{"display_name":"R","kubespawner_override":{"image":"r:1"}}]',
    )
    c = Config()
    configure(c, _hub_env_as_the_container_sees_it(one(items, "Deployment", f"{FULL}-hub")))
    assert {"url": "https://kc.example.com/realms/booth", "audience": "booth-design"} in c.BoothAuthenticator.trusted_issuers
    assert c.BoothSpawner.profile_list[0]["kubespawner_override"]["image"] == "r:1"
