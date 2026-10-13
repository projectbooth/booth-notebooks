"""The hub database's startup retry (ARCHITECTURE.md item 66 (c), booth-core's dbwait pattern)."""

from __future__ import annotations

import logging
import threading

import pytest

from booth_notebooks.dbwait import (
    Cancelled,
    DatabaseNotReady,
    is_connection_failure,
    retry,
    wait_for_database,
)


class OperationalError(Exception):
    """Stands in for psycopg2's / SQLAlchemy's OperationalError (matched by name, as both are)."""


REFUSED = OperationalError('connection to server at "10.0.0.5", port 5432 failed: Connection refused')
STARTING = OperationalError("connection to server at \"10.0.0.5\", port 5432 failed: FATAL:  the database system is starting up")
BAD_PASSWORD = OperationalError('connection to server at "10.0.0.5", port 5432 failed: FATAL:  password authentication failed for user "x"')
NO_DATABASE = OperationalError('connection to server at "10.0.0.5", port 5432 failed: FATAL:  database "booth_mod_notebooks" does not exist')


class Clock:
    """Fake time, advanced only by the fake waits below, so the tests take no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Waits(threading.Event):
    """Records every backoff and advances the fake clock instead of sleeping."""

    def __init__(self, clock: Clock, cancel_on: int | None = None) -> None:
        super().__init__()
        self.clock, self.cancel_on, self.delays = clock, cancel_on, []

    def wait(self, timeout=None) -> bool:
        self.delays.append(timeout)
        self.clock.now += timeout
        if self.cancel_on is not None and len(self.delays) >= self.cancel_on:
            self.set()
        return self.is_set()


def failing(times: int, error: Exception = REFUSED):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) <= times:
            raise error

    return fn, calls


def test_first_attempts_refused_then_connects(caplog):
    clock = Clock()
    waits = Waits(clock)
    fn, calls = failing(3)
    with caplog.at_level(logging.WARNING):
        retry("hub database", fn, clock=clock, cancel=waits)
    assert len(calls) == 4
    assert waits.delays == [0.5, 1.0, 2.0]
    retries = [r.getMessage() for r in caplog.records if "not ready yet" in r.getMessage()]
    assert retries == [
        "hub database: not ready yet (attempt 1: connection to server at \"10.0.0.5\", port 5432 failed: Connection refused); retrying in 0.5s",
        "hub database: not ready yet (attempt 2: connection to server at \"10.0.0.5\", port 5432 failed: Connection refused); retrying in 1s",
        "hub database: not ready yet (attempt 3: connection to server at \"10.0.0.5\", port 5432 failed: Connection refused); retrying in 2s",
    ]


def test_the_backoff_doubles_to_8s_and_gives_up_after_60s_with_the_last_error_and_attempt_count():
    clock = Clock()
    waits = Waits(clock)
    fn, calls = failing(10**6, STARTING)
    with pytest.raises(DatabaseNotReady, match=r"hub database: still failing after 60s \(12 attempts\): .*starting up") as e:
        retry("hub database", fn, clock=clock, cancel=waits)
    assert e.value.__cause__ is STARTING
    # attempt 11 fails at 55.5 s, still inside the bound, so one more 8 s wait; attempt 12 at 63.5 s is the last
    assert waits.delays == [0.5, 1, 2, 4, 8, 8, 8, 8, 8, 8, 8]
    assert len(calls) == 12


def test_a_short_bound_never_succeeding_fails_quickly_in_real_time():
    fn, calls = failing(10**6)
    with pytest.raises(DatabaseNotReady, match=r"after 0.1s \(2 attempts\)"):
        retry("hub database", fn, max_elapsed=0.1)
    assert len(calls) == 2


@pytest.mark.parametrize("error", [BAD_PASSWORD, NO_DATABASE, ValueError("syntax error at or near")])
def test_non_retryable_errors_fail_at_once(error):
    clock = Clock()
    waits = Waits(clock)
    fn, calls = failing(10**6, error)
    with pytest.raises(type(error)) as e:
        retry("hub database", fn, clock=clock, cancel=waits)
    assert e.value is error
    assert len(calls) == 1 and waits.delays == []


def test_cancel_during_a_wait_stops_retrying():
    clock = Clock()
    waits = Waits(clock, cancel_on=2)
    fn, calls = failing(10**6)
    with pytest.raises(Cancelled, match="cancelled after 2 attempts"):
        retry("hub database", fn, clock=clock, cancel=waits)
    assert len(calls) == 2


def test_cancel_from_another_thread_interrupts_a_real_wait():
    cancel = threading.Event()
    fn, calls = failing(10**6)
    threading.Timer(0.05, cancel.set).start()
    with pytest.raises(Cancelled):
        retry("hub database", fn, cancel=cancel)  # first backoff is 500 ms; the cancel lands inside it
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        REFUSED,
        STARTING,
        OperationalError("connection to server at \"10.0.0.5\", port 5432 failed: timeout expired"),
        OperationalError('could not translate host name "booth-database" to address: Name or service not known'),
        OperationalError("connection to server at \"10.0.0.5\", port 5432 failed: No route to host"),
        ConnectionRefusedError(),
        TimeoutError(),
    ],
)
def test_connection_level_failures_are_retryable(error):
    assert is_connection_failure(error)


@pytest.mark.parametrize("error", [BAD_PASSWORD, NO_DATABASE, ValueError("x"), KeyError("x")])
def test_everything_else_is_not(error):
    assert not is_connection_failure(error)


def test_sqlite_needs_no_wait():
    wait_for_database("sqlite:////tmp/jupyterhub.sqlite", max_elapsed=0)


def test_against_the_real_driver_a_refused_port_is_retried_then_reported_without_the_password(caplog):
    # Skipped where psycopg2 isn't installed (CI installs .[dev]; the hub image installs .[postgres]).
    pytest.importorskip("psycopg2")
    with caplog.at_level(logging.WARNING), pytest.raises(DatabaseNotReady, match=r"attempts\): .*[Cc]onnection refused") as e:
        wait_for_database("postgresql+psycopg2://hub:s3cret-pw@127.0.0.1:1/booth_mod_notebooks", max_elapsed=1)
    assert isinstance(e.value.__cause__, Exception)
    assert "s3cret-pw" not in str(e.value) and "s3cret-pw" not in caplog.text
