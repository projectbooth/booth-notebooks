# booth-notebooks

Project Booth's notebooks module (nav group **Build**): JupyterHub with KubeSpawner, running **one isolated
notebook pod per person per workspace** (ADR 0011), embedded in the shell through core's iframe proxy
(ADR 0005). Kernels read registered datasets and storage through `booth-catalog`/`booth-storage`, as a
short-lived, role-capped platform identity (ADR 0056/0057). Nothing here needs Spark or any other module
to be installed (ADR 0006).

> **Status: works through the real shell.** With ADR 0069 shipped (core `cdd621e`, design `f8b6804`), a
> user's notebook spawns and runs through the real booth-design shell and real booth-core. Verified end
> to end on kind with Keycloak, including a kernel websocket and a core-minted platform token
> ([docs/decisions/0004](docs/decisions/0004-adr-0069-adoption-and-real-stack-findings.md),
> [0005](docs/decisions/0005-third-pass-real-stack-verification.md)). A kernel reads registered datasets from
> real booth-catalog/booth-storage and registers its output, attributed to the notebook's run identity.

## How it fits together

```
browser ──shell──▶ booth-core iframe proxy ──(X-Booth-Identity JWT, X-Booth-Workspace)──▶ proxy (CHP) ─┬─▶ hub
                                                                                                        └─▶ notebook pod (/user/<name>/)
notebook pod ──(its own JupyterHub API token)──▶ hub /hub/api/booth/platform-token ──(minting credential)──▶ booth-core
notebook pod ──(Bearer <10-min workload token>, X-Workspace)──▶ booth-core gateway /modules/{storage,catalog}
```

| Component | Holds | Runs user code |
|---|---|---|
| **hub** (`images/hub`, `src/booth_notebooks`) | hub DB DSN (ADR 0053), workload-minting credential (ADR 0056), crypt key, cookie secret, K8s token for KubeSpawner | **never** |
| **proxy** (configurable-http-proxy) | the route-API token only | no |
| **notebook pod** (`images/singleuser`) | its own per-user JupyterHub API token only; no SA token, no module Secret | **yes** |

- `identity.py`: verifies a JWT from a configured trusted issuer (OIDC discovery + JWKS), derives the
  workspace role from its groups claim, rejects a forwarded role stronger than the token (ADR 0041), and
  never lets a workload (`<kind>:<id>`) token log in as a person.
- `authenticator.py`: auto-login from that identity, **re-verified on every browser request** to the hub.
  A lost membership or a workspace switch takes effect immediately; a hub cookie alone is worth nothing.
- `spawner.py`: KubeSpawner, plus a last check that refuses to create a pod that could read a module
  credential, however it was configured.
