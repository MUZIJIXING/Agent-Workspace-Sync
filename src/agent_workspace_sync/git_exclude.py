"""Best-effort Git hygiene for a newly initialized workspace.

A managed project keeps its state in ``.agent-workspace/`` inside the project.
That directory is not meant to be tracked, and leaving it untracked pollutes
``git status`` — which matters beyond cosmetics, because the working tree is
also what a handoff records as objective evidence.

So initialization asks Git where the repository's *local* exclude file lives and
adds one rule for the state directory. The rule is **anchored to the repository
top level and scoped to this workspace**, so initializing ``repo/project-a``
never ignores ``repo/project-b/.agent-workspace`` as a side effect:

.. code-block:: text

    workspace at the repo root -> /.agent-workspace/
    workspace at repo/project-a -> /project-a/.agent-workspace/

Whether the directory is already covered is decided by asking Git
(``git check-ignore``) instead of pattern-matching the exclude file, so the
answer reflects what Git actually honours. In particular a negation in a
``.gitignore`` outranks this local file, and that case is reported rather than
papered over.

One limit is inherent to ``info/exclude``: it belongs to the repository's
*common* Git directory, so all linked worktrees of a repository share it. The
anchoring above is what keeps that from leaking *sideways* — a rule for
``repo/project-a`` never covers ``repo/project-b`` — but the same relative path
inside another worktree of the same repository does match, because one rule
cannot be scoped to one checkout. A separate repository, and a separate
submodule, have their own file and are unaffected.

This is deliberately best-effort and stays strictly project-local:

* it never creates or edits a tracked ``.gitignore``;
* it never touches global or user Git configuration (``core.excludesFile``,
  ``$HOME`` config, system config);
* a missing Git, a directory that is not a repository, or an unreadable exclude
  file all leave workspace initialization completely unaffected.

Nothing here participates in workspace identity or ownership safety.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The state directory name, as written inside a rule, and as a plain path.
EXCLUDE_DIRECTORY_NAME = ".agent-workspace/"
_STATE_DIRECTORY_NAME = EXCLUDE_DIRECTORY_NAME.rstrip("/")

OUTCOME_EXCLUDED = "excluded"
OUTCOME_ALREADY_EXCLUDED = "already_excluded"
OUTCOME_IGNORE_OVERRIDDEN = "ignore_overridden"
OUTCOME_UNVERIFIED = "unverified"
OUTCOME_NOT_A_REPOSITORY = "not_a_repository"
OUTCOME_OUTSIDE_REPOSITORY = "outside_repository"
OUTCOME_GIT_UNAVAILABLE = "git_unavailable"
OUTCOME_UNREADABLE = "unreadable_exclude"
OUTCOME_WRITE_FAILED = "write_failed"

_GIT_TIMEOUT_SECONDS = 5
_GITIGNORE_PATH_META_CHARACTERS = frozenset("*?[]\\")


def _escape_gitignore_path(path: str) -> str:
    """Escape pattern metacharacters while preserving path separators."""
    return "".join(
        f"\\{character}" if character in _GITIGNORE_PATH_META_CHARACTERS else character
        for character in path
    )


def exclude_pattern(workspace_relative_path: str) -> str:
    """Return the anchored exclude rule for a workspace inside a repository.

    ``workspace_relative_path`` is the workspace's path relative to the Git top
    level, using forward slashes (``""`` or ``"."`` for the repository root).
    Gitignore pattern metacharacters in directory names are escaped so the rule
    always identifies the literal workspace path.
    """
    relative = workspace_relative_path.strip("/")
    if relative in ("", "."):
        return f"/{EXCLUDE_DIRECTORY_NAME}"
    return f"/{_escape_gitignore_path(relative)}/{EXCLUDE_DIRECTORY_NAME}"


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess | None:
    """Run git in ``root``; ``None`` when Git could not be started at all."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_repository_paths(root: Path) -> tuple[Path | None, Path | None, str | None]:
    """Resolve ``(top_level, exclude_file, failure)`` for a workspace root.

    The exclude file comes from ``git rev-parse --git-path info/exclude`` rather
    than from guessing ``.git/info/exclude``: in a linked worktree or a submodule
    ``.git`` is a file, and the real Git directory lives elsewhere. That path is
    the repository's *common* directory, so it is shared by every worktree of the
    repository — see the module docstring note on cross-worktree scope.
    """
    if not root.is_dir():
        return None, None, OUTCOME_NOT_A_REPOSITORY

    completed = _run_git(root, "rev-parse", "--show-toplevel")
    if completed is None:
        return None, None, OUTCOME_GIT_UNAVAILABLE
    if completed.returncode != 0 or not completed.stdout.strip():
        return None, None, OUTCOME_NOT_A_REPOSITORY
    top_level = Path(completed.stdout.strip())

    completed = _run_git(root, "rev-parse", "--git-path", "info/exclude")
    if completed is None:
        return None, None, OUTCOME_GIT_UNAVAILABLE
    if completed.returncode != 0 or not completed.stdout.strip():
        return None, None, OUTCOME_NOT_A_REPOSITORY

    exclude_file = Path(completed.stdout.strip())
    if not exclude_file.is_absolute():
        # the path is reported relative to the directory git ran in
        exclude_file = root / exclude_file
    return top_level, exclude_file, None


