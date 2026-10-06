"""booth.s3: the location helper for the s3 credential sidecar's two files (ADR 0095 fourth amendment).

The engines are stand-ins here (the dev environment needn't have DuckDB); what each real engine does with
these arguments was measured against the real sidecar and a real MinIO (docs/decisions/0009).
"""

from __future__ import annotations

import sys
import types

import pytest

import booth
import booth.s3 as s3

CREDS = "[default]\naws_access_key_id = AKIDEXAMPLE\naws_secret_access_key = s3cr3t'\n"
# Exactly what booth-core's ff7572b sidecar writes for a self-hosted backend.
CONFIG = "[default]\nendpoint_url = http://minio.storage.svc:9000\nregion = us-east-1\n"


@pytest.fixture
def files(tmp_path, monkeypatch):
    def write(config: str | None = CONFIG, creds: str | None = CREDS):
        cred, conf = tmp_path / "credentials", tmp_path / "credentials.config"
        for path, body in ((cred, creds), (conf, config)):
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(body, encoding="utf-8")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(conf))
        monkeypatch.delenv("AWS_PROFILE", raising=False)

    write()
    return write


# ---- location() ----------------------------------------------------------------------------------


def test_reads_what_the_sidecar_writes_today(files):
    loc = s3.location()
    assert loc == s3.Location("http://minio.storage.svc:9000", "us-east-1", None)
    assert loc.host == "minio.storage.svc:9000" and loc.scheme == "http"


@pytest.mark.parametrize(
    "extra",
    ["s3 =\n    addressing_style = path\n", "addressing_style = path\n"],
    ids=["botocore-nested", "flat"],
)
def test_reads_the_addressing_style_in_either_form(files, extra):
    files(CONFIG + extra)
    assert s3.location().addressing_style == "path"


def test_a_named_profile_uses_awss_profile_section_name(files, monkeypatch):
    files("[default]\nendpoint_url = http://wrong:1\n[profile booth]\nendpoint_url = https://s3.example:443\n")
    monkeypatch.setenv("AWS_PROFILE", "booth")
    assert s3.location().endpoint_url == "https://s3.example:443"


def test_real_aws_has_no_config_and_means_the_engines_defaults(files):
    files(config=None)
    assert s3.location() == s3.Location(None, None, None)


def test_without_the_sidecar_says_why(monkeypatch):
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    with pytest.raises(booth.BoothError, match="no lakehouse warehouse yet"):
        s3.location()


def test_before_the_first_lease_says_so(files):
    files(creds=None)
    with pytest.raises(booth.BoothError, match="first lease"):
        s3.location()


def test_the_file_is_reread_on_every_call(files):
    assert s3.location().addressing_style is None
    files(CONFIG + "addressing_style = virtual\n")
    assert s3.location().addressing_style == "virtual"


# ---- pyarrow_filesystem() --------------------------------------------------------------------------


@pytest.fixture
def fake_pyarrow(monkeypatch):
    made = []

    class S3FileSystem:
        def __init__(self, **kw):
            made.append(kw)

    fs = types.ModuleType("pyarrow.fs")
    fs.S3FileSystem = S3FileSystem
    pa = types.ModuleType("pyarrow")
    pa.fs = fs
    monkeypatch.setitem(sys.modules, "pyarrow", pa)
    monkeypatch.setitem(sys.modules, "pyarrow.fs", fs)
    return made


def test_pyarrow_gets_the_location_and_never_the_keys(files, fake_pyarrow):
    s3.pyarrow_filesystem()
    (kw,) = fake_pyarrow
    assert kw == {"region": "us-east-1", "endpoint_override": "minio.storage.svc:9000", "scheme": "http"}


def test_pyarrow_virtual_addressing_only_when_stated_and_caller_kwargs_win(files, fake_pyarrow):
    files(CONFIG + "addressing_style = virtual\n")
    s3.pyarrow_filesystem(region="eu-west-1")
    assert fake_pyarrow[0]["force_virtual_addressing"] is True and fake_pyarrow[0]["region"] == "eu-west-1"


def test_pyarrow_on_real_aws_gets_nothing_but_its_defaults(files, fake_pyarrow):
    files(config=None)
    s3.pyarrow_filesystem()
    assert fake_pyarrow == [{}]


# ---- duckdb_secret() --------------------------------------------------------------------------------


class Con:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, q):
        self.sql.append(q)


def test_duckdb_secret_sets_the_location_and_refreshes_keys_from_the_chain(files):
    files(CONFIG + "s3 =\n    addressing_style = path\n")
    con = Con()
    s3.duckdb_secret(con)
    load, create = con.sql
    assert "LOAD httpfs" in load and "LOAD aws" in load
    assert create == ("CREATE OR REPLACE SECRET booth_s3 (TYPE s3, PROVIDER credential_chain, REFRESH auto, "
                      "REGION 'us-east-1', ENDPOINT 'minio.storage.svc:9000', USE_SSL false, URL_STYLE 'path')")
    assert "AKIDEXAMPLE" not in "".join(con.sql) and "s3cr3t" not in "".join(con.sql)  # keys never in SQL


def test_duckdb_without_a_stated_style_leaves_duckdbs_default(files):
    con = Con()
    s3.duckdb_secret(con)
    assert "URL_STYLE" not in con.sql[1]


def test_duckdb_https_and_vhost(files):
    files("[default]\nendpoint_url = https://s3.example\naddressing_style = virtual\n")
    con = Con()
    s3.duckdb_secret(con)
    assert "ENDPOINT 's3.example', USE_SSL true, URL_STYLE 'vhost'" in con.sql[1] and "REGION" not in con.sql[1]


def test_duckdb_location_values_are_quoted(files):
    files("[default]\nendpoint_url = http://h:1\nregion = x'); DROP TABLE t; --\n")
    con = Con()
    s3.duckdb_secret(con)
    assert "REGION 'x''); DROP TABLE t; --'" in con.sql[1]


def test_duckdb_secret_name_is_an_identifier(files):
    with pytest.raises(ValueError):
        s3.duckdb_secret(Con(), name="x; DROP")
