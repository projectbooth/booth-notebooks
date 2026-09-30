// ADR 0075 handshake, embedded side: who we answer to and what we act on. Runs on Node's built-in test
// runner against a fake window (npm test compiles src/protocol.ts to test-build/ first).
const test = require("node:test");
const assert = require("node:assert/strict");
const { startThemeSync, themeFromMessage, READY, THEME, JUPYTERLAB_THEMES } = require("../test-build/protocol.js");

const ORIGIN = "http://localhost:8090";

function fakeWindow({ embedded = true } = {}) {
  const posted = [];
  const listeners = new Set();
  const parent = { postMessage: (message, targetOrigin) => posted.push({ message, targetOrigin }) };
  const win = {
    location: { origin: ORIGIN },
    parent: null,
    posted,
    listeners,
    addEventListener: (type, fn) => type === "message" && listeners.add(fn),
    removeEventListener: (type, fn) => type === "message" && listeners.delete(fn),
    dispatch: event => [...listeners].forEach(fn => fn(event)),
    shell: parent
  };
  win.parent = embedded ? parent : win; // a top-level window is its own parent
  return win;
}

function start(win) {
  const applied = [];
  const stop = startThemeSync(win, theme => applied.push(theme));
  return { applied, stop };
}

const fromShell = (win, data) => ({ origin: ORIGIN, source: win.shell, data });

test("announces readiness to the parent, same-origin only, and listens before announcing", () => {
  const win = fakeWindow();
  let listeningWhenPosted = null;
  const post = win.shell.postMessage;
  win.shell.postMessage = (m, o) => { listeningWhenPosted = win.listeners.size === 1; post(m, o); };
  start(win);
  assert.deepEqual(win.posted, [{ message: { type: READY }, targetOrigin: ORIGIN }]);
  assert.equal(listeningWhenPosted, true, "the shell replies immediately; the listener must already be in place");
});

test("a shell theme message applies JupyterLab's own theme names", () => {
  const win = fakeWindow();
  const { applied } = start(win);
  win.dispatch(fromShell(win, { type: THEME, theme: "dark" }));
  win.dispatch(fromShell(win, { type: THEME, theme: "light" }));
  assert.deepEqual(applied, ["JupyterLab Dark", "JupyterLab Light"]);
  assert.deepEqual(JUPYTERLAB_THEMES, { dark: "JupyterLab Dark", light: "JupyterLab Light" });
});

test("a message from another origin is ignored, even from the parent", () => {
  const win = fakeWindow();
  const { applied } = start(win);
  win.dispatch({ origin: "https://evil.example", source: win.shell, data: { type: THEME, theme: "dark" } });
  assert.deepEqual(applied, []);
});

test("a same-origin message that isn't from our parent is ignored (a popup, a nested frame, another tab)", () => {
  const win = fakeWindow();
  const { applied } = start(win);
  win.dispatch({ origin: ORIGIN, source: {}, data: { type: THEME, theme: "dark" } });
  win.dispatch({ origin: ORIGIN, source: win, data: { type: THEME, theme: "dark" } });
  win.dispatch({ origin: ORIGIN, source: null, data: { type: THEME, theme: "dark" } });
  assert.deepEqual(applied, []);
});

test("malformed or unrelated payloads are ignored", () => {
  const win = fakeWindow();
  const { applied } = start(win);
  for (const data of [null, "booth:theme", { type: THEME }, { type: THEME, theme: "Dark" }, { type: THEME, theme: "solarized" },
                      { type: "booth:iframe-ready" }, { type: "something-else", theme: "dark" }, [THEME, "dark"]]) {
    win.dispatch(fromShell(win, data));
  }
  assert.deepEqual(applied, []);
});

test("not embedded: nothing is posted and nothing is listened to", () => {
  const win = fakeWindow({ embedded: false });
  start(win);
  assert.deepEqual(win.posted, []);
  assert.equal(win.listeners.size, 0);
});

test("stopping removes the listener", () => {
  const win = fakeWindow();
  const { applied, stop } = start(win);
  stop();
  win.dispatch(fromShell(win, { type: THEME, theme: "dark" }));
  assert.deepEqual(applied, []);
});

test("themeFromMessage is the single trust decision", () => {
  const win = fakeWindow();
  assert.equal(themeFromMessage(fromShell(win, { type: THEME, theme: "dark" }), win), "dark");
  assert.equal(themeFromMessage({ ...fromShell(win, { type: THEME, theme: "dark" }), origin: "null" }, win), null);
});
