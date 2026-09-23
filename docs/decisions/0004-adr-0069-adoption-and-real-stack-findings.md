# 0004: Adopting ADR 0069, verified against the real stack — and what that found

Status: **adopted; verified end to end against real booth-core (`cdd621e`), the real booth-design shell
(`f8b6804`) and Keycloak on kind, 2026-09-23.** Two new cross-cutting findings below are for the
coordinator. Neither is fixable in this module.

## What changed here: configuration only

As ADR 0069 predicted, no code changed. `identity.issuerUrl` now defaults to booth-core's iframe issuer,
`http://booth-core.booth-system.svc.cluster.local:8080/iframe-identity`: exactly the value core's chart
renders for a release `booth-core` in `booth-system`, confirmed against core's live discovery document.
Before trusting "pure configuration" I read core's `iframeidentity.Service.Mint` rather than assume the
shape. A bare JWT in `X-Booth-Identity`, RS256, `aud` = `notebooks`, `sub` = the person, groups under
the configured claim name, 2-minute expiry: all as `identity.py` already verifies. A contract test pins
the default.

**The one trap:** the issuer is compared *exactly* (token `iss` and discovery `issuer`). Core's chart
uses `…svc.cluster.local:8080`, while every other booth URL in the fleet (`core.url`, the workload issuer)
is spelled `…svc:8080`. Copying that spelling refuses every login. Called out in `values.yaml`.

## What the real-stack run proved (`tests/realstack`, 10 passed)

With cookies only after the initial iframe-URL fetch, exactly as an iframe behaves:

- core issues the iframe URL, sets its session cookie, and the shell's new `/iframe/` route and
  cookie-keyed fallback carry every JupyterHub request (`/hub/…`, `/user/…`) to core and on to the module;
- the hub logs the person in **from core's real signed assertion** (hub user `acme-analytics.<hash of
  their Keycloak sub>`), spawns their isolated pod (workspace label, no SA token, no Secret volumes), and
  core reports the module **Healthy** via the proxy;
- core provisioned the hub's database and minting Secret itself (ADR 0053/0056), nothing hand-made;
- JupyterLab loads and a kernel runs code **over its websocket through the shell's nginx and core's
  proxy**;
- the kernel's platform token was minted by the real core: `iss` = core's workload issuer,
  `sub` = `notebook:<hub user>`, `groups` = `[/workspaces/acme-analytics/editor]` (the owner-user is capped
  at the editor ceiling), `booth_module` = `notebooks`. Core's gateway accepted it (the call to the
  not-installed catalog got `404 catalog: not found`, not `401`);
- a page-supplied `X-Booth-Identity`/`X-Booth-Role` is replaced by core's own; a self-signed assertion
  sent straight to the notebooks proxy (bypassing core) gets 403;
- stopping the server deletes the pod.

Not exercised: a real browser (Keycloak's in-cluster issuer host isn't resolvable from this machine
without editing its hosts file). So the shell's session-renewal JS (ADR 0069 item C) and CSP framing
in an actual browser are unverified here. A kernel reaching a *real* booth-catalog/booth-storage also
wasn't tested, because neither was installed in this stack.

## Finding 1 (booth-design + booth-core, ADR 0069 item B): the fallback hijacks the shell itself

While a `booth_iframe_session` cookie exists (`Path=/`, 15 minutes, and kept alive by the new renewal
while a notebooks pane is open), **every top-level page load of the shell is proxied into the iframe
module**, including `/`. Measured with the cookie: `/`, `/storage`, `/notebooks` and `/catalog/datasets`
each got a 302 from JupyterHub to `/hub/<path>`. Without the cookie, the same `/storage` gets the SPA.
So a user who opens Notebooks and then refreshes the browser, or follows any shell link in a new tab,
lands in JupyterHub full-page, outside the shell, until the cookie lapses.

Cause: nginx's `@iframe_fallback` (and core's own `IframeFallbackHandler`, for a deployment exposing core
directly) decides "iframe follow-up or shell route?" purely by cookie presence.

Suggested fix: browsers label the request. A navigation *inside* an iframe carries
`Sec-Fetch-Dest: iframe`; a top-level page load carries `Sec-Fetch-Dest: document`. Serving the SPA for
`document` navigations (and letting `iframe`/`empty`/`script`/… through to core) keeps every JupyterHub
follow-up working and gives the shell its routes back. `tests/realstack` carries this as a strict xfail, so
it flips (XPASS) the day it's fixed.

## Finding 2 (booth-core): the iframe URL's origin can't be configured through the chart

Core builds the iframe URL from `BOOTH_PUBLIC_BASE_URL`, defaulting to `http://localhost:8080`. Core's
chart has no value that sets it, and the shell uses that absolute URL as the iframe `src`. So in any real
deployment the iframe points at `localhost:8080`. The run only worked because the shell was
port-forwarded to exactly `localhost:8080`. (A relative `/iframe/<id>/…` URL would sidestep this entirely,
since the shell now routes `/iframe/` itself.)

## Also worth the coordinator's attention

- **booth-e2e doesn't include booth-notebooks** in its representative set, and nothing automated
  exercises a logged-in shell session. `tests/realstack` + `hack/real-stack-e2e.md` are written to be
  portable into that harness, but adding it is booth-e2e's call.
- Kernel access to a real catalog/storage (DoD: "access to registered storage/catalog entries") has
  only been proven against a stand-in (`tests/integration`). The real-core leg up to core's gateway is now
  proven, but not a real booth-catalog/booth-storage accepting the token.
