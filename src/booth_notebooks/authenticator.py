"""JupyterHub authenticator: log a person in from a verified platform identity, and keep re-checking it.

Two things JupyterHub does by default are wrong for this platform, and this class exists to undo them:

1. **Its own login.** Users never see a JupyterHub login page. Every browser request reaches the hub
   through booth-core (the proxy only admits core, see the chart's NetworkPolicy), and carries a
   signed identity assertion (``identity.py``). ``auto_login`` + ``authenticate`` turn that into a hub
   user with no form.
2. **Trusting its own cookie indefinitely.** A hub cookie outlives any single token. So
   ``refresh_user`` re-verifies the *current* request's assertion on every browser request (see
   ``refresh_on_every_request`` for why ``auth_refresh_age`` alone doesn't achieve that): a person
   removed from the workspace, demoted below ``allowed_roles``, or who switched workspace in the
   shell stops being this hub user on their very next hub request. Switching workspace therefore
   transparently logs the browser in as the *other* (person, workspace) hub user.

Requests authenticated by a JupyterHub API token (a notebook pod calling the hub API, the idle
culler) are not browser sessions and don't carry an assertion; those are recognised by actually
re-resolving the token, never by the mere presence of an ``Authorization`` header (which a stale
cookie holder could add).
"""

from __future__ import annotations

import asyncio

from jupyterhub.auth import Authenticator
from tornado import web
from traitlets import Dict, List, Unicode

from . import identity as ident

AUTH_STATE_KEYS = ("sub", "workspace", "role", "displayName")


def refresh_on_every_request() -> None:
    """Make JupyterHub call ``refresh_user`` on every request, not "at most every
    ``auth_refresh_age`` seconds".

    ``auth_refresh_age`` can't do it: 0 *disables* refreshing, and at 1 JupyterHub skips the check
    for a second after the last refresh — and it records that time **per user, not per session**
    (``User._auth_refreshed``). So a stale or stolen hub cookie could ride on the real user's request
    from a moment earlier. Found by tests/hub (``test_the_hub_cookie_alone_is_worth_nothing``), not
    theorised. JupyterHub still refreshes at most once per request.

    This replaces a private JupyterHub attribute, so it fails loudly at hub startup — rather than
    silently weakening — if an upgrade renames it; tests/hub pins the behaviour either way.
    """
    from jupyterhub.user import User

    current = vars(User).get("_auth_refreshed", "missing")
    if isinstance(current, property):
        return
    if current is not None:
        raise RuntimeError(
            "jupyterhub.user.User._auth_refreshed has changed shape in this JupyterHub version; "
            "booth-notebooks cannot guarantee per-request re-verification (authenticator.refresh_on_every_request)"
        )
    User._auth_refreshed = property(lambda self: None, lambda self, value: None)


class BoothAuthenticator(Authenticator):
    auto_login = True
    login_service = "Project Booth"

    identity_header = Unicode(
        "X-Booth-Identity",
        config=True,
        help="Request header carrying the signed identity assertion (a JWT). Not `Authorization`: "
        "JupyterHub and jupyter-server both parse that header as their own API token.",
    )
    workspace_header = Unicode("X-Booth-Workspace", config=True, help="Gateway-forwarded active workspace (ADR 0025).")
    role_header = Unicode("X-Booth-Role", config=True, help="Gateway-forwarded role; read, never trusted alone (ADR 0041).")
    trusted_issuers = List(
        Dict(),
        config=True,
        help="[{url, audience}] issuers whose tokens may identify a person here.",
    )
    groups_claim = Unicode("groups", config=True, help="Must match booth-core's (ADR 0025).")
    allowed_roles = List(Unicode(), default_value=[ident.OWNER, ident.EDITOR, ident.VIEWER], config=True)

    _verifier: ident.Verifier | None = None

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        refresh_on_every_request()

    def _get_verifier(self) -> ident.Verifier:
        if self._verifier is None:
            issuers = [ident.TrustedIssuer(str(i["url"]).rstrip("/"), str(i.get("audience", ""))) for i in self.trusted_issuers]
            self._verifier = ident.Verifier(issuers, self.groups_claim)
        return self._verifier

    def set_verifier(self, verifier: ident.Verifier) -> None:
        """Tests (and nothing else) inject a verifier with in-memory keys."""
        self._verifier = verifier

    async def identify(self, handler) -> ident.Identity:
        headers = handler.request.headers
        raw = (headers.get(self.identity_header) or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            raise ident.AuthError("no platform identity on this request; open notebooks from the platform")
        # Discovery/JWKS fetches are blocking; keep them off the hub's event loop.
        claims = await asyncio.get_running_loop().run_in_executor(None, self._get_verifier().verify, raw)
        return ident.resolve(
            claims,
            (headers.get(self.workspace_header) or "").strip(),
            (headers.get(self.role_header) or "").strip(),
            frozenset(self.allowed_roles),
        )

    @staticmethod
    def _auth_state(who: ident.Identity) -> dict:
        return {"sub": who.subject, "workspace": who.workspace, "role": who.role, "displayName": who.display_name}

    async def authenticate(self, handler, data=None):
        try:
            who = await self.identify(handler)
        except ident.AuthError as e:
            self.log.warning("notebook login refused: %s", e)
            raise web.HTTPError(403, reason=str(e)) from None
        self.log.info("login: %s as %s in %s (%s)", who.display_name, who.hub_username, who.workspace, who.role)
        return {"name": who.hub_username, "auth_state": self._auth_state(who)}

    async def refresh_user(self, user, handler=None):
        if handler is None:
            # No request to re-verify (e.g. an internal call). The last verified state stands.
            return True
        token_user = handler.get_current_user_token()
        if token_user is not None and getattr(token_user, "name", None) == user.name:
            # A JupyterHub API token (a pod or the culler), not a browser session.
            return True
        try:
            who = await self.identify(handler)
        except ident.AuthError as e:
            self.log.info("session for %s no longer verifiable: %s", user.name, e)
            return False
        if who.hub_username != user.name:
            # Another person, or the same person in another workspace: log in again as them.
            return False
        fresh = self._auth_state(who)
        if await user.get_auth_state() == fresh:
            return True  # unchanged: skip the database write
        return {"auth_state": fresh}  # e.g. a role change, which the next platform token must reflect
