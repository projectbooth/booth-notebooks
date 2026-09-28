# 0005: Third pass — fixed iframe path verified, a session-lifetime bug fixed, real data access proven

Status: **done on the real stack** (kind; real booth-core `fa11da0`, booth-design `526764e` including
`af1fc39`, booth-storage `43542db`, booth-catalog `3d36f5d`, Keycloak), 2026-09-27. The real-browser
check is recorded separately below.

## 1. Both ADR 0069 follow-up fixes hold, from this module's side

`tests/realstack` now sends the `Sec-Fetch-Dest` header a real browser sends (`iframe` for navigations
inside the pane, `empty` for JupyterLab's own calls, `document` for a top-level load), since that's what
the shell and core route on now. With the iframe session cookie live:

- a top-level load of `/`, `/storage`, `/notebooks`, `/catalog/datasets` gets the shell (previously
  JupyterHub's 302 to `/hub/<path>`). The strict xfail is gone; these are ordinary tests;
- the same session inside the pane still reaches JupyterHub and the notebook server;
- core mints a **relative** iframe URL, and the whole flow works with the shell on `localhost:8090`: the
  "shell must be at `localhost:8080`" constraint is gone.

## 2. Bug found and fixed: notebooks broke once the hub session window lapsed (ADR 0070 item 7)

`hub.sessionSeconds` (JupyterHub `oauth_token_expires_in`) was 900s, chosen as "how long an open tab
outlives a lost membership". Measured on the real stack with it set to 120s: JupyterLab's API calls and
new websockets returned **403** once it lapsed. Core's own request log shows 200 → 403 between 20:55
and 20:58, i.e. 120s plus jupyter-server's 5-minute `HubAuth` token cache. At the old default that is
every notebook breaking 15–20 minutes in, however well the shell renews core's iframe session, which is
exactly the ADR 0070 item 7 dependency.

**Fix:** the default is now 86400s (the hub cookie's own lifetime), and the hub-side window is no
longer treated as the membership bound. The bound it was standing in for is enforced one layer out,
and better: every request reaches the pod only through core's iframe session (the proxy admits only
core). The shell renews that session every 10 minutes via core's `iframe-url` endpoint, which
re-verifies the person's live token and workspace membership, and it lapses 15 minutes after the last
successful renewal. Re-measured after the fix, same probe: API 200 and websocket open at t+0, 150, 330
and 420s (the old setting was already 403 by t+300). So a lost membership still loses the open tab within about 15 minutes, while a
legitimate session no longer breaks at a fixed time. Hub pages themselves are still re-verified on
every request (`authenticator.py`). A unit test pins the session to at least the hub cookie lifetime;
the knob remains for operators.

(The same probe's final sample returned 404, not 403. That was not a second bug: the host was so
memory-starved that the "t+450s" request actually ran 22 minutes after the session started, past core's
15-minute cookie, with no shell renewing it, which is exactly the lapse ADR 0069 C's renewal prevents.)

## 3. DoD item proven with every hop real: a kernel reads registered data

`test_a_kernel_reads_a_registered_dataset_and_registers_its_output`, against real booth-storage and
booth-catalog (`hack/real-stack-data.sh`):

- The person registers a filesystem backend, uploads a CSV and catalogs it, through the shell.
- In a kernel, `booth.read_dataset("<name>")` returns a DataFrame (sum 30). That's catalog lookup, then
  storage read, with a core-minted workload token that real storage and catalog verify against core's
  issuer, all through core's gateway.
- The kernel writes `out/total.csv` back to storage and registers it. Real booth-catalog records the new
  dataset's `createdBy` as `notebook:<hub user>`: the run, never the person (ADR 0056).

## 4. Tooling

`hack/real-stack-up.sh` (+ `hack/real-stack-data.sh`) now stands up the whole real stack reproducibly,
instead of the hand-run steps from the second pass. Everything is installed the way booth-e2e does it,
with one difference: booth-catalog's BoothModule still doesn't declare `database: {enabled: true}` (an
existing booth-e2e finding), so the script patches the live resource instead of using booth-e2e's Helm
post-renderer.

## 5. Branch protection

`main` now requires CI's `python` and `images` jobs, strict (up to date), enforced for admins, mirroring
booth-design, the only other protected booth repo. Direct pushes to `main` are therefore blocked, so
changes land by PR.
