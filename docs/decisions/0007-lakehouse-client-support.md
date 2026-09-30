# 0007: A public platform-token accessor (ADR 0084) and Iceberg datasets in `read_dataset` (ADR 0085)

Status: **built and unit-tested (2026-09-29)**, including the calls checked against the real
`booth_lakehouse` package. Not run against a real Iceberg table (see "Not verified").

## 1. `booth.platform_token()` (ADR 0084)

booth-lakehouse's client found a kernel's token by reaching into `booth._default._http.token`, a
private attribute. `booth.platform_token()` is now the public way. It returns this notebook's current
platform token (a booth-core workload token for this notebook server and workspace, capped at the
person's role, ADR 0056), fetched from the hub only when the cached one is within a minute of expiring.

It is a zero-argument callable, so another client can pass `booth.platform_token` itself as its token
source. That is exactly the `Callable[[], str]` booth-lakehouse's `Lakehouse` already accepts (checked
against the real package), which matters because the token is short-lived.

**For booth-lakehouse** (its call to make, not done here): its `_notebook_token()` can become
`getattr(booth, "platform_token", None)`. The private attribute still exists and still works (checked
against its real discovery code), so nothing breaks in the meantime.

## 2. `read_dataset` on `format: "iceberg"` (ADR 0085)

A small branch ahead of the unchanged file path:

- `format` absent (every record from before ADR 0085) or `"file"`: exactly the previous behaviour,
  including refusing a directory location.
- `"iceberg"`: the table is named by the dataset's `table: {namespace, name}` block and read with
  `Lakehouse.from_env(env=...).read("<namespace>.<name>", columns=, where=, snapshot_id=)`. It returns a
  DataFrame when pandas is installed, as the file path does; otherwise the pyarrow Table. No raw
  storage read is attempted.
- **The catalog's `currentSnapshotId` is not pinned by default.** ADR 0085 lets catalog updates be
  debounced, so the catalog's snapshot can lag the table. The latest state is read unless the caller
  passes `snapshot_id=`.
- Refused with a clear `BoothError`, never guessed:
  - `as_bytes=True`, or a file-reader option (e.g. `sep=`), on a table;
  - an `iceberg` record with no `table` block;
  - an unknown future `format`;
  - `booth_lakehouse` not installed.
- booth-lakehouse's own errors surface as `BoothError`, keeping their status.

## Tests

`tests/client/test_lakehouse_and_token.py` (15 tests). `booth_lakehouse` is replaced by a recording fake
to pin exactly what booth asks of it. One more test binds booth's real calls against the **real**
package's signatures (`from_env(env=)`, `read(name, columns=, where=, snapshot_id=)`,
`LakehouseError`); it runs when the package is installed and passed locally against booth-lakehouse's
current client.

## Not verified / flagged

- **Not run against a real Iceberg table.** A real read also needs the ADR 0080 credential broker,
  which booth-core hasn't built yet (booth-lakehouse tests against a stand-in). Worth one end-to-end
  pass once the broker and booth-catalog's `format` field (ADR 0085) both land.
- **`booth_lakehouse` isn't in the default notebook image.** The branch imports it lazily and says so
  when it's missing, but there's nothing to install it from yet: booth-lakehouse isn't published
  anywhere, and its repo has no git history (ADR 0084). Once it has a release (a wheel, a tag to install
  from), adding it to `images/singleuser` is a one-line change. It brings PyIceberg with it, which
  enlarges the image. Flagged for the coordinator: how module client libraries get distributed.
- **The notebook pod has no `BOOTH_CREDENTIAL_BROKER_URL`.** `Lakehouse.from_env` reads it, and a read
  fails with booth-lakehouse's "no credential broker is configured" error without it. The spawner should
  set it (and the pod NetworkPolicy allow it, if the broker isn't behind core's gateway) once ADR 0080's
  broker has an address. Not guessed now.
