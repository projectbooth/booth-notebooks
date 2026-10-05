"""Keep the notebook's platform token in a file for booth-core's credential sidecar (ADR 0095).

The sidecar authenticates to the credential broker with "whatever identity the pod already has"
(contracts/credential-sidecar.md) and re-reads it from ``--token-file`` on every call. A notebook pod's
identity is the one ``booth.platform_token()`` already uses — a short-lived booth-core workload token
the hub mints for this notebook server — so this loop writes exactly that token, refreshed well before
it expires, to a file on a memory-backed volume shared only with the sidecar.

    python -m booth.sidecar_token write /var/run/booth-sidecar/token
    python -m booth.sidecar_token probe http://127.0.0.1:5432/healthz [http://127.0.0.1:9472/healthz ...]

``probe`` exists because the sidecar's ``/healthz`` listens on loopback only (by contract) and its image
is distroless: the kubelet's own httpGet probe connects to the pod IP and can't reach a loopback
listener, and there's nothing in the sidecar's image to exec. This container shares the pod's network
namespace, so it runs the readiness check for it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.request

from . import BoothError, Client

REFRESH_SECONDS = 30  # how often the loop looks; cheap, since the token is cached
# Re-fetch once less than this remains, not at platform_token()'s own 60s margin: the sidecar depends on
# this file, and a missed minute (seen on a starved node) left it holding an expired token, which the
# broker refused with 401 until the next refresh. Workload tokens live 10 minutes.
REFRESH_AHEAD_SECONDS = 300


def write_atomically(path: str, token: str) -> None:
    """Replace ``path`` with ``token`` so a reader never sees a partial file. Group-readable only: the
    sidecar reads it via the pod's shared fsGroup."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".token-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_loop(path: str, client: Client | None = None, interval: float = REFRESH_SECONDS, once: bool = False) -> None:
    client = client or Client()
    last = None
    while True:
        try:
            if client._http._token and client._http._expires - time.time() < REFRESH_AHEAD_SECONDS:
                client._http.token(force=True)
            token = client.platform_token()
            if token != last:
                write_atomically(path, token)
                last = token
                print("booth-token: platform token refreshed", flush=True)  # never the token itself
        except BoothError as e:
            # Keep the last good file: the sidecar keeps its current lease until it truly can't renew.
            print(f"booth-token: could not refresh the platform token: {e}", file=sys.stderr, flush=True)
        if once:
            return
        time.sleep(interval)


def probe(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "write":
        write_loop(argv[1])
        return 0
    if len(argv) >= 2 and argv[0] == "probe":  # one /healthz per sidecar; ready only when every one is
        return 0 if all(probe(url) for url in argv[1:]) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
