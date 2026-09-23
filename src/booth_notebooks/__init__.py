"""booth-notebooks: JupyterHub + KubeSpawner for Project Booth (ADR 0011).

The hub side lives here: ``identity`` (who is asking), ``authenticator`` (JupyterHub login from a
verified platform identity), ``spawner`` (the hardened per-user pod), ``workload`` + ``handlers``
(platform tokens for kernels, ADR 0056/0057), and ``hubconfig`` (the whole hub configuration). The
kernel side — the ``booth`` package preinstalled in the default notebook image — lives in ``client/``.
"""

__version__ = "0.1.0"
