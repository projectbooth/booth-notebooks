"""BoothAuthenticator: login from a verified identity, and re-verification on every browser request."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tornado import web
from tornado.httputil import HTTPHeaders

from booth_notebooks.authenticator import BoothAuthenticator
from booth_notebooks.identity import hub_username

from .keys import ISSUER, Signer, verifier


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer(ISSUER)


@pytest.fixture
def auth(signer) -> BoothAuthenticator:
    a = BoothAuthenticator()
    a.set_verifier(verifier(signer))
    return a


def handler(headers: dict | None = None, token_user=None):
    return SimpleNamespace(request=SimpleNamespace(headers=HTTPHeaders(headers or {})), get_current_user_token=lambda: token_user)


class FakeUser:
    def __init__(self, name: str, state: dict | None = None) -> None:
        self.name = name
        self._state = state

    async def get_auth_state(self):
        return self._state


def browser(signer, workspace="acme", sub="user-1", role_header="", **tok):
    h = {"X-Booth-Identity": signer.token(sub=sub, **tok), "X-Booth-Workspace": workspace}
    if role_header:
        h["X-Booth-Role"] = role_header
    return handler(h)


async def test_login_maps_the_person_and_workspace_to_one_hub_user(auth, signer):
    out = await auth.authenticate(browser(signer, preferred_username="alice"))
    assert out["name"] == hub_username("acme", "user-1")
    assert out["auth_state"] == {"sub": "user-1", "workspace": "acme", "role": "editor", "displayName": "alice"}


async def test_login_accepts_a_bearer_prefixed_assertion(auth, signer):
    h = handler({"X-Booth-Identity": "Bearer " + signer.token(), "X-Booth-Workspace": "acme"})
    assert (await auth.authenticate(h))["name"] == hub_username("acme", "user-1")


@pytest.mark.parametrize(
    "headers",
    [
        {},  # nothing: someone reached the hub without going through core
        {"X-Booth-Workspace": "acme", "X-Booth-Role": "owner"},  # headers alone are never enough (ADR 0041)
        {"X-Booth-Identity": "garbage", "X-Booth-Workspace": "acme"},
    ],
)
async def test_login_without_a_verifiable_identity_is_forbidden(auth, headers):
    with pytest.raises(web.HTTPError) as e:
        await auth.authenticate(handler(headers))
    assert e.value.status_code == 403


async def test_login_is_refused_for_a_workspace_the_token_does_not_grant(auth, signer):
    with pytest.raises(web.HTTPError):
        await auth.authenticate(browser(signer, workspace="beta"))


async def test_login_is_refused_for_a_forged_stronger_role_header(auth, signer):
    with pytest.raises(web.HTTPError):
        await auth.authenticate(browser(signer, role_header="owner"))


async def test_the_identity_header_name_is_configurable(signer):
    a = BoothAuthenticator(identity_header="X-Other")
    a.set_verifier(verifier(signer))
    h = handler({"X-Other": signer.token(), "X-Booth-Workspace": "acme"})
    assert (await a.authenticate(h))["name"].startswith("acme.")


# ---- refresh_user: the session is only as good as the current request's identity ---------------


def state(role="editor", workspace="acme", sub="user-1"):
    return {"sub": sub, "workspace": workspace, "role": role, "displayName": "user-1"}


async def test_an_unchanged_identity_keeps_the_session_without_a_write(auth, signer):
    user = FakeUser(hub_username("acme", "user-1"), state())
    assert await auth.refresh_user(user, browser(signer)) is True


async def test_a_role_change_is_recorded(auth, signer):
    user = FakeUser(hub_username("acme", "user-1"), state(role="owner"))
    out = await auth.refresh_user(user, browser(signer))
    assert out == {"auth_state": state(role="editor")}


async def test_a_browser_request_without_an_identity_ends_the_session(auth):
    user = FakeUser(hub_username("acme", "user-1"), state())
    assert await auth.refresh_user(user, handler({})) is False


async def test_losing_the_workspace_membership_ends_the_session(auth, signer):
    user = FakeUser(hub_username("acme", "user-1"), state())
    assert await auth.refresh_user(user, browser(signer, groups=("/workspaces/beta/owner",))) is False


async def test_switching_workspace_in_the_shell_forces_a_login_as_the_other_hub_user(auth, signer):
    user = FakeUser(hub_username("acme", "user-1"), state())
    h = browser(signer, workspace="beta", groups=("/workspaces/acme/editor", "/workspaces/beta/editor"))
    assert await auth.refresh_user(user, h) is False
    assert (await auth.authenticate(h))["name"] == hub_username("beta", "user-1")


async def test_another_persons_identity_never_continues_this_session(auth, signer):
    user = FakeUser(hub_username("acme", "user-1"), state())
    assert await auth.refresh_user(user, browser(signer, sub="user-2")) is False


async def test_an_api_token_request_from_the_users_own_pod_is_not_a_browser_session(auth):
    user = FakeUser(hub_username("acme", "user-1"), state())
    assert await auth.refresh_user(user, handler({}, token_user=user)) is True


async def test_a_bogus_authorization_header_does_not_bypass_reverification(auth):
    """Someone holding only a stale hub cookie adds `Authorization: token junk`: the token doesn't
    resolve, so this is still a cookie session and must re-verify (and fail)."""
    user = FakeUser(hub_username("acme", "user-1"), state())
    assert await auth.refresh_user(user, handler({"Authorization": "token junk"}, token_user=None)) is False


async def test_another_users_api_token_does_not_vouch_for_this_session(auth):
    user = FakeUser(hub_username("acme", "user-1"), state())
    other = FakeUser(hub_username("acme", "user-2"))
    assert await auth.refresh_user(user, handler({}, token_user=other)) is False


async def test_no_handler_means_no_request_to_reverify(auth):
    assert await auth.refresh_user(FakeUser("acme.x", state()), None) is True
