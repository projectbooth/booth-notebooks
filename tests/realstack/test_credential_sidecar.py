"""ADR 0095 on a real cluster: booth-core's credential sidecar in a real notebook pod, issuing real leases
from a real booth-database (bundled) through real booth-core's broker, on the notebook's own identity.

Driven like tests/realstack/test_through_shell.py (a real Keycloak user, core's real /iframe/ path, real
spawn), but against core directly (no shell needed). Stack: `WITH_BOOTH_DATABASE=1 SKIP_DESIGN=1
BDB_MIN_TTL=90s BDB_REAP_INTERVAL=3s sh hack/real-stack-up.sh` — the short lease floor makes rotation and
lease expiry observable in minutes. Skipped unless BOOTH_REAL_SIDECAR=1 (see hack/real-stack-e2e.md).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from booth_notebooks.spawner import workspace_database

from .test_through_shell import Browser, keycloak_token, wait_for

pytestmark = pytest.mark.skipif(os.environ.get("BOOTH_REAL_SIDECAR") != "1", reason="set BOOTH_REAL_SIDECAR=1 (hack/real-stack-e2e.md)")

CORE = os.environ.get("BOOTH_REAL_CORE_URL", "http://localhost:18080").rstrip("/")
CONTEXT = os.environ.get("BOOTH_REAL_KUBE_CONTEXT", "kind-booth-nb-e2e")
WORKSPACE = os.environ.get("BOOTH_REAL_WORKSPACE", "acme-analytics")
NS, BDB = "booth-notebooks", "booth-database-postgres.booth-database.svc.cluster.local"
CHART = Path(__file__).resolve().parents[2] / "charts" / "booth-notebooks"
REPORT = Path(os.environ.get("BOOTH_REAL_SIDECAR_REPORT", "sidecar-rotation.json"))


def run(*args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)


def kubectl(*args: str, timeout: float = 600) -> str:
    out = run("kubectl", "--context", CONTEXT, *args, timeout=timeout)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def helm_set(*sets: str) -> None:
    args = ["helm", "--kube-context", CONTEXT, "upgrade", "notebooks", str(CHART), "-n", NS, "--reuse-values", "--wait", "--timeout", "5m"]
    for s in sets:
        args += ["--set", s]
    out = run(*args)
    assert out.returncode == 0, out.stderr[-2000:]


def spawn() -> tuple[Browser, str]:
    token = keycloak_token("owner-user")
    r = httpx.get(f"{CORE}/api/modules/notebooks/iframe-url", headers={"Authorization": f"Bearer {token}", "X-Workspace": WORKSPACE})
    assert r.status_code == 200, r.text
    b = Browser(CORE)
    b.request("GET", r.json()["url"], dest="iframe")
    name = b.request("GET", "/hub/api/user").json()["name"]
    b.request("GET", "/hub/spawn", dest="iframe")
    wait_for(lambda: (b.request("GET", f"/hub/api/users/{name}").json().get("servers", {}).get("") or {}).get("ready"), 300, "the server")
    return b, name


def stop(b: Browser, name: str) -> None:
    b.request("GET", "/hub/home", dest="iframe")
    b.request("DELETE", f"/hub/api/users/{name}/server", headers={"X-XSRFToken": b.cookie("_xsrf", "/hub/")})
    wait_for(lambda: not pod_of(name), 180, "the pod to be deleted")


def pod_of(name: str) -> dict | None:
    pods = json.loads(kubectl("-n", NS, "get", "pods", "-l", "component=singleuser-server", "-o", "json"))["items"]
    mine = [p for p in pods if p["metadata"]["annotations"].get("hub.jupyter.org/username") == name and not p["metadata"].get("deletionTimestamp")]
    return mine[0] if mine else None


def py_in_notebook(pod: str, code: str, timeout: float = 120) -> str:
    out = run("kubectl", "--context", CONTEXT, "-n", NS, "exec", pod, "-c", "notebook", "--", "python", "-c", code, timeout=timeout)
    assert out.returncode == 0, (out.stdout[-2000:], out.stderr[-2000:])
    return out.stdout


CONNECT_PROBE = (
    "import socket, sys\n"
    "for host in sys.argv[1:]:\n"
    "    try:\n"
    "        socket.create_connection((host, 5432), timeout=4).close(); print(host, 'open')\n"
    "    except OSError as e:\n"
    "        print(host, 'closed', type(e).__name__)\n"
)


def test_nothing_opens_when_booth_database_url_is_unset():
    helm_set("boothDatabase.url=")
    b, name = spawn()
    try:
        p = pod_of(name)
        assert [c["name"] for c in p["spec"]["containers"]] == ["notebook"]
        assert "DATABASE_URL" not in {e["name"] for e in p["spec"]["containers"][0].get("env", [])}
        out = run("kubectl", "--context", CONTEXT, "-n", NS, "exec", p["metadata"]["name"], "-c", "notebook", "--",
                  "python", "-c", CONNECT_PROBE, "127.0.0.1", BDB).stdout
        assert "127.0.0.1 closed" in out, out  # no sidecar listening
        assert f"{BDB} closed" in out, out  # and no egress to booth-database (ADR 0092 rule not rendered)
    finally:
        stop(b, name)


class S:
    pod = ""
    name = ""
    browser: Browser | None = None


def test_database_url_resolves_and_a_real_query_works():
    helm_set(f"boothDatabase.url={BDB}:5432", "credentialSidecar.renewMarginSeconds=45", "credentialSidecar.renewIntervalSeconds=5")
    S.browser, S.name = spawn()
    p = pod_of(S.name)
    S.pod = p["metadata"]["name"]
    assert [c["name"] for c in p["spec"]["containers"]] == ["notebook", "booth-token", "credential-sidecar"]
    # Ready = the token helper's probe of the sidecar's /healthz = the sidecar holds a real lease.
    kubectl("-n", NS, "wait", f"pod/{S.pod}", "--for=condition=Ready", "--timeout=180s")
    out = py_in_notebook(S.pod, (
        "import os, json, psycopg\n"
        "url = os.environ['DATABASE_URL']\n"
        "with psycopg.connect(url, autocommit=True) as c:\n"
        "    c.execute('create table if not exists nb_sidecar_check (x int)')\n"
        "    c.execute('insert into nb_sidecar_check values (42)')\n"
        "    total = c.execute('select sum(x) from nb_sidecar_check').fetchone()[0]\n"
        "    db, login, role = c.execute('select current_database(), session_user, current_user').fetchone()\n"
        "print(json.dumps({'url': url, 'total': total, 'db': db, 'login': login, 'role': role}))\n"
    ))
    got = json.loads(out.strip().splitlines()[-1])
    assert got["url"] == f"postgresql://localhost:5432/{workspace_database(WORKSPACE)}"
    assert got["db"] == workspace_database(WORKSPACE)  # the URL names the database the credential grants
    # Logged in as a short-lived broker lease role, acting as the workspace's readwrite group (booth-database's
    # design: a lease role is a member of the group that owns the objects). Not a standing role.
    assert got["login"].startswith("bdb_lease_"), got
    assert got["role"] == workspace_database(WORKSPACE) + "_rw", got
    assert got["total"] >= 42


ROTATION = r"""
import json, os, time, psycopg
url = os.environ['DATABASE_URL']
held = psycopg.connect(url, autocommit=True)
pid0, user0 = held.execute('select pg_backend_pid(), session_user').fetchone()
t0, held_alive, events = time.time(), True, []
while time.time() - t0 < DURATION:
    ev = {'t': round(time.time() - t0)}
    if held_alive:
        try:
            pid, user = held.execute('select pg_backend_pid(), session_user').fetchone()
            ev.update(held='ok', held_pid=pid, held_user=user)
        except Exception as e:
            held_alive = False
            ev.update(held='dropped', held_error=f'{type(e).__name__}: {str(e).strip()[:200]}')
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as n:
            ev['new_user'] = n.execute('select session_user').fetchone()[0]
    except Exception as e:
        ev['new_error'] = f'{type(e).__name__}: {str(e).strip()[:200]}'
    events.append(ev)
    time.sleep(5)
