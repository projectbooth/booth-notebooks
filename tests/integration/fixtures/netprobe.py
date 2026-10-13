"""A TCP connect probe for test_kind.py's egress test, run as user code inside a live notebook pod
(``kubectl exec -i <pod> -- python - '<spec>'``). Adapted from booth-streamlit's
test/integration/fixtures/netprobe.py (0e6b084); the targets are probed concurrently.

    python netprobe.py '{"targets": {"name": ["host", port], ...}, "dns": "kubernetes.default.svc.cluster.local", "timeout": 3}'

Prints one JSON object, name -> result:
  "open"                          the connection was accepted
  "closed:TimeoutError"           no answer at all (packets dropped: Calico's NetworkPolicy denial)
  "closed:ConnectionRefusedError" a reset: nothing listening, or a REJECT (kube-router's denial on k3s)
  "closed:<other>"                anything else (unreachable network, ...)
and "dns" -> the resolved address, or "failed:<error>".

Neither "closed" result alone says the policy blocked anything. The test only counts one as "blocked"
inside the sequence reachable (policy deleted) -> blocked (policy applied) -> reachable (deleted again),
on the same address and port.
"""

import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

spec = json.loads(sys.argv[1])
timeout = float(spec.get("timeout", 3))


def probe(target):
    host, port = target
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return "open"
    except TimeoutError:
        return "closed:TimeoutError"
    except OSError as e:
        return "closed:" + type(e).__name__


targets = spec.get("targets", {})
with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
    out = dict(zip(targets, pool.map(probe, targets.values()), strict=True))
if spec.get("dns"):
    try:
        out["dns"] = socket.gethostbyname(spec["dns"])
    except OSError as e:
        out["dns"] = "failed:" + type(e).__name__
print(json.dumps(out))
