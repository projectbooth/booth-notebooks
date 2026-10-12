"""ADR 0108's key-fetch override (``oidc.jwksUrl`` / ``BOOTH_OIDC_JWKS_URL``), through the real key path.

A plain-http JWKS server stands in for Keycloak's in-cluster Service; the issuer is an https:// URL that
can't be reached at all, as behind a self-signed Ingress. Discovery must never be contacted, and ``iss``
is still compared exactly with the configured issuer.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from traitlets.config import Config

from booth_notebooks.authenticator import BoothAuthenticator
from booth_notebooks.hubconfig import ConfigError, configure, trusted_issuers
from booth_notebooks.identity import AuthError, TrustedIssuer, Verifier

from .keys import Signer
from .test_hubconfig import ENV

UNREACHABLE_ISSUER = "https://keycloak.unreachable.invalid/realms/booth"  # .invalid never resolves (RFC 6761)


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer(UNREACHABLE_ISSUER)


@pytest.fixture(scope="module")
def jwks_server(signer):
    """Serves only /jwks. Every other path (discovery included) is recorded and answered 404."""
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signer.key.public_key()))
    jwk.update(kid=signer.kid, alg="RS256", use="sig")
    body = json.dumps({"keys": [jwk]}).encode()
    other_paths: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/jwks":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            other_paths.append(self.path)
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/jwks", other_paths
    srv.shutdown()


def real_verifier(jwks_url: str, audience: str = "notebooks") -> Verifier:
    return Verifier([TrustedIssuer(UNREACHABLE_ISSUER, audience, jwks_url)])  # the real DiscoveryKeySource


def test_a_valid_token_verifies_with_keys_from_jwks_url_and_an_unreachable_issuer(signer, jwks_server):
    url, _ = jwks_server
    claims = real_verifier(url).verify(signer.token(sub="u-1"))
    assert (claims.subject, claims.issuer) == ("u-1", UNREACHABLE_ISSUER)


def test_a_wrong_iss_with_the_same_key_is_rejected(signer, jwks_server):
    url, _ = jwks_server
    v = real_verifier(url)
    for iss in (UNREACHABLE_ISSUER + "x", "http://keycloak.unreachable.invalid/realms/booth", "https://evil.example.test"):
        with pytest.raises(AuthError):
            v.verify(Signer(iss, key=signer.key).token())


def test_discovery_is_never_contacted(signer, jwks_server):
    url, other_paths = jwks_server
    v = real_verifier(url)
    for _ in range(3):
        v.verify(signer.token())
    assert other_paths == []  # only /jwks; never /.well-known/openid-configuration, here or at the issuer


def test_audience_is_still_checked(signer, jwks_server):
    url, _ = jwks_server
    with pytest.raises(AuthError):
        real_verifier(url).verify(signer.token(aud="catalog"))


# ---- configuration --------------------------------------------------------------------------------

OIDC = {"BOOTH_OIDC_ISSUER_URL": UNREACHABLE_ISSUER, "BOOTH_OIDC_CLIENT_ID": "booth-design"}
JWKS = "http://keycloak.keycloak.svc.cluster.local:8080/realms/booth/protocol/openid-connect/certs"


def test_jwks_url_attaches_to_the_oidc_issuer_only():
    out = trusted_issuers({**ENV, **OIDC, "BOOTH_OIDC_JWKS_URL": JWKS})
    iframe, oidc = out
    assert "jwks_url" not in iframe  # core's iframe-identity issuer keeps discovery
    assert oidc == {"url": UNREACHABLE_ISSUER, "audience": "booth-design", "jwks_url": JWKS}


def test_unset_is_exactly_todays_configuration():
    assert trusted_issuers({**ENV, **OIDC}) == [
        {"url": ENV["BOOTH_IDENTITY_ISSUER_URL"], "audience": "notebooks"},
        {"url": UNREACHABLE_ISSUER, "audience": "booth-design"},
    ]


def test_jwks_url_without_an_issuer_url_is_a_startup_error():
    with pytest.raises(ConfigError, match="BOOTH_OIDC_ISSUER_URL"):
        trusted_issuers({**ENV, "BOOTH_OIDC_JWKS_URL": JWKS})
    with pytest.raises(ConfigError, match="BOOTH_OIDC_ISSUER_URL"):
        configure(Config(), {**ENV, "BOOTH_OIDC_JWKS_URL": JWKS})


def test_startup_logs_each_issuer_and_where_its_keys_come_from_once(caplog):
    c = Config()
    configure(c, {**ENV, **OIDC, "BOOTH_OIDC_JWKS_URL": JWKS})
    with caplog.at_level(logging.INFO):
        BoothAuthenticator(config=c)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("trusting issuer")]
    assert lines == [
        f"trusting issuer {ENV['BOOTH_IDENTITY_ISSUER_URL']}; signing keys from discovery ({ENV['BOOTH_IDENTITY_ISSUER_URL']}/.well-known/openid-configuration)",
        f"trusting issuer {UNREACHABLE_ISSUER}; signing keys from {JWKS}",
    ]
