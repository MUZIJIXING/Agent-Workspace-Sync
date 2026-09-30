"""Exclusive workspace ownership leases for Agent Workspace Sync.

At most one session may hold development ownership of a workspace at any
committed state. The invariant is enforced by the database itself (a partial
unique index over ``status = 'active'``), and every transition below runs inside
one ``BEGIN IMMEDIATE`` transaction so it cannot be raced.

Two rules matter more than the rest:

* Expiration is not ownership transfer. A lease whose ``expires_at`` has passed
  stays ``active`` and keeps blocking ordinary acquisition until its own session
  releases it or an explicit takeover retires it.
* Ownership is bound to a session as well as a lease id. A different session can
  never refresh, release, or otherwise touch a lease it does not own.
* Takeover is for successors. A session that wants its own lapsed lease back
  releases it and acquires a new one; it can never take over from itself.

Acquiring ownership is not implied by creating a session, and releasing a lease
does not end the owning session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import storage
from .workspace import load_workspace

LEASE_ID_PREFIX = "lea_"

# Mirrors the CHECK constraint on ownership_leases.status.
LEASE_STATUS_ACTIVE = "active"
LEASE_STATUS_RELEASED = "released"
LEASE_STATUS_EXPIRED = "expired"


class OwnershipError(Exception):
    """Base class for ownership lease failures."""


class InvalidOwnershipArgumentError(OwnershipError):
    """Raised when an ownership argument is missing, empty, or out of range."""


class UnknownSessionError(OwnershipError):
    """Raised when the requested session does not exist in this workspace."""


class EndedSessionError(OwnershipError):
    """Raised when an ended session tries to acquire ownership."""


class LeaseNotFoundError(OwnershipError):
    """Raised when a lease id is unknown within the owning workspace."""


class OwnershipConflictError(OwnershipError):
    """Raised when another session already holds a fresh active lease."""


class TakeoverRequiredError(OwnershipError):
    """Raised when the active lease has expired and only takeover may replace it.

    The expired lease is left untouched: expiration never authorizes
    acquisition, and this error carries the lease that must be taken over.
    """

    def __init__(self, message: str, lease: "Lease") -> None:
        super().__init__(message)
        self.lease = lease


class LeaseOwnershipMismatchError(OwnershipError):
    """Raised when a session tries to act on a lease owned by another session."""


class LeaseNotActiveError(OwnershipError):
    """Raised when a released or expired lease is treated as active."""


class StaleLeaseError(OwnershipError):
    """Raised when a lapsed active lease is refreshed or released.

    A stale lease must be retired through explicit takeover; no timestamp is
    modified and no new owner is created here.
    """

    def __init__(self, message: str, lease: "Lease") -> None:
        super().__init__(message)
        self.lease = lease


class NoActiveLeaseError(OwnershipError):
    """Raised when takeover is requested but no active lease exists."""


class OwnershipChangedError(OwnershipError):
    """Raised when the active lease is no longer the one takeover was confirmed for.

    A confirmation that went stale must never retire a different, newer owner.
    """


class SameSessionTakeoverError(OwnershipError):
    """Raised when a session tries to take over its own lease.

    Takeover is a successor replacing a previous owner. A session recovering its
    own lapsed lease must release it and acquire a new one, so takeover can
    never be used as a back door for reviving its own ownership.
    """


@dataclass(frozen=True)
class Lease:
    """One ownership lease as persisted in the owning workspace database."""

    lease_id: str
    session_id: str
    acquired_at: str
    last_heartbeat_at: str
    expires_at: str
    status: str

    @property
    def is_active(self) -> bool:
        return self.status == LEASE_STATUS_ACTIVE


def new_lease_id() -> str:
    """Return a fresh opaque lease identifier.

    It is generated, never derived from process id, hostname, username,
    workspace path, harness type, or the owning session id.
    """
    return f"{LEASE_ID_PREFIX}{uuid.uuid4()}"


def acquire_ownership(
    workspace_root: Path | str,
    session_id: str,
    ttl_seconds: int,
) -> Lease:
    """Acquire exclusive workspace ownership for a session, or fail explicitly.

    A fresh active lease held by anyone else raises ``OwnershipConflictError``.
    An expired one raises ``TakeoverRequiredError`` and is left exactly as it
    was: expiration alone never hands ownership over.
    """
    _require_non_empty_string(session_id, "session_id")
    ttl = _require_ttl_seconds(ttl_seconds)
    now = _now()

    outcome, row = storage.try_acquire_lease(
        _workspace_database(workspace_root),
        lease_id=new_lease_id(),
        session_id=session_id,
        now=now.isoformat(),
        expires_at=_expires_at(now, ttl),
    )

    if outcome == storage.OUTCOME_ACQUIRED:
        return Lease(**row)
    if outcome == storage.OUTCOME_ACTIVE_CONFLICT:
        raise OwnershipConflictError(
            f"workspace already has an active owner (lease {row['lease_id']!r}, "
            f"session {row['session_id']!r})"
        )
    if outcome == storage.OUTCOME_TAKEOVER_REQUIRED:
        raise TakeoverRequiredError(
            f"active lease {row['lease_id']!r} has expired and must be taken over "
            "explicitly; expiration does not authorize acquisition",
            Lease(**row),
        )
    if outcome == storage.OUTCOME_SESSION_MISSING:
        raise UnknownSessionError(f"no session {session_id!r} in this workspace")
    if outcome == storage.OUTCOME_SESSION_ALREADY_ENDED:
        raise EndedSessionError(f"session {session_id!r} has ended and cannot acquire ownership")
    raise OwnershipError(f"unexpected acquire outcome: {outcome!r}")


def get_active_ownership(workspace_root: Path | str) -> Lease | None:
    """Return the active lease, or ``None`` when no one owns the workspace."""
    row = storage.fetch_active_lease(_workspace_database(workspace_root))
    return None if row is None else Lease(**row)


def get_lease(workspace_root: Path | str, lease_id: str) -> Lease:
    """Return one lease, active or historical."""
    _require_non_empty_string(lease_id, "lease_id")

    row = storage.fetch_lease(_workspace_database(workspace_root), lease_id)
    if row is None:
        raise LeaseNotFoundError(f"no lease {lease_id!r} in this workspace")
    return Lease(**row)


def heartbeat_ownership(
    workspace_root: Path | str,
    session_id: str,
    lease_id: str,
    ttl_seconds: int,
) -> Lease:
    """Refresh a lease owned by this session.

    The lease id and the session must both match, so an old heartbeat can never
    refresh a newer lease that happens to share the session. A lapsed lease is
    refused without touching either timestamp and is never revived.
    """
    _require_non_empty_string(session_id, "session_id")
    _require_non_empty_string(lease_id, "lease_id")
    ttl = _require_ttl_seconds(ttl_seconds)
    now = _now()

    outcome, row = storage.try_refresh_lease(
        _workspace_database(workspace_root),
        lease_id=lease_id,
        session_id=session_id,
        now=now.isoformat(),
        expires_at=_expires_at(now, ttl),
    )

    if outcome == storage.OUTCOME_REFRESHED:
        return Lease(**row)
    if outcome == storage.OUTCOME_LEASE_MISSING:
        raise LeaseNotFoundError(f"no lease {lease_id!r} in this workspace")
    if outcome == storage.OUTCOME_SESSION_MISMATCH:
        raise LeaseOwnershipMismatchError(
            f"lease {lease_id!r} is owned by session {row['session_id']!r}, not {session_id!r}"
        )
    if outcome == storage.OUTCOME_LEASE_STALE:
        raise StaleLeaseError(
            f"lease {lease_id!r} has expired and cannot be refreshed; "
            "it must be retired by explicit takeover",
            Lease(**row),
        )
    if outcome == storage.OUTCOME_LEASE_NOT_ACTIVE:
        raise LeaseNotActiveError(
            f"lease {lease_id!r} is {row['status']!r} and cannot be refreshed"
        )
    raise OwnershipError(f"unexpected heartbeat outcome: {outcome!r}")


def release_ownership(
    workspace_root: Path | str,
    session_id: str,
    lease_id: str,
) -> Lease:
    """Release a lease owned by this session.

    Release is the current owner voluntarily giving up ownership, so it stays
    available even after the lease lapsed: a stalled session can free the
    workspace instead of leaving a stale owner blocking ordinary acquisition.
    Only the owning session may do this, and a lapsed lease still cannot be
    refreshed — this is a way out, not a way to continue.

    Releasing an already released lease is a no-op that returns the recorded
    lease, which makes retries safe.
    """
    _require_non_empty_string(session_id, "session_id")
    _require_non_empty_string(lease_id, "lease_id")

    outcome, row = storage.try_release_lease(
        _workspace_database(workspace_root),
        lease_id=lease_id,
        session_id=session_id,
    )

    if outcome in (storage.OUTCOME_RELEASED, storage.OUTCOME_ALREADY_RELEASED):
        return Lease(**row)
    if outcome == storage.OUTCOME_LEASE_MISSING:
        raise LeaseNotFoundError(f"no lease {lease_id!r} in this workspace")
    if outcome == storage.OUTCOME_SESSION_MISMATCH:
        raise LeaseOwnershipMismatchError(
            f"lease {lease_id!r} is owned by session {row['session_id']!r}, not {session_id!r}"
        )
    if outcome == storage.OUTCOME_LEASE_NOT_ACTIVE:
        raise LeaseNotActiveError(
            f"lease {lease_id!r} is {row['status']!r} and cannot be released"
        )
    raise OwnershipError(f"unexpected release outcome: {outcome!r}")


def takeover_ownership(
    workspace_root: Path | str,
    session_id: str,
    expected_previous_lease_id: str,
    ttl_seconds: int,
) -> Lease:
    """Retire a lapsed active lease and become the new owner, atomically.

    ``expected_previous_lease_id`` is required so that a confirmation made
    against an older lease cannot retire a newer owner that appeared in the
    meantime. The target session must not be the current owner: takeover is a
    successor replacing a previous owner, never a session reviving its own
    lease. Retiring the previous lease and inserting the new one commit or roll
    back together, so no committed state ever has two active leases.
    """
    _require_non_empty_string(session_id, "session_id")
    _require_non_empty_string(expected_previous_lease_id, "expected_previous_lease_id")
    ttl = _require_ttl_seconds(ttl_seconds)
    now = _now()

    outcome, row = storage.try_takeover_lease(
        _workspace_database(workspace_root),
        expected_previous_lease_id=expected_previous_lease_id,
        lease_id=new_lease_id(),
        session_id=session_id,
        now=now.isoformat(),
        expires_at=_expires_at(now, ttl),
    )

    if outcome == storage.OUTCOME_TAKEN_OVER:
        return Lease(**row)
    if outcome == storage.OUTCOME_NO_ACTIVE_LEASE:
        raise NoActiveLeaseError("this workspace has no active lease to take over")
    if outcome == storage.OUTCOME_SAME_SESSION_TAKEOVER:
        raise SameSessionTakeoverError(
            f"session {session_id!r} already owns lease {row['lease_id']!r}; "
            "release it and acquire a new lease instead of taking over from itself"
        )
    if outcome == storage.OUTCOME_LEASE_CHANGED:
        raise OwnershipChangedError(
            f"active lease is {row['lease_id']!r}, not the expected "
            f"{expected_previous_lease_id!r}; the takeover confirmation is stale"
        )
    if outcome == storage.OUTCOME_LEASE_FRESH:
        raise OwnershipConflictError(
            f"lease {row['lease_id']!r} is still fresh and cannot be taken over"
        )
    if outcome == storage.OUTCOME_SESSION_MISSING:
        raise UnknownSessionError(f"no session {session_id!r} in this workspace")
    if outcome == storage.OUTCOME_SESSION_ALREADY_ENDED:
        raise EndedSessionError(f"session {session_id!r} has ended and cannot take over ownership")
    raise OwnershipError(f"unexpected takeover outcome: {outcome!r}")


def _workspace_database(workspace_root: Path | str) -> Path:
    """Validate the workspace and return its database path.

    ``load_workspace`` is the single gate: it verifies the workspace document,
    the database schema version, and that both agree on ``workspace_id``.
    """
    return load_workspace(workspace_root).database_path


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidOwnershipArgumentError(f"{field_name} must be a non-empty string")


def _require_ttl_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidOwnershipArgumentError("ttl_seconds must be a positive integer")
    return value


def _expires_at(now: datetime, ttl_seconds: int) -> str:
    return (now + timedelta(seconds=ttl_seconds)).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)
