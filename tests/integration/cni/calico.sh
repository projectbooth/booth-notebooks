#!/usr/bin/env bash
# Installs Calico v3.33.0 on a kind cluster created from cni/kind-calico.yaml: the upstream
# manifest, refused unless its sha256 matches, with every image pinned by digest (all on quay.io,
# so nothing here pulls from Docker Hub). Resolved 2026-10-10. Copied unchanged from booth-streamlit
# (0e6b084, test/integration/cni/), the same pin.
set -euo pipefail
version=v3.33.0
manifest_sha=2de8f47595fb9c41b3f47d7b767a1f8e72ecf84057af834738ff12689a234da5
tmp=$(mktemp)
curl -fsSL "https://raw.githubusercontent.com/projectcalico/calico/$version/manifests/calico.yaml" -o "$tmp"
echo "$manifest_sha  $tmp" | sha256sum -c - >/dev/null || { echo "calico.yaml does not match the pinned sha256" >&2; exit 1; }
sed -i \
  -e "s#quay.io/calico/calico:$version#quay.io/calico/calico@sha256:4ebc20730127bc9e8bf1db5f5a2d8b58cf37fe0e891b3c7b5471992221d213e5#" \
  -e "s#quay.io/calico/node:$version#quay.io/calico/node@sha256:565d8db3200e443955557f8f28ff44ab8a4a456754bc74a19c4f2f3444af9b1c#" \
  -e "s#quay.io/calico/third-party-cni-plugins:$version#quay.io/calico/third-party-cni-plugins@sha256:0f32f48279484c0d6f0e0d0e767b2d0e1a6c6ca34cb3421a61b989d992f3df41#" \
  "$tmp"
if grep -E "image: " "$tmp" | grep -v "@sha256:" ; then echo "an image is not pinned by digest" >&2; exit 1; fi
kubectl apply --server-side -f "$tmp" >/dev/null
rm -f "$tmp"
kubectl -n kube-system rollout status ds/calico-node --timeout=600s
kubectl -n kube-system rollout status deploy/calico-kube-controllers --timeout=600s
kubectl wait --for=condition=Ready nodes --all --timeout=300s
# The pool must be kind-calico.yaml's podSubnet: the egress test relies on 192.168.0.0/16 holding no pods.
pool=""
for _ in $(seq 1 60); do pool=$(kubectl get ippools.crd.projectcalico.org -o jsonpath='{.items[*].spec.cidr}' 2>/dev/null || true); [ -n "$pool" ] && break; sleep 2; done
echo "Calico IP pool: $pool"
[ "$pool" = 10.244.0.0/16 ] || { echo "Calico's pool is '$pool', not kind-calico.yaml's 10.244.0.0/16" >&2; exit 1; }
