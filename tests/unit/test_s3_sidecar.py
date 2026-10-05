"""ADR 0095 third amendment: the s3 credential sidecar, scoped by a spawn-time warehouse lookup.

The pod is the one KubeSpawner really builds; core's mint endpoint and booth-lakehouse's
``GET /api/warehouse`` (behind core's gateway) are a stand-in on an httpx MockTransport, driven through
the real ``WorkloadMinter`` and ``warehouse_scope``.
"""

from __future__ import annotations

import json

import httpx
import pytest
from traitlets.config import Config

from booth_notebooks.hubconfig import ConfigError, configure
from booth_notebooks.spawner import S3_CREDENTIALS_FILE, UnsafePodSpec, check_pod, serialize
from booth_notebooks.warehouse import WarehouseUnavailable, warehouse_scope
from booth_notebooks.workload import WorkloadMinter

from .test_credential_sidecar import DB_ENV, DIGEST, User, _pod, containers, env_of, spawner
from .test_hubconfig import ENV

S3_ENV = {"BOOTH_NOTEBOOKS_BOOTH_STORAGE_URL": "minio.storage.svc:9000", "BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE": DIGEST}
WAREHOUSE = {"workspace": "acme", "backendId": "minio-1", "path": "lakehouse/acme", "warehouseName": "acme",
             "storageRoot": "s3://booth/lakehouse/acme", "createdBy": "user-1", "createdAt": "2026-10-05T00:00:00Z"}
MINTED = "minted-workload-token-for-the-lookup"


@pytest.fixture(autouse=True)
def no_kubernetes_client(monkeypatch):
    import kubespawner.spawner as ks

    monkeypatch.setattr(ks, "load_config", lambda **kw: None)
    monkeypatch.setattr(ks, "shared_client", lambda name: None)


class StandIn:
    """booth-core's mint endpoint plus the gateway's /modules/lakehouse/api/warehouse."""

    def __init__(self, warehouse=(200, WAREHOUSE), mint=200) -> None:
        self.warehouse, self.mint_status = warehouse, mint
        self.mints: list[dict] = []
        self.lookups: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/internal/workload-tokens":
            self.mints.append(json.loads(req.content))
            if self.mint_status != 200:
                return httpx.Response(self.mint_status)
            return httpx.Response(200, json={"token": MINTED, "expiresAt": "2026-10-05T00:10:00Z", "role": "editor"})
        if req.url.path == "/modules/lakehouse/api/warehouse":
            self.lookups.append(req)
            status, body = self.warehouse
            if isinstance(body, Exception):
                raise body
            return httpx.Response(status, json=body) if not isinstance(body, str) else httpx.Response(status, text=body)
        return httpx.Response(404)


def s3_spawner(stand_in: StandIn, env=None, user=None):
    s = spawner({**S3_ENV, **(env or {})}, user)
    transport = httpx.MockTransport(stand_in)
    s.workload_minter = WorkloadMinter("mint-cred", "http://booth-core.booth-system.svc:8080/api/internal/workload-tokens", transport=transport)
    s.lakehouse_transport = transport
    return s


async def s3_pod(stand_in: StandIn, env=None, user=None) -> dict:
    return serialize(await s3_spawner(stand_in, env, user).get_pod_manifest())


def args_of(c: dict) -> dict[str, str]:
    return dict(a.split("=", 1) for a in c["args"])


# ---- the lookup itself ---------------------------------------------------------------------------


async def test_one_lookup_as_the_notebook_itself():
    si = StandIn()
    await s3_pod(si)
    (mint,) = si.mints
    assert mint["workspace"] == "acme" and mint["owner"] == "user-1" and mint["roleCeiling"] == "editor"
    assert mint["subject"].startswith("notebook:acme.")  # the notebook server's identity, not the person's
    (req,) = si.lookups
    assert req.method == "GET" and str(req.url) == "http://booth-core.booth-system.svc:8080/modules/lakehouse/api/warehouse"
    assert req.headers["authorization"] == f"Bearer {MINTED}" and req.headers["x-workspace"] == "acme"


