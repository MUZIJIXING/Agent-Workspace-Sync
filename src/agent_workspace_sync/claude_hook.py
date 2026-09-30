"""Claude Code hook adapter for Agent Workspace Sync."""

from __future__ import annotations

import argparse
import json
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
from .workflow import FRESHNESS_FRESH, FRESHNESS_STALE, WorkflowError, inspect_workspace
from .workspace import WorkspaceError, WorkspaceNotInitializedError


SESSION_START_MODE = "session-start"
GUARD_PRE_TOOL_MODE = "guard-pre-tool"
MODES = (SESSION_START_MODE, GUARD_PRE_TOOL_MODE)
HARNESS_TYPE = "claude-code"
PROTECTED_TOOLS = frozenset(
    {"Write", "Edit", "NotebookEdit", "Bash", "PowerShell", "EnterWorktree", "ExitWorktree", "Task"}
)
FILE_TARGET_FIELDS = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}

CANNOT_VERIFY_TARGET = (
    "Cannot verify the target path for this operation. Agent Workspace Sync "
    "denies mutations whose target workspace it cannot determine."
)
NO_OWNER_REASON = (
    "This managed workspace has no active owner, so Write, Edit, NotebookEdit, "
    "Bash, PowerShell, and other guarded mutations are blocked. First inspect the workspace, "
    "then enter it with this Claude Code instance. Do not take over unless the "
    "user explicitly requests it."
)
FOREIGN_FRESH_OWNER_REASON = (
    "Another harness instance owns this workspace. A fresh owner cannot be taken "
    "over. Do not enter and Do not take over; report the ownership conflict."
)
FOREIGN_STALE_OWNER_REASON = (
    "Another owner's status remains active even though stale, so ordinary enter "
    "is still refused. Stale ownership does not disappear automatically; takeover "
    "requires explicit user intent."
)
CURRENT_OWNER_STALE_REASON = (
    "This Claude Code instance owns the workspace, but its stale ownership cannot "
    "be revived with heartbeat. Ordinary continue does not authorize release. "
    "Only after the user explicitly requests handoff or recovery that releases "
    "this ownership, use workspace_leave and, if continued work was requested, "
    "workspace_enter."
)
INVALID_STATE_REASON = (
    "Agent Workspace Sync found incomplete or inconsistent ownership state and "
    "failed closed. Inspect the workspace before modifying it."
)
WORKTREE_ESCAPE_REASON = (
    "Creating a Claude worktree from this managed workspace would create a "
    "worktree protection escape because the new checkout does not inherit the "
    "workspace identity. Continue in the current workspace instead."
)

_VERIFICATION_ERRORS = (
    WorkspaceError,
    SessionError,
    OwnershipError,
    HandoffError,
    WorkflowError,
    StorageError,
)


def session_context(session_id: str) -> str:
    """Context injected for one real Claude Code session."""
    return (
        "This Claude Code session participates in Agent Workspace Sync.\n"
        f'harness_type = "{HARNESS_TYPE}"\n'
        f'harness_instance_id = "{session_id}"\n'
        "Claude session_id is not an Agent Workspace Sync session_id.\n\n"
        "Use the Agent Workspace Sync MCP workflow for natural-language requests "
        "to continue, hand off, inspect, or explicitly take over this workspace. "
        "A managed workspace may be modified only while this exact Claude Code "
        "instance is its fresh owner. Never automatically take over another "
        "owner; stale ownership requires explicit user intent. Ordinary users "
        "do not need session, lease, handoff, workspace, MCP, or internal protocol "
        "details. Hide those details in ordinary successful final replies. "
        "Task completion, successful verification, status questions, and idle time "
        "do not authorize workspace_leave. Keep ownership until an explicit user "
        "handoff, release, or recovery request that authorizes release."
    )


def session_start_result(payload: dict) -> dict | None:
    """Return SessionStart context, or ``None`` for an invalid payload."""
    if not _valid_session_start(payload):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": session_context(payload["session_id"]),
        }
    }


def guard_result(payload: dict) -> dict | None:
    """Return a Claude PreToolUse denial, or ``None`` to allow the call."""
    tool_name = payload.get("tool_name")
    if tool_name not in PROTECTED_TOOLS:
        return None
    if payload.get("hook_event_name") != "PreToolUse":
        return _deny(
            "Agent Workspace Sync received an invalid PreToolUse payload.",
            recovery_context(_session_id(payload)),
        )

    session_id = _session_id(payload)
    cwd = payload.get("cwd")
    if tool_name in FILE_TARGET_FIELDS:
        target = _resolve_target(payload, cwd, FILE_TARGET_FIELDS[tool_name])
        if target is None:
            return _deny(CANNOT_VERIFY_TARGET, recovery_context(session_id))
        return _guard_workspace(target.parent, session_id)

    if tool_name in {"Bash", "PowerShell"}:
        if not isinstance(cwd, str) or not cwd.strip():
            return _deny(INVALID_STATE_REASON, recovery_context(session_id))
        return _guard_workspace(cwd, session_id)

    if tool_name == "EnterWorktree":
        return _guard_worktree_creation(cwd, session_id)

    if tool_name == "Task":
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return _deny(INVALID_STATE_REASON, recovery_context(session_id))
        if tool_input.get("isolation") != "worktree":
            return None
        return _guard_worktree_creation(cwd, session_id)

    if tool_name == "ExitWorktree":
        if not isinstance(cwd, str) or not cwd.strip():
            return _deny(INVALID_STATE_REASON, recovery_context(session_id))
        return _guard_workspace(cwd, session_id)

    # Defensive fallback if the protected-tool set and dispatch ever diverge.
    return _deny(INVALID_STATE_REASON, recovery_context(session_id))


