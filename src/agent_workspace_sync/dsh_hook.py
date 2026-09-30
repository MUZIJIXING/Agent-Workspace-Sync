"""Python ownership-policy bridge for the DSH Cordis adapter.

The Node adapter owns DSH event and tool parsing. This module accepts its small
JSON request, resolves the workspace to inspect, delegates the ownership
decision to the shared guard policy, and returns platform-neutral decision JSON.
"""

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


GUARD_TOOL_MODE = "guard-tool"
HARNESS_TYPE = "dsh"
FILE_TARGET_FIELDS = {"write": "file_path", "edit": "file_path"}
SHELL_TOOLS = frozenset({"pwsh", "bash"})
STR_REPLACE_MUTATIONS = frozenset({"create", "str_replace", "insert"})
RECOVERY_TOOLS = frozenset(
    {
        "mcp__agent-workspace-sync__workspace_inspect",
        "mcp__agent-workspace-sync__workspace_enter",
        "mcp__agent-workspace-sync__workspace_heartbeat",
        "mcp__agent-workspace-sync__workspace_leave",
        "mcp__agent-workspace-sync__workspace_takeover",
    }
)

CANNOT_VERIFY_TARGET = (
    "Cannot verify the target path for this operation. Agent Workspace Sync "
    "denies mutations whose target workspace it cannot determine."
)
INVALID_STATE_REASON = (
    "Agent Workspace Sync found incomplete or inconsistent ownership state and "
    "failed closed. Inspect the workspace before modifying it."
)
NO_OWNER_REASON = (
    "This managed workspace has no active owner. Inspect the workspace, enter "
    "it with this DSH instance, then retry the operation."
)
FOREIGN_FRESH_OWNER_REASON = (
    "Another harness instance owns this workspace and is fresh. Do not enter "
    "and do not take over; a fresh owner cannot be taken over."
)
FOREIGN_STALE_OWNER_REASON = (
    "Another owner's stale status remains active, so ordinary enter is still "
    "refused. Takeover requires explicit user intent."
)
CURRENT_OWNER_STALE_REASON = (
    "This DSH instance owns the workspace, but heartbeat cannot revive stale "
    "ownership. Ordinary continue does not authorize release. Only after the user "
    "explicitly requests handoff or recovery that releases this ownership, use "
    "workspace_leave and, if continued work was requested, workspace_enter."
)

_VERIFICATION_ERRORS = (
    WorkspaceError,
    SessionError,
    OwnershipError,
    HandoffError,
    WorkflowError,
    StorageError,
)


def guard_tool_result(payload: object) -> dict[str, str]:
    """Return an allow/deny decision for one normalized DSH tool execution."""
    if not isinstance(payload, dict):
        return _deny(INVALID_STATE_REASON, GuardDecision.INVALID_STATE)

    tool_name = payload.get("tool_name")
    if tool_name in RECOVERY_TOOLS:
        return _allow()
    if not isinstance(tool_name, str) or not tool_name:
        return _deny(INVALID_STATE_REASON, GuardDecision.INVALID_STATE)

    arguments = payload.get("arguments")
    if tool_name == "str_replace_editor":
        if not isinstance(arguments, dict):
            return _deny(CANNOT_VERIFY_TARGET, GuardDecision.INVALID_STATE)
        command = arguments.get("command")
        if command == "view":
            return _allow()
        if command not in STR_REPLACE_MUTATIONS:
            return _deny(CANNOT_VERIFY_TARGET, GuardDecision.INVALID_STATE)
        return _guard_file_target(payload, arguments, "path")

    target_field = FILE_TARGET_FIELDS.get(tool_name)
    if target_field is not None:
        if not isinstance(arguments, dict):
            return _deny(CANNOT_VERIFY_TARGET, GuardDecision.INVALID_STATE)
        return _guard_file_target(payload, arguments, target_field)

    if tool_name in SHELL_TOOLS:
        if not isinstance(arguments, dict):
            return _deny(INVALID_STATE_REASON, GuardDecision.INVALID_STATE)
        return _guard_shell(payload, arguments)

    # Outer run_code/subagent/workflow calls and unrelated tools have no target
    # this adapter can authoritatively infer. Their nested mutation calls re-enter
    # DSH's complete ToolRuntime and are guarded under the actual child session.
    return _allow()


