"""ADR 0095: the postgres credential sidecar in the pod KubeSpawner actually builds (real manifest code)."""

from __future__ import annotations

import json

import pytest
from jupyterhub.objects import Hub, Server
from traitlets.config import Config

from booth_notebooks.hubconfig import ConfigError, configure
from booth_notebooks.identity import hub_username
from booth_notebooks.spawner import (
    SIDECAR_TOKEN_FILE,
    BoothSpawner,
    UnsafePodSpec,
    check_pod,
    serialize,
    workspace_database,
)

from .test_hubconfig import ENV

DIGEST = "ghcr.io/projectbooth/credential-sidecar@sha256:" + "0" * 64
DB_ENV = {"BOOTH_NOTEBOOKS_BOOTH_DATABASE_URL": "booth-database-postgres.booth-database.svc:5432",
          "BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE": DIGEST}


@pytest.fixture(autouse=True)
def no_kubernetes_client(monkeypatch):
    import kubespawner.spawner as ks

    monkeypatch.setattr(ks, "load_config", lambda **kw: None)
    monkeypatch.setattr(ks, "shared_client", lambda name: None)


class User:
    def __init__(self, workspace: str = "acme", role: str = "editor") -> None:
        self.name = hub_username(workspace, "user-1")
        self.id, self.url, self.escaped_name, self.settings, self.spawners = 7, f"/user/{self.name}/", self.name, {}, {}
        self._role = role

    async def get_auth_state(self):
        return {"sub": "user-1", "workspace": self.name.split(".")[0], "role": self._role}


def spawner(env=None, user=None) -> BoothSpawner:
    c = Config()
    configure(c, {**ENV, **(env or {})})
    u = user or User()
    s = BoothSpawner(config=c, user=u, hub=Hub(), _mock=True)
    s.api_token, s.server, s.oauth_client_id = "hub-issued-api-token", Server(base_url=u.url), "jupyterhub-user-x"
    return s


async def pod(env=None, user=None) -> dict:
    return serialize(await spawner(env, user).get_pod_manifest())


def containers(p: dict) -> dict[str, dict]:
    return {c["name"]: c for c in p["spec"]["containers"]}


def env_of(c: dict) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in c.get("env") or []}


# ---- off by default: today's pod, unchanged ------------------------------------------------------


async def test_without_booth_database_url_nothing_is_added():
    p = await pod()
    assert list(containers(p)) == ["notebook"]
    assert "DATABASE_URL" not in env_of(containers(p)["notebook"])
    assert not [v for v in p["spec"].get("volumes", []) if v["name"] == "booth-sidecar"]


# ---- on: the sidecar, its token helper, and DATABASE_URL -----------------------------------------


async def test_database_url_points_at_the_loopback_sidecar_and_names_the_workspace_database():
    nb = containers(await pod(DB_ENV))["notebook"]
    url = env_of(nb)["DATABASE_URL"]
    assert url == f"postgresql://localhost:5432/{workspace_database('acme')}"
    assert "@" not in url and "booth-database" not in url  # no credential, no real host


def test_the_database_name_matches_booth_databases_own_derivation():
    """Computed by booth-database's internal/naming.ForWorkspace("acme-analytics") (Go), 2026-10-02."""
    assert workspace_database("acme-analytics") == "bdb_ws_a716adca12a9ec8861a00c31"


async def test_the_sidecar_is_the_pinned_image_in_postgres_mode_on_loopback_for_this_workspace():
    sc = containers(await pod(DB_ENV))["credential-sidecar"]
    assert sc["image"] == DIGEST
    args = dict(a.split("=", 1) for a in sc["args"])
    assert args["--kind"] == "postgres" and args["--listen"] == "127.0.0.1:5432"
    assert json.loads(args["--scope"]) == {"workspace": "acme"} and args["--workspace"] == "acme"
    assert args["--core-url"] == "http://booth-core.booth-system.svc:8080"
    assert args["--token-file"] == SIDECAR_TOKEN_FILE
    assert "--token" not in args  # never a token on the command line (visible in the pod spec)


@pytest.mark.parametrize(("role", "access"), [("owner", "readwrite"), ("editor", "readwrite"), ("viewer", "read")])
async def test_access_follows_the_persons_role_so_a_viewer_never_crash_loops_the_sidecar(role, access):
    sc = containers(await pod(DB_ENV, User(role=role)))["credential-sidecar"]
    assert f"--access={access}" in sc["args"]


