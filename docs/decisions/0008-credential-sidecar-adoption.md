# 0008: Adopting booth-core's credential sidecar (ADR 0095) — postgres shipped, s3 blocked

Status: **`postgres` mode built and verified on a real cluster (2026-10-02)** against real booth-core
(`eb24bb3`, its broker and workload identity), a real bundled booth-database, and Keycloak. **`s3` mode not
built**: three gaps, below, need decisions outside this repo. Two cross-module findings for the coordinator.

## What was built (postgres)

Gated by ADR 0092's existing `boothDatabase.url` (no new gate, per the contract). When it is set, every
notebook pod gets:

- **`credential-sidecar`**: `ghcr.io/projectbooth/credential-sidecar@sha256:05e96332…` (the digest `eb24bb3`'s
  publish run recorded; the chart and the hub config both refuse a tag). `--kind=postgres
  --listen=127.0.0.1:5432 --scope={"workspace":<ws>} --token-file=…`. Non-root (65532), read-only root,
  capabilities dropped, small limits.
- **`booth-token`**: a tiny loop (`python -m booth.sidecar_token write …`, the notebook image) that keeps
  the notebook's own platform token, the same `booth.platform_token()` source, fresh in a memory-backed
  file only it and the sidecar mount. That is "whatever identity your kernel already has": the hub-minted
  workload token for this notebook server, `sub=notebook:<hub user>`. No new identity, no new credential.
- **`DATABASE_URL=postgresql://localhost:5432/bdb_ws_<hash>`** in the notebook container. No host, no
  credential. The database name is the workspace's real one (checked against booth-database's own
  `naming.ForWorkspace`), so it agrees with `current_database()`; the proxy uses the credential's database
  whatever a client asks for.
- **`--access` follows the person's role**: `readwrite` for owner/editor, `read` for a viewer. The broker
  refuses `readwrite` to a viewer, and per the contract a refusal makes the sidecar exit (crash-loop).
  Viewers may open notebooks (ADR 0070), so asking for more than the role allows would break every
  viewer's pod.
- The default image gains `psycopg[binary]` and `sqlalchemy`, so `DATABASE_URL` works with
  `psycopg.connect`, `pandas.read_sql` and SQLAlchemy out of the box (judgment call: a URL with no driver
  is useless).
- `spawner.check_pod` (which refuses any pod that could read a module credential) now allows exactly a
  credential-free loopback `DATABASE_URL`, and refuses a sidecar `--listen` that isn't loopback.

## Verified on a real cluster (`tests/realstack/test_credential_sidecar.py`)

Stack: `WITH_BOOTH_DATABASE=1 SKIP_DESIGN=1 BDB_MIN_TTL=90s BDB_REAP_INTERVAL=3s hack/real-stack-up.sh`.
A real Keycloak user logs in through core's real `/iframe/` path and spawns.

- **Unset:** the pod has only the notebook container and no `DATABASE_URL`. Nothing listens on
  `127.0.0.1:5432`, and booth-database's Postgres is unreachable (no ADR 0092 rule).
- **Set:** the pod becomes Ready only once the sidecar holds a lease (the token helper's probe of the
  sidecar's `/healthz`). Then in the notebook, `psycopg.connect(os.environ["DATABASE_URL"])` creates a table,
  inserts and selects against the real workspace database. `session_user` is a `bdb_lease_…` role and
  `current_user` the workspace's `_rw` group: a real broker lease, not a standing role.
- **Rotation:** booth-database's audit log shows a new lease about every 45s, each for
  `subject=notebook:acme-analytics.… role=editor`. A connection held open kept the same backend PID and its
  original lease role across rotations, and new connections never failed. Run twice; the second run's
  timeline: rotation observed at t=5s, held connection alive across it, then terminated at t=50s, when its
  own lease expired (Finding 1).
- **Found and fixed while testing:** the token helper first refreshed the token file at `booth`'s API-client
  margin, 60s before expiry. On a memory-starved node that minute was missed. The file briefly held an
  expired token, and the sidecar's renewals got `401 invalid token` until the next refresh. The sidecar
  correctly kept its current lease and recovered on its own, but a longer stall would have let the lease
  lapse. The helper now refreshes once under 5 minutes of the token's 10 remain (unit-tested; the image
  carrying this fix was not redeployed to the cluster).
- **Not cluster-verified, environment-limited:** one spawn timed out waiting for the hub to mark the server
  ready. The hub checked the (already running) server 16 minutes late, with the host at 0.5 GB free. The
  query and rotation checks were then run against that live pod with the test module's own code.

