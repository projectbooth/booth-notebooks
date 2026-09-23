# 0003: First-pass judgment calls — flagged for ratification

Status: **built and tested; each call below is cheap to change and awaiting the coordinator's
ratification.** The cross-cutting gap is in [0002](0002-iframe-proxy-identity-gap.md). The kernel
language decision is in [0001](0001-default-kernel-python-only.md).

## How ADR 0056/0057 were adopted (fleet patterns, not re-derived)

| Pipeline's shape | Notebooks' equivalent |
|---|---|
| API/scheduler pod holds DB + minting credential, never runs task code | **Hub** holds DB DSN, minting credential, crypt key, cookie secret, proxy token, and never runs user code |
| Runner pod: no module credential, no SA token, NetworkPolicy allowlist | **Per-user notebook pod** (KubeSpawner): same, and `spawner.check_pod` *refuses to create a pod* whose serialized spec would mount a forbidden Secret or SA token, env-ref one, carry a hub-side setting, or be privileged, however it was configured |
| Token minted before dispatch, refresh *pushed* to the runner | Token *pulled* by the kernel: `POST /hub/api/booth/platform-token`, authenticated by the pod's own JupyterHub API token (which JupyterHub already gives every server, scoped to that one user). The pod names nothing; workspace, owner and ceiling come from the hub's verified record. Browser cookies and service tokens are refused. |
| `subject = run:<id>`, ceiling `editor`, never `owner` | `subject = notebook:<hub-username>`, ceiling `editor`, never `owner`; core caps it at the person's live role, so a viewer's kernel is read-only |

Pull rather than push because notebook pods are many and long-lived, and the hub has no channel into
them. Pull also means a refused mint (owner not seen recently, ADR 0058's 7-day window) surfaces in the
notebook as a readable `BoothError` at the moment of the call.

**One real difference from pipeline, flagged:** notebooks use workload tokens **even during an
interactive session**, not only unattended. There's no alternative: the browser's token never reaches
the iframe path (0002), let alone a pod. ADR 0056's framing is "unattended runs". This is the same
mechanism applied to "code that isn't the browser". I think it fits the ADR's intent (the token names
the server, never the person; the role is live-capped), but it widens the ADR's stated scope, so it
should be ratified explicitly.

## Judgment calls

