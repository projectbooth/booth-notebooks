#!/usr/bin/env sh
# Real booth-storage + booth-catalog on the hack/real-stack-up.sh cluster, so a notebook kernel's
# `booth` calls reach real modules through core's gateway with a core-minted workload token. Configured
# the way booth-e2e's bringup does (its storage-values.yaml / storage-pvc.yaml are reused as-is).
#
# One deliberate difference from booth-e2e: booth-catalog's BoothModule still doesn't declare
# `database: {enabled: true}` (a booth-e2e finding), and booth-e2e opts it in with a Helm post-renderer.
# Here the live BoothModule is patched after install instead (a post-renderer is awkward on Windows) —
# the same effect: real booth-core provisions the catalog's database and credentials Secret.
set -eu

CLUSTER="${CLUSTER:-booth-nb-e2e}"
KIND="${KIND:-kind}"
REPOS="${BOOTH_REPOS_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
KC="kubectl --context kind-$CLUSTER"
HELM="helm --kube-context kind-$CLUSTER"
ISSUER=http://keycloak.keycloak.svc:8080/realms/booth
CORE=http://booth-core.booth-system.svc:8080
E="$REPOS/booth-e2e/bringup"

for m in booth-storage booth-catalog; do
  docker build -q -t "$m:e2e" "$REPOS/$m" >/dev/null
  $KC create namespace "$m" >/dev/null 2>&1 || true
done
"$KIND" load docker-image --name "$CLUSTER" booth-storage:e2e booth-catalog:e2e

$KC apply -f "$E/storage-pvc.yaml" >/dev/null
$HELM upgrade --install booth-storage "$REPOS/booth-storage/charts/booth-storage" -n booth-storage \
  -f "$E/storage-values.yaml" \
  --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design --set oidc.groupsClaim=groups \
  --set oidc.workloadIssuerUrl=$CORE --set postgres.dsnSecret.name=booth-database-credentials \
  --set image.repository=booth-storage --set image.tag=e2e --set image.pullPolicy=Never >/dev/null

$HELM upgrade --install booth-catalog "$REPOS/booth-catalog/charts/booth-catalog" -n booth-catalog \
  --set oidc.issuerUrl=$ISSUER --set oidc.clientId=booth-design --set oidc.groupsClaim=groups \
  --set workloadIdentity.issuerUrl=$CORE --set nats.url=nats://booth-core-nats.booth-system.svc:4222 \
  --set postgres.dsnSecret.name=booth-database-credentials \
  --set image.repository=booth-catalog --set image.tag=e2e --set image.pullPolicy=Never >/dev/null
# The catalog's database opt-in (see header). A no-op once its chart declares the field itself.
$KC -n booth-catalog get boothmodule catalog -o jsonpath='{.spec.database.enabled}' | grep -q true || \
  $KC -n booth-catalog patch boothmodule catalog --type=merge -p '{"spec":{"database":{"enabled":true}}}' >/dev/null

$KC -n booth-storage rollout status deploy/booth-storage --timeout=300s
$KC -n booth-catalog rollout status deploy/booth-catalog --timeout=300s
