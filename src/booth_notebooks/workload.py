"""Workload identity, minting side (ADR 0056/0058): a short-lived platform token for a notebook server.

A kernel has no human token to call booth-storage/booth-catalog with: the browser holds the user's
token in memory (ADR 0032) and it never reaches the iframe path, let alone a pod. So even an
interactive kernel calls other modules with a core-minted workload token — the same mechanism
booth-pipeline uses for scheduled runs, and the only answer the platform has for "code that is not the
browser calls a module".

Only the hub ever holds the minting credential (Secret ``booth-workload-minting-credentials``). The
hub never runs user code; kernels run in per-user pods that hold no module credential at all
(ADR 0057). A pod asks the hub for a token with its own JupyterHub API token (see
``handlers.PlatformTokenHandler``) and receives only the resulting 10-minute, role-ceilinged bearer.

A token names the *server*, never a person: ``sub`` = ``notebook:<hub-username>``. Core caps its role
at the lesser of our ceiling and what ``owner`` (the person's ``sub``) currently holds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Nothing a notebook does needs workspace administration, and a token that cannot administer cannot
# be turned into one. Core additionally caps this at the owner's live role (a viewer gets viewer).
ROLE_CEILING = "editor"

OWNER_NOT_CURRENT = (
    "booth-core refused to give this notebook platform access: you haven't signed in to the platform "
    "recently enough, or no longer have access to this workspace. Reload the notebook from the platform "
    "to sign in again."
)


class MintRefused(Exception):
    """Core said no (401/403). Expected, not a bug — retrying won't change the answer."""


class MintUnavailable(Exception):
    """Core could not be reached or errored. Worth retrying."""


@dataclass(frozen=True)
class WorkloadToken:
    token: str
    expires_at: datetime
    role: str


def subject_for(hub_username: str) -> str:
    # ADR 0058 grammar: ^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._:-]{1,200}$ — hub usernames are
    # "<workspace-slug>.<hex>", always inside the id alphabet.
    return f"notebook:{hub_username}"


class WorkloadMinter:
    def __init__(self, credential: str, url: str, issuer: str = "", transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0) -> None:
        self._credential = credential
        self._url = url
        self.issuer = issuer
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_dir(cls, directory: str) -> WorkloadMinter | None:
        """Load the Secret core wrote (keys ``credential``, ``url``, ``issuer``), or None when this
        deployment has none — notebooks still work, they just can't reach other modules."""
        d = Path(directory)
        try:
            cred = (d / "credential").read_text(encoding="utf-8").strip()
            url = (d / "url").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not cred or not url:
            return None
        try:
            issuer = (d / "issuer").read_text(encoding="utf-8").strip()
        except OSError:
            issuer = ""
        return cls(cred, url, issuer)

    async def mint(self, workspace: str, subject: str, owner: str, role_ceiling: str = ROLE_CEILING) -> WorkloadToken:
        body = {"workspace": workspace, "subject": subject, "roleCeiling": role_ceiling, "owner": owner}
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http:
                resp = await http.post(self._url, json=body, headers={"Authorization": f"Bearer {self._credential}"})
        except httpx.HTTPError as e:
            raise MintUnavailable(f"booth-core is unreachable: {e.__class__.__name__}") from e
        if resp.status_code == 403:
            log.warning("mint refused (403) for %s in %s", subject, workspace)
            raise MintRefused(OWNER_NOT_CURRENT)
        if resp.status_code == 401:
            # Our own credential was rejected: an operator problem, not the user's.
            raise MintRefused(
                "booth-core rejected this module's workload-minting credential (401); an operator must "
                "check the booth-workload-minting-credentials Secret"
            )
        if resp.status_code >= 500:
            raise MintUnavailable(f"booth-core returned HTTP {resp.status_code} while minting")
        if resp.status_code != 200:
            raise MintUnavailable(f"booth-core rejected the mint request (HTTP {resp.status_code})")
        try:
            data = resp.json()
            expires = datetime.fromisoformat(str(data["expiresAt"]).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            return WorkloadToken(str(data["token"]), expires, str(data.get("role", "")))
        except (ValueError, KeyError, TypeError) as e:
            raise MintUnavailable("booth-core returned an unreadable mint response") from e
