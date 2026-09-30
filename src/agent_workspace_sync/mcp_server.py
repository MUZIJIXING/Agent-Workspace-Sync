"""MCP server exposing the high-level workspace workflows.

The server is a thin adapter: each tool resolves its arguments, calls one
workflow (or the core heartbeat), and serializes the result. It keeps no state
of its own — identifiers flow out through tool results and come back as
arguments, so the harness decides what to remember. Nothing here shells out to
the CLI or parses its output.

The MCP SDK is an optional dependency. Importing this module never fails: when
the SDK is missing (or is an older major version without the v2 API) the module
simply reports that MCP support is not installed, and nothing in the core
package depends on it.

Because MCP uses stdio, stdout belongs to the protocol. This module never prints
to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import sys
from typing import Any, TypedDict

from .handoff import (
    Handoff,
    HandoffAlreadyFinalizedError,
    HandoffError,
    HandoffNotAuthorizedError,
)
from .ownership import (
    EndedSessionError,
    Lease,
    LeaseNotActiveError,
    LeaseNotFoundError,
    LeaseOwnershipMismatchError,
    NoActiveLeaseError,
    OwnershipChangedError,
    OwnershipConflictError,
    OwnershipError,
    SameSessionTakeoverError,
    StaleLeaseError,
    TakeoverRequiredError,
    UnknownSessionError,
)
from .session import Session, SessionError, SessionOwnsActiveLeaseError
from .storage import StorageError
from .workflow import (
    LeavePartialError,
    TakeoverContextError,
    WorkflowCleanupError,
    WorkflowError,
    enter_workspace,
    heartbeat_workspace,
    inspect_workspace,
    leave_workspace,
    takeover_workspace,
)
from .workspace import WorkspaceError
from .context import DEFAULT_CONTEXT_MAX_CHARS, compact_handoff, validate_context_options

try:  # the MCP SDK is installed only with the "mcp" extra
    from mcp.server import MCPServer
    from mcp.types import CallToolResult, TextContent
except ImportError:  # no SDK, or an older major version without the v2 API
    MCPServer = None  # type: ignore[assignment,misc]
    CallToolResult = None  # type: ignore[assignment,misc]
    TextContent = None  # type: ignore[assignment,misc]

SERVER_NAME = "agent-workspace-sync"

# The MCP 2.0.x line validates a tool's return value against the declared output
# schema, which rejects the structured error result this server returns and
# replaces the domain error with a schema-validation message. 2.1.1 is the
# earliest line verified to pass the whole suite, so it is the floor.
SUPPORTED_MCP_RANGE = "mcp>=2.1.1,<3"

# Covers both "no SDK installed" and "an older incompatible SDK is installed",
# since an mcp 1.x or 2.0.x install cannot run this server.
MCP_UNAVAILABLE_MESSAGE = (
    f"MCP support requires {SUPPORTED_MCP_RANGE}.\n"
    "Install the package with the MCP extra: "
    'pip install "agent-workspace-sync[mcp]"'
)

SERVER_INSTRUCTIONS = """\
Agent Workspace Sync tracks which coding harness may develop a workspace, and
carries a handoff between harnesses. Identifier fields in results (session ids,
lease ids, handoff ids) are opaque; pass them back unchanged when a follow-up
tool needs them.

Map user intent to tools like this:
- "continue", "resume", "keep going", "接着做", "继续" -> workspace_inspect first;
  workspace_enter only with no owner, or heartbeat and reuse this exact instance's fresh owner
- "hand off", "stop for now", "I'm done", "交接一下", "我先走了" -> workspace_leave
- "what is the status", "who is using this", "现在什么状态", "谁在使用" -> workspace_inspect
- "take over", "接管" -> call workspace_inspect first, then workspace_takeover only
  when the user explicitly asks for it and the workspace shows a lapsed owner

workspace_heartbeat is internal lease maintenance for a harness that is still
working. It is not normally a user-initiated action.

Do not take ownership on your own initiative, and do not take over a workspace
just because a continue or resume request failed. Ordinary continue/resume
never authorizes release or takeover. Task completion, successful verification,
status questions, and idle time do not authorize workspace_leave. A stale current
owner needs explicit handoff or recovery intent that authorizes release before
leave-and-enter. A fresh foreign owner cannot be taken over.

