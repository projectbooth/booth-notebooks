# 0002: The iframe-proxy path carries no verifiable identity — ADR candidate for the coordinator

Status: **resolved by ADR 0069 (accepted 2026-09-23)**, adopting the proposal below as written: core
`cdd621e` (item A), booth-design `f8b6804` (items B and C). This module adopted it as configuration only.
Verified against the real stack in [0004](0004-adr-0069-adoption-and-real-stack-findings.md), which also
records two follow-up findings. Kept below as the original analysis.

## What I found (from reading the code, commits as of 2026-09-22)

**1. No verifiable identity reaches an iframed module, and no user subject at all.**
`booth-core/internal/gateway/iframeproxy.go` `proxyIframeRequest` forwards the browser's request with only
`X-Booth-Workspace` and `X-Booth-Role` set. There's no bearer token: the browser's OIDC token lives in
memory (ADR 0032) and a plain iframe navigation can't carry it. And no subject, even though core has one
(`IframeClaims.Subject`). Consequences:
- A module can't meet core-platform-api.md "Auth enforcement" / ADR 0041 on this path: there's no token
  to re-verify, and a role header alone is exactly what ADR 0041 forbids trusting.
- JupyterHub can't work at all: per-user pods need to know *who* the user is. `X-Booth-Role` with no
  subject gives nothing to spawn for.
- This is fleet-wide, not notebooks-specific: superset, metabase and spark (all `iframe-proxy` per
  ui-integration.md) will hit the same thing.

**2. The shell doesn't route the iframe path at all.** `booth-design/docker/nginx.conf.template`
proxies only `/api/` and `/modules/` to core. The iframe URL core mints is
`<publicBaseURL>/iframe/<id>/...`. Then JupyterHub (like any third-party UI) issues root-relative
`/hub/...`, `/user/...` requests, which core's cookie-keyed `IframeFallbackHandler` exists to catch. So:
- If core's public base URL **is** the shell's origin: `/iframe/...` and every root-relative follow-up
  hit nginx's SPA fallback (`location /` → index.html) and never reach core.
- If it's a **separate** origin: the iframe works only while the two are same-*site*. The
  `booth_iframe_session` cookie is `SameSite=Lax`, which isn't sent on cross-site subresource/iframe
  requests. And every third-party UI's default `frame-ancestors 'self'` blocks the embedding unless it's
  configured with the shell's origin (this module exposes `singleuser.frameAncestors` for that).

**3. The iframe session hard-expires after 15 minutes.** `CookieTTL = 15 * time.Minute`, and
`booth-design`'s `IframeProxyPane` fetches the iframe URL once per module/workspace change, with no
refresh. A notebook session is typically hours: after 15 minutes every hub/JupyterLab request (saves,
kernel restarts, new websocket connections) 401s at core's gateway until the user navigates away and back.

## Proposal (for an ADR — core + design work, small on the module side)

**A. Core forwards a signed identity assertion on every iframe-proxied request.** In `proxyIframeRequest`
(both the entry and fallback handlers):
- Strip any client-supplied `X-Booth-Identity`, then set `X-Booth-Identity: <JWT>`, RS256, with:
  `iss` = a **distinct** issuer URL for this token class (e.g. `<core>/iframe-identity`), `aud` = the
  module id, `sub` = the person's `sub` (already in `IframeClaims`), `groups` = `["/workspaces/<ws>/<role>"]`
  (ADR 0025 grammar, so every module's existing ADR 0041 code reads it unchanged), `exp` ≤ 2 minutes.
- Publish it via its own `/.well-known/openid-configuration` + JWKS, exactly like the workload issuer
  (ADR 0058). A separate issuer, not the workload one, because workload tokens are defined as *never a
  person* (ADR 0056/0058), and a module trusting one class must not implicitly accept the other.
- **Why a dedicated header, not `Authorization`:** third-party UIs own their `Authorization` header.
  JupyterHub and jupyter-server both parse `Authorization: bearer …` as *their own* API token, so an
  injected JWT there collides with their auth (verified reading `jupyterhub/handlers/base.py`).
  Superset/Metabase have their own semantics too. A module-agnostic header avoids every collision.

**B. The shell routes the iframe path to core.** Either nginx proxies `/iframe/` plus a catch-all to
core for requests carrying `booth_iframe_session` (the fallback's whole premise, with websocket upgrade
and long timeouts as `/modules/` already has), or the deployment docs require core's public URL to be
same-site with the shell and every iframe module to set `frame-ancestors` to the shell origin. The first
is the "general fix, not a per-module patch" ui-integration.md asks for.

**C. The iframe session is renewable.** The shell re-requests an iframe URL (or a lighter
cookie-refresh endpoint) before `CookieTTL` lapses, while the pane is mounted. Or core slides the
cookie on activity, bounded by a fresh check of the user's token at some cadence. Otherwise every
long-lived iframe UI breaks at 15 minutes.

## What this module already does, so adopting A is configuration only

- `identity.py` verifies any JWT from a configured list of trusted issuers (dispatch by `iss`, OIDC
  discovery + JWKS, asymmetric algorithms only, audience per issuer), derives role from the groups claim,
  rejects a forwarded role stronger than the token (ADR 0041), and rejects workload-shaped subjects
  (`<kind>:<id>`), so a pipeline run's token can never open a notebook session.
- The hub reads the assertion from `X-Booth-Identity` (configurable: `identity.header`), with the issuer
  as `identity.issuerUrl`. Proposal A would mean setting `identity.issuerUrl` to core's iframe issuer.
- It re-verifies on **every** browser request, not just at login (`authenticator.py`), so a lost
  membership or a workspace switch takes effect on the next request.
- Tested against a real JupyterHub process (`tests/hub/`) and on kind (`tests/integration/`), with a
  stand-in core signing assertions exactly as A describes.
- If the ruling is instead "core forwards the person's own OIDC token", the module needs only
  `oidc.issuerUrl` (also implemented and tested), but that re-raises the `Authorization`-collision
  problem above, so it would need the dedicated header either way.

## Not in scope of this gap

Kernel → storage/catalog calls don't use the iframe path at all. They use ADR 0056 workload tokens
through the ordinary gateway route, which works today (ADR 0059). See 0003.
