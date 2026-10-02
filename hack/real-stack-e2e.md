# Verifying against the real stack (real booth-core, booth-design shell, Keycloak)

`tests/realstack/test_through_shell.py` drives booth-notebooks through the **real** shell and **real**
booth-core (ADR 0069), not the stand-in core `tests/integration` uses. It's a manual, operator-run check:
booth-e2e's droplet harness doesn't include notebooks yet (see docs/decisions/0004). Last run:
2026-09-27, core `fa11da0`, design `526764e`, storage `43542db`, catalog `3d36f5d`: **16 passed**.

## Stack

```sh
KIND=kind sh hack/real-stack-up.sh            # Keycloak, booth-core, the shell, this chart
WITH_DATA=1 KIND=kind sh hack/real-stack-up.sh # ...plus real booth-storage + booth-catalog
```

Needs sibling checkouts (booth-core, booth-design, booth-e2e; booth-storage + booth-catalog for data),
booth-design's `node_modules`, and ~5 GB of Docker memory (more with data). The generated test password is
in `.real-stack/password` (gitignored). Core provisions `booth-database-credentials` and
`booth-workload-minting-credentials` itself; nothing is hand-made.

## Run

```sh
kubectl -n keycloak port-forward svc/keycloak 8080:8080 &          # hosts: 127.0.0.1 keycloak.keycloak.svc
kubectl -n booth-design port-forward svc/booth-design 8090:8080 &
kubectl -n booth-notebooks port-forward svc/notebooks-booth-notebooks-proxy-public 18082:8000 &   # optional
BOOTH_REAL_SHELL_URL=http://localhost:8090 BOOTH_REAL_PASSWORD=$(cat .real-stack/password) BOOTH_REAL_KUBE_CONTEXT=kind-booth-nb-e2e BOOTH_REAL_PROXY_DIRECT_URL=http://127.0.0.1:18082 BOOTH_REAL_WITH_DATA=1 pytest tests/realstack -v
```

Needs `websocket-client` (in the dev extra). The shell can be on any port since core `fa11da0` mints a
relative iframe URL. Keycloak must answer as `keycloak.keycloak.svc:8080` because tokens carry that issuer.

## ADR 0095: the credential sidecar against a real booth-database

```sh
WITH_BOOTH_DATABASE=1 SKIP_DESIGN=1 BDB_MIN_TTL=90s BDB_REAP_INTERVAL=3s KIND=kind sh hack/real-stack-up.sh
kubectl -n keycloak port-forward svc/keycloak 8080:8080 &
kubectl -n booth-system port-forward svc/booth-core 18080:8080 &     # no shell: core serves /iframe/ itself
BOOTH_REAL_SHELL_URL=http://localhost:18080 BOOTH_REAL_SIDECAR=1 BOOTH_REAL_PASSWORD=$(cat .real-stack/password)   pytest tests/realstack/test_credential_sidecar.py -v -s
```

The short lease floor (90s) makes rotation and lease expiry visible in minutes. The test flips
`boothDatabase.url` itself (unset, then set) with `helm upgrade --reuse-values`. It writes the rotation
timeline to `BOOTH_REAL_SIDECAR_REPORT` (default `sidecar-rotation.json`). Needs ~5 GB of free memory: on a
starved host the hub's own readiness check of a spawned server can lag by minutes (seen).
