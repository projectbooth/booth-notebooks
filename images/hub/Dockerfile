# The JupyterHub hub: this repo's booth_notebooks package on a plain Python base. It never runs user
# code (ADR 0057) — kernels run in per-user pods built from ../singleuser — so it holds the module's
# credentials: the hub database DSN (ADR 0053) and the workload-minting credential (ADR 0056).
# Build context: the repository root.
FROM python:3.13-slim

RUN useradd --uid 1000 --no-create-home --home-dir /tmp hub

WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]" && rm -rf /src

COPY images/hub/jupyterhub_config.py /etc/jupyterhub/jupyterhub_config.py

USER 1000
# The chart mounts a writable emptyDir here (read-only root filesystem); JupyterHub writes nothing else.
WORKDIR /tmp
EXPOSE 8081
CMD ["jupyterhub", "-f", "/etc/jupyterhub/jupyterhub_config.py"]
