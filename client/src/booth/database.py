"""This workspace's booth-database from a notebook, through booth-core's credential sidecar (ADR 0095).

With booth-database enabled, the notebook gets ``DATABASE_URL=postgresql://localhost:5432/<db>``: a
loopback proxy that holds a short-lived lease, with no host or credential in the URL. Any Postgres
client works with it directly. Prefer this engine, though, for anything that keeps connections open:

    import booth.database, pandas as pd
    engine = booth.database.engine()
    pd.read_sql("SELECT now()", engine)

**Connection lifetime (ADR 0095 fifth amendment).** A connection through the sidecar ends no later than
the lease it was opened on expires: booth-database terminates a lease's sessions at expiry. Each
connection is guaranteed at least the sidecar's renewal margin. The engine therefore checks a pooled
connection before reusing it (``pool_pre_ping``) and retires connections well before the guarantee
(``pool_recycle``), so a kernel left open all afternoon reconnects quietly on a fresh lease instead of
failing its next query. A query or transaction that is *running* when its lease expires is still lost:
re-run it.
"""

from __future__ import annotations

import os

from . import BoothError

__all__ = ["RECYCLE_SECONDS", "url", "engine"]

# Comfortably under the guaranteed lifetime (the renewal margin; ~30 min with booth-core's postgres default).
RECYCLE_SECONDS = 15 * 60

NOT_ENABLED = (
    "this notebook has no database: DATABASE_URL isn't set. This deployment doesn't enable booth-database "
    "for notebooks (booth-notebooks' boothDatabase.url)."
)


def url(env=None) -> str:
    """``DATABASE_URL`` with SQLAlchemy's psycopg (v3) driver named, the driver the default image ships."""
    env = os.environ if env is None else env
    raw = env.get("DATABASE_URL", "")
    if not raw:
        raise BoothError(NOT_ENABLED)
    for prefix in ("postgresql://", "postgres://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix):]
    return raw  # already names a driver


def engine(**kwargs):
    """A SQLAlchemy engine for this workspace's database that survives lease expiry between queries.
    Keyword arguments pass through to ``sqlalchemy.create_engine`` and win."""
    from sqlalchemy import create_engine

    opts = {"pool_pre_ping": True, "pool_recycle": RECYCLE_SECONDS}
    opts.update(kwargs)
    return create_engine(url(), **opts)
