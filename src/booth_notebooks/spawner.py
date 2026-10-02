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

import hashlib
import json

from kubespawner import KubeSpawner
from traitlets import Integer, Unicode

from .identity import EDITOR, OWNER, workspace_of

WORKSPACE_LABEL = "booth.projectbooth.io/workspace"

# ADR 0095: booth-core's credential sidecar, postgres mode. Loopback only, by contract.
SIDECAR_LISTEN = "127.0.0.1:5432"
SIDECAR_DIR = "/var/run/booth-sidecar"  # memory-backed, shared by the token helper and the sidecar only
SIDECAR_TOKEN_FILE = f"{SIDECAR_DIR}/token"
SIDECAR_VOLUME = "booth-sidecar"
_LOOPBACK_URL_PREFIXES = ("postgresql://localhost:", "postgresql://127.0.0.1:")


def _literal(value: str) -> str:
    """KubeSpawner str.format()s every string in extra_containers (for "{username}" templating), so a
    literal brace — the JSON in --scope — must be doubled to survive as itself."""
    return value.replace("{", "{{").replace("}", "}}")


def workspace_database(workspace: str) -> str:
    """The workspace's database name in booth-database (its internal/naming.ForWorkspace). Informational
    only: the sidecar always connects to the database its credential names, whatever a client asks for,
    so this just makes DATABASE_URL and current_database() agree."""
    return "bdb_ws_" + hashlib.sha256(f"booth-database/workspace/{workspace}".encode()).hexdigest()[:24]

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
            value = str(e.get("value", ""))
            if e.get("name") == "DATABASE_URL" and value.startswith(_LOOPBACK_URL_PREFIXES) and "@" not in value:
                continue  # the credential sidecar's loopback listener (ADR 0095): no host, no credential in it
            if any(f in str(e.get("name", "")).upper() for f in _FORBIDDEN_ENV_FRAGMENTS):
                raise UnsafePodSpec(f"notebook pods must not receive hub-side setting {e.get('name')}")
        args = [str(a) for a in (c.get("args") or [])]
        if any(a.startswith("--kind=") for a in args):
            listen = next((a.split("=", 1)[1] for a in args if a.startswith("--listen=")), SIDECAR_LISTEN)
            if not listen.startswith(("127.0.0.1:", "localhost:", "unix://")):
                raise UnsafePodSpec(f"the credential sidecar must listen on loopback only, not {listen}")
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

    # ADR 0095: native Postgres access to a workspace's booth-database through booth-core's credential
    # sidecar. Gated by the same boothDatabase.url value as the ADR 0092 egress rule: unset means no
    # sidecar, no token helper, no DATABASE_URL.
    booth_database_url = Unicode("", config=True, help="Non-empty enables the postgres credential sidecar.")
    core_url = Unicode("", config=True, help="booth-core's base URL; the sidecar calls its credential broker.")
    credential_sidecar_image = Unicode("", config=True, help="ghcr.io/projectbooth/credential-sidecar@sha256:... (pinned).")
    sidecar_renew_margin_seconds = Integer(0, config=True, help="0 = the sidecar's own default.")
    sidecar_renew_interval_seconds = Integer(0, config=True, help="0 = the sidecar's own default.")

    @property
    def database_sidecar_enabled(self) -> bool:
        return bool(self.booth_database_url)

    def get_env(self):
        env = super().get_env()
        env["BOOTH_WORKSPACE"] = workspace_of(self.user.name)
        if self.gateway_url:
            env["BOOTH_GATEWAY_URL"] = self.gateway_url
        # JUPYTERHUB_API_URL is set by JupyterHub; the kernel client builds the token URL from it.
        env["BOOTH_PLATFORM_TOKEN_PATH"] = self.platform_token_path
        if self.database_sidecar_enabled:
            # Read like any other DATABASE_URL: no host, no credential, no platform knowledge in it. The
            # sidecar on loopback holds all of that (ADR 0095).
            env["DATABASE_URL"] = f"postgresql://localhost:5432/{workspace_database(workspace_of(self.user.name))}"
        return env

    def database_sidecar_containers(self, env: dict, access: str) -> list[dict]:
        """The token helper and the postgres-mode credential sidecar (contracts/credential-sidecar.md)."""
        ws = workspace_of(self.user.name)
        locked = {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        token_env = ["JUPYTERHUB_API_URL", "JUPYTERHUB_API_TOKEN", "BOOTH_WORKSPACE", "BOOTH_PLATFORM_TOKEN_PATH"]
        sidecar_env = []
        if self.sidecar_renew_margin_seconds:
            sidecar_env.append({"name": "RENEW_MARGIN_SECONDS", "value": str(self.sidecar_renew_margin_seconds)})
        if self.sidecar_renew_interval_seconds:
            sidecar_env.append({"name": "RENEW_INTERVAL_SECONDS", "value": str(self.sidecar_renew_interval_seconds)})
        return [
            {
                # Keeps the notebook's own platform token (booth.platform_token(), ADR 0056) fresh in a
                # file for the sidecar, and runs the sidecar's readiness check: its /healthz is
                # loopback-only and its image distroless, so neither a kubelet httpGet nor an exec in its
                # own container can reach it. This container shares the pod's network namespace.
                "name": "booth-token",
                "image": self.image,
                "imagePullPolicy": self.image_pull_policy,
                "command": ["python", "-m", "booth.sidecar_token", "write", SIDECAR_TOKEN_FILE],
                "env": [{"name": k, "value": _literal(str(env[k]))} for k in token_env if k in env],
                "volumeMounts": [{"name": SIDECAR_VOLUME, "mountPath": SIDECAR_DIR}],
                "securityContext": {**locked, "runAsUser": 1000, "runAsGroup": 100},
                "readinessProbe": {
                    "exec": {"command": ["python", "-m", "booth.sidecar_token", "probe", f"http://{SIDECAR_LISTEN}/healthz"]},
                    "periodSeconds": 5,
                    "timeoutSeconds": 5,
                },
                "resources": {"requests": {"cpu": "10m", "memory": "48Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}},
            },
            {
                "name": "credential-sidecar",
                "image": self.credential_sidecar_image,
                "imagePullPolicy": "IfNotPresent",
                "args": [
                    "--kind=postgres",
                    "--scope=" + _literal(json.dumps({"workspace": ws}, separators=(",", ":"))),
                    f"--access={access}",
                    f"--listen={SIDECAR_LISTEN}",
                    f"--core-url={self.core_url}",
                    f"--workspace={ws}",
                    f"--token-file={SIDECAR_TOKEN_FILE}",
                ],
                "env": sidecar_env,
                "volumeMounts": [{"name": SIDECAR_VOLUME, "mountPath": SIDECAR_DIR, "readOnly": True}],
                # distroless nonroot (65532); reads the 0640 token file through the pod's fsGroup (100)
                "securityContext": {**locked, "runAsUser": 65532, "runAsGroup": 65532, "readOnlyRootFilesystem": True},
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}},
            },
        ]

    async def get_pod_manifest(self):
        # Every pod carries its workspace as a label: what a per-workspace NetworkPolicy, quota or
        # "stop everything in workspace X" operation selects on.
        self.extra_labels = {**self.extra_labels, WORKSPACE_LABEL: workspace_of(self.user.name)}
        saved = (self.extra_containers, self.volumes)
        if self.database_sidecar_enabled:
            # The broker refuses `readwrite` to a viewer, and a refusal makes the sidecar exit (contract:
            # a config error should crash-loop visibly). Viewers may open notebooks (ADR 0070), so ask for
            # exactly what the person's role allows. The token is role-capped live either way.
            state = await self.user.get_auth_state() or {}
            access = "readwrite" if state.get("role") in (OWNER, EDITOR) else "read"
            self.extra_containers = [*saved[0], *self.database_sidecar_containers(self.get_env(), access)]
            self.volumes = [*saved[1], {"name": SIDECAR_VOLUME, "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}}]
        try:
            pod = await super().get_pod_manifest()
        finally:
            self.extra_containers, self.volumes = saved
        check_pod(pod)
        return pod
