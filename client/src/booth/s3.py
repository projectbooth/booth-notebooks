"""Object storage from a notebook, through booth-core's credential sidecar (ADR 0095, third and fourth amendments).

When this workspace has a lakehouse warehouse and the deployment enables it, the notebook pod runs an
``s3``-mode sidecar that keeps two standard AWS files fresh and points the usual variables at them:
``AWS_SHARED_CREDENTIALS_FILE`` (short-lived keys, renewed before they expire) and ``AWS_CONFIG_FILE``
(the backend's ``endpoint_url``, ``region`` and addressing style). boto3-based tools read both files
unassisted. pyarrow and DuckDB, the default kernel's engines, read the keys but **not** the endpoint
(measured, docs/decisions/0009), so against a self-hosted backend they would go to real AWS. These helpers
hand them the location:

    import booth.s3, pyarrow.parquet as pq, duckdb
    fs = booth.s3.pyarrow_filesystem()
    pq.write_table(table, "bucket/warehouses/acme/x.parquet", filesystem=fs)

    con = duckdb.connect()
    booth.s3.duckdb_secret(con)
    con.sql("SELECT * FROM 's3://bucket/warehouses/acme/x.parquet'")

Only the *location* is passed explicitly. The keys are left to each engine's own AWS credential chain,
which reads the sidecar's file, so a renewal reaches them without any code here. Measured
(docs/decisions/0009): an already-created filesystem and secret both kept working across a key rotation,
from their very next call (DuckDB's secret is created with ``REFRESH auto``). No key ever passes through
this module or into SQL text.
"""

from __future__ import annotations

import configparser
import os
import urllib.parse
from dataclasses import dataclass

from . import BoothError

__all__ = ["Location", "location", "pyarrow_filesystem", "duckdb_secret"]

NOT_ENABLED = (
    "this notebook has no object-storage credentials: AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE aren't set. "
    "Either this workspace has no lakehouse warehouse yet, or this deployment doesn't enable it "
    "(booth-notebooks' boothStorage.url). A warehouse created while this server runs is picked up when "
    "the server is next started."
)


@dataclass(frozen=True)
class Location:
    """Where this notebook's object storage is. ``endpoint_url`` is None for real AWS S3."""

    endpoint_url: str | None
    region: str | None
    addressing_style: str | None  # "path" | "virtual" | "auto" | None (not stated: the engine's default)

    @property
    def scheme(self) -> str | None:
        return urllib.parse.urlsplit(self.endpoint_url).scheme if self.endpoint_url else None

    @property
    def host(self) -> str | None:
        """``host[:port]``, the form pyarrow and DuckDB take."""
        return urllib.parse.urlsplit(self.endpoint_url).netloc if self.endpoint_url else None


def _profile_section(name: str) -> str:
    # AWS's config file names a non-default profile "[profile <name>]"; the credentials file doesn't.
    return name if name == "default" else f"profile {name}"


def _nested(value: str) -> dict[str, str]:
    """botocore's nested form: ``s3 =\\n    addressing_style = path``."""
    out = {}
    for line in value.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def location(env=None) -> Location:
    """Read the sidecar's config file (re-read on every call; it's tiny). Raises ``BoothError`` when this
    notebook has no s3 sidecar, or when it hasn't written its first lease yet. Once the credentials file
    exists, a config file with no section for the profile means real AWS S3 (the sidecar writes none,
    having no endpoint to set): ``Location(None, None, None)``, the engines' own defaults."""
    env = os.environ if env is None else env
    conf_path, cred_path = env.get("AWS_CONFIG_FILE", ""), env.get("AWS_SHARED_CREDENTIALS_FILE", "")
    if not conf_path or not cred_path:
        raise BoothError(NOT_ENABLED)
    if not os.path.exists(cred_path):
        raise BoothError(
            "object-storage credentials aren't ready yet: the credential sidecar hasn't written its first "
            "lease. Try again in a few seconds; if it persists, the platform refused this notebook a lease."
        )
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(conf_path, encoding="utf-8")
    except configparser.Error as e:
        raise BoothError(f"the credential sidecar's config file is unreadable: {e.__class__.__name__}") from None
    section = _profile_section(env.get("AWS_PROFILE", "") or "default")
    if not parser.has_section(section):
        return Location(None, None, None)  # real AWS S3: the sidecar writes no config at all
    sec = parser[section]
    style = sec.get("addressing_style") or _nested(sec.get("s3", "")).get("addressing_style")
    return Location(sec.get("endpoint_url") or None, sec.get("region") or None, style or None)


def pyarrow_filesystem(**kwargs):
    """A ``pyarrow.fs.S3FileSystem`` pointed at this notebook's backend. Keys come from the sidecar's file via
    the AWS SDK's own profile chain. Extra keyword arguments pass through (and win)."""
    from pyarrow import fs

    loc = location()
    opts = {}
    if loc.region:
        opts["region"] = loc.region
    if loc.endpoint_url:
        opts["endpoint_override"] = loc.host
        opts["scheme"] = loc.scheme
    if loc.addressing_style == "virtual":
        opts["force_virtual_addressing"] = True  # pyarrow's default with an endpoint override is path style
    opts.update(kwargs)
    return fs.S3FileSystem(**opts)


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def duckdb_secret(con, name: str = "booth_s3") -> None:
    """Create (or replace) a DuckDB S3 secret on ``con`` for this notebook's backend. Loads ``httpfs`` and
    ``aws`` (installing them on first use). The secret uses DuckDB's AWS credential chain with
    ``REFRESH auto``, so renewed keys are picked up; only the location is set here."""
    if not name.replace("_", "").isalnum():
        raise ValueError("secret name must be letters, digits and underscores")
    loc = location()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;")
    parts = ["TYPE s3", "PROVIDER credential_chain", "REFRESH auto"]
    if loc.region:
        parts.append(f"REGION {_sql_str(loc.region)}")
    if loc.endpoint_url:
        parts.append(f"ENDPOINT {_sql_str(loc.host)}")
        parts.append(f"USE_SSL {'true' if loc.scheme == 'https' else 'false'}")
    if loc.addressing_style in ("path", "virtual"):
        parts.append(f"URL_STYLE {_sql_str('path' if loc.addressing_style == 'path' else 'vhost')}")
    con.execute(f"CREATE OR REPLACE SECRET {name} ({', '.join(parts)})")
