"""Who is asking, and in which workspace: this module's side of core-platform-api.md "Auth enforcement".

JupyterHub has its own session (a hub cookie), but that cookie only says "this browser logged in as
X at some point". The platform's rules are stricter: every module re-verifies a token itself and
re-derives the caller's workspace role from the token's own groups claim, never trusting a forwarded
``X-Booth-Role`` header alone (ADR 0041). So the hub never logs anyone in, and never keeps them
logged in, on the strength of headers — only on a JWT verified here, against an issuer this
deployment was explicitly configured to trust.

Where that JWT comes from is the one open, cross-cutting question (see docs/decisions/0002): today
booth-core's iframe proxy forwards only ``X-Booth-Workspace``/``X-Booth-Role`` — no token and no
subject — so there is nothing verifiable to read. This module expects a signed identity assertion in
the ``X-Booth-Identity`` header (name configurable) and is written so that "which issuer signs it"
is configuration, not code: trusting booth-core as an assertion issuer, or the deployment's IdP
directly, is one more entry in ``trusted_issuers``.

Same claim grammar, same role-derivation and same fail-closed rules as booth-pipeline, booth-catalog
and booth-storage.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

OWNER, EDITOR, VIEWER = "owner", "editor", "viewer"
RANK = {OWNER: 3, EDITOR: 2, VIEWER: 1}

# ADR 0025's workspace-membership group shape.
_GROUP_RE = re.compile(r"^/workspaces/([a-z0-9-]+)/(owner|editor|viewer)$")
WORKSPACE_RE = re.compile(r"^[a-z0-9-]+$")

# ADR 0058: a workload token's `sub` is always `<kind>:<id>`, which no person's `sub` ever matches.
# A run's token must never log in to a notebook server as if it were a person, whichever issuer
# signed it — so this is checked on every token, not only ones from core's workload issuer.
_WORKLOAD_SUBJECT_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._:-]{1,200}$")

# Asymmetric algorithms only: never HS* (algorithm confusion against a public key) and never "none".
_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class TrustedIssuer:
    """One issuer whose tokens may identify a person here. ``audience`` empty = don't check ``aud``
    (a per-deployment policy choice, core-platform-api.md)."""

    url: str
    audience: str = ""


@dataclass(frozen=True)
class Claims:
    subject: str
    display_name: str
    groups: tuple[str, ...]
    issuer: str


class _IssuerKeys:
    """Lazy, retried OIDC discovery for one issuer. The hub must come up (and pass its health check)
    even if an issuer is momentarily unreachable, and start verifying once it is."""

    def __init__(self, issuer: TrustedIssuer, transport: httpx.BaseTransport | None = None) -> None:
        self.issuer = issuer
        self._jwks: PyJWKClient | None = None
        self._lock = threading.Lock()
        self._transport = transport

    def client(self) -> PyJWKClient:
        with self._lock:
            if self._jwks is None:
                with httpx.Client(transport=self._transport, timeout=10) as http:
                    resp = http.get(f"{self.issuer.url}/.well-known/openid-configuration")
                    resp.raise_for_status()
                    doc = resp.json()
                if doc.get("issuer", "").rstrip("/") != self.issuer.url:
                    raise AuthError("OIDC discovery document's issuer does not match the configured issuer")
                self._jwks = PyJWKClient(doc["jwks_uri"], cache_keys=True, lifespan=300)
            return self._jwks


class KeySource:
    """Resolves a token's signing key. Separate from ``Verifier`` so tests can supply keys directly."""

    def signing_key(self, issuer_url: str, raw_token: str):  # pragma: no cover - interface
        raise NotImplementedError


class DiscoveryKeySource(KeySource):
    def __init__(self, issuers: list[TrustedIssuer]) -> None:
        self._by_url = {i.url: _IssuerKeys(i) for i in issuers}

    def signing_key(self, issuer_url: str, raw_token: str):
        return self._by_url[issuer_url].client().get_signing_key_from_jwt(raw_token).key


