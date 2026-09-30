"""Command line interface for Agent Workspace Sync.

The CLI is a thin adapter over the core: it resolves paths, calls existing core
functions, and formats the result. No workspace, session, ownership, or handoff
rule is reimplemented here — not session lifecycle, not ownership conflict
handling, not staleness, not takeover, not finalization authorization, and not
any transaction.

The CLI is stateless: it keeps no current session, current lease, or hidden
state file. Session and lease identifiers are always passed explicitly between
commands, or the core generates them and the caller copies them forward.

``aws status``, ``aws handoff show``, and ``aws handoff latest`` are observation
commands. They perform no writes at all — no session, no lease, no heartbeat, no
release, no takeover, no handoff, and no metadata change. Commands that do change
state keep the core operations separate: starting a session never acquires
ownership, finalizing a handoff never releases a lease or ends a session, and
taking over never ends the previous session.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .client_setup import ClientSetupError
from .context import DEFAULT_CONTEXT_MAX_CHARS, compact_handoff, validate_context_options
from .handoff import (
    Handoff,
    HandoffError,
    finalize_handoff,
    get_handoff,
    get_latest_handoff,
)
from .ownership import (
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
from .workspace import (
    WorkspaceError,
    WorkspaceIdentity,
    find_workspace,
    identity_file_path,
    init_workspace,
)

EXIT_SUCCESS = 0
EXIT_DOMAIN_ERROR = 1
# argparse itself exits with 2 on a usage error.

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"


class CLIInputError(Exception):
    """Raised when a command line input file cannot be used."""


# Only the project's own error hierarchies plus the CLI's own input error are
# turned into clean messages; an unexpected exception is a bug and must surface
# as a traceback.
_DOMAIN_ERRORS = (
    WorkspaceError,
    SessionError,
    OwnershipError,
    HandoffError,
    StorageError,
    CLIInputError,
    ClientSetupError,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``aws`` command."""
    parser = argparse.ArgumentParser(
        prog="aws",
        description=(
            "Agent Workspace Sync — project-local workspace state for AI coding harnesses."
        ),
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    init_command = commands.add_parser(
        "init",
        help="initialize an Agent Workspace Sync workspace",
        description="Initialize a workspace. Re-initializing returns the existing identity.",
    )
    init_command.add_argument(
        "path",
        nargs="?",
        default=None,
        help="workspace root (default: the current directory)",
    )

    status_command = commands.add_parser(
        "status",
        help="show the workspace's current state",
        description="Show workspace identity, ownership, and handoff state. Read-only.",
    )
    status_command.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    doctor_command = commands.add_parser(
        "doctor", help="check the Python runtime, MCP connection, and workspace (read-only)"
    )
    doctor_command.add_argument("path", nargs="?", default=None)
    doctor_command.add_argument("--python", default=sys.executable, help="Python executable used by the client")
    doctor_command.add_argument("--json", action="store_true", help="print the diagnostic report as JSON")

    setup_command = commands.add_parser(
        "setup", help="prepare project-local client files without registering or launching a client"
    )
    setup_command.add_argument("path", nargs="?", default=None)
    setup_command.add_argument("--client", choices=("zcode", "dsh", "claude-code"), required=True)
    setup_command.add_argument("--python", default=sys.executable, help="Python used by both MCP and guard")
    setup_command.add_argument("--source", help="override integration templates directory (default: bundled templates or source checkout)")
    setup_command.add_argument("--dsh-preset", help="derive a project-local discovery preset from the installed DSH standard agent.cordis.yml")

    session_command = commands.add_parser(
        "session",
        help="manage development sessions",
        description="Record the start and end of development sessions. Not ownership.",
    )
    session_commands = session_command.add_subparsers(
        dest="session_command", metavar="<action>", required=True
    )

    session_start = session_commands.add_parser(
        "start",
        help="record the start of a development session",
        description=(
            "Record a new development session. This does not acquire workspace "
            "ownership; run 'aws acquire' separately."
        ),
    )
    session_start.add_argument(
        "--harness",
        required=True,
        help="harness type, for example zcode, codex, dsh, claude-code",
    )
    session_start.add_argument(
        "--instance-id",
        default=None,
        help="opaque harness instance id (default: generate a new one)",
    )
    session_start.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    session_end = session_commands.add_parser(
        "end",
        help="record the end of a development session",
        description=(
            "Record the end of a session. A session that still owns an active "
            "lease cannot be ended: release it or hand it over first."
        ),
    )
    session_end.add_argument("--session", required=True, help="session id (ses_...)")
    session_end.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    acquire_command = commands.add_parser(
        "acquire",
        help="acquire exclusive workspace ownership",
        description=(
            "Acquire exclusive ownership for a session. The ttl must be given "
            "explicitly; no product-level timeout policy is assumed."
        ),
    )
    acquire_command.add_argument("--session", required=True, help="session id (ses_...)")
    acquire_command.add_argument(
        "--ttl",
        type=int,
        required=True,
        help="lease lifetime in seconds (positive integer, no default)",
    )
    acquire_command.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    heartbeat_command = commands.add_parser(
        "heartbeat",
        help="refresh an ownership lease",
        description="Refresh the lease of its owning session. A lapsed lease is never revived.",
    )
    heartbeat_command.add_argument("--session", required=True, help="session id (ses_...)")
    heartbeat_command.add_argument("--lease", required=True, help="lease id (lea_...)")
    heartbeat_command.add_argument(
        "--ttl",
        type=int,
        required=True,
        help="new lease lifetime in seconds (positive integer, no default)",
    )
    heartbeat_command.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    release_command = commands.add_parser(
        "release",
        help="release an ownership lease",
        description="Release a lease held by its owning session. This does not end the session.",
    )
    release_command.add_argument("--session", required=True, help="session id (ses_...)")
    release_command.add_argument("--lease", required=True, help="lease id (lea_...)")
    release_command.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    takeover_command = commands.add_parser(
        "takeover",
        help="take over a lapsed ownership lease",
        description=(
            "Retire a lapsed active lease and become the new owner. Running this "
            "command is itself the explicit takeover; --expected-lease is the "
            "stale confirmation token."
        ),
    )
    takeover_command.add_argument("--session", required=True, help="successor session id")
    takeover_command.add_argument(
        "--expected-lease",
        required=True,
        help="the lease being retired; refuses to act if ownership already moved on",
    )
    takeover_command.add_argument(
        "--ttl",
        type=int,
        required=True,
        help="new lease lifetime in seconds (positive integer, no default)",
    )
    takeover_command.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    handoff_command = commands.add_parser(
        "handoff",
        help="finalize and read handoffs",
        description=(
            "Finalize and read handoffs. A finalized handoff is immutable and is "
            "written only by the session that currently owns the workspace."
        ),
    )
    handoff_commands = handoff_command.add_subparsers(
        dest="handoff_command", metavar="<action>", required=True
    )

    handoff_finalize = handoff_commands.add_parser(
        "finalize",
        help="finalize the workspace handoff",
        description=(
            "Persist the authoritative handoff. This does not release ownership "
            "and does not end the session; run 'aws release' and 'aws session "
            "end' separately."
        ),
    )
    handoff_finalize.add_argument("--session", required=True, help="owning session id (ses_...)")
    handoff_finalize.add_argument(
        "--semantic",
        required=True,
        metavar="FILE",
        help="JSON file holding the semantic context object",
    )
    handoff_finalize.add_argument(
        "--evidence",
        required=True,
        metavar="FILE",
        help="JSON file holding the objective evidence object",
    )
    handoff_finalize.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    handoff_show = handoff_commands.add_parser(
        "show",
        help="print one finalized handoff",
        description="Print one finalized handoff. Read-only.",
    )
    handoff_show.add_argument("--handoff", required=True, help="handoff id (hnd_...)")
    handoff_show.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    handoff_latest = handoff_commands.add_parser(
        "latest",
        help="print the most recent finalized handoff",
        description=(
            "Print the most recent finalized handoff, or report that none exists. "
            "Read-only, and having no handoff is a normal state rather than an error."
        ),
    )
    handoff_latest.add_argument(
        "path",
        nargs="?",
        default=None,
        help="any path inside the workspace (default: the current directory)",
    )

    handoff_context = handoff_commands.add_parser(
        "context", help="print a bounded historical context view (read-only)"
    )
    handoff_context.add_argument("path", nargs="?", default=None)
    handoff_context.add_argument("--max-chars", type=int, default=DEFAULT_CONTEXT_MAX_CHARS,
                                 help="compact JSON character budget, 1024–64000; not a token count")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    if sys.platform == "win32":
        # Windows console streams already handle Unicode; redirected streams
        # otherwise fall back to a locale encoding that corrupts Chinese in
        # UTF-8 consumers (including client diagnostic panels).
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    arguments = build_parser().parse_args(argv)
    try:
        return _dispatch(arguments)
    except _DOMAIN_ERRORS as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_DOMAIN_ERROR


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "setup":
        from .client_setup import prepare_client

        target = prepare_client(
            arguments.path or Path.cwd(), arguments.client, arguments.python, arguments.source,
            dsh_preset=arguments.dsh_preset,
        )
        print(f"项目内接入文件已准备：{target}")
        if arguments.dsh_preset is not None:
            print(f"此配置须使用独立 DSH_HOME：{target / 'home'}；在 awsync-workspace 模式中新建会话。")
        print("尚未注册或启动客户端。请在独立测试实例中加载，并验证无所有权时写入会被拒绝。")
        return EXIT_SUCCESS
    if arguments.command == "doctor":
        from .diagnostics import describe, diagnose

        report = diagnose(arguments.path or Path.cwd(), arguments.python)
        print(json.dumps(report, ensure_ascii=False) if arguments.json else describe(report))
        return EXIT_SUCCESS if report["ready_for_client_check"] else EXIT_DOMAIN_ERROR
    if arguments.command == "init":
        return _run_init(arguments.path)
    if arguments.command == "status":
        return _run_status(arguments.path)
    if arguments.command == "session":
        if arguments.session_command == "start":
            return _run_session_start(arguments)
        return _run_session_end(arguments)
    if arguments.command == "acquire":
        return _run_acquire(arguments)
    if arguments.command == "heartbeat":
        return _run_heartbeat(arguments)
    if arguments.command == "release":
        return _run_release(arguments)
    if arguments.command == "takeover":
        return _run_takeover(arguments)
    if arguments.command == "handoff":
        if arguments.handoff_command == "finalize":
            return _run_handoff_finalize(arguments)
        if arguments.handoff_command == "show":
            return _run_handoff_show(arguments)
        if arguments.handoff_command == "context":
            validate_context_options("compact", arguments.max_chars)
            root = _resolve_workspace(arguments.path)
            handoff = get_latest_handoff(root)
            result = compact_handoff(handoff, arguments.max_chars) if handoff is not None else None
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return EXIT_SUCCESS
        return _run_handoff_latest(arguments)
    # argparse already restricts the command set, so this is unreachable.
    raise AssertionError(f"unhandled command: {arguments.command!r}")


def _run_init(path: str | None) -> int:
    root = Path(path) if path is not None else Path.cwd()
    already_initialized = identity_file_path(root).is_file()

    identity = init_workspace(root)

    if already_initialized:
        print("Agent Workspace Sync workspace already initialized")
    else:
        print("Initialized Agent Workspace Sync workspace")
    print(f"workspace_id: {identity.workspace_id}")
    print(f"root: {identity.root}")
    return EXIT_SUCCESS


def _run_status(path: str | None) -> int:
    start = Path(path) if path is not None else Path.cwd()
    identity = find_workspace(start)

    active_lease = get_active_ownership(identity.root)
    owner_session = None
    if active_lease is not None:
        owner_session = get_session(identity.root, active_lease.session_id)
    latest_handoff = get_latest_handoff(identity.root)

    print(
        describe_status(
            identity,
            active_lease=active_lease,
            owner_session=owner_session,
            latest_handoff=latest_handoff,
        )
    )
    return EXIT_SUCCESS


def _run_session_start(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)
    instance_id = (
        arguments.instance_id
        if arguments.instance_id is not None
        else new_harness_instance_id()
    )

    session = create_session(root, arguments.harness, instance_id)

    print("Session started")
    print(f"  session id: {session.session_id}")
    print(f"  harness type: {session.harness_type}")
    print(f"  harness instance: {session.harness_instance_id}")
    return EXIT_SUCCESS


def _run_session_end(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)

    session = end_session(root, arguments.session)

    print("Session ended")
    print(f"  session id: {session.session_id}")
    print(f"  ended at: {session.ended_at}")
    return EXIT_SUCCESS


def _run_acquire(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)

    lease = acquire_ownership(root, arguments.session, arguments.ttl)

    print("Ownership acquired")
    print(f"  lease id: {lease.lease_id}")
    print(f"  session id: {lease.session_id}")
    print(f"  expires at: {lease.expires_at}")
    return EXIT_SUCCESS


def _run_heartbeat(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)

    lease = heartbeat_ownership(root, arguments.session, arguments.lease, arguments.ttl)

    print("Ownership heartbeat refreshed")
    print(f"  lease id: {lease.lease_id}")
    print(f"  session id: {lease.session_id}")
    print(f"  expires at: {lease.expires_at}")
    return EXIT_SUCCESS


def _run_release(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)

    lease = release_ownership(root, arguments.session, arguments.lease)

    print("Ownership released")
    print(f"  lease id: {lease.lease_id}")
    print(f"  session id: {lease.session_id}")
    print(f"  status: {lease.status}")
    return EXIT_SUCCESS


def _run_takeover(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)

    lease = takeover_ownership(
        root,
        arguments.session,
        arguments.expected_lease,
        arguments.ttl,
    )

    print("Ownership takeover completed")
    print(f"  previous lease: {arguments.expected_lease}")
    print(f"  new lease: {lease.lease_id}")
    print(f"  session id: {lease.session_id}")
    print(f"  expires at: {lease.expires_at}")
    return EXIT_SUCCESS


def _resolve_workspace(path: str | None) -> Path:
    """Find the workspace owning the given path, or the current directory."""
    start = Path(path) if path is not None else Path.cwd()
    return find_workspace(start).root


def _run_handoff_finalize(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)
    semantic_context = _read_json_object(arguments.semantic, "--semantic")
    objective_evidence = _read_json_object(arguments.evidence, "--evidence")

    handoff = finalize_handoff(
        root,
        arguments.session,
        semantic_context,
        objective_evidence,
    )

    print("Handoff finalized")
    print(f"  handoff id: {handoff.handoff_id}")
    print(f"  from session: {handoff.from_session_id}")
    print(f"  created at: {handoff.created_at}")
    return EXIT_SUCCESS


def _run_handoff_show(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)

    handoff = get_handoff(root, arguments.handoff)

    print(describe_handoff(handoff))
    return EXIT_SUCCESS


def _run_handoff_latest(arguments: argparse.Namespace) -> int:
    root = _resolve_workspace(arguments.path)

    handoff = get_latest_handoff(root)
    if handoff is None:
        # Having no handoff is a normal state, not a failure.
        print("No finalized handoff")
        return EXIT_SUCCESS

    print(describe_handoff(handoff))
    return EXIT_SUCCESS


def _read_json_object(path_value: str, option_name: str) -> dict:
    """Read one CLI input file that must hold a JSON object.

    The core owns the serialization rules; this only turns "file missing",
    "unreadable", "not JSON", and "not an object" into a clean CLI error.
    """
    path = Path(path_value)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CLIInputError(f"cannot read {option_name} file {path}: {exc}") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CLIInputError(f"{option_name} file {path} is not valid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise CLIInputError(
            f"{option_name} file {path} must contain a JSON object, "
            f"not {type(document).__name__}"
        )
    return document


def describe_handoff(handoff: Handoff) -> str:
    """Render one finalized handoff. Pure formatting: nothing is modified."""
    return "\n".join(
        [
            "Handoff",
            f"  id: {handoff.handoff_id}",
            f"  from session: {handoff.from_session_id}",
            f"  created at: {handoff.created_at}",
            "",
            "Semantic Context",
            json.dumps(handoff.semantic_context, ensure_ascii=False, indent=2, sort_keys=True),
            "",
            "Objective Evidence",
            json.dumps(handoff.objective_evidence, ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )


def describe_status(
    identity: WorkspaceIdentity,
    *,
    active_lease: Lease | None,
    owner_session: Session | None,
    latest_handoff: Handoff | None,
    now: datetime | None = None,
) -> str:
    """Render the status report. Pure formatting: it reads nothing and writes nothing."""
    lines = [
        "Workspace",
        f"  id: {identity.workspace_id}",
        f"  root: {identity.root}",
        "",
        "Session",
    ]
    if owner_session is None:
        lines.append("  active owner session: none")
    else:
        lines.append(f"  active owner session: {owner_session.session_id}")
        lines.append(f"  harness type: {owner_session.harness_type}")
        lines.append(f"  harness instance: {owner_session.harness_instance_id}")

    lines += ["", "Ownership"]
    if active_lease is None:
        lines.append("  lease: none")
    else:
        lines.append(f"  lease id: {active_lease.lease_id}")
        lines.append(f"  status: {active_lease.status}")
        lines.append(f"  freshness: {lease_freshness(active_lease, now=now)}")
        lines.append(f"  expires at: {active_lease.expires_at}")

    lines += ["", "Handoff"]
    if latest_handoff is None:
        lines.append("  latest handoff: none")
    else:
        lines.append(f"  latest handoff: {latest_handoff.handoff_id}")
        lines.append(f"  from session: {latest_handoff.from_session_id}")
        lines.append(f"  created at: {latest_handoff.created_at}")

    return "\n".join(lines)


def lease_freshness(lease: Lease, *, now: datetime | None = None) -> str:
    """Report whether a lease is still fresh.

    Display only. A stale lease is reported as stale and left completely alone:
    nothing is refreshed, released, expired, or taken over.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    return FRESHNESS_FRESH if _parse_timestamp(lease.expires_at) > moment else FRESHNESS_STALE


def _parse_timestamp(value: str) -> datetime:
    """Parse a stored timestamp for display.

    A malformed value means the database is corrupt, so this fails rather than
    guessing a freshness that the core would not agree with.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StorageError(f"malformed timestamp in workspace database: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
