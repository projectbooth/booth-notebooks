"""The kernel-side ``booth`` package, against a fake hub + gateway on a real local HTTP port."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from booth import BoothError, Client

DATASETS = [
    {"id": "d1", "name": "daily-sales", "location": {"backendId": "lake", "path": "sales/daily.csv"}},
    {"id": "d2", "name": "dup", "location": {"backendId": "lake", "path": "a.csv"}},
    {"id": "d3", "name": "dup", "location": {"backendId": "lake", "path": "b.csv"}},
    {"id": "d4", "name": "partitioned", "location": {"backendId": "lake", "path": "events/"}},
    {"id": "d5", "name": "blob", "location": {"backendId": "lake", "path": "model.bin"}},
    # ADR 0085: an Iceberg table registered as a dataset (a directory location, plus its table block)
    {"id": "d6", "name": "daily-orders", "format": "iceberg", "location": {"backendId": "lake", "path": "warehouse/sales/daily/"},
     "table": {"namespace": "sales", "name": "daily", "uuid": "u-1", "currentSnapshotId": 42}},
    {"id": "d7", "name": "broken-table", "format": "iceberg", "location": {"backendId": "lake", "path": "warehouse/x/"}},
    {"id": "d8", "name": "future-format", "format": "delta", "location": {"backendId": "lake", "path": "d/"}},
    {"id": "d9", "name": "explicit-file", "format": "file", "location": {"backendId": "lake", "path": "sales/daily.csv"}},
]
OBJECTS = {("lake", "sales/daily.csv"): b"day,amount\n1,10\n2,20\n", ("lake", "model.bin"): b"\x00\x01"}


class Fake:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.token_requests = 0
        self.hub_status = 200
        self.expire_first_token = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def env(self) -> dict:
        return {
            "BOOTH_WORKSPACE": "acme",
            "BOOTH_GATEWAY_URL": f"{self.url}/modules",
            "JUPYTERHUB_API_URL": f"{self.url}/hub/api",
            "JUPYTERHUB_API_TOKEN": "hub-token",
            "BOOTH_PLATFORM_TOKEN_PATH": "/booth/platform-token",
        }

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path == "/hub/api/booth/platform-token":
                    assert self.headers["Authorization"] == "token hub-token"
                    fake.token_requests += 1
                    if fake.hub_status != 200:
                        return self._send(fake.hub_status, {"message": "workload identity is not configured"})
                    return self._send(200, {"token": f"wl-{fake.token_requests}", "expiresAt": "2099-01-01T00:00:00Z", "role": "editor", "workspace": "acme"})
                self._gateway("POST")

            def do_GET(self):
                self._gateway("GET")

            def do_PUT(self):
                self._gateway("PUT")

            def _gateway(self, method):
                u = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.calls.append({"method": method, "path": u.path, "query": parse_qs(u.query), "auth": self.headers.get("Authorization"), "ws": self.headers.get("X-Workspace"), "body": body})
                if fake.expire_first_token and self.headers.get("Authorization") == "Bearer wl-1":
                    return self._send(401, {"error": "expired"})
                p = u.path
                if p == "/modules/catalog/api/datasets" and method == "GET":
                    q = parse_qs(u.query).get("q", [""])[0]
                    return self._send(200, {"items": [d for d in DATASETS if q in d["name"]], "total": 0})
                if p == "/modules/catalog/api/datasets" and method == "POST":
                    return self._send(201, {"id": "new", **json.loads(body)})
                if p.startswith("/modules/catalog/api/datasets/"):
                    ds = [d for d in DATASETS if d["id"] == p.rsplit("/", 1)[1]]
                    return self._send(200, ds[0]) if ds else self._send(404, {"error": "not found"})
                if p.startswith("/modules/storage/api/backends/lake/objects/"):
                    key = ("lake", p.split("/objects/", 1)[1])
                    if method == "PUT":
                        return self._send(200, {"path": key[1], "size": len(body)})
                    return self._send(200, OBJECTS[key], "application/octet-stream") if key in OBJECTS else self._send(404, {"error": "no"})
                if p == "/modules/storage/api/backends":
                    return self._send(200, {"items": [{"id": "lake"}]})
                self._send(404, {"error": "unknown"})

        return H

    def close(self):
        self._server.shutdown()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


@pytest.fixture
def client(fake) -> Client:
    return Client(env=fake.env())


def test_calls_go_through_the_gateway_as_the_notebooks_platform_identity(client, fake):
    client.catalog.datasets()
    (call,) = fake.calls
    assert call["path"] == "/modules/catalog/api/datasets"
    assert call["auth"] == "Bearer wl-1" and call["ws"] == "acme"


def test_the_platform_token_is_cached_between_calls(client, fake):
    client.catalog.datasets()
    client.storage.backends()
    assert fake.token_requests == 1


def test_an_expired_token_is_refreshed_once_and_the_call_retried(client, fake):
    fake.expire_first_token = True
    assert client.catalog.datasets()
    assert fake.token_requests == 2
    assert [c["auth"] for c in fake.calls] == ["Bearer wl-1", "Bearer wl-2"]


def test_read_dataset_by_name_as_a_dataframe(client):
    pd = pytest.importorskip("pandas")
    df = client.read_dataset("daily-sales")
    assert isinstance(df, pd.DataFrame) and list(df.columns) == ["day", "amount"] and df["amount"].sum() == 30


def test_read_dataset_by_id_as_bytes_and_unknown_formats_as_bytes(client):
    assert client.read_dataset("d1", as_bytes=True).startswith(b"day,amount")
    assert client.read_dataset("blob") == b"\x00\x01"


def test_an_ambiguous_name_is_an_error_not_a_guess(client):
    with pytest.raises(BoothError, match="2 datasets are named 'dup'"):
        client.read_dataset("dup")


def test_a_directory_dataset_explains_what_to_do(client):
    with pytest.raises(BoothError, match="storage.list"):
        client.read_dataset("partitioned")


def test_an_unknown_dataset(client):
    with pytest.raises(BoothError, match="no dataset"):
        client.read_dataset("nope")


def test_write_and_register(client, fake):
    client.storage.write("lake", "out/r.csv", "a,b\n")
    ds = client.catalog.register_dataset("result", "lake", "out/r.csv", tags=["nb"])
    assert ds["location"] == {"backendId": "lake", "path": "out/r.csv"}
    assert fake.calls[0]["method"] == "PUT" and fake.calls[0]["body"] == b"a,b\n"


def test_no_platform_access_is_a_clear_error(client, fake):
    fake.hub_status = 503
    with pytest.raises(BoothError, match="no platform access"):
        client.catalog.datasets()


def test_outside_a_booth_notebook_server(fake):
    with pytest.raises(BoothError, match="isn't running in a Project Booth notebook"):
        Client(env={}).catalog.datasets()
    env = {**fake.env(), "BOOTH_GATEWAY_URL": ""}
    with pytest.raises(BoothError, match="no booth-core gateway"):
        Client(env=env).catalog.datasets()


def test_importing_the_package_outside_a_notebook_does_not_fail():
    import booth

    assert booth.workspace == "" or isinstance(booth.workspace, str)
