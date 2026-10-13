#!/usr/bin/env bash
# Copied from booth-streamlit (0e6b084).
# Runs on the kind host (the CI runner; needs sudo), after the cluster exists: makes every address in
# lan-targets.env answer on LAN_PORT, so the egress test can tell a NetworkPolicy drop from an address
# nobody answers. A TCP listener on the kind network's gateway (the host's bridge address), and a DNAT
# for each address, for packets arriving from the kind bridge only (the host's own traffic, e.g. to the
# runner's real metadata service, is untouched). Pod traffic leaves the node SNATed (Calico's
# natOutgoing), reaches the host by the node's default route, and is DNATed to the listener; Calico's
# policy sees the original destination, before any of this.
#
#   lan-targets.sh CLUSTER-NAME
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
source "$here/lan-targets.env"
cluster=$1
addrs="$LAN_10 $LAN_172 $LAN_192 $LAN_CGNAT $LAN_METADATA $LAN_CONTROL"
br=br-$(docker network inspect kind -f '{{.Id}}' | cut -c1-12)
gw=$(ip -4 -o addr show dev "$br" | awk 'NR == 1 {split($4, a, "/"); print a[1]}')
[ -n "$gw" ] || { echo "no IPv4 address on $br" >&2; exit 1; }
echo "kind bridge $br, gateway $gw"
# None of the addresses may be on a network the host is attached to (kind's, docker0, the runner's
# own): each must leave the node by its default route, or it is not "a LAN host nobody routes to".
for a in $addrs; do
  route=$(ip -4 route get "$a")
  echo "host route to $a: $route"
  echo "$route" | grep -q " via " || { echo "$a is on a network the host is attached to" >&2; exit 1; }
done
setsid nohup python3 -m http.server "$LAN_PORT" --bind "$gw" >/tmp/lan-listener.log 2>&1 </dev/null &
for a in $addrs; do
  sudo iptables -t nat -I PREROUTING -i "$br" -d "$a" -p tcp --dport "$LAN_PORT" -j DNAT --to-destination "$gw:$LAN_PORT"
done
sudo iptables -t nat -S PREROUTING
node=$(kind get nodes --name "$cluster" | awk 'NR == 1')
# From the node itself (host network, no NetworkPolicy): every address must answer.
for a in $addrs; do
  code=""
  for _ in $(seq 1 20); do
    code=$(docker exec "$node" curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$a:$LAN_PORT/" || true)
    [ "$code" = 200 ] && break
    sleep 1
  done
  echo "from $node: $a:$LAN_PORT -> $code"
  [ "$code" = 200 ] || { echo "$a does not answer from the kind node" >&2; cat /tmp/lan-listener.log >&2; exit 1; }
done
