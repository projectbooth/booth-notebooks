"""Retry the hub database's first connection at startup (ARCHITECTURE.md item 66 (c)).

On the homelab a freshly-scheduled pod's first connections to PostgreSQL are refused for about 3
seconds, most likely while k3s's kube-router adds the new pod's IP to the NetworkPolicy allow set.
JupyterHub's ``init_db`` exits on the first refused connection, so the hub restarted once on every
first start. The jupyterhub_config.py entrypoint calls :func:`wait_for_database` before JupyterHub
connects, riding out that window. A copy of booth-core's ``internal/dbwait`` pattern, not an import:
500 ms doubling to 8 s, 60 s in all, every retry logged, then a loud failure with the last error.

Only connection-level failures are retried (refused, timeout, DNS, "starting up"). A wrong password,
a missing database or an SQL error fails at once: waiting would not fix them.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

DEFAULT_MAX_ELAPSED = 60.0
INITIAL_BACKOFF = 0.5
MAX_BACKOFF = 8.0

# libpq's wording for failures that happen before a session exists and that time can fix.
_TRANSIENT = (
    "connection refused",
    "timeout expired",
    "timed out",
    "could not translate host name",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "no route to host",
    "network is unreachable",
    "the database system is starting up",
)


class DatabaseNotReady(RuntimeError):
    """The database never accepted a connection within the bound. Fatal: the hub must not start."""


class Cancelled(RuntimeError):
    """The wait was cancelled (the ``cancel`` event was set) before a connection succeeded."""


def is_connection_failure(exc: BaseException) -> bool:
    """True only for failures to reach the server. SQLAlchemy wraps the driver's error, so both the
    wrapper and the original (``.orig``) are considered; anything that isn't an OperationalError
    (an SQL error, a bad URL) is never retried, and neither is an OperationalError whose message
    names a reason other than the connection-level ones above (bad password, missing database)."""
    if isinstance(exc, (ConnectionRefusedError, TimeoutError, socket.gaierror)):
        return True
    for e in (exc, getattr(exc, "orig", None)):
        if e is not None and type(e).__name__ == "OperationalError":
            message = str(e).lower()
            return any(reason in message for reason in _TRANSIENT)
    return False


def retry(
    what: str,
    fn: Callable[[], object],
    *,
    retryable: Callable[[BaseException], bool] = is_connection_failure,
    max_elapsed: float = DEFAULT_MAX_ELAPSED,
    cancel: threading.Event | None = None,
    log: logging.Logger | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Call ``fn`` (one connection attempt) until it succeeds, retrying retryable failures with
    exponential backoff (500 ms doubling to 8 s) for at most ``max_elapsed`` seconds after the first
    attempt. A non-retryable error is re-raised at once; a retryable one still failing at the bound
    raises :class:`DatabaseNotReady` with the attempt count and the last error. Setting ``cancel``
    stops the wait (:class:`Cancelled`)."""
    clock = clock or time.monotonic
    cancel = cancel or threading.Event()
    log = log or logging.getLogger(__name__)
    deadline = clock() + max_elapsed
    backoff = INITIAL_BACKOFF
    attempt = 0
    while True:
        attempt += 1
        try:
            fn()
            if attempt > 1:
                log.warning("%s: connected (attempt %d)", what, attempt)
            return
        except Exception as e:
            if not retryable(e):
                raise
            if clock() >= deadline:
                raise DatabaseNotReady(f"{what}: still failing after {max_elapsed:g}s ({attempt} attempts): {_one_line(e)}") from e
            log.warning("%s: not ready yet (attempt %d: %s); retrying in %gs", what, attempt, _one_line(e), backoff)
        if cancel.wait(backoff):
            raise Cancelled(f"{what}: cancelled after {attempt} attempts")
        backoff = min(backoff * 2, MAX_BACKOFF)


def _one_line(e: BaseException) -> str:
    # SQLAlchemy appends a multi-line "(Background on this error at: ...)" tail; the driver's first line is the cause.
    text = str(getattr(e, "orig", None) or e).strip()
    return text.splitlines()[0] if text else type(e).__name__


def wait_for_database(db_url: str, *, max_elapsed: float = DEFAULT_MAX_ELAPSED, log: logging.Logger | None = None) -> None:
    """Block until the hub's PostgreSQL accepts a connection (see the module docstring). Only for a
    PostgreSQL ``db_url``; SQLite (development, tests) needs no wait. The URL, which carries the
    password, is never logged."""
    if not db_url.startswith("postgresql"):
        return
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(db_url, poolclass=NullPool, connect_args={"connect_timeout": 5})

    def connect() -> None:
        with engine.connect():
            pass

    try:
        retry("hub database", connect, max_elapsed=max_elapsed, log=log)
    finally:
        engine.dispose()
