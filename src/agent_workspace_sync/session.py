"""Development sessions for Agent Workspace Sync.

A session records one period during which one concrete harness instance
participates in development of the current workspace. It is a record of
participation, not a claim of ownership:

    create session != acquire workspace ownership
    end session    != release ownership

Ownership lives in :mod:`agent_workspace_sync.ownership`. A session that still
owns an active lease cannot be ended normally — ownership has to be resolved
first. This module never touches harness configuration, and harness instance
identity is generated rather than derived from PID, username, machine name,
executable path, workspace path, or any global registry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from .workspace import load_workspace

SESSION_ID_PREFIX = "ses_"
HARNESS_INSTANCE_ID_PREFIX = "hri_"


class SessionError(Exception):
    """Base class for development session failures."""


class InvalidSessionFieldError(SessionError):
    """Raised when a session field is missing, empty, or not a string."""


class SessionNotFoundError(SessionError):
    """Raised when a session id is unknown within the owning workspace."""


class SessionOwnsActiveLeaseError(SessionError):
    """Raised when a session that still owns an active lease is ended normally.

    Ownership must be resolved first, either by releasing the lease or by an
    explicit takeover. Ending a session never releases ownership on its behalf.
    """


@dataclass(frozen=True)
class Session:
    """One development session as persisted in the owning workspace database."""

    session_id: str
    harness_type: str
    harness_instance_id: str
    started_at: str
    ended_at: str | None

    @property
    def is_ended(self) -> bool:
        return self.ended_at is not None


def new_session_id() -> str:
    """Return a fresh opaque session identifier."""
    return f"{SESSION_ID_PREFIX}{uuid.uuid4()}"


def new_harness_instance_id() -> str:
    """Return a fresh opaque harness instance identifier.

    It is generated, never derived from process id, username, machine name,
    installation path, workspace path, or harness global configuration.
    """
    return f"{HARNESS_INSTANCE_ID_PREFIX}{uuid.uuid4()}"


def create_session(
    workspace_root: Path | str,
    harness_type: str,
    harness_instance_id: str,
) -> Session:
    """Record a new development session in an initialized workspace.

    The workspace is loaded and validated first, which enforces the workspace
    document, the database schema version, and workspace identity consistency
    before any row is written. Creating a session grants no ownership.
    """
    _require_non_empty_string(harness_type, "harness_type")
    _require_non_empty_string(harness_instance_id, "harness_instance_id")

    session = Session(
        session_id=new_session_id(),
        harness_type=harness_type,
        harness_instance_id=harness_instance_id,
        started_at=_now(),
        ended_at=None,
    )
    storage.insert_session(
        _workspace_database(workspace_root),
        session_id=session.session_id,
        harness_type=session.harness_type,
        harness_instance_id=session.harness_instance_id,
        started_at=session.started_at,
    )
    return session


def get_session(workspace_root: Path | str, session_id: str) -> Session:
    """Return one session recorded in this workspace."""
    stored = storage.fetch_session(_workspace_database(workspace_root), session_id)
    if stored is None:
        raise SessionNotFoundError(f"no session {session_id!r} in this workspace")
    return Session(**stored)


def end_session(workspace_root: Path | str, session_id: str) -> Session:
    """Record the end of a session.

    A session that still owns an active lease cannot be ended normally: the
    ownership check and the ``ended_at`` update commit as one transaction, so no
    committed state ever has an ended session holding an active lease. Resolve
    ownership first, by releasing the lease or by handing it over through an
    explicit takeover. Ending a session does not release ownership.

    Ending an already-ended session is idempotent: the ``ended_at`` recorded
    first is returned unchanged and is never rewritten.
    """
    outcome, stored = storage.end_session_if_unowned(
        _workspace_database(workspace_root), session_id, _now()
    )

    if outcome in (storage.OUTCOME_ENDED, storage.OUTCOME_ALREADY_ENDED):
        return Session(**stored)
    if outcome == storage.OUTCOME_OWNS_ACTIVE_LEASE:
        raise SessionOwnsActiveLeaseError(
            f"session {session_id!r} still owns active lease {stored['lease_id']!r}; "
            "release it or retire it through explicit takeover before ending the session"
        )
    if outcome == storage.OUTCOME_SESSION_MISSING:
        raise SessionNotFoundError(f"no session {session_id!r} in this workspace")
    raise SessionError(f"unexpected end-session outcome: {outcome!r}")


def _workspace_database(workspace_root: Path | str) -> Path:
    """Validate the workspace and return its database path.

    ``load_workspace`` is the single gate: it verifies the workspace document,
    the database schema version, and that both agree on ``workspace_id``.
    """
    return load_workspace(workspace_root).database_path


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidSessionFieldError(f"{field_name} must be a non-empty string")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