async def test_the_token_helper_carries_only_the_notebooks_own_identity_and_runs_the_sidecars_probe():
    tk = containers(await pod(DB_ENV))["booth-token"]
    assert tk["command"][:3] == ["python", "-m", "booth.sidecar_token"] and tk["command"][-1] == SIDECAR_TOKEN_FILE
    assert set(env_of(tk)) == {"JUPYTERHUB_API_URL", "JUPYTERHUB_API_TOKEN", "BOOTH_WORKSPACE", "BOOTH_PLATFORM_TOKEN_PATH"}
    assert env_of(tk)["JUPYTERHUB_API_TOKEN"] == "hub-issued-api-token"  # the same per-user token the notebook has
    probe = tk["readinessProbe"]["exec"]["command"]
    assert probe[-1] == "http://127.0.0.1:5432/healthz"  # the sidecar's /healthz, not the notebook's


async def test_the_shared_volume_is_memory_backed_and_read_only_for_the_sidecar():
    p = await pod(DB_ENV)
    (vol,) = [v for v in p["spec"]["volumes"] if v["name"] == "booth-sidecar"]
    assert vol["emptyDir"]["medium"] == "Memory"
    c = containers(p)
    assert {"name": "booth-sidecar", "mountPath": "/var/run/booth-sidecar", "readOnly": True} in c["credential-sidecar"]["volumeMounts"]
    assert not [m for m in c["notebook"].get("volumeMounts", []) if m["name"] == "booth-sidecar"]  # user code never sees the token file


async def test_every_added_container_is_locked_down():
    for name, c in containers(await pod(DB_ENV)).items():
        sc = c.get("securityContext") or {}
        assert sc.get("runAsNonRoot") is True and sc.get("allowPrivilegeEscalation") is False, name
        if name != "notebook":
            assert sc["capabilities"]["drop"] == ["ALL"] and "limits" in c["resources"], name
    assert containers(await pod(DB_ENV))["credential-sidecar"]["securityContext"]["readOnlyRootFilesystem"] is True


async def test_renewal_knobs_reach_the_sidecar_only_when_set():
    sc = containers(await pod(DB_ENV))["credential-sidecar"]
    assert env_of(sc) == {}
    sc = containers(await pod({**DB_ENV, "BOOTH_NOTEBOOKS_SIDECAR_RENEW_MARGIN_SECONDS": "290",
                               "BOOTH_NOTEBOOKS_SIDECAR_RENEW_INTERVAL_SECONDS": "5"}))["credential-sidecar"]
    assert env_of(sc) == {"RENEW_MARGIN_SECONDS": "290", "RENEW_INTERVAL_SECONDS": "5"}


async def test_spawning_twice_does_not_accumulate_containers():
    s = spawner(DB_ENV)
    first, second = serialize(await s.get_pod_manifest()), serialize(await s.get_pod_manifest())
    assert len(first["spec"]["containers"]) == len(second["spec"]["containers"]) == 3


# ---- config validation -----------------------------------------------------------------------------


def test_the_sidecar_image_must_be_pinned_by_digest():
    for bad in ("ghcr.io/projectbooth/credential-sidecar:latest", "ghcr.io/projectbooth/credential-sidecar:eb24bb3", ""):
        with pytest.raises(ConfigError, match="pinned by digest"):
            configure(Config(), {**ENV, **DB_ENV, "BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE": bad})


def test_the_sidecar_needs_core():
    with pytest.raises(ConfigError, match="BOOTH_CORE_URL"):
        configure(Config(), {**ENV, **DB_ENV, "BOOTH_CORE_URL": ""})


# ---- check_pod: what it now allows, and what it still refuses --------------------------------------


def _pod(*containers_):
    return {"spec": {"automountServiceAccountToken": False, "containers": list(containers_)}}


def test_check_pod_allows_only_a_credential_free_loopback_database_url():
    check_pod(_pod({"name": "n", "env": [{"name": "DATABASE_URL", "value": "postgresql://localhost:5432/db"}]}))
    for bad in ("postgresql://booth-database.svc:5432/db", "postgresql://u:secret@localhost:5432/db", "sqlite:///x"):
        with pytest.raises(UnsafePodSpec):
            check_pod(_pod({"name": "n", "env": [{"name": "DATABASE_URL", "value": bad}]}))


def test_check_pod_refuses_a_sidecar_listening_beyond_loopback():
    check_pod(_pod({"name": "s", "args": ["--kind=postgres", "--listen=127.0.0.1:5432"]}))
    with pytest.raises(UnsafePodSpec, match="loopback"):
        check_pod(_pod({"name": "s", "args": ["--kind=postgres", "--listen=0.0.0.0:5432"]}))
