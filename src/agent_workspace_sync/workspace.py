"""Workspace identity for Agent Workspace Sync.

Identity is a random UUID persisted inside the project itself at
``<workspace>/.agent-workspace/workspace.json`` and mirrored into
``workspace.db`` metadata so the two can be cross-checked. Git remotes, paths,
user names, machine names, PIDs, HOME, and any global registry take no part in
determining identity.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import git_exclude
from . import storage

WORKSPACE_DIR_NAME = ".agent-workspace"
IDENTITY_FILE_NAME = "workspace.json"
DATABASE_FILE_NAME = "workspace.db"
WORKSPACE_ID_PREFIX = "aws_"
WORKSPACE_SCHEMA_VERSION = 1

_REQUIRED_IDENTITY_KEYS = ("schema_version", "workspace_id", "created_at", "root_hint")


class WorkspaceError(Exception):
    """Base class for workspace identity failures."""


class WorkspaceNotInitializedError(WorkspaceError):
    """Raised when no ``.agent-workspace/workspace.json`` can be found."""


class IncompleteWorkspaceError(WorkspaceError):
    """Raised when workspace state exists but is not complete and consistent."""


class InvalidIdentityFileError(WorkspaceError):
    """Raised when ``workspace.json`` exists but cannot serve as an identity."""


class IdentityMismatchError(WorkspaceError):
    """Raised when ``workspace.json`` and ``workspace.db`` disagree."""


class UnsupportedWorkspaceVersionError(WorkspaceError):
    """Raised when ``workspace.json`` declares a version this build cannot read.

    There is no migration: an unsupported version is reported, never rewritten.
    """


@dataclass(frozen=True)
class WorkspaceIdentity:
    """The authoritative identity of one workspace, plus its current location."""

    workspace_id: str
    schema_version: int
    created_at: str
    root_hint: str
    root: Path
    identity_file: Path
    database_path: Path


def new_workspace_id() -> str:
    """Return a fresh opaque workspace identity."""
    return f"{WORKSPACE_ID_PREFIX}{uuid.uuid4()}"


def identity_file_path(root: Path) -> Path:
    """Return the identity file location for a workspace root."""
    return Path(root) / WORKSPACE_DIR_NAME / IDENTITY_FILE_NAME


def database_path(root: Path) -> Path:
    """Return the database location for a workspace root."""
    return Path(root) / WORKSPACE_DIR_NAME / DATABASE_FILE_NAME


def init_workspace(root_path: Path | str) -> WorkspaceIdentity:
    """Initialize a workspace exactly once and return its identity.

    Re-initializing an existing workspace returns the stored identity unchanged:
    no new UUID is generated and no existing identity is overwritten. A
    workspace directory that exists without an identity file is refused rather
    than adopted, so a partial state is never silently completed.

    Re-initializing also re-applies the Git hygiene below, which is how a
    workspace created before that step existed picks it up.

    Concurrent initializers of the same root converge on a single identity.
    Publishing is one rename that never replaces an existing
    ``.agent-workspace``, so only one identity can become authoritative; a
    process that loses the race adopts the published winner instead of
    replacing it.

    After publishing, the state directory is added to the enclosing Git
    repository's local exclude on a best-effort basis, so ``git status`` stays
    clean. That step never affects identity and never fails initialization.
    """
    root = _absolute(root_path)
    if not root.is_dir():
        raise WorkspaceError(f"workspace root is not an existing directory: {root}")

    workspace_dir = root / WORKSPACE_DIR_NAME

    published = _load_if_published(root)
    if published is not None:
        _apply_git_hygiene(root)
        return published

    workspace_id = new_workspace_id()
    document = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root_hint": str(root),
    }

    # Build the whole workspace in a sibling staging directory, then publish it
    # with a single rename. os.replace is deliberately not used: rename refuses
    # to clobber an existing directory on Windows, and on POSIX it fails with
    # ENOTEMPTY because a published workspace is never empty.
    staging_dir = root / f"{WORKSPACE_DIR_NAME}.init-{uuid.uuid4().hex}"
    try:
        staging_dir.mkdir()
        staging_identity_file = staging_dir / IDENTITY_FILE_NAME
        staging_database = staging_dir / DATABASE_FILE_NAME

        _write_identity_document(staging_identity_file, document)
        storage.initialize_database(staging_database, workspace_id)
        _assert_identity_consistency(staging_identity_file, staging_database, workspace_id)

        try:
            os.rename(staging_dir, workspace_dir)
        except OSError as exc:
            published = _load_if_published(root)
            if published is None:
                raise WorkspaceError(
                    f"could not finalize workspace at {workspace_dir}: {exc}"
                ) from exc
            # Another initializer published first; that identity stands.
            return published
    finally:
        # Removes the staging directory on every exit path, including
        # KeyboardInterrupt and SystemExit. It is a no-op once publishing has
        # renamed the directory into place, and finally never swallows or
        # converts the propagating exception.
        shutil.rmtree(staging_dir, ignore_errors=True)

    # Best-effort local Git hygiene, deliberately after the identity is
    # published and never inside that transaction: a repository that cannot be
    # inspected or written to must not turn a successful initialization into a
    # failure. See git_exclude for the full contract.
    _apply_git_hygiene(root)

    return load_workspace(root)


def _apply_git_hygiene(root: Path) -> None:
    """Add the state directory to the repository's local exclude, if possible.

    Hygiene is cosmetic and must never fail or roll back workspace
    initialization, so every failure — including an unexpected bug in
    ``git_exclude`` — is contained here.
    """
    try:
        git_exclude.ensure_locally_excluded(root)
    except Exception:  # noqa: BLE001 - hygiene must never fail an initialization
        pass


def load_workspace(root_path: Path | str) -> WorkspaceIdentity:
    """Load the identity stored at ``root_path`` and verify it against the database."""
    root = _absolute(root_path)
    identity_file = identity_file_path(root)
    database_file = database_path(root)

    # Validation order is deliberate: the workspace document, then the database
    # metadata and its schema version, then the identity both sources must
    # agree on. A matching workspace_id never excuses a bad database version.
    document = _read_identity_document(identity_file)
    if not database_file.is_file():
        raise IncompleteWorkspaceError(f"{identity_file} exists without {DATABASE_FILE_NAME}")

    _assert_database_schema_supported(database_file)
    _assert_identity_consistency(identity_file, database_file, document["workspace_id"])

    return WorkspaceIdentity(
        workspace_id=document["workspace_id"],
        schema_version=document["schema_version"],
        created_at=document["created_at"],
        root_hint=document["root_hint"],
        root=root,
        identity_file=identity_file,
        database_path=database_file,
    )


def find_workspace(start_path: Path | str) -> WorkspaceIdentity:
    """Find the nearest workspace at or above ``start_path``.

    The search walks upward from the given directory and stops at the first
    ``.agent-workspace`` entry. A missing identity inside an existing state
    directory is incomplete state, not an unmanaged directory. Broken or
    unreadable state raises instead of falling back to a parent's ownership.
    """
    start = Path(start_path)
    if start.is_file():
        start = start.parent
    start = _absolute(start)

    for candidate in (start, *start.parents):
        published = _load_if_published(candidate)
        if published is not None:
            return published

    raise WorkspaceNotInitializedError(f"no workspace found at or above {start}")


def _absolute(path: Path | str) -> Path:
    path = Path(path)
    return path if path.is_absolute() else path.absolute()


def _load_if_published(root: Path) -> WorkspaceIdentity | None:
    """Load existing workspace state, refusing incomplete or unreadable state."""
    if _path_exists(root / WORKSPACE_DIR_NAME):
        try:
            return load_workspace(root)
        except WorkspaceNotInitializedError as exc:
            # The marker was observed above. Losing it during the read must not
            # turn a verification failure into an unmanaged-directory bypass.
            raise IncompleteWorkspaceError(
                f"workspace state disappeared while loading {root}"
            ) from exc
    return None


def _path_exists(path: Path) -> bool:
    """Observe an entry without hiding I/O errors or dangling symlinks."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WorkspaceError(f"cannot inspect {path}: {exc}") from exc
    return True


