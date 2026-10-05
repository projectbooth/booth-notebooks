"""booth.sidecar_token: the notebook's platform token kept in a file for the credential sidecar."""

from __future__ import annotations

import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from booth import Client
from booth.sidecar_token import main, probe, write_atomically, write_loop

from .test_client import Fake


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


def test_writes_the_notebooks_own_platform_token(tmp_path, fake):
    path = tmp_path / "token"
    write_loop(str(path), Client(env=fake.env()), once=True)
    assert path.read_text() == "wl-1"  # exactly what booth.platform_token() returns


def test_the_file_is_replaced_atomically_and_never_world_readable(tmp_path):
    path = tmp_path / "token"
    write_atomically(str(path), "first")
    write_atomically(str(path), "second")
    assert path.read_text() == "second"
    assert [p.name for p in tmp_path.iterdir()] == ["token"]  # no temp file left behind
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_a_refresh_failure_keeps_the_last_good_file(tmp_path, fake, capsys):
    path = tmp_path / "token"
    client = Client(env=fake.env())
    write_loop(str(path), client, once=True)
    fake.hub_status = 503
    client._http._expires = 0  # force a re-fetch, which now fails
    write_loop(str(path), client, once=True)
    assert path.read_text() == "wl-1"
    err = capsys.readouterr().err
    assert "could not refresh" in err and "wl-1" not in err  # the token itself is never logged


def test_the_file_is_refreshed_well_before_the_token_expires(tmp_path, fake):
    """Not at platform_token()'s own 60s margin: once under 5 minutes remain, a fresh token is fetched."""
    import time

    path, client = tmp_path / "token", Client(env=fake.env())
    write_loop(str(path), client, once=True)
    assert path.read_text() == "wl-1"
    client._http._expires = time.time() + 600  # plenty left: no refetch
    write_loop(str(path), client, once=True)
    assert fake.token_requests == 1
    client._http._expires = time.time() + 240  # inside the 5-minute window, outside the 60s one
    write_loop(str(path), client, once=True)
    assert fake.token_requests == 2 and path.read_text() == "wl-2"


def _server(status: int):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(status if self.path == "/healthz" else 404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize(("status", "ok"), [(200, True), (503, False)])
def test_probe_mirrors_the_sidecars_healthz(status, ok):
    srv = _server(status)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/healthz"
        assert probe(url) is ok
        assert main(["probe", url]) == (0 if ok else 1)
    finally:
        srv.shutdown()


def test_probe_with_two_sidecars_is_ready_only_when_both_are():
    up, down = _server(200), _server(503)
    try:
        u, d = (f"http://127.0.0.1:{s.server_address[1]}/healthz" for s in (up, down))
        assert main(["probe", u, u]) == 0
        assert main(["probe", u, d]) == 1 and main(["probe", d, u]) == 1
    finally:
        up.shutdown()
        down.shutdown()


def test_probe_when_nothing_is_listening_is_not_ready():
    assert probe("http://127.0.0.1:9/healthz", timeout=1) is False


def test_usage_errors():
    assert main([]) == 2 and main(["bogus", "x"]) == 2
