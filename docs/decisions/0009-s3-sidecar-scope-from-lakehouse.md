# 0009: The s3 credential sidecar, scoped by a spawn-time booth-lakehouse lookup (ADR 0095, third amendment)

Status: **scope resolution built and tested (2026-10-05)** with unit tests against a stand-in core and
lakehouse, and with chart contract tests. A kind step against the in-cluster stand-in is written; it runs
on CI's Integration workflow, because this host is too short of memory for a local kind run. The chart
now pins booth-core's re-published sidecar, `ff7572b`
(`sha256:a3a0f90b…`, run 37353562693), which writes `<path>` and `<path>.config`. **Finding 3, below:
the default kernel's engines don't read the endpoint from that file**, so s3 access from a notebook isn't
yet usable without an explicit endpoint.

This resolves gaps 2 and 3 in [0008](0008-credential-sidecar-adoption.md) as Architecture ruled. Gap 1
(endpoint and bucket) is booth-core's fix. Findings 1 and 2 in 0008 are still open.

## New coupling: booth-notebooks → booth-lakehouse, at spawn time

Recorded here so it isn't folded in silently. When `boothStorage.url` is set, **every spawn** makes one
call to booth-lakehouse through core's gateway:

    GET {core}/modules/lakehouse/api/warehouse    Authorization: Bearer <notebook workload token>, X-Workspace: <ws>

- **Identity.** The hub mints the token exactly as it mints the kernel's own: subject
  `notebook:<hub user>`, owner = the person, ceiling `editor`. It uses the token for this one call, then
  drops it. The token never enters the pod spec (a test checks this).
- **Scope.** Only `backendId` and `path` are taken from the reply. They become
  `--scope={"backendId":…,"path":…}` (brace-escaped for KubeSpawner's templating, which a test also
  checks).
- **Soft dependency.** A 404 means no s3 sidecar. That covers "no warehouse yet", and also "no
  booth-lakehouse installed", because core's gateway 404s an unknown module. **Judgment call:** every
  other outcome also means no s3 sidecar, logged as a warning, and never a failed spawn. That includes an
  unreachable lakehouse, a 5xx, an unreadable reply, a refused mint, and no minting Secret. This is the
  same rule as the postgres path (0008, "must never block a notebook from starting").
- **Cost and staleness.** A spawn can take up to about 15s longer in the worst case (10s mint timeout
  plus 5s lookup timeout). Scope is fixed for the pod's lifetime: a warehouse created while a server runs
  is picked up at the next server start. It is resolved fresh on every spawn, so nothing lingers.
- **Network.** None new. The hub already egresses to core for minting, and the call goes through core's
  gateway.

## What was built

- **Gate:** `boothStorage.url`, the same value and shape as booth-pipeline's. It adds one egress rule
  (`singleuser.networkPolicy.egress.boothStorage`, port default 9000). Both selectors are **required**
  when it's set, since no standard backend exists to default to. The chart fails otherwise, word for word
  as booth-pipeline does. It is independent of `boothDatabase.url`; the two kinds can be on separately or
  together. Both need `BOOTH_CORE_URL` and the digest-pinned image (chart and hub config both refuse a
  tag).
- **Pod:** a `credential-sidecar-s3` container with these flags:
  - `--kind=s3`
  - `--access` by the person's role (read for viewers, as for postgres)
  - `--credentials-file=/var/run/booth-s3/credentials`
  - `--health-listen=127.0.0.1:9472`, off the default 8080, which user code commonly binds
  - the shared `--token-file`

  The notebook mounts `/var/run/booth-s3` **read-only** and gets `AWS_SHARED_CREDENTIALS_FILE` and
  `AWS_CONFIG_FILE=<that>.config`, the amendment's two-file output. There are no keys in env, and
  `check_pod` now refuses `*SECRET_ACCESS_KEY` and `*SESSION_TOKEN` env vars and any non-loopback
  `--health-listen`.
- **One token helper for both kinds.** `booth-token` is added if either sidecar is present. Its readiness
  probe now takes several URLs and is ready only when every sidecar's `/healthz` is. As before, the
  server counts as up on the notebook container alone.

## A cross-module fact to confirm with booth-core: the files are written 0600

`internal/sidecar/s3file.go` writes both files `0600`, owned by the sidecar's uid. With the sidecar's
default distroless uid (65532), the notebook (uid 1000) could not read them. **Worked around here:** the s3
sidecar runs as uid 1000/gid 100, the notebook's own user, which is the one other reader. It is still
non-root, has a read-only root, and drops all capabilities. The postgres sidecar stays at 65532.