def _write_identity_document(identity_file: Path, document: dict) -> None:
    with open(identity_file, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_identity_document(identity_file: Path) -> dict:
    try:
        document = json.loads(identity_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if _path_exists(identity_file.parent):
            raise IncompleteWorkspaceError(
                f"{identity_file.parent} exists without {IDENTITY_FILE_NAME}; "
                "refusing to adopt or overwrite it"
            ) from exc
        raise WorkspaceNotInitializedError(f"no workspace identity at {identity_file}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidIdentityFileError(f"cannot read {identity_file}: {exc}") from exc

    if not isinstance(document, dict):
        raise InvalidIdentityFileError(f"{identity_file} is not a JSON object")

    missing = [key for key in _REQUIRED_IDENTITY_KEYS if key not in document]
    if missing:
        raise InvalidIdentityFileError(f"{identity_file} is missing: {', '.join(missing)}")

    schema_version = document["schema_version"]
    # bool is a subclass of int, but it is not a legitimate version number.
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise InvalidIdentityFileError(
            f"{identity_file} has a non-integer schema_version: {schema_version!r}"
        )
    if schema_version != WORKSPACE_SCHEMA_VERSION:
        raise UnsupportedWorkspaceVersionError(
            f"{identity_file} declares schema_version {schema_version}, "
            f"but this build only supports {WORKSPACE_SCHEMA_VERSION}"
        )

    workspace_id = document["workspace_id"]
    if not isinstance(workspace_id, str) or not workspace_id:
        raise InvalidIdentityFileError(f"{identity_file} carries an empty workspace_id")

    return document


def _assert_database_schema_supported(database_file: Path) -> None:
    """Fail safely unless the database metadata declares a supported schema version.

    ``UnsupportedDatabaseVersionError`` is re-raised unchanged because it is
    already the precise failure; every other storage problem means the database
    cannot be interpreted at all.
    """
    try:
        storage.read_database_schema_version(database_file)
    except storage.UnsupportedDatabaseVersionError:
        raise
    except storage.StorageError as exc:
        raise IncompleteWorkspaceError(
            f"{database_file} has no usable metadata: {exc}"
        ) from exc


def _assert_identity_consistency(
    identity_file: Path,
    database_file: Path,
    workspace_id: str,
) -> None:
    """Fail safely unless the database mirrors the identity file exactly."""
    try:
        stored_id = storage.read_workspace_id(database_file)
    except storage.StorageError as exc:
        raise IncompleteWorkspaceError(f"{database_file} has no usable identity: {exc}") from exc

    if stored_id != workspace_id:
        raise IdentityMismatchError(
            "workspace identity mismatch: "
            f"{identity_file} says {workspace_id!r} but {database_file} says {stored_id!r}"
        )
