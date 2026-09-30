"""Finalized handoff state for Agent Workspace Sync.

A handoff is the minimum sufficient workspace context that lets a later
development session continue the work. It is not a copy of chat history, and it
carries no successor binding: no target session, no target harness. Whoever
holds valid ownership next can read it.

Handoff state and ownership stay separate, always:

    finalize_handoff() != release_ownership()
    get_handoff()      != acquire_ownership()

Only the session that currently holds the workspace's active lease may finalize
the authoritative handoff — and finalizing never releases the lease, expires
it, or ends the session. The normal lifecycle is explicit at every step:

    finalize handoff -> release ownership -> end session

A session that lost ownership to a takeover can no longer write authoritative
handoff state. Finalized handoffs are immutable historical state: this module
offers no update, delete, supersede, or versioning path.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from .ownership import EndedSessionError, UnknownSessionError
from .workspace import load_workspace

HANDOFF_ID_PREFIX = "hnd_"


class HandoffError(Exception):
    """Base class for handoff failures."""


class InvalidHandoffArgumentError(HandoffError):
    """Raised when a handoff field is missing, empty, or not a JSON object."""


class HandoffNotFoundError(HandoffError):
    """Raised when a handoff id is unknown within the owning workspace."""


class HandoffAlreadyFinalizedError(HandoffError):
    """Raised when a session that already finalized a handoff tries again.

    A session produces at most one finalized handoff, and the existing record is
    never overwritten.
    """

    def __init__(self, message: str, handoff: "Handoff") -> None:
        super().__init__(message)
        self.handoff = handoff


class HandoffNotAuthorizedError(HandoffError):
    """Raised when finalization is attempted by anything but the current owner.

    Covers both "no active owner exists" (ownership was released or never
    acquired) and "the active owner is a different session" (ownership moved on,
    for example through takeover).
    """


@dataclass(frozen=True)
class Handoff:
    """One finalized handoff as persisted in the owning workspace database."""

    handoff_id: str
    from_session_id: str
    created_at: str
    semantic_context: dict
    objective_evidence: dict


def new_handoff_id() -> str:
    """Return a fresh opaque handoff identifier.

    It is generated, never derived from the session id, harness type, workspace
    path, process id, username, or machine name.
    """
    return f"{HANDOFF_ID_PREFIX}{uuid.uuid4()}"


def finalize_handoff(
    workspace_root: Path | str,
    from_session_id: str,
    semantic_context: dict,
    objective_evidence: dict,
) -> Handoff:
    """Persist the authoritative handoff of the session that owns the workspace.

    Authorization and the insert commit as one transaction, so ownership cannot
    slip away between them. A session whose lease has lapsed but is still the
    recorded active owner may finalize: it still holds the ownership slot, so it
    can persist context and then release cleanly.

    Neither argument is interpreted field by field; each is stored as one JSON
    object. Both may be empty, but neither may be a list, string, number, or
    null.
    """
    _require_non_empty_string(from_session_id, "from_session_id")
    semantic_context_json = _serialize(semantic_context, "semantic_context")
    objective_evidence_json = _serialize(objective_evidence, "objective_evidence")

    outcome, row = storage.try_finalize_handoff(
        _workspace_database(workspace_root),
        handoff_id=new_handoff_id(),
        from_session_id=from_session_id,
        created_at=_now().isoformat(),
        semantic_context=semantic_context_json,
        objective_evidence=objective_evidence_json,
    )

    if outcome == storage.OUTCOME_HANDOFF_FINALIZED:
        return _handoff_from(row)
    if outcome == storage.OUTCOME_HANDOFF_ALREADY_EXISTS:
        raise HandoffAlreadyFinalizedError(
            f"session {from_session_id!r} already finalized handoff {row['handoff_id']!r}",
            _handoff_from(row),
        )
    if outcome == storage.OUTCOME_SESSION_MISSING:
        raise UnknownSessionError(f"no session {from_session_id!r} in this workspace")
    if outcome == storage.OUTCOME_SESSION_ALREADY_ENDED:
        raise EndedSessionError(
            f"session {from_session_id!r} has ended and cannot finalize a handoff"
        )
    if outcome == storage.OUTCOME_NO_ACTIVE_OWNER:
        raise HandoffNotAuthorizedError(
            "this workspace has no active owner; only the current owner may finalize a handoff"
        )
    if outcome == storage.OUTCOME_NOT_ACTIVE_OWNER:
        raise HandoffNotAuthorizedError(
            f"session {from_session_id!r} does not hold the active lease "
            f"(held by {row['session_id']!r})"
        )
    raise HandoffError(f"unexpected finalize outcome: {outcome!r}")


def get_handoff(workspace_root: Path | str, handoff_id: str) -> Handoff:
    """Return one finalized handoff.

    Reading requires no ownership and changes nothing: no lease is created, no
    session is created, and no ownership is acquired.
    """
    _require_non_empty_string(handoff_id, "handoff_id")

    row = storage.fetch_handoff(_workspace_database(workspace_root), handoff_id)
    if row is None:
        raise HandoffNotFoundError(f"no handoff {handoff_id!r} in this workspace")
    return _handoff_from(row)


def get_handoff_for_session(workspace_root: Path | str, from_session_id: str) -> Handoff | None:
    """Return the finalized handoff a particular session produced, if any.

    A session produces at most one finalized handoff, so this is an exact lookup
    instead of "whatever is newest". That distinction matters when a later
    session has since finalized its own handoff: the earlier session's record is
    still there and still attributable to it.

    Reading requires no ownership and changes nothing.
    """
    _require_non_empty_string(from_session_id, "from_session_id")

    row = storage.fetch_handoff_by_session(_workspace_database(workspace_root), from_session_id)
    return None if row is None else _handoff_from(row)


def get_latest_handoff(workspace_root: Path | str) -> Handoff | None:
    """Return the most recent finalized handoff, or ``None`` when none exists.

    Also a pure read. Ordering is by ``created_at`` with ``handoff_id`` as a
    stable tie-breaker, so the result is deterministic.
    """
    row = storage.fetch_latest_handoff(_workspace_database(workspace_root))
    return None if row is None else _handoff_from(row)


def _workspace_database(workspace_root: Path | str) -> Path:
    """Validate the workspace and return its database path.

    ``load_workspace`` is the single gate: it verifies the workspace document,
    the database schema version, and that both agree on ``workspace_id``.
    """
    return load_workspace(workspace_root).database_path


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidHandoffArgumentError(f"{field_name} must be a non-empty string")


def _serialize(document: object, field_name: str) -> str:
    """Serialize one context object canonically, or refuse to persist it.

    Keys are sorted and separators are compact so the same document always
    produces the same text. NaN and Infinity are rejected rather than written as
    non-standard JSON.
    """
    if not isinstance(document, dict):
        raise InvalidHandoffArgumentError(
            f"{field_name} must be a JSON object (dict), not {type(document).__name__}"
        )
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidHandoffArgumentError(
            f"{field_name} is not JSON-serializable: {exc}"
        ) from exc


def _deserialize(text: str, field_name: str) -> dict:
    try:
        document = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HandoffError(f"stored {field_name} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise HandoffError(f"stored {field_name} is not a JSON object")
    return document


def _handoff_from(row: dict) -> Handoff:
    return Handoff(
        handoff_id=row["handoff_id"],
        from_session_id=row["from_session_id"],
        created_at=row["created_at"],
        semantic_context=_deserialize(row["semantic_context"], "semantic_context"),
        objective_evidence=_deserialize(row["objective_evidence"], "objective_evidence"),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
