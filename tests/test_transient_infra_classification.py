"""A dropped backing-service connection must not be a terminal job failure.

Regression guard for Defect 1 of
knowledge-base/knowledge/issues/transient_db_error_hard_fails_job_and_destroys_vm.md.

On 2026-07-27 psycopg raised ``the connection is closed`` while the LangGraph
checkpointer wrote. The taxonomy was binary — ``WorkspaceUnavailableError`` was
recoverable and everything else was death — so a sub-second blip terminally
failed a 46-hour job and its VM was reaped as a consequence, destroying the only
path by which it could record its own completion.

The predicate is an ALLOW-LIST with a deny-list checked first: an unmatched
exception must keep today's behaviour (terminal), never silently become an
infinite retry.
"""

import pytest

from shared.runtime.core.workspace_backend import (
    WorkspaceUnavailableError,
    completion_error_payload,
    is_transient_infra_error,
)
from orchestrator.services.completion import (
    INFRA_TRANSIENT_MAX_ATTEMPTS,
    infra_transient_backoff_seconds,
)


# --- stand-ins for driver exception trees (matched by class name) ------------


class Error(Exception):
    pass


class DatabaseError(Error):
    pass


class OperationalError(DatabaseError):
    """psycopg's overloaded class — message-gated."""


class InterfaceError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class PostgresError(Exception):
    pass


class ConnectionDoesNotExistError(PostgresError):
    pass


class AdminShutdownError(PostgresError):
    pass


class IntegrityConstraintViolationError(PostgresError):
    pass


class CheckViolationError(IntegrityConstraintViolationError):
    pass


class UndefinedColumnError(PostgresError):
    pass


class TestTheIncidentError:
    def test_the_exact_2026_07_27_message_is_transient(self):
        assert is_transient_infra_error(OperationalError("the connection is closed"))

    def test_it_reports_as_recoverable_infra_transient(self):
        payload = completion_error_payload(
            OperationalError("the connection is closed")
        )["error"]
        assert payload["type"] == "infra_transient"
        assert payload["recoverable"] is True

    def test_the_disk_full_error_that_killed_job_1a_is_transient(self):
        """Job e1192a9d died to the raw ENOSPC error, verbatim.

        An operator can expand the volume inside the retry window; if they
        don't, the ceiling fails the job anyway with the cause named. Either
        outcome beats killing a 20-hour job on the first write failure.
        """
        assert is_transient_infra_error(
            OperationalError(
                'could not extend file "base/16384/701778.14": No space left on '
                "device\nHINT:  Check free disk space."
            )
        )

    def test_disk_full_by_driver_class_not_just_message(self):
        """Both drivers raise a dedicated class; don't rely on message text."""

        class DiskFull(OperationalError):
            pass

        assert is_transient_infra_error(DiskFull("53100"))


class TestTransientCases:
    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionDoesNotExistError("connection was closed in the middle"),
            AdminShutdownError("terminating connection due to administrator command"),
            ConnectionResetError("[Errno 104] Connection reset by peer"),
            BrokenPipeError("[Errno 32] Broken pipe"),
            InterfaceError("connection already closed"),
            OperationalError("server closed the connection unexpectedly"),
            OperationalError("the database system is in recovery mode"),
            OperationalError("could not connect to server"),
        ],
    )
    def test_connection_state_failures_are_transient(self, exc):
        assert is_transient_infra_error(exc)


class TestNeverTransient:
    @pytest.mark.parametrize(
        "exc",
        [
            CheckViolationError('violates check constraint "valid_memory_type"'),
            UndefinedColumnError('column "version" does not exist'),
            IntegrityError("duplicate key value violates unique constraint"),
            ProgrammingError("syntax error at or near"),
            ValueError("ordinary bug"),
            RuntimeError("the connection is closed"),  # message alone is not enough
        ],
    )
    def test_real_bugs_stay_terminal(self, exc):
        assert not is_transient_infra_error(exc)
        assert completion_error_payload(exc)["error"]["type"] == "job_error"

    def test_deny_list_beats_a_matching_message(self):
        """A constraint violation whose message mentions a closed connection is
        still a bug — the deny-list is checked first, on purpose."""

        class OperationalIntegrityError(IntegrityError):
            pass

        assert not is_transient_infra_error(
            OperationalIntegrityError("the connection is closed")
        )

    def test_unmatched_operational_error_is_not_transient(self):
        """OperationalError is overloaded; only connection-state messages count."""
        assert not is_transient_infra_error(
            OperationalError("password authentication failed for user")
        )


class TestClassPrecedence:
    def test_workspace_death_still_wins(self):
        """Checked first — a socket-ish workspace error must not be reclassified."""
        payload = completion_error_payload(
            WorkspaceUnavailableError("Failed to connect to workspace 10.0.0.1:22")
        )["error"]
        assert payload["type"] == "workspace_unavailable"
        assert payload["recoverable"] is True


class TestBackoffAndCeiling:
    def test_ladder_escalates_and_caps(self):
        delays = [infra_transient_backoff_seconds(n) for n in range(1, 8)]
        assert delays[0] == 60
        assert delays == sorted(delays), "must never go backwards"
        assert max(delays) <= 3600, (
            "cap bounds how long the reaper carve-out holds a VM"
        )

    def test_ceiling_is_small_enough_to_bound_a_misclassification(self):
        total = sum(
            infra_transient_backoff_seconds(n)
            for n in range(1, INFRA_TRANSIENT_MAX_ATTEMPTS + 1)
        )
        assert total <= 4 * 3600, (
            "a permanent error wrongly matched as transient must fail in hours, "
            "not retry forever"
        )