When ordinary continue encounters a foreign owner, stop after reporting the
conflict. Do not call enter merely to demonstrate its refusal, scan files, run
shell commands, or open a blocking question card. For a fresh foreign owner,
tell the user to hand off in the original tool; never offer forced takeover.
For a stale foreign owner, explain that takeover requires confirmation that the
previous tool stopped modifying files and an explicit takeover request. Do not
infer that a tool is running or stopped from lease freshness or timestamps.

After acquisition, use the returned handoff's pending work and next steps;
verify relevant evidence before claiming previous work is intact. If no handoff
exists, say so. Record unrun verification as unrun, never as passing. Only report
handoff success after leave succeeds. On partial failure, inspect and retry with
the original session and lease identifiers; do not invent a new session to hide it.

For routine continuation, request context_mode="compact" to read a bounded
historical handoff. Its context_info labels inherited verification, omitted
fields, and whether a full read is required. If requires_full_context is true,
read workspace_inspect with context_mode="full" before development. An omitted
or missing pending_work field is unknown, not an empty backlog. Check current
files and tests; historical verification is not a new test execution.
"""

INSPECT_DESCRIPTION = (
    "Report the current state of a workspace: its identity, owning session, "
    "active lease with freshness, and the latest finalized handoff. Read-only. "
    "Use this when the user asks about workspace status, current ownership, "
    "whether another harness holds ownership, or whether explicit recovery may be necessary."
)

ENTER_DESCRIPTION = (
    "Create a session, take ownership of an already initialized workspace, and "
    "return the session, the lease, and the latest finalized handoff so work can "
    "continue. Use when the user wants to continue, resume, start working on, or "
    "enter a workspace with no owner. Inspect first; reuse this exact instance's "
    "fresh ownership with heartbeat instead of entering again. "
    "This never initializes a workspace and never takes over: "
    "if another harness holds a fresh lease, or a lapsed lease would have to be "
    "taken over explicitly, it fails safely. Report a fresh ownership conflict "
    "and direct the user to hand off in the original tool. For stale ownership, "
    "require confirmation that the previous tool stopped modifying files and "
    "an explicit takeover request; ordinary continue is not that request."
)

HEARTBEAT_DESCRIPTION = (
    "Maintenance tool: extend the lease of a session that is still working, so "
    "ownership does not lapse while work continues. This is not a user-facing "
    "action; a harness calls it periodically for its own lease. It cannot revive "
    "a lapsed or released lease, and it never changes who owns the workspace."
)

LEAVE_DESCRIPTION = (
    "Finalize the workspace handoff, release ownership, and end the session, so "
    "another coding harness can pick the work up. Use when the user wants to "
    "hand off, stop working, leave the workspace, or prepare the project for "
    "another harness. Safe to retry: an already finalized handoff is reused "
    "rather than rewritten. Task completion or idle time alone never authorizes "
    "this operation. Report success only after the operation succeeds."
)

TAKEOVER_DESCRIPTION = (
    "Retire a lapsed active lease, become the new owner, and return the latest "
    "finalized handoff. Use only when the user explicitly wants to take over "
    "stale workspace ownership, after inspecting the workspace and passing the "
    "lease id observed there; if ownership changed in the meantime the call "
    "fails instead of retiring a newer owner. The returned context may be "
    "incomplete, because the previous owner did not finish a normal handover. Do "
    "not use this for ordinary continue or resume intent."
)


class WorkspaceRef(TypedDict):
    """The workspace's authoritative identity and current location."""

    status: str
    workspace_id: str
    workspace_root: str


class SessionPayload(TypedDict):
    """One development session."""

    session_id: str
    harness_type: str
    harness_instance_id: str
    started_at: str
    ended_at: str | None


class LeasePayload(TypedDict):
    """One ownership lease."""

    lease_id: str
    session_id: str
    status: str
    acquired_at: str
    last_heartbeat_at: str
    expires_at: str


class _HandoffPayload(TypedDict):
    """One finalized handoff."""

    handoff_id: str
    from_session_id: str
    created_at: str
    semantic_context: dict
    objective_evidence: dict


class HandoffPayload(_HandoffPayload, total=False):
    """Full legacy payload, or a compact view with explicit coverage metadata."""

    context_info: dict


