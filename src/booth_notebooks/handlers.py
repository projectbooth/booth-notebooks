"""Two hub endpoints this module adds: a health check for booth-core, and platform tokens for kernels.

Both are registered via ``JupyterHub.extra_handlers`` — deprecated since JupyterHub 3.1 in favour of
hub-managed services, still fully supported in 5.x (pinned ``<6``). A service would add a second
process, port and NetworkPolicy rule for two small handlers; revisit before JupyterHub 6 (see
docs/decisions/0003).
"""

from __future__ import annotations

import json

from jupyterhub.apihandlers.base import APIHandler
from jupyterhub.handlers.base import BaseHandler
from jupyterhub.user import User
from sqlalchemy import text
from tornado import web

from . import __version__
from .identity import workspace_of
from .workload import MintRefused, MintUnavailable, WorkloadMinter, subject_for


class HealthHandler(BaseHandler):
    """``GET /hub/booth/healthz`` — the manifest's ``healthCheckPath``, polled by booth-core via the
    proxy (so a dead proxy *or* a dead hub both show as unhealthy). Unauthenticated, like every
    module's health check. Checks the hub database, since a hub that cannot read it cannot route
    anyone to their server."""

    def initialize(self, platform_access: bool = False) -> None:
        self._platform_access = platform_access

    def check_xsrf_cookie(self):  # a GET health probe carries no session
        return

    async def get(self):
        body = {"status": "ok", "module": "notebooks", "version": __version__, "platformAccess": self._platform_access}
        try:
            self.db.execute(text("SELECT 1"))
        except Exception as e:  # noqa: BLE001 - any database failure is "unhealthy", with the reason
            self.db.rollback()
            body.update(status="unavailable", error=f"hub database: {e.__class__.__name__}")
            self.set_status(503)
        self.set_header("Content-Type", "application/json")
        self.set_header("Cache-Control", "no-store")
        self.finish(json.dumps(body))


class PlatformTokenHandler(APIHandler):
    """``POST /hub/api/booth/platform-token`` — a notebook pod's way to get a booth-core workload
    token for its own (person, workspace), ADR 0056/0057.

    Authenticated **only** by a JupyterHub API token (the pod's ``JUPYTERHUB_API_TOKEN``, scoped by
    JupyterHub to that one user), never by a browser cookie: the browser has no use for a kernel's
    token, and refusing cookies means a page script can't mint one either. The caller never names a
    workspace, owner or role — all three come from the hub's own verified record of the user, so a
    pod can only ever get a token for exactly the identity it was spawned for.
    """

    def initialize(self, minter: WorkloadMinter | None = None, gateway_url: str = "") -> None:
        self._minter = minter
        self._gateway_url = gateway_url

    def check_xsrf_cookie(self):
        # Token-authenticated only (checked below); cookie sessions are refused outright, so there is
        # no cross-site cookie request to defend against here.
        return

    async def post(self):
        user = self.get_current_user_token()
        if not isinstance(user, User):
            raise web.HTTPError(403, "a notebook server's JupyterHub API token is required")
        state = await user.get_auth_state() or {}
        workspace, owner = state.get("workspace", ""), state.get("sub", "")
        if not owner or workspace != workspace_of(user.name):
            # No verified record of who this user is (e.g. auth_state lost): don't guess.
            raise web.HTTPError(403, "no verified platform identity for this user; open notebooks from the platform again")
        if self._minter is None:
            raise web.HTTPError(
                503,
                "this deployment gives notebooks no platform access (booth-core workload identity is not configured), "
                "so kernels cannot call storage or the catalog",
            )
        try:
            tok = await self._minter.mint(workspace, subject_for(user.name), owner)
        except MintRefused as e:
            raise web.HTTPError(403, str(e)) from None
        except MintUnavailable as e:
            raise web.HTTPError(503, str(e)) from None
        self.log.info("platform token for %s in %s (role %s)", user.name, workspace, tok.role)
        self.set_header("Cache-Control", "no-store")
        self.write(
            {
                "token": tok.token,
                "expiresAt": tok.expires_at.isoformat(),
                "role": tok.role,
                "workspace": workspace,
                "gatewayUrl": self._gateway_url,
            }
        )