class Verifier:
    """Verifies a JWT against whichever trusted issuer its ``iss`` names (dispatch by ``iss``, the
    same pattern core's gateway uses for its two issuers, ADR 0059). An untrusted ``iss`` is
    rejected before any network call is made on its behalf."""

    def __init__(self, issuers: list[TrustedIssuer], groups_claim: str = "groups", keys: KeySource | None = None) -> None:
        if not issuers:
            raise ValueError("at least one trusted issuer is required")
        self._issuers = {i.url.rstrip("/"): TrustedIssuer(i.url.rstrip("/"), i.audience) for i in issuers}
        self._groups_claim = groups_claim or "groups"
        self._keys = keys or DiscoveryKeySource(list(self._issuers.values()))

    def verify(self, raw_token: str) -> Claims:
        try:
            unverified = jwt.decode(raw_token, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed token: {e}") from e
        iss = str(unverified.get("iss", "")).rstrip("/")
        issuer = self._issuers.get(iss)
        if issuer is None:
            raise AuthError("token issuer is not trusted by this module")
        try:
            key = self._keys.signing_key(issuer.url, raw_token)
            payload = jwt.decode(
                raw_token,
                key,
                algorithms=_ALGORITHMS,
                issuer=issuer.url,
                audience=issuer.audience or None,
                options={"require": ["exp", "iss", "sub"], "verify_aud": bool(issuer.audience)},
            )
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, ValueError) as e:
            raise AuthError(f"token verification failed: {e}") from e
        claims = claims_from_payload(payload, self._groups_claim, issuer.url)
        if _WORKLOAD_SUBJECT_RE.match(claims.subject):
            raise AuthError("a workload (run) token cannot open a notebook session; only a person can")
        return claims


def claims_from_payload(payload: dict, groups_claim: str, issuer: str) -> Claims:
    raw_groups = payload.get(groups_claim)
    # A claim of the wrong shape means "no groups" (fail closed), not an error.
    groups = tuple(g for g in raw_groups if isinstance(g, str)) if isinstance(raw_groups, list) else ()
    display = payload["sub"]
    for name in ("email", "preferred_username"):  # later wins: preferred_username beats email
        v = payload.get(name)
        if isinstance(v, str) and v.strip():
            display = v.strip()
    return Claims(subject=str(payload["sub"]), display_name=display, groups=groups, issuer=issuer)


def role_in_workspace(groups: tuple[str, ...] | list[str], workspace: str) -> str:
    """The highest role the groups grant in ``workspace``, or ""."""
    best = ""
    for g in groups:
        m = _GROUP_RE.match(g)
        if m and m.group(1) == workspace and RANK[m.group(2)] > RANK.get(best, 0):
            best = m.group(2)
    return best


@dataclass(frozen=True)
class Identity:
    subject: str
    display_name: str
    workspace: str
    role: str

    @property
    def hub_username(self) -> str:
        return hub_username(self.workspace, self.subject)


def resolve(claims: Claims, workspace: str, forwarded_role: str = "", allowed_roles: frozenset[str] = frozenset(RANK)) -> Identity:
    """The whole authorization decision, as a pure function (unit-testable without a hub).

    ``workspace`` comes from the gateway's forwarded ``X-Booth-Workspace`` — which is safe to *read*
    because it can only ever select a workspace the verified token itself grants a role in. The role
    is the token's; a forwarded role stronger than that is a forged or corrupted request (ADR 0041).
    """
    if not workspace or not WORKSPACE_RE.match(workspace):
        raise AuthError("no (valid) active workspace on this request")
    granted = role_in_workspace(claims.groups, workspace)
    if not granted:
        raise AuthError("your token grants no role in this workspace")
    forwarded = (forwarded_role or "").strip()
    if forwarded and RANK.get(forwarded, 99) > RANK[granted]:
        raise AuthError("the forwarded role exceeds what your token grants in this workspace")
    # A gateway may legitimately narrow, never widen.
    role = forwarded if forwarded in RANK and RANK[forwarded] < RANK[granted] else granted
    if role not in allowed_roles:
        raise AuthError(f"the {role} role cannot open notebooks in this deployment")
    return Identity(claims.subject, claims.display_name, workspace, role)


def hub_username(workspace: str, subject: str) -> str:
    """One JupyterHub user per (person, workspace) — docs/decisions/0003.

    Isolation is the point: alice-in-acme and alice-in-beta are different hub users, so they get
    different pods, different home volumes and different kernel tokens, and losing membership of one
    workspace can never leave a path into its server via the other. The subject is hashed (not a
    display name) because it is the only stable, unique identifier a token carries; a username or
    email can be renamed or reused. ``.`` never appears in a workspace slug, so the split is
    unambiguous.
    """
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
    return f"{workspace}.{digest}"


def workspace_of(username: str) -> str:
    return username.split(".", 1)[0]