| Call | Why | Alternative |
|---|---|---|
| **Own Helm chart, not the zero-to-jupyterhub (Z2JH) chart as a subchart** | Matches every other booth-* chart and keeps the ADR 0057 topology small enough to pin in contract tests (14 objects vs. Z2JH's ~50, many of them RBAC). JupyterHub/KubeSpawner/CHP are used unmodified; only the packaging is ours. | Z2JH brings user-scheduler, image pre-puller, a tuned culler. Worth revisiting if we need those. |
| **One hub user per (person, workspace)**: `"<workspace>.<sha256(sub)[:16]>"` | Isolation by construction: different pod, home volume and platform token per workspace. A lost membership can never leave a path in via another workspace's session. Subject hashed because it's the only stable identifier (names get renamed or reused). | One user per person with a named server per workspace: fewer hub users, but per-server authorization logic we'd have to write. |
| **Viewers may open notebooks** (`identity.allowedRoles`, default all three) | Mission is "exploration and analytics". The kernel acts with the person's own live role, so a viewer can read but not write storage/catalog. Compute is bounded by per-pod limits. | Default to `[owner, editor]`, treating compute as a write. One value to change. |
| **Re-verify identity on every browser request to the hub** (patches JupyterHub's private `User._auth_refreshed`) | `auth_refresh_age` can't express "always": 0 disables refresh, and ≥1 skips checks for N seconds **per user, not per session**, so a stale cookie rides the real user's recent request. Found by `tests/hub`, not theorised. Fails loudly at startup if a JupyterHub upgrade changes the internal. | Accept a 1-second per-user window. |
| **Open-tab window bounded to 15 min** (`hub.sessionSeconds` → `oauth_token_expires_in`) | The single-user server re-checks with the hub only when its OAuth token expires. That's how long an already-open JupyterLab tab outlives a lost membership. Matches core's iframe cookie TTL. | Shorter = more hub round-trips; a per-request check would mean an auth proxy in front of every pod. |
| **Hub `extra_handlers` for `/hub/booth/healthz` and the platform-token endpoint** | Deprecated in JupyterHub 3.1 in favour of hub-managed services, still supported in 5.x (we pin `<6`). A service means another process, port and NetworkPolicy rule for two handlers. **Must move to a service before JupyterHub 6.** | A `booth-platform` JupyterHub service now. |
| **Notebook files on a per-(person, workspace) PVC**, not in booth-storage | The JupyterHub norm. JupyterLab's file browser works unchanged. PVCs survive server stops *and* chart uninstall (the hub creates them, not Helm; the README documents cleanup). Data *reads/writes* still go through booth-storage via `booth`. Whether notebooks become cataloged assets is `ARCHITECTURE.md` §7 item 12, untouched. | A jupyter-server contents manager backed by booth-storage: notebooks become platform data, at the cost of a custom contents manager. |
| **Notebook pods in the module's own namespace**, not per-workspace namespaces | KubeSpawner supports per-user namespaces, but that needs cluster-scoped RBAC, and ADR 0029 leaves namespace strategy undecided fleet-wide. Pods carry a `booth.projectbooth.io/workspace` label for any future per-workspace policy/quota. | `enable_user_namespaces` once a namespace strategy exists. |
| **Kernel internet egress on by default** (public only: private ranges and 169.254/16 metadata excluded) | `pip install` and public datasets are core notebook use. **This differs from booth-pipeline's runner (off by default)**: an unattended task needs it far less than an interactive analyst. One value (`singleuser.networkPolicy.egress.allowInternet`). | Off by default, matching pipeline. |
| **No JupyterHub admins** (`admin_users = {}`, `admin_access = False`) | The hub admin page can start, stop or impersonate any user, with no workspace boundary. Workspace roles are the only authority. | Map workspace `owner` to a hub admin scoped by JupyterHub RBAC filters. Not v0. |
| **Idle culling at 1 h** (`hub.cullIdleSeconds`), via the standard `jupyterhub-idle-culler` as a least-privilege hub service | Per-user pods must actually go away. The culler's role is exactly `list:users, read:users:activity, read:servers, delete:servers`. | Tune per deployment. |
| **`requiredScopes`: storage/catalog read+write** | What kernels call on a user's behalf. The field's semantics are still loose fleet-wide (pipeline declares its own `pipeline.*`), so this is a best guess. | `notebooks.*` like pipeline. |

## Found only by running it (fixed, each now pinned by a test)

- **JupyterHub's refresh throttle is per user, not per session** (`tests/hub`): a stale cookie could
  ride on the real user's request from a second earlier. Fixed by `refresh_on_every_request`.
- **KubeSpawner's pod is a mix of model objects and raw dicts**: a safety check written against model
  attributes would crash on (or skip) operator-supplied dicts. `check_pod` now inspects the serialized pod.
- **`jupyterhub-singleuser` crashes on a `LazyConfigValue` `tornado_settings`** (kind): the framing
  snippet now assigns a real dict, and CI's image job asserts that shape.
- **The idle culler couldn't reach the hub through the hub's own Service** (kind, no hairpin NAT): it
  now uses loopback.
- **The hub crash-looped on install until the proxy's API was up** (kind): an init container waits for it.

## Known residuals (documented, not fixed)

- **`JUPYTERHUB_API_TOKEN` is in the pod spec's env**, readable by anyone with `get pods` in the module's
  namespace. Standard KubeSpawner behaviour. It's scoped to that one user's server, and via our endpoint
  worth at most that user's own role-capped 10-minute platform token.
- **The NetworkPolicies are the real boundary** for "the proxy only admits core" and "pods can't reach
  the database". They're silently inert on a CNI that doesn't enforce NetworkPolicy, including kind's
  default, so layer 3 can't prove them.
- **A notebook pod's resources aren't per-workspace quota'd**: each pod has limits, but nothing caps a
  workspace's total. A per-namespace ResourceQuota covers the whole module, not one tenant.
- **Nothing here has run against a real booth-core.** Core's identity assertion doesn't exist yet (0002).
  Minting and the gateway were exercised against a stand-in following ADR 0058 and core's code as read.
