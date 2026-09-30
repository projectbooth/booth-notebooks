#!/usr/bin/env sh
# Stand up the REAL stack tests/realstack runs against, on kind: Keycloak (booth-e2e's realm), real
# booth-core, the real booth-design shell, and this chart — plus, with WITH_DATA=1, real booth-storage and
# booth-catalog. See hack/real-stack-e2e.md. Idempotent enough to re-run after a failure.
#
# Needs sibling checkouts (booth-core, booth-design, booth-e2e; booth-storage/booth-catalog for WITH_DATA),
# docker, kind, kubectl, helm, and booth-design's node_modules (its SPA is built locally, like booth-e2e does).
set -eu

CLUSTER="${CLUSTER:-booth-nb-e2e}"
KIND="${KIND:-kind}"
REPOS="${BOOTH_REPOS_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${STATE_DIR:-$HERE/.real-stack}"   # gitignored: the generated test password lives here
KC="kubectl --context kind-$CLUSTER"
ISSUER=http://keycloak.keycloak.svc:8080/realms/booth
CORE=http://booth-core.booth-system.svc:8080
mkdir -p "$STATE"

step() { printf '\n==> %s\n' "$*"; }

step "images"
docker build -q -t booth-core:e2e "$REPOS/booth-core" >/dev/null
(cd "$REPOS/booth-design" && npx vite build >/dev/null)
ctx="$STATE/design-ctx"; rm -rf "$ctx"; mkdir -p "$ctx"
cp -r "$REPOS/booth-design/dist" "$REPOS/booth-design/docker" "$ctx/"
cp "$REPOS/booth-e2e/bringup/Dockerfile.design" "$ctx/Dockerfile"
sed -i 's/\r$//' "$ctx"/docker/*.sh "$ctx"/docker/*.template; chmod +x "$ctx"/docker/*.sh
docker build -q -t booth-design:e2e "$ctx" >/dev/null
docker build -q -t booth-notebooks-hub:ci -f "$HERE/images/hub/Dockerfile" "$HERE" >/dev/null
docker build -q -t booth-notebooks-singleuser:ci -f "$HERE/images/singleuser/Dockerfile" "$HERE" >/dev/null

step "cluster + image load"
"$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER" || "$KIND" create cluster --name "$CLUSTER" --wait 120s
"$KIND" load docker-image --name "$CLUSTER" booth-core:e2e booth-design:e2e booth-notebooks-hub:ci
# The 2 GB notebook image via an archive: streaming it spiked host memory on Docker Desktop.
docker save -o "$STATE/su.tar" booth-notebooks-singleuser:ci
"$KIND" load image-archive --name "$CLUSTER" "$STATE/su.tar"; rm -f "$STATE/su.tar"

step "keycloak"
for ns in keycloak booth-system booth-notebooks booth-design; do $KC create namespace "$ns" >/dev/null 2>&1 || true; done
[ -f "$STATE/password" ] || python -c "import secrets;print(secrets.token_hex(10), end='')" >"$STATE/password"
sed "s/__TEST_PASSWORD__/$(cat "$STATE/password")/g" "$REPOS/booth-e2e/bringup/realm-booth.json.tpl" >"$STATE/realm.json"
$KC -n keycloak create configmap keycloak-realm --from-file=realm-booth.json="$STATE/realm.json" --dry-run=client -o yaml | $KC apply -f - >/dev/null
rm -f "$STATE/realm.json"
$KC -n keycloak get secret keycloak-admin >/dev/null 2>&1 || \
  $KC -n keycloak create secret generic keycloak-admin --from-literal=password="$(python -c 'import secrets;print(secrets.token_hex(12))')" >/dev/null
$KC apply -f "$REPOS/booth-e2e/bringup/keycloak.yaml" >/dev/null
$KC -n keycloak rollout status deploy/keycloak --timeout=400s

step "booth-core"
helm --kube-context "kind-$CLUSTER" upgrade --install booth-core "$REPOS/booth-core/charts/booth-core" -n booth-system \
  --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design --set workloadIdentity.issuerUrl=$CORE \
  --set-string iframeSigningKey="$(python -c 'import secrets;print(secrets.token_hex(32))')" \
  --set image.repository=booth-core --set image.tag=e2e --set image.pullPolicy=Never --wait --timeout 8m >/dev/null

step "booth-design (the shell)"
helm --kube-context "kind-$CLUSTER" upgrade --install booth-design "$REPOS/booth-design/charts/booth-design" -n booth-design \
  --set core.gatewayUrl=$CORE --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design \
  --set image.repository=booth-design --set image.tag=e2e --set image.pullPolicy=Never --wait --timeout 5m >/dev/null

if [ "${WITH_DATA:-0}" = "1" ]; then
  step "booth-storage + booth-catalog (real modules a kernel reads through core's gateway)"
  sh "$HERE/hack/real-stack-data.sh"
fi

step "booth-notebooks (no identity values set: the chart default IS core's issuer)"
helm --kube-context "kind-$CLUSTER" upgrade --install notebooks "$HERE/charts/booth-notebooks" -n booth-notebooks \
  --set hub.image.repository=booth-notebooks-hub --set hub.image.tag=ci --set hub.image.pullPolicy=Never \
  --set singleuser.image.repository=booth-notebooks-singleuser --set singleuser.image.tag=ci \
  --set singleuser.image.pullPolicy=Never --set singleuser.storage.capacity=1Gi --set singleuser.cpu.guarantee=0.05 \
  ${NOTEBOOKS_EXTRA_ARGS:-} --wait --timeout 6m >/dev/null
$KC -n booth-notebooks get boothmodule notebooks -o jsonpath='{.status.phase}{"\n"}'

cat <<EOF

Stack is up. Port-forward, then run tests/realstack (see hack/real-stack-e2e.md):
  $KC -n keycloak port-forward svc/keycloak 8080:8080 &          # needs hosts: 127.0.0.1 keycloak.keycloak.svc
  $KC -n booth-design port-forward svc/booth-design 8090:8080 &   # the shell: http://localhost:8090
Test password: $STATE/password
EOF
