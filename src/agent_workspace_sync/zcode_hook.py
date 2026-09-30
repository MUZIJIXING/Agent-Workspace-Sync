"""ZCode hook adapter for Agent Workspace Sync.

One executable serves two hooks, selected by the first argument:

``session-start``
    Injects this ZCode session's Agent Workspace Sync identity as context. It
    acquires nothing: opening a ZCode session is not taking ownership of a
    workspace, and this hook never initializes or takes over anything.

``guard-write``
    The PreToolUse ownership guard for ZCode's ``Write``, ``Edit``, and ``Bash``
    tools. It asks :func:`~agent_workspace_sync.workflow.inspect_workspace` who
    owns the workspace and decides from that answer. It never reads the
    ownership tables, never judges lease timestamps itself, and never performs
    workspace discovery on its own.

    Write and Edit are enforced against the **target** workspace: the workspace
    containing ``tool_input.file_path``, because that is what the tool actually
    mutates. Bash is enforced against the **hook cwd** workspace, because a
    shell command's real mutation targets cannot be identified safely.

Safety direction — a managed workspace fails closed. If ownership cannot be
verified (no owner, another owner, a lapsed lease, an identity mismatch, a
storage problem, an unreadable payload, or an unverifiable write target) the
tool call is denied. A directory that is not an Agent Workspace Sync workspace
is allowed with a note, because the plugin must not lock down projects that
never opted in.

stdout carries only the hook JSON contract; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from .guard_policy import (
    GuardDecision,
    GuardFreshness,
    GuardIdentity,
    WorkspaceGuardState,
    decide_workspace_access,
)
from .handoff import HandoffError
from .lease_maintenance import refresh_for_activity
from .ownership import OwnershipError
from .session import SessionError
from .storage import StorageError
from .workflow import FRESHNESS_FRESH, WorkflowError, inspect_workspace
from .workspace import WorkspaceError, WorkspaceNotInitializedError

SESSION_START_MODE = "session-start"
GUARD_WRITE_MODE = "guard-write"
MODES = (SESSION_START_MODE, GUARD_WRITE_MODE)

# A ZCode session is one harness instance: the ZCode session id *is* the Agent
# Workspace Sync harness_instance_id, so the guard can compare the session that
# is really making the tool call with the recorded owner.
HARNESS_TYPE = "zcode"

# Only these ZCode tools are guarded in this first version.
PROTECTED_TOOLS = frozenset({"Write", "Edit", "Bash"})

# Write and Edit mutate the file named by tool_input.file_path, so they are
# enforced against that file's workspace rather than the hook's cwd.
WRITE_EDIT_TOOLS = frozenset({"Write", "Edit"})

UNMANAGED_NOTE = (
    "Agent Workspace Sync is not initialized for this workspace. "
    "Coordination protection is inactive."
)

CANNOT_VERIFY_TARGET = (
    "Cannot verify the target path for this write operation. "
    "Agent Workspace Sync denies writes whose target workspace it cannot determine."
)

# Reasons are the machine-facing recovery contract: they must be accurate for
# read-only commands too (Bash is guarded as a whole), name the next call, and
# never suggest a shortcut that the ownership model does not allow.

NO_OWNER_REASON = (
    "This managed workspace has no active Agent Workspace Sync owner, so ZCode's "
    "Write, Edit, and Bash tools are blocked here - including read-only shell "
    "commands such as `git status` or `ls`. Call workspace_inspect, then "
    "workspace_enter with harness_type \"zcode\" and harness_instance_id set to "
    "this ZCode session id, and retry. Do not take over unless the user "
    "explicitly requests it."
)

FOREIGN_FRESH_OWNER_REASON = (
    "Another harness/session owns this workspace and its lease is still fresh. "
    "Do not enter and do not take over: a fresh owner can never be taken over. "
    "Do not take over unless the user explicitly requests it. Report the "
    "ownership conflict and stop."
)

FOREIGN_STALE_OWNER_REASON = (
    "Another harness/session owns this workspace and its lease has lapsed. "
    "Ordinary workspace_enter is still refused while that lease row remains "
    "active, and it does not expire by itself. Do not take over unless the user "
    "explicitly requests it: only then call workspace_inspect and "
    "workspace_takeover with the lease id observed there."
)

STALE_OWNER_REASON = (
    "This ZCode session owns the workspace, but its lease has lapsed, so its "
    "Write, Edit, and Bash tools are blocked. A lapsed lease cannot be refreshed "
    "with workspace_heartbeat, and a session cannot take over from itself. "
    "Ordinary continue does not authorize release. Only after the user explicitly "
    "requests handoff or recovery that releases this ownership, use workspace_leave "
    "and, if continued work was requested, workspace_enter."
)

def recovery_context(session_id: str) -> str:
    """Machine-facing identity and recovery hint attached to a denial."""
    return (
        "For Agent Workspace Sync, this ZCode session is: "
        f'harness_type = "{HARNESS_TYPE}", harness_instance_id = "{session_id}". '
        "Recover with workspace_inspect followed by workspace_enter using those "
        "values. Use workspace_takeover only when the user explicitly asks to "
        "take over a lapsed owner. Do not show these identifiers to the user "
        "unless they ask for diagnostics."
    )

# Only the project's own error hierarchies are treated as verification failures
# that a guard can report; anything else is a bug, and is logged to stderr
# before the call is denied.
_VERIFICATION_ERRORS = (
    WorkspaceError,
    SessionError,
    OwnershipError,
    HandoffError,
    WorkflowError,
    StorageError,
)


def session_context(session_id: str) -> str:
    """The context injected into a ZCode session when it starts."""
    return (
        f"You are ZCode session {session_id}.\n"
        "\n"
        "For Agent Workspace Sync:\n"
        f'harness_type = "{HARNESS_TYPE}"\n'
        f'harness_instance_id = "{session_id}"\n'
        "\n"
        "Before modifying an initialized managed workspace, ensure this ZCode "
        "session owns a fresh workspace lease. Use the Agent Workspace Sync MCP "
        "workflow tools (workspace_inspect, workspace_enter, "
        "workspace_heartbeat, workspace_leave, workspace_takeover). Never "
        "automatically take over another owner: an explicit takeover requires "
        "the user to ask for it.\n"
        "\n"
        "Ordinary task completion is NOT a handoff trigger. Successful "
        "verification is NOT a handoff trigger. No pending work is NOT a "
        "handoff trigger. After ordinary successful work, KEEP the current "
        "workspace ownership. NEVER call workspace_leave merely because the "
        "requested task is complete. Only release or hand off when the CURRENT "
        "USER MESSAGE explicitly requests handoff, release, or leaving the "
        "workspace. Do not proactively clean up ownership for another harness.\n"
        "\n"
        "For ordinary successful user-facing replies, do not expose internal "
        "session, lease, handoff, or workspace IDs, lease expiry, or MCP or "
        "ownership-tool protocol narration. Report only the product-level work "
        "result."
    )


def session_start_result(payload: dict) -> dict:
    """Build the SessionStart hook result. It only contributes context."""
    session_id = str(payload.get("session_id") or "")
    return _hook_output("SessionStart", additional_context=session_context(session_id))


def guard_result(payload: dict) -> dict | None:
    """Decide one PreToolUse call.

    Returns the hook JSON to emit, or ``None`` to pass the call through with no
    effect at all.

    ``Write`` and ``Edit`` are judged by the workspace that holds the file they
    are about to change; ``Bash`` is judged by the workspace of the hook cwd,
    because a shell command's real mutation targets cannot be identified safely.
    """
    tool_name = payload.get("tool_name")
    if tool_name not in PROTECTED_TOOLS:
        return None

    session_id = str(payload.get("session_id") or "")
    working_directory = payload.get("cwd") or os.getcwd()

    if tool_name in WRITE_EDIT_TOOLS:
        return _guard_write_target(payload, working_directory, session_id)
    return _guard_cwd_workspace(working_directory, session_id)


def _guard_write_target(payload: dict, working_directory, session_id: str) -> dict:
    """Judge a Write/Edit by the workspace that owns the file being written.

    The conversation's working directory is not the mutation target: a write
    launched from an unmanaged directory into a managed workspace is still
    governed by that managed workspace, and vice versa.
    """
    target, problem = _resolve_write_target(payload, working_directory)
    if problem is not None:
        return _deny(problem, session_id=session_id)

    view, decision = _inspect_for_guard(target.parent, session_id)
    if decision is not None:
        return decision
    return _decide(view, session_id)


def _guard_cwd_workspace(working_directory, session_id: str) -> dict | None:
    """Judge a Bash call by the workspace of the hook cwd."""
    view, decision = _inspect_for_guard(working_directory, session_id)
    if decision is not None:
        return decision
    return _decide(view, session_id)


def _inspect_for_guard(start, session_id: str = "") -> tuple[object | None, dict | None]:
    """Inspect a workspace, returning either a view or the decision to emit."""
    try:
        return inspect_workspace(start), None
    except WorkspaceNotInitializedError:
        # Not an Agent Workspace Sync workspace: never lock down a project that
        # never opted in. This is a different case from a failed verification.
        return None, _hook_output("PreToolUse", additional_context=UNMANAGED_NOTE)
    except _VERIFICATION_ERRORS as error:
        return None, _deny(_unverifiable_reason(error), session_id=session_id)
    except Exception as error:  # noqa: BLE001
        # Fail closed: a guard must never pass a write it cannot justify. The
        # traceback still goes to stderr, so a bug stays visible instead of
        # being silently converted into a policy decision.
        traceback.print_exc(file=sys.stderr)
        return None, _deny(_unverifiable_reason(error), session_id=session_id)


def _decide(view, session_id: str) -> dict | None:
    """Translate the shared ownership decision into ZCode's hook contract."""
    decision = decide_workspace_access(
        GuardIdentity(HARNESS_TYPE, session_id),
        _guard_state(view),
    )
    reasons = {
        GuardDecision.NO_OWNER: NO_OWNER_REASON,
        GuardDecision.FOREIGN_FRESH_OWNER: FOREIGN_FRESH_OWNER_REASON,
        GuardDecision.FOREIGN_STALE_OWNER: FOREIGN_STALE_OWNER_REASON,
        GuardDecision.CURRENT_OWNER_STALE: STALE_OWNER_REASON,
        # Preserve the pre-extraction ZCode response for any incomplete
        # owner/lease snapshot while the shared policy still classifies it
        # explicitly and fails closed.
        GuardDecision.INVALID_STATE: NO_OWNER_REASON,
    }
    if decision is GuardDecision.ALLOW:
        try:
            refresh_for_activity(view, HARNESS_TYPE, session_id)
        except _VERIFICATION_ERRORS as error:
            return _deny(_unverifiable_reason(error), session_id=session_id)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _deny(_unverifiable_reason(error), session_id=session_id)
        return None
    return _deny(reasons[decision], session_id=session_id)


