"""In-cluster stand-in for the booth-core surfaces a notebook deployment touches (integration test only).

Runs from a ConfigMap on a stock python image, stdlib only:
* the identity-assertion issuer (discovery + JWKS; the test holds the private key and signs assertions),
* ``POST /api/internal/workload-tokens`` (ADR 0058 shapes), checking the minting credential,
* ``/modules/catalog/api/datasets`` as the gateway would route it, so a kernel's call can be observed,
* ``GET /_records`` — what was minted and which gateway calls arrived, for the test to assert on.

It is not booth-core: it proves this module's side of each contract, not core's.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ISSUER = os.environ["ISSUER"]
CREDENTIAL = os.environ["CREDENTIAL"]
RECORDS = {"mints": [], "gateway": []}


class H(BaseHTTPRequestHandler):
    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/iframe-identity/.well-known/openid-configuration":
            return self._json(200, {"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks.json"})
        if self.path == "/iframe-identity/jwks.json":
            # Read per request: a re-run of the test re-signs with a new key and updates the ConfigMap.
            with open("/config/jwks.json") as f:
                return self._json(200, json.load(f))
        if self.path == "/_records":
            return self._json(200, RECORDS)
        if self.path.startswith("/modules/catalog/api/datasets"):
            RECORDS["gateway"].append({"path": self.path, "auth": self.headers.get("Authorization"), "workspace": self.headers.get("X-Workspace")})
            return self._json(200, {"items": [{"id": "d1", "name": "from-fake-catalog", "location": {"backendId": "lake", "path": "x.csv"}}], "total": 1})
        if self.path == "/healthz":
            return self._json(200, {})
        self._json(404, {})

    def do_POST(self):
        if self.path != "/api/internal/workload-tokens":
            return self._json(404, {})
        if self.headers.get("Authorization") != "Bearer " + CREDENTIAL:
            return self._json(401, {"error": "bad credential"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RECORDS["mints"].append(body)
        self._json(200, {"token": "wl-{}".format(len(RECORDS["mints"])), "tokenType": "Bearer", "expiresAt": "2099-01-01T00:00:00Z", "role": "editor"})


ThreadingHTTPServer(("", 8080), H).serve_forever()