## Fixed after merge: the database path must never block a notebook from starting

As first merged (#5), a notebook couldn't start whenever the sidecar couldn't get a lease. KubeSpawner
counts a server as started only when *every* container is ready, and the token helper's readiness is the
sidecar holding a lease. So a refused request, a down broker or a down booth-database left the user with no
notebook at all, not just no database. `main`'s post-merge Integration run caught it: the kind suite's
stand-in core has no broker, and the second workspace's notebook never started.

`BoothSpawner.is_pod_running` now counts the server as up when the **notebook** container is ready. The
sidecar and token helper don't gate it. **Judgment call against the contract's letter:** it says a kernel
that starts before its sidecar has a lease "should wait, not fail". A notebook is useful without a
database, so it doesn't wait. Instead, a connection made before the lease gets the proxy's own error (not
an opaque connection-refused, per the sidecar's code), and the pod's readiness still shows the sidecar's
state. The kind suite keeps this as a regression test: the second workspace's notebook is spawned with the
sidecar present, against a core with no broker, and must start.

## Finding 1 (booth-core contract vs booth-database): an open connection dies when its lease expires

The sidecar contract says connections open on a prior credential "are left alone until they close
naturally". But booth-database's reaper **terminates sessions when their lease expires**, by design
(its `leases.minTTL` comment says exactly that). Measured: the held connection survived rotation, then at
exactly its own lease's expiry (90s in the test) got `AdminShutdown: terminating connection due to
administrator command`, alongside the reaper's "ended 1 expired lease(s)".

With the defaults (a 1-hour lease floor), every connection through the sidecar is killed within an hour of
its lease being issued, whatever renewal does. Long-lived connections (an SQLAlchemy pool in a kernel left
open over an afternoon, a BI tool) will see hourly disconnects. Rotation itself is fine; the contract and
the provider disagree on what happens afterwards. Options for the coordinator: the sidecar re-dials
long-lived sessions onto a fresh lease before expiry (not possible transparently mid-session for
Postgres), booth-database stops reaping live sessions (keeping only login expiry), or the contract
documents a maximum connection lifetime. Not something this module can fix.

## Finding 2 (booth-core): the sidecar's `/healthz` can't be probed by the kubelet

The contract tells each chart to target the sidecar's `/healthz` with its own probes, but also requires
the sidecar to listen on loopback only. A kubelet `httpGet`/`tcpSocket` probe connects to the pod IP and
can't reach a loopback listener. An `exec` probe needs a tool in the sidecar's image, which is distroless.
**Workaround here:** the `booth-token` helper (same network namespace, has Python) carries the readiness
probe and hits `127.0.0.1:5432/healthz`, so the pod is Ready only when the sidecar has a lease. **Proper
fix (booth-core):** a `credential-sidecar healthcheck <url>` subcommand, so a chart can `exec` the
sidecar's own binary. booth-pipeline will hit the same wall.

## Not built: `s3` mode. Three gaps, all outside this repo

1. **The sidecar writes keys only.** `--kind=s3` writes `aws_access_key_id/secret/session_token`, but
   booth-storage's s3 grant also carries the location: endpoint, bucket, prefix, region, path style
   (`internal/backend/s3/s3.go`, ADR 0084's ask). For an in-cluster MinIO, PyIceberg and DuckDB can't do
   anything with keys alone. The sidecar would need to write the endpoint (e.g. `endpoint_url` in an AWS
   config file next to the credentials) or expose the grant's location some other way.
2. **The scope is per-workspace and dynamic; `--scope` is fixed per pod.** An s3 grant is scoped to the
   workspace's lakehouse warehouse (a booth-storage backend + path that belongs to that workspace).
   Notebook pods are per-workspace, so the hub *could* compute it at spawn, but only by asking
   booth-lakehouse for the workspace's warehouse location. That's a new hub-to-module dependency nobody has
   specified, or the warehouse location would have to become derivable.
3. **No gate exists.** `boothDatabase.url` gates postgres. Nothing gates s3, and the egress question
   depends on the backend: an AWS S3 endpoint is reachable via the public-internet rule, an in-cluster
   MinIO is not (private ranges are excluded). A new `boothLakehouse`-style value would be invented here
   otherwise.

Plus a consumer-side fact: `booth_lakehouse` (the client PyIceberg is driven through) currently requests
its own s3 grants from the broker (`HttpBroker`); using a sidecar-written file instead is a change in that
client, not this chart.