def _guard_state(view) -> WorkspaceGuardState:
    """Normalize a workflow inspection without leaking it into shared policy."""
    owner = view.owner_session
    # The previous ZCode guard treated every non-fresh value as stale. Keep
    # that adapter behavior unchanged; the shared model itself rejects a
    # missing/unknown normalized freshness value as invalid.
    freshness = (
        GuardFreshness.FRESH
        if view.lease_freshness == FRESHNESS_FRESH
        else GuardFreshness.STALE
    )
    return WorkspaceGuardState(
        managed=True,
        owner_session_exists=owner is not None,
        active_lease_exists=view.lease is not None,
        owner_harness_type=owner.harness_type if owner is not None else None,
        owner_harness_instance_id=owner.harness_instance_id if owner is not None else None,
        owner_freshness=freshness,
    )


def _resolve_write_target(payload: dict, working_directory) -> tuple[object | None, str | None]:
    """Resolve the file a Write/Edit will change, or explain why it cannot be.

    A relative path is taken against the hook cwd; an absolute path is used as
    given. Anything the guard cannot resolve with confidence is refused, because
    an unverifiable target is exactly what this guard exists to catch.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, CANNOT_VERIFY_TARGET

    raw_path = tool_input.get("file_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, CANNOT_VERIFY_TARGET

    try:
        target = Path(raw_path)
        if not target.is_absolute():
            target = Path(working_directory) / target
        return target.resolve(strict=False), None
    except (OSError, RuntimeError, ValueError):
        return None, CANNOT_VERIFY_TARGET


def main(argv: list[str] | None = None) -> int:
    """Run one hook invocation and return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="agent-workspace-zcode-hook",
        description="ZCode hook adapter for Agent Workspace Sync.",
    )
    parser.add_argument("mode", choices=MODES, help="which hook is being served")
    arguments = parser.parse_args(argv)

    payload, problem = _read_payload()

    if arguments.mode == SESSION_START_MODE:
        # This hook has no protection duty, so unreadable input simply means no
        # context is contributed.
        if problem is None:
            _emit(session_start_result(payload))
        else:
            print(f"agent-workspace-sync: {problem}", file=sys.stderr)
        return 0

    if problem is not None:
        # The guard cannot verify ownership without its input, so it denies.
        _emit(_deny(f"Agent Workspace Sync could not verify ownership: {problem}"))
        return 0

    _emit(guard_result(payload))
    return 0