def _workspace_relative_path(root: Path, top_level: Path) -> str | None:
    """Path of ``root`` inside the repository, or ``None`` when it is outside.

    The second attempt resolves both sides first, because Git may report the
    same directory with different casing or separators than the caller's path
    (notably on Windows).
    """
    for candidate_root, candidate_top in ((root, top_level), (root.resolve(), top_level.resolve())):
        try:
            return candidate_root.relative_to(candidate_top).as_posix()
        except ValueError:
            continue
    return None


def _is_effectively_ignored(root: Path) -> bool | None:
    """Ask Git whether the state directory (which exists) is really ignored.

    ``None`` means the question could not be answered. Plain ``check-ignore`` is
    used — not ``--no-index`` — so the answer matches what ``git status``
    actually honours, including the fact that a tracked path is never ignored.

    The queried path deliberately has no trailing slash. Asked about
    ``<path>/``, ``git check-ignore`` answers from the exclude file's text
    alone, and on a file with CRLF line endings *and a blank line* it reports
    every such path as ignored — including ones no rule mentions, which would
    turn a missing rule into a false "already excluded". Without the slash the
    answer comes from the real path, and a directory-only rule still matches a
    directory that exists. The state directory does exist in every call made by
    initialization, which is what makes the answer meaningful.
    """
    completed = _run_git(root, "check-ignore", "-q", _STATE_DIRECTORY_NAME)
    if completed is None:
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def ensure_locally_excluded(workspace_root: Path | str) -> str:
    """Add the workspace state directory to the repository's local exclude.

    Returns one of the ``OUTCOME_*`` constants. Expected Git conditions are
    reported rather than raised, because this is hygiene and not a safety
    invariant. Existing exclude content is preserved and the exact rule is never
    written twice.

    ``OUTCOME_ALREADY_EXCLUDED`` is only ever returned on a positive check that
    Git already ignores the state directory — never merely because a rule
    mentioning it appears in the file. Because that check needs the directory to
    exist (a directory-only rule cannot be confirmed for a path Git cannot see),
    a call made before the state directory exists returns
    ``OUTCOME_UNVERIFIED``: a missing rule is written, but nothing about its
    effect is claimed.
    """
    root = Path(workspace_root)
    top_level, exclude_file, failure = resolve_repository_paths(root)
    if failure is not None:
        return failure
    assert top_level is not None and exclude_file is not None

    relative = _workspace_relative_path(root, top_level)
    if relative is None:
        # the workspace is not inside the repository Git reported
        return OUTCOME_OUTSIDE_REPOSITORY

    pattern = exclude_pattern(relative)
    verifiable = (root / _STATE_DIRECTORY_NAME).is_dir()
    if verifiable:
        already_ignored = _is_effectively_ignored(root)
        if already_ignored is None:
            return OUTCOME_GIT_UNAVAILABLE
        if already_ignored:
            # covered by this file, a .gitignore, or any other ignore source
            return OUTCOME_ALREADY_EXCLUDED

    existing = ""
    if exclude_file.is_file():
        try:
            existing = exclude_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # never rewrite content we cannot faithfully read back
            return OUTCOME_UNREADABLE
        if pattern in {line.strip() for line in existing.splitlines()}:
            # the exact rule is already there, so when the answer could be
            # checked it must be something with higher precedence — a negation
            # in a .gitignore — defeating it; adding it again cannot help
            return OUTCOME_IGNORE_OVERRIDDEN if verifiable else OUTCOME_UNVERIFIED

    try:
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        separator = "" if (not existing or existing.endswith("\n")) else "\n"
        with open(exclude_file, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{separator}{pattern}\n")
    except OSError:
        return OUTCOME_WRITE_FAILED

    if not verifiable:
        return OUTCOME_UNVERIFIED
    if _is_effectively_ignored(root) is False:
        # written but outranked (for example by a negation in a .gitignore);
        # report it instead of claiming success
        return OUTCOME_IGNORE_OVERRIDDEN
    return OUTCOME_EXCLUDED
