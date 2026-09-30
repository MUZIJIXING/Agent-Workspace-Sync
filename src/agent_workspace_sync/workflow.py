"""High-level workspace workflows for Agent Workspace Sync.

This layer composes the atomic core operations into the few workflows a harness
actually needs, so callers do not have to orchestrate session, lease, and
handoff bookkeeping themselves. It adds no persistence of its own and keeps no
hidden state: every identifier is either passed in or returned, and all project
state still lives inside ``<workspace>/.agent-workspace/``.

The workflows call the Python core directly. They never shell out to the CLI and
never parse its output — the CLI and this layer are sibling adapters over the
same core.

The core commits each operation in its own transaction, so a workflow is not one
atomic unit. Each workflow is therefore written to be safely retryable: partial
progress is either compensated on failure or recognised and reused on the next
attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .handoff import (
    Handoff,
    HandoffAlreadyFinalizedError,
    HandoffNotAuthorizedError,
    finalize_handoff,
    get_handoff_for_session,
    get_latest_handoff,
)
from .ownership import (
    EndedSessionError,
    Lease,
    OwnershipError,
    acquire_ownership,
    get_active_ownership,
    heartbeat_ownership,
    release_ownership,
    takeover_ownership,
)
from .session import (
    Session,
    SessionError,
    create_session,
    end_session,
    get_session,
    new_harness_instance_id,
)
from .storage import StorageError
from .workspace import find_workspace

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"


class WorkflowError(Exception):
    """Base class for workflow-level failures."""


class LeavePartialError(WorkflowError):
    """A finalized handoff exists, but a later leave step was not confirmed."""

    def __init__(
        self, *, failed_step: str, handoff: Handoff, session_id: str,
        lease_id: str, original_error: BaseException,
    ) -> None:
        if failed_step == "release":
            message = "Handoff saved; workspace release was not confirmed."
        else:
            message = "Handoff saved and lease released; session end was not confirmed."
        super().__init__(message)
        self.failed_step = failed_step
        self.handoff_id = handoff.handoff_id
        self.session_id = session_id
        self.lease_id = lease_id
        self.release_confirmed = failed_step == "end"
        self.original_error = original_error


class WorkflowCleanupError(WorkflowError):
    """Raised when a workflow failed and its compensation also failed.

    Both failures are preserved: ``original_error`` is the failure that started
    the compensation, and ``cleanup_errors`` holds everything that went wrong
    while trying to undo partial progress.
    """

    def __init__(
        self,
        message: str,
        *,
        original_error: BaseException,
        cleanup_errors: tuple[BaseException, ...],
    ) -> None:
        super().__init__(message)
        self.original_error = original_error
        self.cleanup_errors = cleanup_errors


class TakeoverContextError(WorkflowError):
    """Raised when a takeover committed but loading the follow-up context failed.

    The new ownership stands and is deliberately not reversed: the previous
    lease is already expired and the new one is active, so an automatic rollback
    would have to fabricate a reverse takeover across separate transactions.
    The error carries the new identifiers so the caller can carry on with the
    workspace it now owns.
    """

    def __init__(
        self,
        message: str,
        *,
        session: Session,
        lease: Lease,
        original_error: BaseException,
    ) -> None:
        super().__init__(message)
        self.session = session
        self.lease = lease
        self.session_id = session.session_id
        self.lease_id = lease.lease_id
        self.original_error = original_error


@dataclass(frozen=True)
class EnterWorkspaceResult:
    """Everything a harness needs to start working in a workspace."""

    workspace_id: str
    workspace_root: Path
    session: Session
    lease: Lease
    latest_handoff: Handoff | None


@dataclass(frozen=True)
class LeaveWorkspaceResult:
    """Everything a harness leaves behind when it hands the workspace over."""

    workspace_id: str
    workspace_root: Path
    handoff: Handoff
    handoff_was_existing: bool
    lease: Lease
    session: Session


@dataclass(frozen=True)
class TakeoverWorkspaceResult:
    """The successor's view after an explicit takeover."""

    workspace_id: str
    workspace_root: Path
    session: Session
    lease: Lease
    previous_lease_id: str
    latest_handoff: Handoff | None
    # Always True in this version: taking over means the previous session did
    # not complete a normal release lifecycle, so the latest handoff may be an
    # older record rather than the previous owner's final state.
    recovery_context_may_be_incomplete: bool