class InspectPayload(TypedDict):
    """Result of ``workspace_inspect``."""

    status: str
    workspace_id: str
    workspace_root: str
    owner_session: SessionPayload | None
    active_lease: LeasePayload | None
    lease_freshness: str | None
    latest_handoff: HandoffPayload | None


class EnterPayload(TypedDict):
    """Result of ``workspace_enter``."""

    status: str
    workspace_id: str
    workspace_root: str
    session: SessionPayload
    lease: LeasePayload
    latest_handoff: HandoffPayload | None


class HeartbeatPayload(TypedDict):
    """Result of ``workspace_heartbeat``."""

    status: str
    lease: LeasePayload


class LeavePayload(TypedDict):
    """Result of ``workspace_leave``."""

    status: str
    handoff: HandoffPayload
    handoff_was_existing: bool
    released_lease: LeasePayload
    ended_session: SessionPayload


class TakeoverPayload(TypedDict):
    """Result of ``workspace_takeover``."""

    status: str
    workspace_id: str
    workspace_root: str
    session: SessionPayload
    lease: LeasePayload
    previous_lease_id: str
    latest_handoff: HandoffPayload | None
    recovery_context_may_be_incomplete: bool


# Only this project's own error hierarchies become tool errors. Anything else is
# a bug and must surface as a crash rather than a polite message.
_DOMAIN_ERRORS = (
    WorkspaceError,
    SessionError,
    OwnershipError,
    HandoffError,
    WorkflowError,
    StorageError,
)

# Ordered most specific first; the first match supplies the next-step hint.
_HINTS: tuple[tuple[type[BaseException], str], ...] = (
    (
        LeavePartialError,
        "The handoff is saved, but the leave workflow did not finish. Do not "
        "tell another harness it can start. Call workspace_inspect, then retry "
        "workspace_leave with the same session and lease identifiers; the saved "
        "handoff will be reused.",
    ),
    (
        TakeoverContextError,
        "The takeover already succeeded and the new session and lease remain "
        "active; continue with the returned identifiers instead of retrying.",
    ),
    (
        OwnershipConflictError,
        "Another session holds a fresh lease. Call workspace_inspect to see who "
        "owns the workspace. A fresh owner cannot be taken over; wait or ask "
        "the user to hand off from the owning tool.",
    ),
    (
        TakeoverRequiredError,
        "The active lease has lapsed. Call workspace_inspect, then "
        "workspace_takeover with the lease id observed there if the user asks to "
        "take over.",
    ),
    (
        OwnershipChangedError,
        "Ownership changed since it was observed. Call workspace_inspect again "
        "and confirm the intended recovery with the user; do not ask them to "
        "copy internal identifiers.",
    ),
    (
        SameSessionTakeoverError,
        "A session cannot take over its own lease. Ordinary continue does not "
        "authorize release; get explicit handoff or recovery intent authorizing "
        "release before workspace_leave and workspace_enter.",
    ),
    (
        LeaseOwnershipMismatchError,
        "This lease belongs to a different session; use the identifiers returned "
        "by workspace_enter.",
    ),
    (
        StaleLeaseError,
        "The lease has lapsed, so it cannot be refreshed. Call workspace_inspect "
        "and identify the owner. Recovering current ownership requires explicit "
        "release intent; replacing a foreign stale owner requires explicit takeover.",
    ),
    (
        LeaseNotActiveError,
        "This lease is no longer active; call workspace_inspect for the current "
        "state.",
    ),
    (
        LeaseNotFoundError,
        "Unknown lease for this workspace; use the identifiers from "
        "workspace_enter.",
    ),
    (
        NoActiveLeaseError,
        "There is nothing to take over: the workspace has no active lease. Use "
        "workspace_enter instead.",
    ),
    (
        SessionOwnsActiveLeaseError,
        "Release the lease or hand it over before ending the session.",
    ),
    (
        EndedSessionError,
        "This session has ended; start a new one with workspace_enter.",
    ),
    (
        UnknownSessionError,
        "Unknown session for this workspace; use the identifiers from "
        "workspace_enter.",
    ),
    (
        HandoffAlreadyFinalizedError,
        "A handoff is already finalized for this session; it is never rewritten.",
    ),
    (
        HandoffNotAuthorizedError,
        "Only the session that currently owns the workspace may finalize a "
        "handoff; call workspace_inspect first.",
    ),
)


