/**
 * ADR 0075's iframe-proxy live theme-sync protocol, embedded side — deliberately free of any JupyterLab
 * import, so the security-relevant part (who we believe, what we act on) is unit-testable in plain Node
 * (test/protocol.test.mjs) without a JupyterLab runtime.
 *
 *   us    -> shell : {type: "booth:iframe-ready"}                 once, when we can act on a theme change
 *   shell -> us    : {type: "booth:theme", theme: "dark"|"light"}  in reply, and on every shell toggle
 *
 * Trust: a message is acted on only if it comes from the same origin (the shell and every iframe-proxied
 * module are same-origin by construction since ADR 0069's relative /iframe/ URLs) AND from our own parent
 * window — not from a popup, a nested frame, or another tab that happens to share the origin.
 */

export const READY = "booth:iframe-ready";
export const THEME = "booth:theme";

export type ShellTheme = "dark" | "light";

/** JupyterLab's own built-in theme names, the only two this protocol maps to. */
export const JUPYTERLAB_THEMES: Record<ShellTheme, string> = {
  dark: "JupyterLab Dark",
  light: "JupyterLab Light"
};

/** The slice of `window` the protocol needs — injectable so tests can supply a fake. */
export interface WindowLike {
  location: { origin: string };
  parent: { postMessage(message: unknown, targetOrigin: string): void } | null;
  addEventListener(type: "message", listener: (event: MessageEventLike) => void): void;
  removeEventListener(type: "message", listener: (event: MessageEventLike) => void): void;
}

export interface MessageEventLike {
  origin: string;
  source: unknown;
  data: unknown;
}

/** The theme a message asks for, or null if it isn't a trusted, well-formed `booth:theme` message. */
export function themeFromMessage(event: MessageEventLike, win: Pick<WindowLike, "location" | "parent">): ShellTheme | null {
  if (event.origin !== win.location.origin) return null;
  if (!win.parent || event.source !== win.parent) return null;
  const data = event.data as { type?: unknown; theme?: unknown } | null;
  if (!data || typeof data !== "object" || data.type !== THEME) return null;
  return data.theme === "dark" || data.theme === "light" ? data.theme : null;
}

/**
 * Start listening, then announce readiness to the shell. Returns a function that stops listening.
 *
 * Does nothing (and posts nothing) when not embedded — JupyterLab opened directly in its own tab has
 * `window.parent === window`, and there is no shell to talk to.
 */
export function startThemeSync(win: WindowLike, applyTheme: (jupyterLabTheme: string) => void): () => void {
  if (!win.parent || (win.parent as unknown) === win) return () => undefined;
  const listener = (event: MessageEventLike): void => {
    const theme = themeFromMessage(event, win);
    if (theme) applyTheme(JUPYTERLAB_THEMES[theme]);
  };
  // Listen before announcing: the shell replies immediately, and a reply must never race the listener.
  win.addEventListener("message", listener);
  // Same-origin target, never "*": the message reaches the shell only if the parent really is our origin.
  win.parent.postMessage({ type: READY }, win.location.origin);
  return () => win.removeEventListener("message", listener);
}
