/**
 * @projectbooth/jupyterlab-theme-sync — JupyterLab follows the Project Booth shell's dark/light toggle
 * live (ADR 0075). The protocol and its trust checks live in ./protocol; this file only wires them to
 * JupyterLab.
 *
 * Applies a theme through JupyterLab's own `apputils:change-theme` command, never a raw settings REST
 * PUT, so ThemeManager's internal state (the Settings > Theme menu checkmarks, etc.) stays consistent.
 * The command also persists the choice in the user's settings, so a notebook opened outside the shell
 * keeps the last theme it was given.
 *
 * Known, accepted limitation (ADR 0075): JupyterLab first paints with its default (light) theme and only
 * switches once the ready-handshake completes and the shell's first reply arrives.
 */
import { JupyterFrontEnd, JupyterFrontEndPlugin } from "@jupyterlab/application";

import { startThemeSync } from "./protocol";

const CHANGE_THEME = "apputils:change-theme";

const plugin: JupyterFrontEndPlugin<void> = {
  id: "@projectbooth/jupyterlab-theme-sync:plugin",
  description: "Follows the Project Booth shell's dark/light theme (ADR 0075).",
  autoStart: true,
  activate: (app: JupyterFrontEnd): void => {
    // Announce readiness only once the app is restored: by then the theme manager's
    // apputils:change-theme command is registered, so the shell's immediate reply can be acted on.
    void app.restored.then(() => {
      startThemeSync(window, theme => {
        if (!app.commands.hasCommand(CHANGE_THEME)) {
          console.warn(`booth theme sync: ${CHANGE_THEME} is not available; ignoring theme ${theme}`);
          return;
        }
        void app.commands.execute(CHANGE_THEME, { theme }).catch(reason => {
          console.warn(`booth theme sync: could not apply theme ${theme}`, reason);
        });
      });
    });
  }
};

export default plugin;
