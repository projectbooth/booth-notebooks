"""The whole JupyterHub configuration, as a function of environment variables (the Helm chart sets them).

``jupyterhub_config.py`` in the hub image is one line: ``configure(c)``. Keeping it here, as a pure
function of an env mapping, is what lets unit tests pin the security-relevant settings (who can log
in, what a pod receives, how long a session lives) without starting a hub.

Names shared with every module (``BOOTH_OIDC_*``, ``BOOTH_CORE_URL``) match booth-pipeline/catalog/
storage so one set of operator values drives the fleet. Module-specific ones are ``BOOTH_NOTEBOOKS_*``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping

from .identity import EDITOR, OWNER, RANK, VIEWER
from .workload import WorkloadMinter


class ConfigError(Exception):
    pass


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    v = env.get(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], name: str, default: int, lo: int = 0) -> int:
    v = env.get(name)
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {v!r}") from None
    if n < lo:
        raise ConfigError(f"{name} must be >= {lo}, got {n}")
    return n


def sqlalchemy_url(dsn: str) -> str:
    """booth-core's ``dsn`` is a libpq URL (``postgres://`` or ``postgresql://``); SQLAlchemy wants a
    driver named. The hub image ships psycopg2."""
    for prefix in ("postgres://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg2://" + dsn[len(prefix) :]
    return dsn


def trusted_issuers(env: Mapping[str, str]) -> list[dict]:
    """Which issuers may identify a person to the hub (``identity.py``).

    * ``BOOTH_IDENTITY_ISSUER_URL`` — the signer of the per-request identity assertion booth-core's
      iframe proxy is proposed to forward (docs/decisions/0002). Audience defaults to this module's id.
    * ``BOOTH_OIDC_ISSUER_URL`` — the deployment's IdP, verified exactly like every other module does,
      for a deployment whose proxy forwards the user's own token instead.
    """
    out: list[dict] = []
    if env.get("BOOTH_IDENTITY_ISSUER_URL"):
        out.append({"url": env["BOOTH_IDENTITY_ISSUER_URL"].rstrip("/"), "audience": env.get("BOOTH_IDENTITY_AUDIENCE", "notebooks")})
    if env.get("BOOTH_OIDC_ISSUER_URL"):
        require_aud = _bool(env, "BOOTH_OIDC_REQUIRE_AUDIENCE", True)
        client_id = env.get("BOOTH_OIDC_CLIENT_ID", "")
        if require_aud and not client_id:
            raise ConfigError("BOOTH_OIDC_CLIENT_ID is required when BOOTH_OIDC_REQUIRE_AUDIENCE is true")
        out.append({"url": env["BOOTH_OIDC_ISSUER_URL"].rstrip("/"), "audience": client_id if require_aud else ""})
    if not out:
        raise ConfigError("set BOOTH_IDENTITY_ISSUER_URL and/or BOOTH_OIDC_ISSUER_URL: the hub verifies every login itself")
    return out


def configure(c, env: Mapping[str, str] | None = None) -> None:  # noqa: PLR0915 - one flat, readable config
    env = os.environ if env is None else env
    from .authenticator import BoothAuthenticator
    from .handlers import HealthHandler, PlatformTokenHandler
    from .spawner import BoothSpawner

    # ---- Hub process, proxy, database ---------------------------------------------------------
    c.JupyterHub.hub_bind_url = "http://0.0.0.0:8081"
    if env.get("BOOTH_NOTEBOOKS_HUB_CONNECT_URL"):
        c.JupyterHub.hub_connect_url = env["BOOTH_NOTEBOOKS_HUB_CONNECT_URL"]
    # configurable-http-proxy runs as its own Deployment so a hub restart never drops open notebooks.
    # Its auth token comes from CONFIGPROXY_AUTH_TOKEN, read by JupyterHub itself.
    c.ConfigurableHTTPProxy.should_start = False
    c.ConfigurableHTTPProxy.api_url = env.get("BOOTH_NOTEBOOKS_PROXY_API_URL", "http://127.0.0.1:8001")
    # Pods outlive the hub process: a hub upgrade must not stop everyone's server.
    c.JupyterHub.cleanup_servers = False
    c.JupyterHub.cleanup_proxy = False

    dsn = env.get("BOOTH_NOTEBOOKS_DATABASE_DSN", "")
    if dsn:
        c.JupyterHub.db_url = sqlalchemy_url(dsn)
    elif _bool(env, "BOOTH_NOTEBOOKS_DEV_SQLITE"):
        c.JupyterHub.db_url = "sqlite:////tmp/jupyterhub.sqlite"
    else:
        raise ConfigError(
            "BOOTH_NOTEBOOKS_DATABASE_DSN is required (core provisions it as the booth-database-credentials Secret, "
            "ADR 0053); set BOOTH_NOTEBOOKS_DEV_SQLITE=true for a throwaway local hub"
        )
    if not env.get("JUPYTERHUB_CRYPT_KEY"):
        # auth_state holds the person's `sub`, which minting needs as `owner` (ADR 0058). Without the
        # key JupyterHub silently drops auth_state and every kernel's platform access would fail.
        raise ConfigError("JUPYTERHUB_CRYPT_KEY is required (the chart generates one)")

    # ---- Authentication (identity.py, authenticator.py) -----------------------------------------
    c.JupyterHub.authenticator_class = BoothAuthenticator
    c.BoothAuthenticator.trusted_issuers = trusted_issuers(env)
    c.BoothAuthenticator.groups_claim = env.get("BOOTH_OIDC_GROUPS_CLAIM", "") or "groups"
    c.BoothAuthenticator.identity_header = env.get("BOOTH_NOTEBOOKS_IDENTITY_HEADER", "") or "X-Booth-Identity"
    roles = [r.strip() for r in env.get("BOOTH_NOTEBOOKS_ALLOWED_ROLES", f"{OWNER},{EDITOR},{VIEWER}").split(",") if r.strip()]
    if not roles or any(r not in RANK for r in roles):
        raise ConfigError(f"BOOTH_NOTEBOOKS_ALLOWED_ROLES must be a non-empty subset of owner,editor,viewer, got {roles}")
    c.BoothAuthenticator.allowed_roles = roles
    # Authorization is the workspace role from the token, decided in the authenticator; there is no
    # separate JupyterHub allow-list to keep in sync with the IdP.
    c.Authenticator.allow_all = True
    c.Authenticator.enable_auth_state = True
    # Must be non-zero (0 DISABLES refreshing in JupyterHub). The authenticator then makes it every
    # request regardless (authenticator.refresh_on_every_request).
    c.Authenticator.auth_refresh_age = 1
    c.Authenticator.refresh_pre_spawn = True
    # Nobody is a JupyterHub admin: workspace roles are the only authority, and the hub's admin page
    # (start/stop/impersonate any user) has no workspace boundary.
    c.Authenticator.admin_users = set()
    c.JupyterHub.admin_access = False
    # The notebook server's own hub session. When it lapses, JupyterLab's API calls and websockets 403
    # (after up to 5 min of jupyter-server's token cache) and the open notebook breaks — measured on the
    # real stack (docs/decisions/0005), so it must not be the per-tab membership bound. That bound is
    # core's renewable iframe session (ADR 0069 C), which every request to the pod passes through and the
    # shell re-verifies against the live token every 10 min. Default: the hub cookie's own lifetime.
    c.JupyterHub.cookie_max_age_days = 1
    c.JupyterHub.oauth_token_expires_in = _int(env, "BOOTH_NOTEBOOKS_SESSION_SECONDS", 86400, 60)
    c.JupyterHub.default_url = "/hub/spawn"
    c.JupyterHub.allow_named_servers = False

    # The page is framed by the shell (ADR 0005). Jupyter's default CSP is frame-ancestors 'self', which
    # only works when the shell and core's iframe origin are the same origin.
    ancestors = env.get("BOOTH_NOTEBOOKS_FRAME_ANCESTORS", "") or "'self'"
    c.JupyterHub.tornado_settings = {"headers": {"Content-Security-Policy": f"frame-ancestors {ancestors}"}}

    # ---- Kernels' platform access (ADR 0056/0057) -----------------------------------------------
    core_url = env.get("BOOTH_CORE_URL", "").rstrip("/")
    gateway = f"{core_url}/modules" if core_url else ""
    minter = WorkloadMinter.from_dir(env.get("BOOTH_WORKLOAD_MINT_DIR", "/etc/booth/workload"))
    c.JupyterHub.extra_handlers = [
        (r"/booth/healthz", HealthHandler, {"platform_access": minter is not None}),
        (r"/api/booth/platform-token", PlatformTokenHandler, {"minter": minter, "gateway_url": gateway}),
    ]

    # ---- Spawner ------------------------------------------------------------------------------
    c.JupyterHub.spawner_class = BoothSpawner
    s = c.BoothSpawner
    s.namespace = env.get("POD_NAMESPACE", "") or "default"
    s.gateway_url = gateway
    # ADR 0095: the postgres credential sidecar, behind the same boothDatabase.url gate as the ADR 0092
    # egress rule. It needs core (the broker) and a digest-pinned image; never a floating tag.
    booth_database_url = env.get("BOOTH_NOTEBOOKS_BOOTH_DATABASE_URL", "")
    if booth_database_url:
        image = env.get("BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE", "")
        if not re.search(r"@sha256:[0-9a-f]{64}$", image):
            raise ConfigError("BOOTH_NOTEBOOKS_CREDENTIAL_SIDECAR_IMAGE must be pinned by digest (…/credential-sidecar@sha256:<64 hex>), never a tag")
        if not core_url:
            raise ConfigError("boothDatabase.url needs BOOTH_CORE_URL: the credential sidecar calls booth-core's broker")
        s.booth_database_url = booth_database_url
        s.core_url = core_url
        s.credential_sidecar_image = image
        s.sidecar_renew_margin_seconds = _int(env, "BOOTH_NOTEBOOKS_SIDECAR_RENEW_MARGIN_SECONDS", 0, 0)
        s.sidecar_renew_interval_seconds = _int(env, "BOOTH_NOTEBOOKS_SIDECAR_RENEW_INTERVAL_SECONDS", 0, 0)
    s.image = env.get("BOOTH_NOTEBOOKS_SINGLEUSER_IMAGE", "ghcr.io/projectbooth/booth-notebooks-singleuser:0.1.0")
    s.image_pull_policy = env.get("BOOTH_NOTEBOOKS_SINGLEUSER_PULL_POLICY", "IfNotPresent")
    s.slug_scheme = "safe"
    s.start_timeout = _int(env, "BOOTH_NOTEBOOKS_START_TIMEOUT", 300, 30)
    s.http_timeout = 120
    s.cpu_guarantee = float(env.get("BOOTH_NOTEBOOKS_CPU_GUARANTEE", "0.1"))
    s.cpu_limit = float(env.get("BOOTH_NOTEBOOKS_CPU_LIMIT", "2"))
    s.mem_guarantee = env.get("BOOTH_NOTEBOOKS_MEM_GUARANTEE", "512M")
    s.mem_limit = env.get("BOOTH_NOTEBOOKS_MEM_LIMIT", "2G")
    # Nothing from the hub's own environment leaks into a pod (Spawner.env_keep defaults to PATH etc.).
    s.env_keep = []
    s.environment = {"BOOTH_FRAME_ANCESTORS": ancestors}
    # The pod runs user code: no API token for Kubernetes, no privilege, no capabilities (ADR 0057).
    s.service_account = ""
    s.automount_service_account_token = False
    s.uid = 1000  # jovyan, docker-stacks' notebook user
    s.gid = 100
    s.fs_gid = 100
    s.privileged = False
    s.allow_privilege_escalation = False
    s.container_security_context = {
        "runAsNonRoot": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    s.extra_labels = {"app.kubernetes.io/part-of": "booth-notebooks"}
    s.common_labels = {"app.kubernetes.io/managed-by": "booth-notebooks-hub"}
    s.delete_stopped_pods = True
    # Notebook files: one volume per (person, workspace) hub user, so a home directory can never be
    # shared across workspaces. Kept when a server stops (that's the point) and when the module is
    # uninstalled (Helm never created them) — see README "Data lifecycle".
    if _bool(env, "BOOTH_NOTEBOOKS_STORAGE_ENABLED", True):
        s.storage_pvc_ensure = True
        s.pvc_name_template = "claim-{user_server}"
        s.storage_capacity = env.get("BOOTH_NOTEBOOKS_STORAGE_CAPACITY", "10Gi")
        if env.get("BOOTH_NOTEBOOKS_STORAGE_CLASS"):
            s.storage_class = env["BOOTH_NOTEBOOKS_STORAGE_CLASS"]
        s.volumes = [{"name": "home", "persistentVolumeClaim": {"claimName": "{pvc_name}"}}]
        s.volume_mounts = [{"name": "home", "mountPath": "/home/jovyan"}]
    # Optional operator-defined environments (e.g. an R image) — docs/decisions/0001. Empty = the one
    # documented default Python kernel, and no chooser page.
    profiles = env.get("BOOTH_NOTEBOOKS_PROFILES", "")
    if profiles:
        try:
            s.profile_list = json.loads(profiles)
        except ValueError:
            raise ConfigError("BOOTH_NOTEBOOKS_PROFILES must be a JSON list (KubeSpawner profile_list)") from None

    # ---- Teardown: idle servers are stopped and their pods deleted ------------------------------
    cull = _int(env, "BOOTH_NOTEBOOKS_CULL_IDLE_SECONDS", 3600, 0)
    services, roles_cfg = [], []
    if cull:
        services.append(
            {
                "name": "idle-culler",
                # The culler runs inside the hub pod: talk to the hub on loopback, not through the hub's own
                # Service (a pod reaching itself via its Service needs hairpin NAT, which not every CNI
                # does — found on kind).
                "command": shlex.split(
                    f"{sys.executable} -m jupyterhub_idle_culler --url=http://127.0.0.1:8081/hub/api "
                    f"--timeout={cull} --cull-every={max(60, cull // 6)}"
                ),
            }
        )
        roles_cfg.append(
            {
                "name": "idle-culler",
                "scopes": ["list:users", "read:users:activity", "read:servers", "delete:servers"],
                "services": ["idle-culler"],
            }
        )
    c.JupyterHub.services = services
    c.JupyterHub.load_roles = roles_cfg