def workspace_inspect(
    workspace_path: str,
    context_mode: str = "full",
    context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> InspectPayload:
    """Read-only: report identity, ownership, freshness, and latest handoff."""
    try:
        validate_context_options(context_mode, context_max_chars)
        view = inspect_workspace(workspace_path)
    except _DOMAIN_ERRORS as error:
        return _error_result(error)

    return {
        "status": "ok",
        "workspace_id": view.workspace_id,
        "workspace_root": str(view.workspace_root),
        "owner_session": _session_payload(view.owner_session),
        "active_lease": _lease_payload(view.lease),
        "lease_freshness": view.lease_freshness,
        "latest_handoff": _handoff_payload(view.latest_handoff, context_mode, context_max_chars),
    }


def workspace_enter(
    workspace_path: str,
    harness_type: str,
    ttl_seconds: int,
    harness_instance_id: str | None = None,
    context_mode: str = "full",
    context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> EnterPayload:
    """Create a session, take ownership, and return the incoming context."""
    try:
        validate_context_options(context_mode, context_max_chars)
        result = enter_workspace(
            workspace_path,
            harness_type,
            ttl_seconds,
            harness_instance_id=harness_instance_id,
        )
    except _DOMAIN_ERRORS as error:
        return _error_result(error)

    return {
        "status": "ok",
        "workspace_id": result.workspace_id,
        "workspace_root": str(result.workspace_root),
        "session": _session_payload(result.session),
        "lease": _lease_payload(result.lease),
        "latest_handoff": _handoff_payload(result.latest_handoff, context_mode, context_max_chars),
    }


def workspace_heartbeat(
    workspace_path: str,
    session_id: str,
    lease_id: str,
    ttl_seconds: int,
) -> HeartbeatPayload:
    """Lease maintenance: extend the caller's own lease."""
    try:
        lease = heartbeat_workspace(workspace_path, session_id, lease_id, ttl_seconds)
    except _DOMAIN_ERRORS as error:
        return _error_result(error)

    return {"status": "ok", "lease": _lease_payload(lease)}


def workspace_leave(
    workspace_path: str,
    session_id: str,
    lease_id: str,
    semantic_context: dict,
    objective_evidence: dict,
    context_mode: str = "full",
    context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> LeavePayload:
    """Finalize the handoff, release ownership, and end the session."""
    try:
        validate_context_options(context_mode, context_max_chars)
        result = leave_workspace(
            workspace_path,
            session_id,
            lease_id,
            semantic_context,
            objective_evidence,
        )
    except _DOMAIN_ERRORS as error:
        return _error_result(error)

    return {
        "status": "ok",
        "handoff": _handoff_payload(result.handoff, context_mode, context_max_chars),
        "handoff_was_existing": result.handoff_was_existing,
        "released_lease": _lease_payload(result.lease),
        "ended_session": _session_payload(result.session),
    }


def workspace_takeover(
    workspace_path: str,
    harness_type: str,
    expected_previous_lease_id: str,
    ttl_seconds: int,
    harness_instance_id: str | None = None,
    context_mode: str = "full",
    context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> TakeoverPayload:
    """Replace a lapsed owner, then return the incoming context."""
    try:
        validate_context_options(context_mode, context_max_chars)
        result = takeover_workspace(
            workspace_path,
            harness_type,
            expected_previous_lease_id,
            ttl_seconds,
            harness_instance_id=harness_instance_id,
        )
    except _DOMAIN_ERRORS as error:
        return _error_result(error)

    return {
        "status": "ok",
        "workspace_id": result.workspace_id,
        "workspace_root": str(result.workspace_root),
        "session": _session_payload(result.session),
        "lease": _lease_payload(result.lease),
        "previous_lease_id": result.previous_lease_id,
        "latest_handoff": _handoff_payload(result.latest_handoff, context_mode, context_max_chars),
        "recovery_context_may_be_incomplete": result.recovery_context_may_be_incomplete,
    }


def create_server() -> Any:
    """Build the MCP server with the five workspace workflow tools.

    Each success payload is a typed mapping, so the tool schema advertises its
    structured output; failures come back as tool errors carrying the error type,
    the message, and a next-step hint.
    """
    if MCPServer is None:
        raise MCPUnavailableError(MCP_UNAVAILABLE_MESSAGE)

    server = MCPServer(
        name=SERVER_NAME,
        version=_package_version(),
        instructions=SERVER_INSTRUCTIONS,
    )
    server.add_tool(workspace_inspect, name="workspace_inspect", description=INSPECT_DESCRIPTION)
    server.add_tool(workspace_enter, name="workspace_enter", description=ENTER_DESCRIPTION)
    server.add_tool(
        workspace_heartbeat, name="workspace_heartbeat", description=HEARTBEAT_DESCRIPTION
    )
    server.add_tool(workspace_leave, name="workspace_leave", description=LEAVE_DESCRIPTION)
    server.add_tool(workspace_takeover, name="workspace_takeover", description=TAKEOVER_DESCRIPTION)
    return server


def main() -> int:
    """Run the MCP server over stdio. Returns a process exit code."""
    if MCPServer is None:
        print(MCP_UNAVAILABLE_MESSAGE, file=sys.stderr)
        return 1

    create_server().run(transport="stdio")
    return 0


class MCPUnavailableError(RuntimeError):
    """Raised when the MCP SDK is not available in this interpreter."""


def _session_payload(session: Session | None) -> SessionPayload | None:
    if session is None:
        return None
    return {
        "session_id": session.session_id,
        "harness_type": session.harness_type,
        "harness_instance_id": session.harness_instance_id,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
    }


def _lease_payload(lease: Lease | None) -> LeasePayload | None:
    if lease is None:
        return None
    return {
        "lease_id": lease.lease_id,
        "session_id": lease.session_id,
        "status": lease.status,
        "acquired_at": lease.acquired_at,
        "last_heartbeat_at": lease.last_heartbeat_at,
        "expires_at": lease.expires_at,
    }


def _handoff_payload(
    handoff: Handoff | None,
    context_mode: str = "full",
    context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
) -> HandoffPayload | None:
    if handoff is None:
        return None
    if context_mode == "compact":
        return compact_handoff(handoff, context_max_chars)
    return {
        "handoff_id": handoff.handoff_id,
        "from_session_id": handoff.from_session_id,
        "created_at": handoff.created_at,
        "semantic_context": handoff.semantic_context,
        "objective_evidence": handoff.objective_evidence,
    }


def _error_result(error: BaseException) -> Any:
    """Convert a known domain failure into a tool error carrying its detail."""
    payload = {
        "status": "error",
        "error_type": type(error).__name__,
        "message": str(error),
        "hint": _hint_for(error),
        "details": _details_for(error),
    }
    return CallToolResult(
        content=[TextContent(type="text", text=f"{type(error).__name__}: {error}")],
        structuredContent=payload,
        isError=True,
    )


def _hint_for(error: BaseException) -> str | None:
    for error_type, hint in _HINTS:
        if isinstance(error, error_type):
            return hint
    return None


def _details_for(error: BaseException) -> dict:
    """Extra machine-readable context for the failures that need it."""
    if isinstance(error, LeavePartialError):
        return {
            "failed_step": error.failed_step,
            "handoff_id": error.handoff_id,
            "session_id": error.session_id,
            "lease_id": error.lease_id,
            "release_confirmed": error.release_confirmed,
            "original_error_type": type(error.original_error).__name__,
        }
    if isinstance(error, TakeoverContextError):
        # the takeover stands, so hand back what the caller now owns
        return {"session_id": error.session_id, "lease_id": error.lease_id}
    if isinstance(error, WorkflowCleanupError):
        return {
            "original_error": repr(error.original_error),
            "cleanup_errors": [repr(item) for item in error.cleanup_errors],
        }
    if isinstance(error, TakeoverRequiredError):
        return {"active_lease_id": error.lease.lease_id, "active_session_id": error.lease.session_id}
    if isinstance(error, StaleLeaseError):
        return {"lease_id": error.lease.lease_id}
    return {}


def _package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.8+
        return "0.0.0+unknown"

    try:
        return version(SERVER_NAME)
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0+unknown"


if __name__ == "__main__":
    sys.exit(main())
