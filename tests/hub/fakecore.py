"""A stand-in for the two booth-core surfaces the hub talks to, on a real local HTTP port.

* An identity-assertion issuer: an OIDC discovery document + JWKS, so the hub's real discovery and
  signature-verification path runs (not an injected key).
* ``POST /api/internal/workload-tokens`` with ADR 0058's request/response/refusal shapes.

Shapes follow ADR 0058 and booth-core's ``internal/api/workload.go`` as read; it is not booth-core.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

CREDENTIAL = "bwmc.notebooks.test-credential"


class FakeCore:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.mints: list[dict] = []
        self.refuse_mint = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.issuer = f"{self.url}/iframe-identity"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()

    def assertion(self, sub="user-1", groups=("/workspaces/acme/editor",), aud="notebooks", ttl=300) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": sub, "aud": aud, "iat": now, "exp": now + ttl, "groups": list(groups), "preferred_username": sub}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "core-1"})

    def _handler(self):
        core = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _json(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/iframe-identity/.well-known/openid-configuration":
                    return self._json(200, {"issuer": core.issuer, "jwks_uri": f"{core.url}/iframe-identity/jwks.json"})
                if self.path == "/iframe-identity/jwks.json":
                    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(core.key.public_key()))
                    jwk.update(kid="core-1", use="sig", alg="RS256")
                    return self._json(200, {"keys": [jwk]})
                self._json(404, {})

            def do_POST(self):
                if self.path != "/api/internal/workload-tokens":
                    return self._json(404, {})
                if self.headers.get("Authorization") != f"Bearer {CREDENTIAL}":
                    return self._json(401, {"error": "bad credential"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                core.mints.append(body)
                if core.refuse_mint:
                    return self._json(403, {"error": "not entitled"})
                self._json(200, {"token": f"wl-{len(core.mints)}", "tokenType": "Bearer", "expiresAt": "2099-01-01T00:00:00Z", "role": "editor"})

        return H
