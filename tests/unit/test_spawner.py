"""The notebook pod KubeSpawner actually builds, from the real hub configuration (ADR 0057).

These render a real ``V1Pod`` through KubeSpawner's own manifest code (``_mock=True`` skips only the
Kubernetes client), so they pin what reaches the cluster, not just what the config says.
"""

from __future__ import annotations

import pytest
from jupyterhub.objects import Hub, Server
from traitlets.config import Config

from booth_notebooks.hubconfig import configure
from booth_notebooks.identity import hub_username
from booth_notebooks.spawner import WORKSPACE_LABEL, BoothSpawner, UnsafePodSpec, check_pod, serialize

from .test_hubconfig import ENV


@pytest.fixture(autouse=True)
def no_kubernetes_client(monkeypatch):
    """KubeSpawner loads a kube config in __init__ even when mocked; building a manifest never uses it."""
    import kubespawner.spawner as ks

    monkeypatch.setattr(ks, "load_config", lambda **kw: None)
    monkeypatch.setattr(ks, "shared_client", lambda name: None)


class FakeUser:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = 7
        self.url = f"/user/{name}/"
        self.escaped_name = name
        self.settings = {}
        self.spawners = {}

    def get_auth_state(self):  # pragma: no cover - unused by manifest building
        return None


def spawner(env=None, **overrides) -> BoothSpawner:
    c = Config()
    configure(c, {**ENV, **(env or {})})
    for k, v in overrides.items():
        setattr(c.BoothSpawner, k, v)
    user = FakeUser(hub_username("acme", "user-1"))
    s = BoothSpawner(config=c, user=user, hub=Hub(), _mock=True)
    s.api_token = "hub-issued-api-token"
    s.server = Server(base_url=user.url)
    s.oauth_client_id = "jupyterhub-user-x"
    return s


@pytest.fixture
async def pod():
    return serialize(await spawner().get_pod_manifest())


def env_of(pod) -> dict[str, str]:
    return {e["name"]: e.get("value") for e in serialize(pod)["spec"]["containers"][0]["env"]}


async def test_the_pod_has_no_kubernetes_token_and_no_privilege(pod):
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False
    sc = spec["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["runAsNonRoot"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"
    assert sc["runAsUser"] == 1000


async def test_the_pod_carries_its_workspace_and_is_resource_limited(pod):
    assert pod["metadata"]["labels"][WORKSPACE_LABEL] == "acme"
    limits = pod["spec"]["containers"][0]["resources"]["limits"]
    assert limits["cpu"] and limits["memory"]


async def test_the_pod_receives_only_jupyterhub_and_booth_settings(pod):
    env = env_of(pod)
    assert env["BOOTH_WORKSPACE"] == "acme"
    assert env["BOOTH_GATEWAY_URL"] == "http://booth-core.booth-system.svc:8080/modules"
    assert env["BOOTH_PLATFORM_TOKEN_PATH"] == "/booth/platform-token"
    assert env["JUPYTERHUB_API_TOKEN"] == "hub-issued-api-token"  # the one per-user credential it gets
    # Nothing from the hub's own environment (env_keep = []), and no module secret in any form.
    assert not {k for k in env if k in ("PATH", "PYTHONPATH", "LANG", "VIRTUAL_ENV")}
    assert not {k for k in env if any(f in k for f in ("DSN", "DATABASE", "MINT", "CRYPT", "OIDC"))}
    joined = " ".join(str(v) for v in env.values())
    assert "bwmc." not in joined and "postgres" not in joined
    secret_vols = [v for v in pod["spec"].get("volumes", []) if "secret" in v]
    assert secret_vols == []


async def test_without_core_the_kernel_simply_has_no_gateway(tmp_path):
    """ADR 0006 spirit: the default kernel works with zero other modules installed."""
    pod = await spawner({"BOOTH_CORE_URL": ""}).get_pod_manifest()
    assert "BOOTH_GATEWAY_URL" not in env_of(pod)


async def test_home_is_a_per_hub_user_volume():
    s = spawner()
    pod = serialize(await s.get_pod_manifest())
    (vol,) = [v for v in pod["spec"]["volumes"] if v["name"] == "home"]
    assert vol["persistentVolumeClaim"]["claimName"] == s.pvc_name
    assert s.pvc_name.startswith("claim-acme")
    other = BoothSpawner(config=s.config, user=FakeUser(hub_username("beta", "user-1")), hub=Hub(), _mock=True)
    assert other.pvc_name != s.pvc_name  # same person, other workspace: another volume


async def test_a_configuration_that_would_mount_a_module_credential_refuses_to_spawn():
    s = spawner(volumes=[{"name": "oops", "secret": {"secretName": "booth-workload-minting-credentials"}}])
    with pytest.raises(UnsafePodSpec, match="booth-workload-minting-credentials"):
        await s.get_pod_manifest()


async def test_a_configuration_that_would_mount_a_service_account_token_refuses_to_spawn():
    s = spawner()
    s.automount_service_account_token = True
    with pytest.raises(UnsafePodSpec, match="service-account"):
        await s.get_pod_manifest()


async def test_privileged_extra_container_config_refuses_to_spawn():
    s = spawner(extra_container_config={"securityContext": {"privileged": True}})
    with pytest.raises(UnsafePodSpec):
        await s.get_pod_manifest()


def _pod_with_env(name, secret=None):
    from kubernetes_asyncio.client import models as m

    env = m.V1EnvVar(name=name, value_from=m.V1EnvVarSource(secret_key_ref=m.V1SecretKeySelector(name=secret, key="k"))) if secret else m.V1EnvVar(name=name, value="x")
    return m.V1Pod(spec=m.V1PodSpec(automount_service_account_token=False, containers=[m.V1Container(name="c", env=[env])]))


def test_check_pod_catches_secret_env_refs_and_hub_setting_names():
    with pytest.raises(UnsafePodSpec):
        check_pod(_pod_with_env("X", secret="booth-database-credentials"))
    with pytest.raises(UnsafePodSpec):
        check_pod(_pod_with_env("BOOTH_NOTEBOOKS_DATABASE_DSN"))
    check_pod(_pod_with_env("HARMLESS"))


async def test_operator_profiles_are_passed_through():
    s = spawner({"BOOTH_NOTEBOOKS_PROFILES": '[{"display_name": "R", "kubespawner_override": {"image": "r:1"}}]'})
    assert s.profile_list[0]["display_name"] == "R"