@dataclass(frozen=True)
class InspectWorkspaceResult:
    """A read-only snapshot of the workspace's current state."""

    workspace_id: str
    workspace_root: Path
    owner_session: Session | None
    lease: Lease | None
    lease_freshness: str | None
    latest_handoff: Handoff | None


def enter_workspace(
    workspace_root: Path | str,
    harness_type: str,
    ttl_seconds: int,
    harness_instance_id: str | None = None,
) -> EnterWorkspaceResult:
    """Start working in a workspace: new session, ownership, and context.

    The workspace must already be initialized; this never initializes one. If
    the workspace is not free, the attempt fails and the session it created is
    ended again so no meaningless open session is left behind.
    """
    identity = find_workspace(workspace_root)
    instance_id = (
        harness_instance_id if harness_instance_id is not None else new_harness_instance_id()
    )

    session = create_session(identity.root, harness_type, instance_id)

    try:
        lease = acquire_ownership(identity.root, session.session_id, ttl_seconds)
    except BaseException as original:
        cleanup_failure = _compensation_failure(
            identity.root, session=session, lease=None, original=original
        )
        if cleanup_failure is not None:
            raise cleanup_failure from original
        raise

    try:
        latest_handoff = get_latest_handoff(identity.root)
    except BaseException as original:
        # Do not hand back a failure while quietly holding ownership.
        cleanup_failure = _compensation_failure(
            identity.root, session=session, lease=lease, original=original
        )
        if cleanup_failure is not None:
            raise cleanup_failure from original
        raise

    return EnterWorkspaceResult(
        workspace_id=identity.workspace_id,
        workspace_root=identity.root,
        session=session,
        lease=lease,
        latest_handoff=latest_handoff,
    )


def leave_workspace(
    workspace_root: Path | str,
    session_id: str,
    lease_id: str,
    semantic_context: dict,
    objective_evidence: dict,
) -> LeaveWorkspaceResult:
    """Hand the workspace over: finalize, release, and end.

    These are three separate core transactions, so this is retryable rather than
    atomic. A retry after a partial run reuses whatever already committed: an
    existing finalized handoff is never rewritten, and release and end are both
    idempotent in the core. ``semantic_context`` and ``objective_evidence`` are
    plain dicts; reading files is a transport concern of the CLI, not of this
    layer.
    """
    identity = find_workspace(workspace_root)

    handoff_was_existing = False
    try:
        handoff = finalize_handoff(
            identity.root, session_id, semantic_context, objective_evidence
        )
    except HandoffAlreadyFinalizedError as already_finalized:
        # A previous attempt got this far; the recorded handoff stands.
        handoff = already_finalized.handoff
        handoff_was_existing = True
    except (EndedSessionError, HandoffNotAuthorizedError):
        # The session has ended, or it no longer holds the active lease, so an
        # earlier attempt already got past finalization. Ask for this session's
        # own handoff by identity rather than trusting "latest": a later session
        # may since have finalized its own, which says nothing about whether
        # this one did.
        existing = get_handoff_for_session(identity.root, session_id)
        if existing is None:
            raise
        handoff = existing
        handoff_was_existing = True

    try:
        lease = release_ownership(identity.root, session_id, lease_id)
    except (OwnershipError, SessionError, StorageError) as error:
        raise LeavePartialError(
            failed_step="release", handoff=handoff, session_id=session_id,
            lease_id=lease_id, original_error=error,
        ) from error
    try:
        session = end_session(identity.root, session_id)
    except (SessionError, StorageError) as error:
        raise LeavePartialError(
            failed_step="end", handoff=handoff, session_id=session_id,
            lease_id=lease_id, original_error=error,
        ) from error

    return LeaveWorkspaceResult(
        workspace_id=identity.workspace_id,
        workspace_root=identity.root,
        handoff=handoff,
        handoff_was_existing=handoff_was_existing,
        lease=lease,
        session=session,
    )


