
# --- booth-notebooks (images/singleuser/booth_server_config.py) ---------------------------------
import os as _booth_os

_booth_ancestors = _booth_os.environ.get("BOOTH_FRAME_ANCESTORS", "").strip()
if _booth_ancestors:
    # Assign a real dict. Unset, this trait reads back as a traitlets LazyConfigValue, and leaving one
    # in place (e.g. via .update()) crashes jupyterhub-singleuser, which does
    # `'log_function' in app.config.ServerApp.get('tornado_settings', {})` (found on kind).
    _booth_ts = c.ServerApp.tornado_settings  # noqa: F821
    _booth_ts = dict(_booth_ts) if isinstance(_booth_ts, dict) else {}
    _booth_ts["headers"] = {**(_booth_ts.get("headers") or {}), "Content-Security-Policy": f"frame-ancestors {_booth_ancestors}"}
    c.ServerApp.tornado_settings = _booth_ts  # noqa: F821