async def test_a_warehouse_becomes_the_s3_sidecars_scope_and_nothing_else_of_the_reply_does():
    p = await s3_pod(StandIn())
    sc = containers(p)["credential-sidecar-s3"]
    a = args_of(sc)
    assert sc["image"] == DIGEST and a["--kind"] == "s3"
    assert json.loads(a["--scope"]) == {"backendId": "minio-1", "path": "lakehouse/acme"}
    assert a["--credentials-file"] == S3_CREDENTIALS_FILE and a["--health-listen"] == "127.0.0.1:9472"
    assert a["--workspace"] == "acme" and "--token" not in a and "--listen" not in a
    assert "storageRoot" not in json.dumps(p) and "s3://booth" not in json.dumps(p)


async def test_the_lookup_token_never_reaches_the_pod():
    assert MINTED not in json.dumps(await s3_pod(StandIn()))


async def test_scope_values_survive_kubespawners_format_templating():
    wh = {**WAREHOUSE, "path": "lake/{username}/x"}  # braces must come through literally, not be templated
    a = args_of(containers(await s3_pod(StandIn(warehouse=(200, wh))))["credential-sidecar-s3"])
    assert json.loads(a["--scope"])["path"] == "lake/{username}/x"


async def test_the_notebook_gets_the_standard_aws_file_variables_and_a_read_only_view_of_the_files():
    p = await s3_pod(StandIn())
    c = containers(p)
    nb_env = env_of(c["notebook"])
    assert nb_env["AWS_SHARED_CREDENTIALS_FILE"] == S3_CREDENTIALS_FILE
    assert nb_env["AWS_CONFIG_FILE"] == S3_CREDENTIALS_FILE + ".config"  # the amendment's two-file output
    assert not {k for k in nb_env if k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
    assert {"name": "booth-s3", "mountPath": "/var/run/booth-s3", "readOnly": True} in c["notebook"]["volumeMounts"]
    assert {"name": "booth-s3", "mountPath": "/var/run/booth-s3"} in c["credential-sidecar-s3"]["volumeMounts"]
    assert not [m for m in c["notebook"]["volumeMounts"] if m["name"] == "booth-sidecar"]  # never the token file
    (vol,) = [v for v in p["spec"]["volumes"] if v["name"] == "booth-s3"]
    assert vol["emptyDir"]["medium"] == "Memory"


async def test_the_s3_sidecar_runs_as_the_notebooks_uid_because_it_writes_0600_files():
    sc = containers(await s3_pod(StandIn()))["credential-sidecar-s3"]["securityContext"]
    assert sc["runAsUser"] == 1000 and sc["runAsGroup"] == 100
    assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]


async def test_s3_alone_brings_the_token_helper_probing_the_s3_health_port():
    c = containers(await s3_pod(StandIn()))
    assert list(c) == ["notebook", "booth-token", "credential-sidecar-s3"]
    assert c["booth-token"]["readinessProbe"]["exec"]["command"][4:] == ["http://127.0.0.1:9472/healthz"]
    assert "DATABASE_URL" not in env_of(c["notebook"])


async def test_both_kinds_share_one_token_helper_that_probes_both():
    c = containers(await s3_pod(StandIn(), env={k: v for k, v in DB_ENV.items()}))
    assert list(c) == ["notebook", "booth-token", "credential-sidecar", "credential-sidecar-s3"]
    assert c["booth-token"]["readinessProbe"]["exec"]["command"][4:] == ["http://127.0.0.1:5432/healthz", "http://127.0.0.1:9472/healthz"]
    assert "DATABASE_URL" in env_of(c["notebook"]) and "AWS_CONFIG_FILE" in env_of(c["notebook"])


@pytest.mark.parametrize(("role", "access"), [("owner", "readwrite"), ("editor", "readwrite"), ("viewer", "read")])
async def test_access_follows_the_persons_role(role, access):
    sc = containers(await s3_pod(StandIn(), user=User(role=role)))["credential-sidecar-s3"]
    assert f"--access={access}" in sc["args"]


# ---- no warehouse, or no answer: no s3 sidecar, and the notebook still starts -----------------------


def _no_s3(p: dict) -> None:
    c = containers(p)
    assert "credential-sidecar-s3" not in c and "booth-token" not in c
    assert not {"AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"} & set(env_of(c["notebook"]))
    assert not [v for v in p["spec"].get("volumes", []) if v["name"] in ("booth-s3", "booth-sidecar")]


