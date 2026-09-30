"""Project Booth from a notebook: registered datasets and storage, as this notebook's own identity.

    import booth
    booth.catalog.datasets(q="sales")          # what's registered in this workspace
    df = booth.read_dataset("daily-sales")     # a registered dataset, by name or id -> DataFrame
    booth.storage.read("lake", "raw/x.csv")    # raw bytes from a storage backend ({backendId, path})
    booth.platform_token()                     # this notebook's current platform token, for other clients

How identity works (ADR 0056/0057): the kernel never sees your browser's login. It asks the hub for
a short-lived platform token (10 min, refreshed automatically), minted by booth-core for *this*
notebook server, in *this* workspace, capped at your current role — so a viewer's notebook can read
but not write, and nothing here can reach another workspace. Every call goes through booth-core's
gateway (ADR 0007/0059), exactly like the shell's own calls.

An Iceberg table registered in the catalog (``format: "iceberg"``, ADR 0085) is read through the
``booth_lakehouse`` client, which must then be installed; everything else stays standard library only.
pandas is used when installed and never required. Nothing here constructs a
direct connection to a storage backend: a location is only ever resolved by booth-storage (ADR 0045).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

__all__ = ["BoothError", "catalog", "storage", "read_dataset", "platform_token", "workspace", "Client"]
__version__ = "0.1.0"


class BoothError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _seg(value) -> str:
    return urllib.parse.quote(str(value), safe="")


def _objpath(path: str) -> str:
    return urllib.parse.quote(path.lstrip("/"), safe="/")


class _Http:
    """One notebook server's session with the platform: token acquisition, caching and a single
    retry on 401 (the token may have expired between the cache check and the call)."""

    def __init__(self, env=None, opener=None) -> None:
        env = os.environ if env is None else env
        self.workspace = env.get("BOOTH_WORKSPACE", "")
        self._gateway = env.get("BOOTH_GATEWAY_URL", "").rstrip("/")
        api = env.get("JUPYTERHUB_API_URL", "").rstrip("/")
        self._token_url = api + env.get("BOOTH_PLATFORM_TOKEN_PATH", "/booth/platform-token") if api else ""
        self._hub_token = env.get("JUPYTERHUB_API_TOKEN", "")
        self._open = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def _need_platform(self) -> None:
        if not (self._token_url and self._hub_token and self.workspace):
            raise BoothError("this kernel isn't running in a Project Booth notebook server, so it has no platform identity")
        if not self._gateway:
            raise BoothError("this deployment has no booth-core gateway configured, so notebooks can't reach storage or the catalog")

    def _fetch_token(self) -> str:
        req = urllib.request.Request(self._token_url, data=b"{}", method="POST")
        req.add_header("Authorization", "token " + self._hub_token)
        req.add_header("Content-Type", "application/json")
        try:
            with self._open(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise BoothError(_hub_error(e), e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise BoothError(f"couldn't reach the notebook hub for a platform token: {e}") from None
        try:
            exp = datetime.fromisoformat(str(data["expiresAt"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            exp = time.time() + 300
        self._token, self._expires = data["token"], exp
        return self._token

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() > self._expires - 60:
                return self._fetch_token()
            return self._token

    def request(self, method: str, module: str, path: str, body: bytes | None = None, content_type: str | None = None, query: dict | None = None):
        self._need_platform()
        url = f"{self._gateway}/{module}{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        for attempt in (0, 1):
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Authorization", "Bearer " + self.token(force=attempt == 1))
            # Through the gateway only X-Workspace is sent; it derives X-Booth-Workspace/-Role itself.
            req.add_header("X-Workspace", self.workspace)
            if content_type:
                req.add_header("Content-Type", content_type)
            try:
                with self._open(req, timeout=120) as resp:
                    return resp.status, resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                raise BoothError(_module_error(module, method, path, e), e.code) from None
            except (urllib.error.URLError, OSError) as e:
                raise BoothError(f"couldn't reach {module} through the platform gateway: {e}") from None
        raise AssertionError("unreachable")

    def json(self, method: str, module: str, path: str, obj=None, query: dict | None = None):
        body = json.dumps(obj).encode() if obj is not None else None
        _, out, _ = self.request(method, module, path, body, "application/json" if body is not None else None, query)
        return json.loads(out) if out else None


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    try:
        doc = json.loads(raw)
        return str(doc.get("message") or doc.get("error") or doc.get("detail") or raw)
    except ValueError:
        return raw.strip()[:300]


def _hub_error(e: urllib.error.HTTPError) -> str:
    detail = _detail(e)
    if e.code == 503:
        return f"this notebook has no platform access right now: {detail}"
    if e.code == 403:
        return f"platform access was refused: {detail}"
    return f"the notebook hub couldn't issue a platform token (HTTP {e.code}): {detail}"


def _module_error(module: str, method: str, path: str, e: urllib.error.HTTPError) -> str:
    detail = _detail(e)
    if e.code == 403:
        return f"{module} refused {method} {path}: {detail} (your notebook acts with your current role in this workspace)"
    if e.code == 404:
        return f"{module}: not found: {path}"
    if e.code in (502, 503) and not detail:
        return f"{module} isn't available (is it installed in this deployment?)"
    return f"{module} returned HTTP {e.code} for {method} {path}: {detail}"


class Storage:
    """booth-storage. A location is always a ``{backendId, path}`` pair (ADR 0045)."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    def backends(self) -> list:
        out = self._h.json("GET", "storage", "/api/backends")
        return out.get("items", out) if isinstance(out, dict) else out

    def list(self, backend: str, prefix: str = "", recursive: bool = False):
        q = {"prefix": prefix, "recursive": "true" if recursive else None}
        return self._h.json("GET", "storage", f"/api/backends/{_seg(backend)}/objects", query=q)

    def read(self, backend: str, path: str) -> bytes:
        _, data, _ = self._h.request("GET", "storage", f"/api/backends/{_seg(backend)}/objects/{_objpath(path)}")
        return data

    def read_text(self, backend: str, path: str, encoding: str = "utf-8") -> str:
        return self.read(backend, path).decode(encoding)

    def write(self, backend: str, path: str, data: bytes | str, content_type: str | None = None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        _, out, _ = self._h.request("PUT", "storage", f"/api/backends/{_seg(backend)}/objects/{_objpath(path)}", data, content_type or "application/octet-stream")
        return json.loads(out) if out else None

    def delete(self, backend: str, path: str) -> None:
        self._h.request("DELETE", "storage", f"/api/backends/{_seg(backend)}/objects/{_objpath(path)}")


class Catalog:
    """booth-catalog: find registered datasets, and register what a notebook produced."""

    def __init__(self, http: _Http) -> None:
        self._h = http

    def datasets(self, q: str = "", limit: int = 50) -> list[dict]:
        out = self._h.json("GET", "catalog", "/api/datasets", query={"q": q or None, "limit": limit})
        return out.get("items", []) if isinstance(out, dict) else out

    def dataset(self, dataset_id: str) -> dict:
        return self._h.json("GET", "catalog", f"/api/datasets/{_seg(dataset_id)}")

    def find(self, ref: str) -> dict:
        """A dataset by id, or by exact name. An ambiguous name is an error, never a guess."""
        try:
            return self.dataset(ref)
        except BoothError as e:
            if e.status not in (400, 404):
                raise
        matches = [d for d in self.datasets(q=ref, limit=200) if d.get("name") == ref]
        if not matches:
            raise BoothError(f"no dataset named or with id {ref!r} in workspace {self._h.workspace!r}", 404)
        if len(matches) > 1:
            ids = ", ".join(d["id"] for d in matches)
            raise BoothError(f"{len(matches)} datasets are named {ref!r}; use an id instead ({ids})")
        return matches[0]

    def register_dataset(self, name: str, backend: str, path: str, description: str = "", schema=None, tags=None, owner: str = "") -> dict:
        body = {
            "name": name,
            "description": description,
            "location": {"backendId": backend, "path": path},
            "schema": schema or [],
            "tags": tags or [],
            "owner": owner,
        }
        return self._h.json("POST", "catalog", "/api/datasets", body)


_READERS = {
    ".csv": ("read_csv", {}),
    ".tsv": ("read_csv", {"sep": "\t"}),
    ".parquet": ("read_parquet", {}),
    ".pq": ("read_parquet", {}),
    ".json": ("read_json", {}),
    ".jsonl": ("read_json", {"lines": True}),
    ".ndjson": ("read_json", {"lines": True}),
}


class Client:
    def __init__(self, env=None, opener=None) -> None:
        self._env = os.environ if env is None else env
        self._http = _Http(env, opener)
        self.storage = Storage(self._http)
        self.catalog = Catalog(self._http)

    @property
    def workspace(self) -> str:
        return self._http.workspace

    def platform_token(self) -> str:
        """This notebook's current platform token: a booth-core workload token for this notebook server,
        in this workspace, capped at your role (ADR 0056). The public way for another client library
        (e.g. ``booth_lakehouse``) to act as the notebook — pass ``booth.platform_token`` itself as a
        token source, since it is short-lived: each call returns a still-valid token, fetched from the
        hub only when the cached one is within a minute of expiring."""
        h = self._http
        if not (h._token_url and h._hub_token and h.workspace):
            raise BoothError("this kernel isn't running in a Project Booth notebook server, so it has no platform identity")
        return h.token()

    def read_dataset(self, ref: str, as_bytes: bool = False, **pandas_kwargs):
        """A registered dataset's contents: a pandas DataFrame for csv/tsv/parquet/json(l) when pandas
        is installed, otherwise (or with ``as_bytes=True``) the raw bytes. v0 reads one object; a
        dataset registered at a directory raises, pointing at ``storage.list``.

        An Iceberg table (``format: "iceberg"``, ADR 0085) is read through ``booth_lakehouse``
        instead, as a DataFrame (a pyarrow Table without pandas); it takes ``columns=``, ``where=``
        (a PyIceberg row filter such as ``"amount > 10"``) and ``snapshot_id=``."""
        ds = self.catalog.find(ref)
        fmt = ds.get("format") or "file"  # absent on records from before ADR 0085: a file
        if fmt == "iceberg":
            if as_bytes:
                raise BoothError(f"dataset {ds.get('name')!r} is an Iceberg table, not one object: read it without as_bytes")
            return self._read_iceberg(ds, **pandas_kwargs)
        if fmt != "file":
            raise BoothError(f"dataset {ds.get('name')!r} has format {fmt!r}, which this version of booth can't read")
        loc = ds.get("location") or {}
        backend, path = loc.get("backendId", ""), loc.get("path", "")
        if not backend or not path:
            raise BoothError(f"dataset {ds.get('name')!r} has no storage location")
        if path.endswith("/"):
            raise BoothError(
                f"dataset {ds.get('name')!r} is a directory ({backend}:{path}); list it with "
                f"booth.storage.list({backend!r}, {path!r}) and read the objects you need"
            )
        data = self.storage.read(backend, path)
        if as_bytes:
            return data
        ext = os.path.splitext(path.lower())[1]
        if ext not in _READERS:
            return data
        try:
            import io

            import pandas as pd
        except ImportError:
            return data
        fn, defaults = _READERS[ext]
        return getattr(pd, fn)(io.BytesIO(data), **{**defaults, **pandas_kwargs})

    def _read_iceberg(self, ds: dict, columns=None, where=None, snapshot_id=None, **unexpected):
        """ADR 0085: an ``iceberg`` dataset names its table in ``table: {namespace, name, ...}``; open it
        through booth_lakehouse (catalog + scoped storage credentials, ADR 0079/0080) rather than reading
        bytes. Reads the table's latest state unless ``snapshot_id`` is given — the catalog's
        ``currentSnapshotId`` can lag behind the table, so it isn't pinned by default."""
        name = ds.get("name")
        if unexpected:
            raise BoothError(f"{', '.join(sorted(unexpected))}: not options for an Iceberg table (use columns=, where=, snapshot_id=)")
        table = ds.get("table") or {}
        namespace, table_name = table.get("namespace", ""), table.get("name", "")
        if not namespace or not table_name:
            raise BoothError(f"dataset {name!r} is marked as an Iceberg table but doesn't say which table")
        try:
            from booth_lakehouse import Lakehouse, LakehouseError
        except ImportError:
            raise BoothError(
                f"dataset {name!r} is an Iceberg table; reading it needs the booth_lakehouse client "
                "(booth-lakehouse-client), which isn't installed in this environment"
            ) from None
        try:
            arrow = Lakehouse.from_env(env=self._env).read(f"{namespace}.{table_name}", columns=columns, where=where, snapshot_id=snapshot_id)
        except LakehouseError as e:
            raise BoothError(f"couldn't read Iceberg table {namespace}.{table_name}: {e}", getattr(e, "status", 0)) from e
        try:
            import pandas  # noqa: F401
        except ImportError:
            return arrow
        return arrow.to_pandas()


_default = Client()
storage = _default.storage
catalog = _default.catalog
read_dataset = _default.read_dataset
platform_token = _default.platform_token
workspace = _default.workspace
