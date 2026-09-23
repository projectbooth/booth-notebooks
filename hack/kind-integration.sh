#!/usr/bin/env sh
# Layer-3 integration test on a local kind cluster: build both images, load them, run
# tests/integration/test_kind.py. The same steps CI's integration workflow runs.
#
#   hack/kind-integration.sh            # create (or reuse) cluster booth-notebooks-it, run, keep it
#   KEEP_CLUSTER=0 hack/kind-integration.sh   # delete the cluster afterwards
#
# Needs: docker, kind, kubectl, helm, and the dev venv (pip install -e ".[dev]" -e client).
set -eu

CLUSTER="${BOOTH_KIND_CLUSTER:-booth-notebooks-it}"
KIND="${KIND:-kind}"
PYTHON="${PYTHON:-python}"
HUB_IMAGE="booth-notebooks-hub:ci"
SINGLEUSER_IMAGE="booth-notebooks-singleuser:ci"

cd "$(dirname "$0")/.."

if ! "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  "$KIND" create cluster --name "$CLUSTER" --wait 120s
fi

docker build -t "$HUB_IMAGE" -f images/hub/Dockerfile .
docker build -t "$SINGLEUSER_IMAGE" -f images/singleuser/Dockerfile .
"$KIND" load docker-image --name "$CLUSTER" "$HUB_IMAGE" "$SINGLEUSER_IMAGE"
# The proxy, fake core and database images (all small) are pulled by the node itself. Not preloaded:
# `kind load` imports with --all-platforms, which fails for a pulled multi-arch image whose other
# platforms' content isn't local (Docker Desktop's containerd image store).

status=0
BOOTH_KIND_CLUSTER="$CLUSTER" BOOTH_HUB_IMAGE="$HUB_IMAGE" BOOTH_SINGLEUSER_IMAGE="$SINGLEUSER_IMAGE" \
  "$PYTHON" -m pytest tests/integration/test_kind.py -v -p no:cacheprovider || status=$?

if [ "$status" -ne 0 ]; then
  echo "---- diagnostics ----"
  kubectl --context "kind-$CLUSTER" -n booth-notebooks get all,pvc || true
  kubectl --context "kind-$CLUSTER" -n booth-notebooks logs deployment/notebooks-booth-notebooks-hub --tail=150 || true
  kubectl --context "kind-$CLUSTER" -n booth-notebooks get events --sort-by=.lastTimestamp | tail -30 || true
fi
if [ "${KEEP_CLUSTER:-1}" = "0" ]; then
  "$KIND" delete cluster --name "$CLUSTER"
fi
exit "$status"