def _guard_file_target(
    payload: dict, arguments: dict, field: str
) -> dict[str, str]:
    target = _resolve_path(arguments.get(field), payload.get("cwd"))
    if target is None:
        return _deny(CANNOT_VERIFY_TARGET, GuardDecision.INVALID_STATE)
    return _guard_workspace(target.parent, payload.get("session_id"))


def _guard_shell(payload: dict, arguments: dict) -> dict[str, str]:
    raw_workdir = arguments.get("workdir")
    if raw_workdir is None:
        raw_workdir = payload.get("cwd")
        base = None
    else:
        base = payload.get("cwd")
    workdir = _resolve_path(raw_workdir, base)
    if workdir is None:
        return _deny(INVALID_STATE_REASON, GuardDecision.INVALID_STATE)
    return _guard_workspace(workdir, payload.get("session_id"))


def _resolve_path(raw_path: object, base: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = Path(raw_path)
        if not path.is_absolute():
            if not isinstance(base, str) or not base.strip():
                return None
            path = Path(base) / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _guard_workspace(start: Path, session_id: object) -> dict[str, str]:
    if not isinstance(session_id, str) or not session_id.strip():
        return _deny(INVALID_STATE_REASON, GuardDecision.INVALID_STATE)
    try:
        view = inspect_workspace(start)
    except WorkspaceNotInitializedError:
        return _allow()
    except _VERIFICATION_ERRORS as error:
        return _deny(_unverifiable_reason(error), GuardDecision.INVALID_STATE)
    except Exception as error:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _deny(_unverifiable_reason(error), GuardDecision.INVALID_STATE)

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
            return _deny(_unverifiable_reason(error), GuardDecision.INVALID_STATE)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _deny(_unverifiable_reason(error), GuardDecision.INVALID_STATE)
        return _allow()
    reasons = {
        GuardDecision.NO_OWNER: NO_OWNER_REASON,
        GuardDecision.FOREIGN_FRESH_OWNER: FOREIGN_FRESH_OWNER_REASON,
        GuardDecision.FOREIGN_STALE_OWNER: FOREIGN_STALE_OWNER_REASON,
        GuardDecision.CURRENT_OWNER_STALE: CURRENT_OWNER_STALE_REASON,
        GuardDecision.INVALID_STATE: INVALID_STATE_REASON,
    }
    return _deny(reasons[decision], decision)


def _allow() -> dict[str, str]:
    return {"decision": "allow"}


def _deny(reason: str, category: GuardDecision) -> dict[str, str]:
    return {"decision": "deny", "reason": reason, "category": category.name}


def _unverifiable_reason(error: BaseException) -> str:
    return (
        "Agent Workspace Sync could not verify workspace ownership "
        f"({type(error).__name__}: {error})."
    )


def _read_payload() -> tuple[object, str | None]:
    raw = sys.stdin.read()
    if not raw.strip():
        return None, "no input on stdin"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as error:
        return None, f"unreadable JSON: {error}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Workspace Sync DSH guard bridge.")
    parser.add_argument("mode", choices=(GUARD_TOOL_MODE,))
    parser.parse_args(argv)

    payload, problem = _read_payload()
    if problem is not None:
        result = _deny(
            f"Agent Workspace Sync could not verify protection ({problem}).",
            GuardDecision.INVALID_STATE,
        )
    else:
        try:
            result = guard_tool_result(payload)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            result = _deny(_unverifiable_reason(error), GuardDecision.INVALID_STATE)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
