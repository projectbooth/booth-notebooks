"""KubeSpawner, hardened: a per-(person, workspace) pod that runs user code and holds nothing else.

A notebook pod executes arbitrary code a workspace member wrote, so per ADR 0057 it must hold none of
this module's standing credentials — not the hub database DSN, not the workload-minting credential,
not the hub's own secrets, no Kubernetes service-account token. The only credential it has is the one
JupyterHub always gives a single-user server: an API token scoped to that one user, which is how it
asks the hub for a short-lived platform token (``handlers.PlatformTokenHandler``).

Most of that is KubeSpawner configuration set in ``hubconfig.py``. This class adds what configuration
alone can't promise: a last check on the finished pod manifest that *refuses to create the pod* if
anything — a stray ``extra_pod_config``, an operator's ``volumes`` override — would mount a module
credential or a service-account token into it. A misconfiguration fails the spawn loudly instead of
quietly handing a tenant the keys.
"""

from __future__ import annotations

from kubespawner import KubeSpawner
from traitlets import Unicode

from .identity import workspace_of

WORKSPACE_LABEL = "booth.projectbooth.io/workspace"

# Secrets that must never be readable from a pod that runs user code.
FORBIDDEN_SECRETS = frozenset(
    {
        "booth-workload-minting-credentials",  # ADR 0056/0058 — mints tokens for any user of this module
        "booth-database-credentials",  # ADR 0053 — the hub's database: every user's servers and tokens
        "booth-event-bus-credentials",  # ADR 0050 — not declared today; never in a pod if it ever is
    }
)
# Env var name fragments that mean "a hub-side secret leaked into the pod".
_FORBIDDEN_ENV_FRAGMENTS = ("DSN", "DATABASE", "MINT", "CRYPT_KEY", "PROXY_AUTH", "COOKIE_SECRET")


class UnsafePodSpec(Exception):
    pass


def serialize(obj):
    """A kubernetes model (or a dict/list mixing models and dicts) as the API-server JSON shape —
    ``ApiClient.sanitize_for_serialization`` without constructing a client (which opens a session)."""
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [serialize(v) for v in obj]
    attribute_map = getattr(obj, "attribute_map", None)
    if attribute_map is not None and hasattr(obj, "openapi_types"):
        return {attribute_map[a]: serialize(getattr(obj, a)) for a in obj.openapi_types if getattr(obj, a) is not None}
    return obj


def check_pod(pod, extra_forbidden: frozenset[str] = frozenset()) -> None:
    """Raise ``UnsafePodSpec`` if the pod could read a module credential.

    ``pod`` is what KubeSpawner builds: a ``V1Pod`` whose parts are a mix of model objects and raw
    dicts (operator-supplied ``volumes``/``extra_container_config`` stay dicts). So the check runs on
    the *serialized* pod — exactly the JSON the API server will receive — never on model attributes,
    which would miss (or crash on) whatever is still a dict.
    """
    doc = serialize(pod)
    spec = doc.get("spec") or {}
    forbidden = FORBIDDEN_SECRETS | extra_forbidden
    if spec.get("automountServiceAccountToken") is not False:
        raise UnsafePodSpec("notebook pods must not mount a Kubernetes service-account token")
    for vol in spec.get("volumes") or []:
        name = (vol.get("secret") or {}).get("secretName")
        if name in forbidden:
            raise UnsafePodSpec(f"notebook pods must not mount the {name} Secret")
        for src in (vol.get("projected") or {}).get("sources") or []:
            if (src.get("secret") or {}).get("name") in forbidden:
                raise UnsafePodSpec(f"notebook pods must not mount the {src['secret']['name']} Secret")
            if src.get("serviceAccountToken") is not None:
                raise UnsafePodSpec("notebook pods must not mount a projected service-account token")
    for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for e in c.get("env") or []:
            ref = ((e.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
            if ref in forbidden:
                raise UnsafePodSpec(f"notebook pods must not read the {ref} Secret")
            if any(f in str(e.get("name", "")).upper() for f in _FORBIDDEN_ENV_FRAGMENTS):
                raise UnsafePodSpec(f"notebook pods must not receive hub-side setting {e.get('name')}")
        for ef in c.get("envFrom") or []:
            ref = (ef.get("secretRef") or {}).get("name")
            if ref in forbidden:
                raise UnsafePodSpec(f"notebook pods must not read the {ref} Secret")
        sc = c.get("securityContext") or {}
        if sc.get("privileged") or sc.get("allowPrivilegeEscalation"):
            raise UnsafePodSpec("notebook containers must not be privileged or allow privilege escalation")


class BoothSpawner(KubeSpawner):
    gateway_url = Unicode(
        "",
        config=True,
        help="booth-core's gateway base for module calls (BOOTH_CORE_URL/modules). Empty = kernels have no "
        "platform access, and the default kernel still works (ADR 0006: no other module is required).",
    )
    platform_token_path = Unicode("/booth/platform-token", help="Under JUPYTERHUB_API_URL.")

    def get_env(self):
        env = super().get_env()
        env["BOOTH_WORKSPACE"] = workspace_of(self.user.name)
        if self.gateway_url:
            env["BOOTH_GATEWAY_URL"] = self.gateway_url
        # JUPYTERHUB_API_URL is set by JupyterHub; the kernel client builds the token URL from it.
        env["BOOTH_PLATFORM_TOKEN_PATH"] = self.platform_token_path
        return env

    async def get_pod_manifest(self):
        # Every pod carries its workspace as a label: what a per-workspace NetworkPolicy, quota or
        # "stop everything in workspace X" operation selects on.
        self.extra_labels = {**self.extra_labels, WORKSPACE_LABEL: workspace_of(self.user.name)}
        pod = await super().get_pod_manifest()
        check_pod(pod)
        return pod
