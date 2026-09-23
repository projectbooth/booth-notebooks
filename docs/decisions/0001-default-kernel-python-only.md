# 0001: v0 ships one default kernel — Python — with an operator seam for more

Status: **decided within the module (no ADR proposed)**, per the brief's 2026-09-22 ruling that this is
booth-notebooks' call, informed by an investigation rather than guessed. Python-only was the stated
acceptable default; nothing found below argues for more in v0.

## What multi-language actually costs under JupyterHub + KubeSpawner

A kernel is not a hub-side choice. The hub spawns one **image** per user server, and a kernel is a
kernelspec *installed inside that image*. So "support R" means one of two things, each with a real cost:

1. **A fatter default image** (Python + IRkernel + R packages in one image, like docker-stacks'
   `datascience-notebook`). Measured compressed sizes, `docker manifest inspect`, 2026-09-22:

   | docker-stacks image | compressed | adds |
   |---|---|---|
   | `base-notebook` | 0.32 GB | Python, JupyterLab (**our base**) |
   | `minimal-notebook` | 0.57 GB | + CLI tools, TeX-lite |
   | `scipy-notebook` | 1.26 GB | + scientific Python |
   | `r-notebook` | 1.36 GB | + R, IRkernel, tidyverse |
   | `datascience-notebook` | 2.41 GB | Python + R + Julia |
   | `all-spark-notebook` | 2.37 GB | pyspark + R/sparklyr (**no Scala kernel**) |

   Every node pulls the image before a user's first spawn there, so size is spawn latency on a fresh
   node, plus a larger CVE-patching surface (CRAN, a JVM) on the image every user runs. Our default is
   base-notebook + pandas/pyarrow/duckdb/matplotlib: 2.06 GB unpacked vs. base's 1.43 GB.

2. **A profile chooser** (KubeSpawner `profile_list`, one image per language). Image sizes stay separate,
   but a user now picks an environment before every spawn, and each image is its own maintained,
   version-pinned artifact that must stay compatible with the hub's JupyterHub major version.

The decisive cost, though, isn't images — it's **platform access**. The DoD requires a notebook to reach
registered storage/catalog entries. In Python that's the `booth` package (`client/`): token acquisition
from the hub, refresh, gateway calls, dataset → DataFrame. An R kernel without an R equivalent would be a
kernel that can't do the one platform-specific thing notebooks exist for here. Every language added
multiplies that client (and its tests) by one.

**Scala specifically** is worse than it looks: docker-stacks dropped its Scala kernel (spylon) — the
current `all-spark-notebook` Dockerfile installs only IRkernel/sparklyr on top of pyspark — so Scala
means us maintaining almond + a JVM image ourselves. It's also most useful with Spark, which ADR 0006
keeps out of the default path. (`booth-pipeline` deferred Scala for its SQL runner too — ADR 0064.)

## Decision

- **v0: one documented default kernel, Python 3.12** (`images/singleuser`), no chooser page.
- **The seam is already there and tested:** `singleuser.profiles` in the chart is passed to KubeSpawner's
  `profile_list` (`tests/unit/test_spawner.py`, `tests/contract/test_chart.py`). An operator can add
  an R (or any) image with no code change. Requirements for such an image are documented in the README:
  `jupyterhub-singleuser` 5.x, uid 1000/gid 100 — and no `booth` client unless they supply one.
- **Revisit** when a real user need for R shows up, which would come with an R `booth` client as its
  real cost. Scala stays tied to a Spark-integration story (ADR 0006: opt-in, only with `booth-spark`).