def _read_payload() -> tuple[dict, str | None]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}, "the hook received no input on stdin"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        return {}, f"the hook received unreadable JSON ({error})"

    if not isinstance(parsed, dict):
        return {}, f"the hook received a {type(parsed).__name__} instead of an object"
    return parsed, None


def _emit(result: dict | None) -> None:
    """Write the hook result as JSON, or nothing at all to pass silently."""
    if result is None:
        return
    print(json.dumps(result, ensure_ascii=False))


def _hook_output(event_name: str, *, additional_context: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": additional_context,
        }
    }


def _deny(reason: str, *, session_id: str = "") -> dict:
    """The deny shape ZCode itself produces for a blocked PreToolUse hook.

    ``additionalContext`` carries the identity mapping and the recovery call for
    the model; the reason repeats the actionable part, because that is the field
    a blocked call reliably surfaces.
    """
    hook_output = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    if session_id:
        hook_output["additionalContext"] = recovery_context(session_id)
    return {
        "continue": False,
        "reason": reason,
        "hookSpecificOutput": hook_output,
    }


def _unverifiable_reason(error: BaseException) -> str:
    return (
        "Agent Workspace Sync could not verify workspace ownership "
        f"({type(error).__name__}: {error}). Inspect the workspace before "
        "modifying it."
    )


if __name__ == "__main__":
    sys.exit(main())
