"""booth.database: a SQLAlchemy engine that survives the end of a sidecar lease (ADR 0095 fifth amendment).

Recovery itself (a pooled connection terminated server-side, the next query on a fresh one) was measured
against a real Postgres from the singleuser image (docs/decisions/0008).
"""

from __future__ import annotations

import sys
import types

import pytest

import booth
import booth.database as db

URL = "postgresql://localhost:5432/bdb_ws_a716adca12a9ec8861a00c31"


def test_url_names_the_psycopg3_driver_the_image_ships():
    assert db.url({"DATABASE_URL": URL}) == "postgresql+psycopg://localhost:5432/bdb_ws_a716adca12a9ec8861a00c31"
    assert db.url({"DATABASE_URL": "postgres://localhost:5432/x"}) == "postgresql+psycopg://localhost:5432/x"
    assert db.url({"DATABASE_URL": "postgresql+psycopg2://localhost:5432/x"}) == "postgresql+psycopg2://localhost:5432/x"


def test_without_database_url_says_why():
    with pytest.raises(booth.BoothError, match="boothDatabase.url"):
        db.url({})


@pytest.fixture
def created(monkeypatch):
    calls = []
    sa = types.ModuleType("sqlalchemy")
    sa.create_engine = lambda url, **kw: calls.append((url, kw)) or "engine"
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa)
    monkeypatch.setenv("DATABASE_URL", URL)
    return calls


def test_engine_checks_connections_and_recycles_well_under_the_lease_guarantee(created):
    assert db.engine() == "engine"
    ((url, kw),) = created
    assert url.startswith("postgresql+psycopg://localhost:5432/")
    assert kw == {"pool_pre_ping": True, "pool_recycle": 900}
    assert db.RECYCLE_SECONDS < 30 * 60  # the guarantee: the sidecar renews at half a lease, ~30 min of a default 1h lease


def test_caller_options_win(created):
    db.engine(pool_recycle=300, echo=True)
    assert created[0][1] == {"pool_pre_ping": True, "pool_recycle": 300, "echo": True}
