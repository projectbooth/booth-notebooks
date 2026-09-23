# Verifying against the real stack (real booth-core, booth-design shell, Keycloak)

`tests/realstack/test_through_shell.py` drives booth-notebooks through the **real** shell and **real**
booth-core (ADR 0069), not the stand-in core `tests/integration` uses. It's a manual, operator-run check:
booth-e2e's droplet harness doesn't include notebooks yet (see docs/decisions/0004). Last run:
2026-09-23, core `cdd621e`, design `f8b6804`: **10 passed, 1 xfailed** (the known shell-reload bug).

## Stack (kind, ~4 GB of Docker memory)

Reuses booth-e2e's known-working pieces (`booth-e2e/bringup/`):

```sh
kind create cluster --name booth-nb-e2e
# images: booth-core (docker build in booth-core), booth-notebooks-hub + -singleuser (images/*),
# booth-design's nginx stage (npx vite build in booth-design, then booth-e2e/bringup/Dockerfile.design)
kind load docker-image --name booth-nb-e2e booth-core:e2e booth-notebooks-hub:ci booth-design:e2e
docker save -o su.tar booth-notebooks-singleuser:ci && kind load image-archive --name booth-nb-e2e su.tar

# Keycloak + realm 'booth' (users owner-user / viewer-user in workspace acme-analytics)
sed "s/__TEST_PASSWORD__/$PW/g" booth-e2e/bringup/realm-booth.json.tpl > realm.json
kubectl -n keycloak create configmap keycloak-realm --from-file=realm-booth.json=realm.json
kubectl -n keycloak create secret generic keycloak-admin --from-literal=password=$(openssl rand -hex 12)
kubectl apply -f booth-e2e/bringup/keycloak.yaml

helm install booth-core booth-core/charts/booth-core -n booth-system \
  --set oidc.issuerUrl=http://keycloak.keycloak.svc:8080/realms/booth --set oidc.clientId=booth-design \
  --set workloadIdentity.issuerUrl=http://booth-core.booth-system.svc:8080 \
  --set-string iframeSigningKey=$(openssl rand -hex 32) \
  --set image.repository=booth-core --set image.tag=e2e --set image.pullPolicy=Never --wait
helm install booth-design booth-design/charts/booth-design -n booth-design \
  --set core.gatewayUrl=http://booth-core.booth-system.svc:8080 \
  --set oidc.issuerUrl=http://keycloak.keycloak.svc:8080/realms/booth --set oidc.clientId=booth-design \
  --set image.repository=booth-design --set image.tag=e2e --set image.pullPolicy=Never --wait
# Nothing about identity is set here: the chart's default identity.issuerUrl IS core's default issuer.
helm install notebooks booth-notebooks/charts/booth-notebooks -n booth-notebooks \
  --set hub.image.repository=booth-notebooks-hub --set hub.image.tag=ci --set hub.image.pullPolicy=Never \
  --set singleuser.image.repository=booth-notebooks-singleuser --set singleuser.image.tag=ci \
  --set singleuser.image.pullPolicy=Never
```

Core provisions `booth-database-credentials` and `booth-workload-minting-credentials` into
`booth-notebooks` itself; nothing is hand-made.

## Run

The shell **must** be at `http://localhost:8080`: core builds iframe URLs from `BOOTH_PUBLIC_BASE_URL`,
which its chart can't set (defaults to `http://localhost:8080`; see 0004).

```sh
kubectl -n booth-design port-forward svc/booth-design 8080:8080 &
kubectl -n keycloak port-forward svc/keycloak 18081:8080 &
kubectl -n booth-notebooks port-forward svc/notebooks-booth-notebooks-proxy-public 18082:8000 &   # optional
BOOTH_REAL_SHELL_URL=http://localhost:8080 BOOTH_REAL_PASSWORD=$PW BOOTH_REAL_KUBE_CONTEXT=kind-booth-nb-e2e \
BOOTH_REAL_PROXY_DIRECT_URL=http://127.0.0.1:18082 pytest tests/realstack -v
```

Needs `pip install websocket-client` (the kernel check runs code over the kernel websocket).
