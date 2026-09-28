# 0006: The ADR 0075 theme-sync JupyterLab extension — how it's built and what was verified

Status: **built and verified in a real JupyterLab** (2026-09-28). Not yet exercised against booth-design's
side of the protocol, which is being built independently (ADR 0075: either can land first).

## What was built (`images/singleuser/theme-sync/`)

A first-party prebuilt (federated) JupyterLab extension, `@projectbooth/jupyterlab-theme-sync`:

- `src/protocol.ts` holds the whole protocol and its trust decisions, with no JupyterLab import. It
  listens *before* announcing (the shell replies immediately), then posts `{type: "booth:iframe-ready"}`
  to `window.parent` with the same-origin target (never `"*"`). It acts on a message only if
  `event.origin === window.location.origin` **and** `event.source === window.parent`, and only for a
  well-formed `{type: "booth:theme", theme: "dark" | "light"}`. Not embedded (`parent === window`, e.g.
  JupyterLab opened in its own tab), it posts and listens to nothing.
- `src/index.ts` is the plugin. It starts the protocol after `app.restored`, i.e. once the theme manager's
  `apputils:change-theme` command exists, which is ADR 0075's "once its own code is able to act on a
  theme change". It applies themes via `app.commands.execute("apputils:change-theme", {theme: "JupyterLab
  Dark" | "JupyterLab Light"})`, as the ADR requires, never a settings REST PUT.

## Build

`images/singleuser/Dockerfile` gains a builder stage on the **same pinned base image** as the runtime.
It installs Node there (only there), runs `npm ci`, the handshake tests, `tsc`, then
`jupyter labextension build`. The resulting `labextension/` is copied into
`/opt/conda/share/jupyter/labextensions/@projectbooth/jupyterlab-theme-sync`, where JupyterLab discovers
it with no rebuild of JupyterLab itself. Building against the runtime's exact JupyterLab (4.4.5) matters
because a federated extension shares JupyterLab's own packages at runtime, so a version skew breaks it.
The runtime image has no Node.

**Maintenance this adds (ADR 0075 consequence, now real):** bumping the base image's JupyterLab means
rebuilding this extension against it. The builder stage does that automatically, and CI's `images` job
fails if the extension doesn't come out installed and enabled.

## Verified

- **Unit (8 tests, Node's built-in runner, `npm test`; run in CI's `theme-sync` job *and* inside the image
  build):**
  - readiness posted to the parent with the same-origin target;
  - listener in place before the post;
  - dark/light mapped to JupyterLab's theme names;
  - other origin ignored even from the parent;
  - a same-origin non-parent source ignored (popup, nested frame, other tab);
  - malformed/unrelated payloads ignored;
  - nothing done when not embedded;
  - unsubscribe works.
- **In a real JupyterLab (the built image, run locally, driven in Chrome).** A second JupyterLab was
  embedded in a same-origin iframe, so the outer page played the shell exactly: same origin, the frame's
  real `window.parent`.
  - `booth:iframe-ready` arrived from the frame, with the right origin, 2.2s after load. That gap is the
    ADR's accepted light-theme flash.
  - The parent sent `dark`: JupyterLab switched to **JupyterLab Dark**, and the persisted user setting
    read `JupyterLab Dark` (the command path, not a CSS swap).
  - A **sibling** same-origin frame sent `light`: ignored. A malformed `solarized` from the parent:
    ignored.
  - The parent sent `light`: back to **JupyterLab Light**, persisted.

## Not verified yet

The round trip with booth-design's real `IframeProxyPane.tsx` listener/sender, which doesn't exist yet on
this machine's checkout. Once it lands, one real-stack browser pass should confirm the shell's reply
arrives and a live toggle re-themes an open notebook. The embedded-side behaviour it depends on is
verified above.

## Judgment calls

- **The chosen theme persists in the user's JupyterLab settings** (a property of the sanctioned command,
  not something added here). Opening the same notebook server outside the shell therefore keeps the last
  shell theme. Harmless, and consistent with the ADR's requirement to use the command.
- **Readiness is announced once per page load**, not re-announced. A shell that remounts the pane
  reloads the iframe, which re-runs the handshake.