def takeover_workspace(
    workspace_root: Path | str,
    harness_type: str,
    expected_previous_lease_id: str,
    ttl_seconds: int,
    harness_instance_id: str | None = None,
) -> TakeoverWorkspaceResult:
    """Replace a lapsed owner: new session, explicit takeover, and context.

    ``expected_previous_lease_id`` is the stale confirmation token: if ownership
    moved on since it was read, the takeover refuses rather than retiring a
    newer owner. If the takeover itself fails, the successor session created for
    it is ended again. If the takeover succeeds but loading the context then
    fails, the takeover is not reversed.
    """
    identity = find_workspace(workspace_root)
    instance_id = (
        harness_instance_id if harness_instance_id is not None else new_harness_instance_id()
    )

    session = create_session(identity.root, harness_type, instance_id)

    try:
        lease = takeover_ownership(
            identity.root,
            session.session_id,
            expected_previous_lease_id,
            ttl_seconds,
        )
    except BaseException as original:
        cleanup_failure = _compensation_failure(
            identity.root, session=session, lease=None, original=original
        )
        if cleanup_failure is not None:
            raise cleanup_failure from original
        raise

    try:
        latest_handoff = get_latest_handoff(identity.root)
    except BaseException as original:
        raise TakeoverContextError(
            f"ownership takeover succeeded (lease {lease.lease_id}) but loading the "
            f"latest handoff failed: {original}",
            session=session,
            lease=lease,
            original_error=original,
        ) from original

    return TakeoverWorkspaceResult(
        workspace_id=identity.workspace_id,
        workspace_root=identity.root,
        session=session,
        lease=lease,
        previous_lease_id=expected_previous_lease_id,
        latest_handoff=latest_handoff,
        recovery_context_may_be_incomplete=True,
    )


def heartbeat_workspace(
    workspace_root: Path | str,
    session_id: str,
    lease_id: str,
    ttl_seconds: int,
) -> Lease:
    """Refresh the lease of a session that is still working.

    A deliberately thin wrapper: it resolves the workspace, delegates to the core
    heartbeat, and returns the refreshed lease. There is no timer, no background
    refresh, and no default lifetime — the caller decides when and for how long,
    and a lapsed or released lease is still never revived.
    """
    identity = find_workspace(workspace_root)
    return heartbeat_ownership(identity.root, session_id, lease_id, ttl_seconds)


def inspect_workspace(workspace_root: Path | str) -> InspectWorkspaceResult:
    """Report the workspace's current state without changing anything.

    A best-effort multi-read snapshot, like the CLI's status command: it creates
    no session, refreshes no lease, and writes nothing. Concurrent changes can
    make its fields describe slightly different instants.
    """
    identity = find_workspace(workspace_root)

    lease = get_active_ownership(identity.root)
    owner_session = get_session(identity.root, lease.session_id) if lease is not None else None
    freshness = _lease_freshness(lease) if lease is not None else None
    latest_handoff = get_latest_handoff(identity.root)

    return InspectWorkspaceResult(
        workspace_id=identity.workspace_id,
        workspace_root=identity.root,
        owner_session=owner_session,
        lease=lease,
        lease_freshness=freshness,
        latest_handoff=latest_handoff,
    )


def _compensation_failure(
    root: Path,
    *,
    session: Session,
    lease: Lease | None,
    original: BaseException,
) -> WorkflowCleanupError | None:
    """Undo partial progress, and report honestly if that also fails.

    Returns ``None`` when compensation succeeded, in which case the caller
    re-raises the original failure unchanged. When cleanup fails too, the
    returned error carries both failures instead of hiding either.
    """
    cleanup_errors: list[BaseException] = []

    if lease is not None:
        try:
            release_ownership(root, lease.session_id, lease.lease_id)
        except Exception as exc:  # noqa: BLE001 - collected and reported below
            cleanup_errors.append(exc)

    try:
        end_session(root, session.session_id)
    except Exception as exc:  # noqa: BLE001 - collected and reported below
        cleanup_errors.append(exc)

    if not cleanup_errors:
        return None

    return WorkflowCleanupError(
        "workflow failed and compensation also failed; "
        f"original: {original!r}; cleanup: {[repr(error) for error in cleanup_errors]}",
        original_error=original,
        cleanup_errors=tuple(cleanup_errors),
    )


def _lease_freshness(lease: Lease) -> str:
    """Report whether a lease is still fresh. Display only; nothing is refreshed."""
    return FRESHNESS_FRESH if _parse_timestamp(lease.expires_at) > _now() else FRESHNESS_STALE


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StorageError(f"malformed timestamp in workspace database: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)
