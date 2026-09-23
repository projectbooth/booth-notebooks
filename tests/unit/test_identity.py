"""identity.py: token verification, role derivation (ADR 0025/0041) and the hub-user mapping."""

from __future__ import annotations

import jwt
import pytest

from booth_notebooks import identity as ident
from booth_notebooks.identity import AuthError, TrustedIssuer, Verifier

from .keys import IDP, ISSUER, Signer, StaticKeys, verifier


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer(ISSUER)


def test_a_valid_token_yields_its_claims(signer):
    c = verifier(signer).verify(signer.token(sub="u-1", preferred_username="alice"))
    assert (c.subject, c.display_name, c.groups, c.issuer) == ("u-1", "alice", ("/workspaces/acme/editor",), ISSUER)


def test_an_untrusted_issuer_is_rejected_before_any_key_lookup(signer):
    other = Signer("https://evil.example.test")
    with pytest.raises(AuthError, match="not trusted"):
        verifier(signer).verify(other.token())


def test_a_token_signed_by_the_wrong_key_is_rejected(signer):
    impostor = Signer(ISSUER)  # same iss claim, different key
    with pytest.raises(AuthError, match="verification failed"):
        verifier(signer).verify(impostor.token())


def test_expired_and_wrong_audience_tokens_are_rejected(signer):
    v = verifier(signer)
    with pytest.raises(AuthError):
        v.verify(signer.token(ttl=-60))
    with pytest.raises(AuthError):
        v.verify(signer.token(aud="catalog"))


def test_audience_is_not_checked_when_not_configured(signer):
    v = Verifier([TrustedIssuer(ISSUER, "")], keys=StaticKeys(signer))
    assert v.verify(signer.token(aud=None)).subject == "user-1"


def test_symmetric_and_unsigned_tokens_are_never_accepted(signer):
    # Algorithm confusion: an HS256 token whose HMAC key is the issuer's *public* key (which anyone
    # can fetch) must not verify. Forged by hand, since PyJWT itself refuses to produce one.
    import base64
    import hashlib
    import hmac
    import json

    from cryptography.hazmat.primitives import serialization

    pem = signer.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    body = b64(json.dumps({"iss": ISSUER, "sub": "x", "aud": "notebooks", "exp": 9999999999, "groups": ["/workspaces/acme/owner"]}).encode())
    sig = b64(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
    with pytest.raises(AuthError):
        verifier(signer).verify(f"{head}.{body}.{sig}")
    unsigned = jwt.encode({"iss": ISSUER, "sub": "x", "aud": "notebooks", "exp": 9999999999}, None, algorithm="none")
    with pytest.raises(AuthError):
        verifier(signer).verify(unsigned)


def test_a_workload_token_can_never_log_in_as_a_person(signer):
    """ADR 0058: `<kind>:<id>` subjects are runs. A pipeline job's token must not open a notebook."""
    with pytest.raises(AuthError, match="workload"):
        verifier(signer).verify(signer.token(sub="run:42"))
    with pytest.raises(AuthError, match="workload"):
        verifier(signer).verify(signer.token(sub="notebook:acme.abc"))


def test_a_malformed_groups_claim_means_no_groups(signer):
    c = verifier(signer).verify(signer.token(groups="not-a-list"))
    assert c.groups == ()


def test_a_custom_groups_claim_name(signer):
    tok = signer.token(groups=(), memberships=["/workspaces/acme/owner"])
    c = verifier(signer, groups_claim="memberships").verify(tok)
    assert ident.role_in_workspace(c.groups, "acme") == "owner"


def test_multiple_issuers_dispatch_by_iss(signer):
    idp = Signer(IDP)
    v = Verifier([TrustedIssuer(ISSUER, "notebooks"), TrustedIssuer(IDP, "booth-design")], keys=StaticKeys(signer, idp))
    assert v.verify(idp.token(aud="booth-design")).issuer == IDP
    with pytest.raises(AuthError):  # each issuer's audience is its own
        v.verify(idp.token(aud="notebooks"))


# ---- resolve(): the authorization decision ------------------------------------------------------


def claims(*groups: str) -> ident.Claims:
    return ident.Claims("user-1", "alice", tuple(groups), ISSUER)


def test_the_role_is_the_tokens_highest_in_the_forwarded_workspace():
    who = ident.resolve(claims("/workspaces/acme/viewer", "/workspaces/acme/editor", "/workspaces/beta/owner"), "acme")
    assert (who.workspace, who.role) == ("acme", "editor")


def test_a_workspace_the_token_does_not_grant_is_refused():
    with pytest.raises(AuthError, match="no role"):
        ident.resolve(claims("/workspaces/acme/owner"), "beta")


def test_missing_or_malformed_workspace_is_refused():
    for ws in ("", "ACME", "a/b", "acme.x"):
        with pytest.raises(AuthError):
            ident.resolve(claims("/workspaces/acme/owner"), ws)


def test_a_forwarded_role_stronger_than_the_token_is_rejected_not_downgraded():
    """ADR 0041: a forged X-Booth-Role is an error, never silently accepted."""
    with pytest.raises(AuthError, match="exceeds"):
        ident.resolve(claims("/workspaces/acme/viewer"), "acme", "owner")
    with pytest.raises(AuthError, match="exceeds"):
        ident.resolve(claims("/workspaces/acme/viewer"), "acme", "superuser")


def test_a_gateway_may_narrow_the_role():
    assert ident.resolve(claims("/workspaces/acme/owner"), "acme", "viewer").role == "viewer"


def test_allowed_roles_gate_who_may_open_notebooks():
    with pytest.raises(AuthError, match="cannot open notebooks"):
        ident.resolve(claims("/workspaces/acme/viewer"), "acme", allowed_roles=frozenset({"owner", "editor"}))


def test_hub_usernames_are_per_person_per_workspace_and_stable():
    a1 = ident.hub_username("acme", "user-1")
    assert a1 == ident.hub_username("acme", "user-1")
    assert a1 != ident.hub_username("beta", "user-1")  # same person, other workspace: another hub user
    assert a1 != ident.hub_username("acme", "user-2")
    assert a1.startswith("acme.") and ident.workspace_of(a1) == "acme"
    assert "user-1" not in a1  # the subject is hashed, never embedded
