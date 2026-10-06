# The documented default notebook environment (docs/decisions/0001): Python 3.12 + JupyterLab from
# Jupyter's own docker-stacks, plus a small data stack and the `booth` platform client. No Spark, and
# nothing that needs any other module installed (ADR 0006/0011).
# Build context: the repository root.
#
# Pinned by digest (tag python-3.12, verified 2026-09-22: jupyterhub 5.3.0, jupyterlab 4.4.5,
# jupyter_server 2.16.0, Ubuntu 24.04, uid 1000 jovyan / gid 100). Bumping it is a deliberate change.
ARG BASE=quay.io/jupyter/base-notebook:python-3.12@sha256:8370819f642e83d0ca9c7f26c6e60580a920a69af80607888723b1784f6b1232

# ---- Build stage: the ADR 0075 theme-sync JupyterLab extension ----------------------------------------
# Built on the SAME base image as the runtime, so `jupyter labextension build` links the extension against
# exactly the JupyterLab it will run in (federated extensions share JupyterLab's own packages at runtime; a
# version mismatch breaks them). Node exists only in this stage, never in the notebook image.
FROM ${BASE} AS theme-sync
RUN mamba install --yes --quiet "nodejs=20" && mamba clean --all --force-pkgs-dirs --yes
COPY --chown=1000:100 images/singleuser/theme-sync /tmp/theme-sync
WORKDIR /tmp/theme-sync
RUN npm ci --no-audit --no-fund && npm test && npm run build && jupyter labextension build .

# ---- The notebook image --------------------------------------------------------------------------------
FROM ${BASE}

RUN pip install --no-cache-dir \
      "pandas>=2.2,<3" \
      "pyarrow>=17" \
      "matplotlib>=3.9,<4" \
      "duckdb>=1.1,<2" \
      "psycopg[binary]>=3.2,<4" \
      "sqlalchemy>=2,<3"

COPY --chown=1000:100 client /tmp/booth-client
RUN pip install --no-cache-dir /tmp/booth-client && rm -rf /tmp/booth-client

# Framing: the shell embeds JupyterLab in an iframe (ADR 0005); jupyter_server's default CSP only allows
# same-origin framing. The hub passes the allowed shell origin(s) in BOOTH_FRAME_ANCESTORS. Appended to
# docker-stacks' own config rather than replacing it.
USER root
COPY images/singleuser/booth_server_config.py /tmp/booth_server_config.py
RUN cat /tmp/booth_server_config.py >> /etc/jupyter/jupyter_server_config.py && rm /tmp/booth_server_config.py
USER 1000

# JupyterLab setting defaults for every user (JupyterLab's own overrides.json mechanism). Never fetch the
# Jupyter news feed or check for updates, and so never show the first-launch "get notified about official
# Jupyter news?" prompt: the pod can't usefully reach it, and its version is the image's choice, not the user's.
COPY --chown=1000:100 images/singleuser/overrides.json /opt/conda/share/jupyter/lab/settings/overrides.json

# ADR 0075: JupyterLab follows the shell's dark/light toggle live, over a same-origin postMessage handshake.
# A prebuilt federated extension: JupyterLab discovers it from this directory, no rebuild of JupyterLab needed.
COPY --from=theme-sync --chown=1000:100 /tmp/theme-sync/labextension /opt/conda/share/jupyter/labextensions/@projectbooth/jupyterlab-theme-sync