def recovery_context(session_id: str) -> str:
    identity = (
        f'harness_type = "{HARNESS_TYPE}", harness_instance_id = "{session_id}". '
        if session_id
        else "The Claude Code session identity is missing. "
    )
    return (
        identity
        + "Follow the recovery action in the denial reason. Never copy internal "
        "identifiers, MCP tool names, lease details, or protocol steps into an "
        "ordinary final reply."
    )


def _resolve_target(payload: dict, cwd: object, field: str) -> Path | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw_target = tool_input.get(field)
    if not isinstance(raw_target, str) or not raw_target.strip():
        return None
    try:
        target = Path(raw_target)
        if not target.is_absolute():
            if not isinstance(cwd, str) or not cwd.strip():
                return None
            target = Path(cwd) / target
        return target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _guard_workspace(start: Path | str, session_id: str) -> dict | None:
    try:
        view = inspect_workspace(start)
    except WorkspaceNotInitializedError:
        return None
    except _VERIFICATION_ERRORS as error:
        return _deny(_unverifiable_reason(error), recovery_context(session_id))
    except Exception as error:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _deny(_unverifiable_reason(error), recovery_context(session_id))

    owner = view.owner_session
    freshness = {
        FRESHNESS_FRESH: GuardFreshness.FRESH,
        FRESHNESS_STALE: GuardFreshness.STALE,
    }.get(view.lease_freshness)
    decision = decide_workspace_access(
        GuardIdentity(HARNESS_TYPE, session_id),
        WorkspaceGuardState(
            managed=True,
            owner_session_exists=owner is not None,
            active_lease_exists=view.lease is not None,
            owner_harness_type=owner.harness_type if owner is not None else None,
            owner_harness_instance_id=(
                owner.harness_instance_id if owner is not None else None
            ),
            owner_freshness=freshness,
        ),
    )
    if decision is GuardDecision.ALLOW:
        try:
            refresh_for_activity(view, HARNESS_TYPE, session_id)
        except _VERIFICATION_ERRORS as error:
            return _deny(_unverifiable_reason(error), recovery_context(session_id))
        except Exception as error:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _deny(_unverifiable_reason(error), recovery_context(session_id))
        return None
    reasons = {
        GuardDecision.NO_OWNER: NO_OWNER_REASON,
        GuardDecision.FOREIGN_FRESH_OWNER: FOREIGN_FRESH_OWNER_REASON,
        GuardDecision.FOREIGN_STALE_OWNER: FOREIGN_STALE_OWNER_REASON,
        GuardDecision.CURRENT_OWNER_STALE: CURRENT_OWNER_STALE_REASON,
        GuardDecision.INVALID_STATE: INVALID_STATE_REASON,
    }
    return _deny(reasons[decision], recovery_context(session_id))


def _guard_worktree_creation(cwd: object, session_id: str) -> dict | None:
    """Block a worktree escape from managed cwd; allow unmanaged cwd."""
    if not isinstance(cwd, str) or not cwd.strip():
        return _deny(INVALID_STATE_REASON, recovery_context(session_id))
    try:
        inspect_workspace(cwd)
    except WorkspaceNotInitializedError:
        return None
    except _VERIFICATION_ERRORS as error:
        return _deny(_unverifiable_reason(error), recovery_context(session_id))
    except Exception as error:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _deny(_unverifiable_reason(error), recovery_context(session_id))
    return _deny(WORKTREE_ESCAPE_REASON, recovery_context(session_id))


def _session_id(payload: dict) -> str:
    value = payload.get("session_id")
    return value if isinstance(value, str) and value.strip() else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Workspace Sync Claude Code hooks.")
    parser.add_argument("mode", choices=MODES)
    mode = parser.parse_args(argv).mode
    payload, problem = _read_payload()

    if mode == SESSION_START_MODE:
        result = None if problem is not None else session_start_result(payload)
        if result is None:
            detail = problem or "required fields are missing or malformed"
            print(
                f"agent-workspace-sync: invalid SessionStart payload ({detail})",
                file=sys.stderr,
            )
        else:
            _emit(result)
        return 0

    if problem is not None:
        _emit(
            _deny(
                f"Agent Workspace Sync could not verify ownership ({problem}).",
                recovery_context(""),
            )
        )
        return 0
    try:
        _emit(guard_result(payload))
    except Exception as error:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        _emit(_deny(_unverifiable_reason(error), recovery_context(_session_id(payload))))
    return 0


def _valid_session_start(payload: dict) -> bool:
    return (
        isinstance(payload.get("session_id"), str)
        and bool(payload["session_id"].strip())
        and isinstance(payload.get("cwd"), str)
        and bool(payload["cwd"].strip())
        and payload.get("hook_event_name") == "SessionStart"
        and isinstance(payload.get("source"), str)
        and bool(payload["source"].strip())
    )


def _read_payload() -> tuple[dict, str | None]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}, "no input on stdin"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        return {}, f"unreadable JSON: {error}"
    if not isinstance(parsed, dict):
        return {}, f"expected an object, got {type(parsed).__name__}"
    return parsed, None


def _deny(reason: str, additional_context: str | None = None) -> dict:
    specific = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    if additional_context:
        specific["additionalContext"] = additional_context
    return {"hookSpecificOutput": specific}


def _unverifiable_reason(error: BaseException) -> str:
    return (
        "Agent Workspace Sync could not verify workspace ownership "
        f"({type(error).__name__}: {error})."
    )


def _emit(result: dict | None) -> None:
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