- `handlers.py` + `workload.py`: `/hub/booth/healthz` (the manifest's health path) and the kernel
  platform-token endpoint.
- `hubconfig.py`: the entire hub configuration as a function of the env the chart sets (unit-testable).
- `client/` (the `booth` package in the default kernel):

  ```python
  import booth
  booth.catalog.datasets(q="sales")
  df = booth.read_dataset("daily-sales")        # by name or id -> pandas DataFrame (csv/tsv/parquet/json)
  booth.storage.read("lake", "raw/file.csv")    # {backendId, path}, ADR 0045
  booth.catalog.register_dataset("result", "lake", "out/result.parquet")
  booth.read_dataset("daily-orders")            # an Iceberg table (format: "iceberg", ADR 0085), via booth_lakehouse
  booth.platform_token()                        # this notebook's platform token, for other clients (ADR 0084)
  booth.s3.pyarrow_filesystem()                 # object storage via the credential sidecar (ADR 0095)
  booth.s3.duckdb_secret(duckdb.connect())      # the same, for DuckDB's s3:// paths
  booth.database.engine()                       # SQLAlchemy on DATABASE_URL, reconnects after a lease expires (ADR 0095)
  ```

## The default kernel environment

`images/singleuser`: Jupyter docker-stacks `base-notebook` (Python 3.12, JupyterLab 4.4, pinned by
digest) + pandas, pyarrow, duckdb, matplotlib + `booth`, plus `@projectbooth/jupyterlab-theme-sync`, a
small JupyterLab extension that follows the shell's dark/light toggle live over a same-origin
`postMessage` handshake (ADR 0075, `images/singleuser/theme-sync/`). **Python only in v0.** See
[docs/decisions/0001](docs/decisions/0001-default-kernel-python-only.md) for the investigation. Operators can
add environments with `singleuser.profiles` (a KubeSpawner `profile_list`). Each image needs
`jupyterhub-singleuser` 5.x and uid 1000/gid 100. Only the default image ships the `booth` client.

## Installing

Requires booth-core (BoothModule CRD, `booth-database-credentials`, `booth-workload-minting-credentials`).

```sh
helm install notebooks charts/booth-notebooks -n booth-notebooks \
  --set core.url=http://booth-core.booth-system.svc:8080
```

`identity.issuerUrl` defaults to booth-core's iframe-identity issuer (ADR 0069), as rendered by core's chart
for a release `booth-core` in `booth-system`. If your core release or namespace differs, set it to core's
`BOOTH_IFRAME_IDENTITY_ISSUER_URL` **exactly**: core spells it `…svc.cluster.local:8080/iframe-identity`,
and a `…svc:8080` spelling refuses every login.

Key values: `identity.*`/`oidc.*` (who may log in), `singleuser.*` (image, resources, storage, profiles,
framing origin, egress), `hub.cullIdleSeconds`, `hub.sessionSeconds`, `workloadIdentity.enabled`,
`core.namespaceSelector`/`podSelector` (must match your core install: they gate proxy ingress and pod
egress), `boothDatabase.url` (set once a bundled-mode booth-database is installed, to let notebook pods reach
its Postgres on 5432 *and* to run booth-core's credential sidecar in every notebook pod, giving kernels
`DATABASE_URL=postgresql://localhost:5432/<db>` with no credential in it (ADR 0092/0095); selectors under
`singleuser.networkPolicy.egress.boothDatabase`; external-mode booth-database goes in `egress.extra`),
`boothStorage.url` (the in-cluster S3 backend's address: one egress rule to it, selectors required under
`singleuser.networkPolicy.egress.boothStorage`, plus the sidecar's s3 mode for every pod whose workspace has
a lakehouse warehouse, found at spawn via booth-lakehouse, giving kernels `AWS_SHARED_CREDENTIALS_FILE` and
`AWS_CONFIG_FILE`; ADR 0095 third amendment, docs/decisions/0009),
`credentialSidecar.image` (pinned by digest; a tag is refused). NetworkPolicies need a CNI that enforces them.

### Data lifecycle

- Stopping a server (or the idle culler, default 1 h) deletes its **pod**. The home **volume**
  (`claim-<hub user>`) is kept.
- Uninstalling the chart deletes hub, proxy and policies. Running notebook pods and all home volumes
  are **not** deleted (the hub created them, not Helm). To remove them:
  `kubectl -n <ns> delete pod -l component=singleuser-server; kubectl -n <ns> delete pvc -l app.kubernetes.io/part-of=booth-notebooks`
  (PVCs carry KubeSpawner's labels; check before deleting).
- The hub's generated Secret is kept across upgrades and uninstalls (`helm.sh/resource-policy: keep`).
  Rotating its crypt key invalidates stored identities: users just log in again.

## Development

```sh
python -m venv .venv && . .venv/bin/activate      # .venv\Scripts\activate on Windows
pip install -e ".[dev]" -e client
ruff check src tests client
pytest tests/unit tests/client tests/hub tests/contract   # layers 1-2; contract tests need `helm`
# tests/realstack: against real booth-core + shell + Keycloak, see hack/real-stack-e2e.md
sh hack/kind-integration.sh                                 # layer 3: needs docker + kind + kubectl + helm
```

| Suite | What it proves |
|---|---|
| `tests/unit` | identity/role decisions, authenticator login + refresh, minting wire contract, the pod KubeSpawner actually builds (real manifest code), hub config |
| `tests/client` | the `booth` package against a fake hub + gateway |
| `images/singleuser/theme-sync` (`npm test`) | the ADR 0075 theme-sync handshake: same-origin/parent-only trust, payload validation, no-op when not embedded |
| `tests/hub` | a **real JupyterHub process** with this config (only spawner/proxy swapped): login, per-request re-verification, workspace switch, platform tokens, refusals |
| `tests/contract` | the BoothModule manifest vs. module-manifest.md, the credential/RBAC/network topology, and that the chart's rendered env is a valid `hubconfig` |
| `tests/realstack` | **real booth-core + booth-design shell + Keycloak**: iframe URL, core's signed assertion, spawn, a kernel websocket through the shell, a core-minted platform token (`hack/real-stack-e2e.md`) |
| `tests/integration` | on kind, stand-in core: deploy, spawn a real pod, JupyterLab through the proxy, kernel → hub → mint → gateway, hub restart without losing servers, a second workspace's pod, teardown keeps the home volume |

CI: `.github/workflows/ci.yml` (layers 1–2 plus image builds, every push/PR, required on `main` via branch
protection), `.github/workflows/integration.yml` (layer 3, merge to `main` and nightly).

## Decisions

- [0001](docs/decisions/0001-default-kernel-python-only.md): Python-only default kernel, with an operator seam.
- [0002](docs/decisions/0002-iframe-proxy-identity-gap.md): the iframe-proxy identity gap, resolved by ADR 0069.
- [0003](docs/decisions/0003-first-pass-judgment-calls.md): how ADR 0056/0057 were adopted, plus judgment calls for ratification.
- [0004](docs/decisions/0004-adr-0069-adoption-and-real-stack-findings.md): ADR 0069 adoption, real-stack verification, two findings (both since fixed in core/design).
- [0005](docs/decisions/0005-third-pass-real-stack-verification.md): third pass: fixes verified, the notebook-session-lifetime bug fixed, a kernel reading real registered data.
- [0006](docs/decisions/0006-adr-0075-theme-sync-extension.md): the ADR 0075 theme-sync JupyterLab extension: build, and end-to-end verification through the real shell.
- [0008](docs/decisions/0008-credential-sidecar-adoption.md): booth-core's credential sidecar (ADR 0095): postgres mode shipped and verified on a real cluster; Finding 1 closed by the fifth amendment (`booth.database.engine()` survives lease expiry; in-flight work is lost); Finding 2 open.
- [0007](docs/decisions/0007-lakehouse-client-support.md): `booth.platform_token()` and reading Iceberg datasets through `booth_lakehouse`.
- [0009](docs/decisions/0009-s3-sidecar-scope-from-lakehouse.md): the s3 sidecar (ADR 0095 third amendment): its scope from a spawn-time booth-lakehouse lookup (a new runtime coupling); measured against the real re-published sidecar; Finding 3 (kernel engines ignore the config file's endpoint), answered by the `booth.s3` helper.