This works, but it couples the sidecar's uid to the consumer's. If booth-core would rather write `0640`
and let charts share a group (fsGroup), that would remove the coupling. Worth a line in the contract
either way, since booth-pipeline's runner will meet the same thing.

## Measured against the real sidecar (`ff7572b`), container-level

The real stack wouldn't fit on this host (0.5 GB free, with other sessions' clusters up), so the new
part was checked with containers laid out exactly as the spawner lays out the pod:
- the real sidecar at the pinned digest, run as uid 1000:100, with a read-only root and all capabilities
  dropped, using the same flags;
- a stand-in broker returning a MinIO lease in booth-storage's `s3CredentialBody` shape;
- a real MinIO;
- the singleuser image, run as uid 1000 with only the two `AWS_*` variables set and the directory mounted
  read-only.

- **Works:** the sidecar's request is `{"kind":"s3","access":"readwrite","scope":{"backendId":…,"path":…}}`
  with `X-Workspace` and a Bearer token. `/healthz` returns 200 on `127.0.0.1:9472`. Both files are written,
  `0600`, uid 1000. The config file reads `[default] endpoint_url = http://minio:9000, region = us-east-1`.
  A renewal (90s lease) rewrote both files atomically, still readable by the notebook.
- **The uid workaround is necessary.** With `0600` files, only the sidecar's own uid can read them.

### Finding 3 (contract design): the default kernel's engines ignore `endpoint_url` in AWS_CONFIG_FILE

Keys and region are picked up from the files. The endpoint is not, so both engines go to real AWS
(`InvalidAccessKeyId`):

| Engine (in the default image) | files only | + `AWS_ENDPOINT_URL` | explicit endpoint |
|---|---|---|---|
| pyarrow 25.0.1 `fs.S3FileSystem()` | AWS | AWS (also `_S3`) | **works**: `endpoint_override="minio:9000", scheme="http"` |
| DuckDB 1.5.6 `CREATE SECRET (TYPE s3, PROVIDER credential_chain)` | `endpoint=s3.amazonaws.com` | same | **works**: `+ ENDPOINT 'minio:9000', URL_STYLE 'path', USE_SSL false` |

The two-file output is right for boto3 and botocore-based tools, which read `endpoint_url` from the
config file (botocore ≥ 1.31). Those aren't in the default image, and the two engines that are don't read
it. Path style isn't in the file either, and DuckDB needs it for MinIO. Options for the coordinator:

1. **A small helper in this module's `booth` client.** It would read the two files and hand back a
   configured `pyarrow.fs.S3FileSystem` and a DuckDB secret: endpoint, path style and SSL from the
   endpoint's scheme. It's cheap and local, but user code has to call it. Fresh keys come from re-reading
   the file, so a long-lived filesystem object would need re-creating after rotation.
2. **The sidecar also writes the location in a non-AWS shape** that engines can be pointed at. That still
   needs a helper.
3. **Accept it, and document "pass the endpoint explicitly".** But user code can't know the endpoint
   without reading the file itself.

Recommendation: option 1, owned here, with `pathStyle` added to the config file by booth-core
(`s3 =\n  addressing_style = path`, which botocore reads) so that nothing is guessed. Not built pending a
ruling.

## Tests

- `tests/unit/test_s3_sidecar.py` (32): the real `WorkloadMinter` and lookup against an httpx stand-in
  for core and lakehouse:
  - the exact request (path, Bearer, X-Workspace) and mint body;
  - the scope, and that nothing else of the reply reaches the pod;
  - the token never reaches the pod;
  - braces in the path survive;
  - env, mounts, uid and role-based access;
  - one helper for both kinds;
  - 404, eight other failure answers, refused mints and no minter all mean no sidecar and a normal pod;
  - re-resolution on each spawn;
  - the config errors and the `check_pod` rules.
- `tests/contract/test_chart.py` (+3): the rule renders only when set, on the configured selectors and
  port; empty selectors fail; a tag fails; the rendered env configures the spawner.
- `tests/integration/test_kind.py`:
  - The ADR 0092 step now also sets `boothStorage.url`, and the stand-in core serves
    `/modules/lakehouse/api/warehouse` (a warehouse for `beta`, 404 otherwise).
  - The beta spawn asserts the s3 sidecar's scope, the lookup made with a minted `wl-` token, the AWS env,
    and that the directory is present and not writable from the live notebook.
  - The notebook must start with no broker present: the same never-blocks guard as for postgres.
- **Deferred until booth-core's digest lands:** a real-stack test (the real broker and real booth-storage
  MinIO, the s3 sidecar writing both files, and DuckDB or PyIceberg in a kernel reading through them).
  Then the pin in `values.yaml` moves to the new digest.
