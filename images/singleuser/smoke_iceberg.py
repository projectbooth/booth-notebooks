"""CI smoke test, run INSIDE the built default-kernel image (``docker run -i <image> python - < this``):
the Iceberg stack a notebook reads lakehouse tables with actually works together, not just imports.

PyIceberg writes and reads a real Iceberg table (a local SQLite catalog and file warehouse stand in for
booth-lakehouse's REST catalog and object storage, which a single container doesn't have), DuckDB counts
its rows per season over the Arrow result, the README's recipe, and booth_lakehouse's ``Lakehouse.from_env``
builds from the environment a notebook pod gets. The real catalog/broker/storage path is not exercised here.
"""

import importlib.metadata as md
import tempfile

import duckdb
import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog

import booth
from booth_lakehouse import Lakehouse

print({d: md.version(d) for d in ("booth-lakehouse-client", "pyiceberg", "duckdb", "pyarrow", "pandas")})

d = tempfile.mkdtemp()
catalog = SqlCatalog("smoke", uri=f"sqlite:///{d}/catalog.db", warehouse=f"file://{d}")
catalog.create_namespace("nfl")
schema = pa.schema([("season", pa.int32()), ("play_id", pa.int64())])
table = catalog.create_table("nfl.team_game_pbp", schema=schema)
table.append(pa.table({"season": [2023, 2023, 2024], "play_id": [1, 2, 3]}, schema=schema))

pbp = catalog.load_table("nfl.team_game_pbp").scan(selected_fields=("season",)).to_arrow()
rows = duckdb.sql("SELECT season, count(*) AS plays FROM pbp GROUP BY season ORDER BY season").fetchall()
assert rows == [(2023, 2), (2024, 1)], rows
assert catalog.load_table("nfl.team_game_pbp").scan(row_filter="season = 2024").to_arrow().num_rows == 1

lh = Lakehouse.from_env(env={
    "BOOTH_GATEWAY_URL": "http://booth-core.booth-system.svc:8080/modules",
    "BOOTH_WORKSPACE": "acme",
    "BOOTH_TOKEN": "not-a-real-token",
    "BOOTH_CREDENTIAL_BROKER_URL": "http://booth-core.booth-system.svc:8080/api/credentials",
})
assert lh.workspace == "acme" and lh._broker is not None
assert callable(booth.read_dataset) and callable(booth.platform_token)
print("iceberg smoke: ok", rows)