async def test_a_404_means_no_s3_sidecar():
    """No warehouse yet, or no booth-lakehouse at all (core's gateway 404s an unknown module)."""
    _no_s3(await s3_pod(StandIn(warehouse=(404, {"detail": "this workspace has no lakehouse warehouse yet"}))))


@pytest.mark.parametrize(
    "warehouse",
    [
        (500, {"detail": "boom"}),
        (502, "bad gateway"),
        (401, {"detail": "invalid token"}),
        (200, "not json"),
        (200, {"backendId": "minio-1"}),  # no path
        (200, {"backendId": "", "path": "p"}),
        (200, {"backendId": 7, "path": "p"}),
        (200, httpx.ConnectError("refused")),
    ],
)
async def test_any_other_answer_means_no_s3_sidecar_never_a_failed_spawn(warehouse):
    _no_s3(await s3_pod(StandIn(warehouse=warehouse)))


@pytest.mark.parametrize("status", [403, 401, 500])
async def test_a_refused_or_failed_mint_means_no_s3_sidecar_and_no_lookup(status):
    si = StandIn(mint=status)
    _no_s3(await s3_pod(si))
    assert si.lookups == []


async def test_no_workload_identity_means_no_s3_sidecar():
    s = spawner(S3_ENV)
    assert s.workload_minter is None  # this deployment has no minting Secret
    _no_s3(serialize(await s.get_pod_manifest()))


async def test_without_booth_storage_url_there_is_no_lookup_at_all():
    si = StandIn()
    s = s3_spawner(si)
    s.booth_storage_url = ""
    _no_s3(serialize(await s.get_pod_manifest()))
    assert si.mints == [] and si.lookups == []


async def test_scope_is_resolved_afresh_on_every_spawn():
    """A warehouse created after a notebook first started is picked up at the next server start."""
    si = StandIn(warehouse=(404, {}))
    s = s3_spawner(si)
    _no_s3(serialize(await s.get_pod_manifest()))
    si.warehouse = (200, WAREHOUSE)
    p = serialize(await s.get_pod_manifest())
    assert "credential-sidecar-s3" in containers(p) and len(si.lookups) == 2
    si.warehouse = (404, {})
    _no_s3(serialize(await s.get_pod_manifest()))  # and nothing lingers from the previous spawn


# ---- warehouse_scope directly ------------------------------------------------------------------------


async def test_warehouse_scope_returns_only_the_two_fields():
    t = httpx.MockTransport(lambda r: httpx.Response(200, json=WAREHOUSE))
    assert await warehouse_scope("http://core/", "acme", "tok", transport=t) == {"backendId": "minio-1", "path": "lakehouse/acme"}


async def test_warehouse_scope_404_is_none_and_errors_say_what_happened():
    assert await warehouse_scope("http://core", "acme", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(404))) is None
    with pytest.raises(WarehouseUnavailable, match="HTTP 503"):
        await warehouse_scope("http://core", "acme", "tok", transport=httpx.MockTransport(lambda r: httpx.Response(503)))


# ---- config and check_pod ----------------------------------------------------------------------------


def test_booth_storage_url_needs_a_digest_pinned_image_and_core():
    with pytest.raises(ConfigError, match="pinned by digest"):
        configure(Config(), {**ENV, **S3_ENV, "BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE": "ghcr.io/projectbooth/credential-sidecar:latest"})
    with pytest.raises(ConfigError, match="boothStorage.url needs BOOTH_CORE_URL"):
        configure(Config(), {**ENV, **S3_ENV, "BOOTH_CORE_URL": ""})


def test_check_pod_refuses_a_health_listener_beyond_loopback():
    check_pod(_pod({"name": "s", "args": ["--kind=s3", "--health-listen=127.0.0.1:9472"]}))
    check_pod(_pod({"name": "s", "args": ["--kind=s3"]}))  # the sidecar's own default is loopback
    with pytest.raises(UnsafePodSpec, match="loopback"):
        check_pod(_pod({"name": "s", "args": ["--kind=s3", "--health-listen=0.0.0.0:9472"]}))


@pytest.mark.parametrize("name", ["AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"])
def test_check_pod_refuses_s3_keys_in_env(name):
    with pytest.raises(UnsafePodSpec):
        check_pod(_pod({"name": "n", "env": [{"name": name, "value": "x"}]}))
