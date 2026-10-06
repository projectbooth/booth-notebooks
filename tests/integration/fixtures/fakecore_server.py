"""In-cluster stand-in for the booth-core surfaces a notebook deployment touches (integration test only).

Runs from a ConfigMap on a stock python image, stdlib only:
* the identity-assertion issuer (discovery + JWKS; the test holds the private key and signs assertions),
* ``POST /api/internal/workload-tokens`` (ADR 0058 shapes), checking the minting credential,
* ``/modules/catalog/api/datasets`` as the gateway would route it, so a kernel's call can be observed,
* ``/modules/lakehouse/api/warehouse``: a warehouse for workspace "beta", 404 for any other,
* ``POST /api/credentials``: the credential broker, s3 kind only, leasing the stand-in MinIO (fixtures/minio.yaml),
* ``GET /_records`` — what was minted and which gateway calls arrived, for the test to assert on.

It is not booth-core: it proves this module's side of each contract, not core's.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ISSUER = os.environ["ISSUER"]
CREDENTIAL = os.environ["CREDENTIAL"]
RECORDS = {"mints": [], "gateway": [], "lakehouse": [], "credentials": []}


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
        if self.path == "/modules/lakehouse/api/warehouse":
            # booth-lakehouse behind the gateway (ADR 0095 third amendment): "beta" has a warehouse,
            # every other workspace gets the real endpoint's 404.
            ws = self.headers.get("X-Workspace")
            RECORDS["lakehouse"].append({"auth": self.headers.get("Authorization"), "workspace": ws})
            if ws == "beta":
                return self._json(200, {"workspace": ws, "backendId": "lake", "path": "warehouses/beta", "warehouseName": "beta"})
            return self._json(404, {"detail": "this workspace has no lakehouse warehouse yet"})
        if self.path == "/healthz":
            return self._json(200, {})
        self._json(404, {})

    def do_POST(self):
        if self.path == "/api/credentials":
            # The credential broker, s3 kind only (ADR 0095): a lease on the stand-in MinIO in booth-storage's
            # s3CredentialBody shape. Every other kind is refused, so the postgres sidecar still never gets a
            # lease here (the "notebook starts anyway" guard depends on that).
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            RECORDS["credentials"].append({"kind": body.get("kind"), "scope": body.get("scope"), "access": body.get("access"),
                                           "workspace": self.headers.get("X-Workspace")})
            if body.get("kind") != "s3":
                return self._json(422, {"error": "scope_not_supported"})
            return self._json(200, {"leaseId": "lease-{}".format(len(RECORDS["credentials"])), "kind": "s3",
                                    "expiresAt": "2099-01-01T00:00:00Z", "scope": body.get("scope"),
                                    "credential": {"accessKeyId": "kindtest", "secretAccessKey": "kindtest-secret",
                                                   "endpoint": "http://minio.storage.svc.cluster.local:9000", "region": "us-east-1",
                                                   "bucket": "lake", "keyPrefix": "warehouses/beta", "pathStyle": True}})
        if self.path != "/api/internal/workload-tokens":
            return self._json(404, {})
        if self.headers.get("Authorization") != "Bearer " + CREDENTIAL:
            return self._json(401, {"error": "bad credential"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RECORDS["mints"].append(body)
        self._json(200, {"token": "wl-{}".format(len(RECORDS["mints"])), "tokenType": "Bearer", "expiresAt": "2099-01-01T00:00:00Z", "role": "editor"})


ThreadingHTTPServer(("", 8080), H).serve_forever()