print(json.dumps({'pid0': pid0, 'user0': user0, 'events': events}))
"""


def test_an_open_connection_survives_credential_rotation():
    """Hold one connection open while the sidecar rotates its credential (each lease is a distinct
    PostgreSQL login role, so a new connection's session_user changes when it does). The held connection must
    keep working, on the same backend and its original role, across rotations.

    Also recorded, not asserted (see docs/decisions/0008): what happens to the held connection once its
    ORIGINAL lease expires — booth-database's reaper ends open sessions at lease expiry."""
    assert S.pod, "needs the previous test's running server"
    duration = int(os.environ.get("BOOTH_REAL_ROTATION_SECONDS", "170"))
    out = py_in_notebook(S.pod, ROTATION.replace("DURATION", str(duration)), timeout=duration + 120)
    result = json.loads(out.strip().splitlines()[-1])
    REPORT.write_text(json.dumps(result, indent=1))
    ev, user0, pid0 = result["events"], result["user0"], result["pid0"]

    rotated = [e for e in ev if e.get("new_user") and e["new_user"] != user0]
    assert rotated, f"no rotation observed in {duration}s: {ev[-3:]}"
    first = rotated[0]["t"]
    survived = [e for e in ev if e["t"] >= first and e.get("held") == "ok"]
    assert survived, f"the held connection did not survive the first rotation (at t={first}s): {ev}"
    assert all(e["held_pid"] == pid0 and e["held_user"] == user0 for e in survived)  # same session, same original role
    assert not [e for e in ev if "new_error" in e], [e for e in ev if "new_error" in e]  # new connections never failed
    dropped = next((e for e in ev if e.get("held") == "dropped"), None)
    print(f"\nrotation first seen at t={first}s; held connection "
          + (f"dropped at t={dropped['t']}s: {dropped['held_error']}" if dropped else f"alive for the whole {duration}s"))


def test_stopping_the_server_removes_the_sidecar_with_it():
    assert S.browser and S.name
    stop(S.browser, S.name)
