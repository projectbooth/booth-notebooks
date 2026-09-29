"""ADR 0084's public token accessor and ADR 0085's Iceberg branch in ``read_dataset``.

``booth_lakehouse`` is replaced by a recording fake here, so these pin *what booth asks of it*.
``test_the_real_booth_lakehouse_client_accepts_what_booth_calls`` checks the same calls against the real
package's signatures when it's installed.
"""

from __future__ import annotations

import inspect
import sys
import types

import pytest

from booth import BoothError, Client

from .test_client import Fake


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


@pytest.fixture
def client(fake) -> Client:
    return Client(env=fake.env())


# ---- ADR 0084: booth.platform_token ------------------------------------------------------------


def test_platform_token_is_the_notebooks_own_token_and_is_cached(client, fake):
    assert client.platform_token() == "wl-1"
    assert client.platform_token() == "wl-1"
    assert fake.token_requests == 1  # cached until close to expiry, like every other call


def test_platform_token_is_the_same_source_the_client_itself_uses(client, fake):
    client.catalog.datasets()
    assert client.platform_token() == fake.calls[0]["auth"].removeprefix("Bearer ")
    assert fake.token_requests == 1


def test_platform_token_outside_a_notebook_is_a_clear_error():
    with pytest.raises(BoothError, match="isn't running in a Project Booth notebook"):
        Client(env={}).platform_token()


def test_platform_token_is_public_api():
    import booth

    assert "platform_token" in booth.__all__ and callable(booth.platform_token)


# ---- ADR 0085: format: "iceberg" -> booth_lakehouse --------------------------------------------


class FakeLakehouse:
    calls: list[dict] = []
    error: Exception | None = None

    @classmethod
    def from_env(cls, env=None, **kwargs):
        cls.calls.append({"from_env": env, "kwargs": kwargs})
        return cls()

    def read(self, name, columns=None, where=None, snapshot_id=None):
        type(self).calls.append({"read": name, "columns": columns, "where": where, "snapshot_id": snapshot_id})
        if type(self).error:
            raise type(self).error
        import pyarrow as pa

        return pa.table({"day": [1, 2], "amount": [10, 20]})


class FakeLakehouseError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


@pytest.fixture
def lakehouse(monkeypatch):
    pytest.importorskip("pyarrow")
    FakeLakehouse.calls, FakeLakehouse.error = [], None
    mod = types.ModuleType("booth_lakehouse")
    mod.Lakehouse, mod.LakehouseError = FakeLakehouse, FakeLakehouseError
    monkeypatch.setitem(sys.modules, "booth_lakehouse", mod)
    return FakeLakehouse


def test_an_iceberg_dataset_is_read_through_booth_lakehouse_not_as_bytes(client, fake, lakehouse):
    pd = pytest.importorskip("pandas")
    df = client.read_dataset("daily-orders")
    assert isinstance(df, pd.DataFrame) and df["amount"].sum() == 30
    (opened, read) = lakehouse.calls
    assert opened["from_env"] is client._env  # the same environment this client was built from
    assert read == {"read": "sales.daily", "columns": None, "where": None, "snapshot_id": None}
    # no raw storage read was attempted for the table's (directory) location
    assert not [c for c in fake.calls if c["path"].startswith("/modules/storage/")]


def test_columns_filter_and_snapshot_are_passed_through(client, lakehouse):
    client.read_dataset("d6", columns=["amount"], where="amount > 10", snapshot_id=7)
    assert lakehouse.calls[-1] == {"read": "sales.daily", "columns": ["amount"], "where": "amount > 10", "snapshot_id": 7}


def test_the_catalogs_snapshot_id_is_not_pinned_by_default(client, lakehouse):
    """The catalog's currentSnapshotId (42 here) can lag the table; the latest state is read."""
    client.read_dataset("daily-orders")
    assert lakehouse.calls[-1]["snapshot_id"] is None


def test_without_pandas_an_iceberg_read_returns_the_arrow_table(client, lakehouse, monkeypatch):
    monkeypatch.setitem(sys.modules, "pandas", None)  # makes `import pandas` raise ImportError
    import pyarrow as pa

    assert isinstance(client.read_dataset("daily-orders"), pa.Table)


def test_as_bytes_and_file_reader_options_are_refused_for_a_table(client, lakehouse):
    with pytest.raises(BoothError, match="not one object"):
        client.read_dataset("daily-orders", as_bytes=True)
    with pytest.raises(BoothError, match="sep: not options for an Iceberg table"):
        client.read_dataset("daily-orders", sep=";")
    assert not [c for c in lakehouse.calls if "read" in c]


def test_an_iceberg_dataset_without_a_table_block_is_an_error_not_a_guess(client, lakehouse):
    with pytest.raises(BoothError, match="doesn't say which table"):
        client.read_dataset("broken-table")


def test_booth_lakehouse_errors_surface_as_booth_errors(client, lakehouse):
    lakehouse.error = FakeLakehouseError("no credential broker is configured", status=0)
    with pytest.raises(BoothError, match="couldn't read Iceberg table sales.daily: no credential broker"):
        client.read_dataset("daily-orders")


def test_without_booth_lakehouse_installed_the_error_says_what_is_missing(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "booth_lakehouse", None)
    with pytest.raises(BoothError, match="needs the booth_lakehouse client"):
        client.read_dataset("daily-orders")


def test_an_unknown_format_is_refused_rather_than_read_as_a_file(client):
    with pytest.raises(BoothError, match="format 'delta'"):
        client.read_dataset("future-format")


def test_the_file_path_is_unchanged(client):
    """An explicit format: "file", and a record with no format at all (pre-ADR 0085), both read as before;
    a directory file-dataset still refuses."""
    pytest.importorskip("pandas")
    assert client.read_dataset("explicit-file")["amount"].sum() == 30
    assert client.read_dataset("daily-sales")["amount"].sum() == 30
    with pytest.raises(BoothError, match="storage.list"):
        client.read_dataset("partitioned")


def test_the_real_booth_lakehouse_client_accepts_what_booth_calls():
    """Against the real package, when installed: the exact calls booth makes bind to its signatures."""
    lh = pytest.importorskip("booth_lakehouse")
    inspect.signature(lh.Lakehouse.from_env).bind(env={})
    inspect.signature(lh.Lakehouse.read).bind(object(), "sales.daily", columns=None, where=None, snapshot_id=None)
    assert issubclass(lh.LakehouseError, Exception)
