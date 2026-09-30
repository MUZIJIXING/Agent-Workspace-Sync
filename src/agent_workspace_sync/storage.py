"""Project-local SQLite storage for Agent Workspace Sync.

This module owns exactly one artifact: ``<workspace>/.agent-workspace/workspace.db``,
and it is the single place where SQL lives.

The ``metadata``, ``sessions``, and ``ownership_leases`` tables exist at this
stage. Handoffs, tasks, events, and file changes are deliberately out of scope.
The database holds no second copy of workspace identity — it mirrors
``workspace_id`` and ``schema_version`` for consistency checking only.

Operational functions that change ownership return an ``(outcome, row)`` pair
instead of raising, so every caller maps the same explicit outcome set. Each
such function owns exactly one ``BEGIN IMMEDIATE`` transaction, which makes its
guarded reads and writes a single serialization point.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# The database format version is deliberately independent of the workspace
# document version in ``workspace.json``. The schema changed when the sessions,
# ownership_leases, and handoffs tables were added, so this is 4 while the
# workspace document stays at 1.
DATABASE_SCHEMA_VERSION = 4

# Every statement needed to bring an empty database to DATABASE_SCHEMA_VERSION.
# There is no migration framework: an older version is reported, never upgraded.
_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id          TEXT PRIMARY KEY,
        harness_type        TEXT NOT NULL,
        harness_instance_id TEXT NOT NULL,
        started_at          TEXT NOT NULL,
        ended_at            TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ownership_leases (
        lease_id          TEXT PRIMARY KEY,
        session_id        TEXT NOT NULL REFERENCES sessions(session_id),
        acquired_at       TEXT NOT NULL,
        last_heartbeat_at TEXT NOT NULL,
        expires_at        TEXT NOT NULL,
        status            TEXT NOT NULL CHECK (status IN ('active', 'released', 'expired'))
    )
    """,
    # The single-active invariant is enforced by the database, not by the
    # application: a partial unique index makes a second active row impossible
    # to commit even for a caller that bypasses this module entirely.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ownership_leases_single_active
        ON ownership_leases (status) WHERE status = 'active'
    """,
    # The cross-table invariant "an ended session never owns an active lease"
    # cannot be expressed as a CHECK constraint, so it is guarded by triggers.
    # Together these make the illegal committed state unrepresentable for raw
    # SQL as well; the transactional application logic above still decides what
    # happens, and these triggers are the last line of defence behind it.
    """
    CREATE TRIGGER IF NOT EXISTS ownership_leases_active_needs_open_session_insert
    BEFORE INSERT ON ownership_leases
    WHEN NEW.status = 'active'
    BEGIN
        SELECT RAISE(ABORT, 'active lease requires a session that has not ended')
        WHERE (SELECT ended_at FROM sessions WHERE session_id = NEW.session_id) IS NOT NULL;
    END
    """,
    # Fires for every lease update, not only status changes, so an active lease
    # cannot be reassigned to an ended session either.
    """
    CREATE TRIGGER IF NOT EXISTS ownership_leases_active_needs_open_session_update
    BEFORE UPDATE ON ownership_leases
    WHEN NEW.status = 'active'
    BEGIN
        SELECT RAISE(ABORT, 'active lease requires a session that has not ended')
        WHERE (SELECT ended_at FROM sessions WHERE session_id = NEW.session_id) IS NOT NULL;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS sessions_end_requires_no_active_lease
    BEFORE UPDATE OF ended_at ON sessions
    WHEN NEW.ended_at IS NOT NULL AND OLD.ended_at IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'cannot end a session that owns an active lease')
        WHERE EXISTS (
            SELECT 1 FROM ownership_leases
            WHERE session_id = NEW.session_id AND status = 'active'
        );
    END
    """,
    # A finalized handoff is historical state: at most one per originating
    # session, never rewritten, never deleted. Successor-neutral by design, so
    # there is no target session, harness, or status column.
    """
    CREATE TABLE IF NOT EXISTS handoffs (
        handoff_id         TEXT PRIMARY KEY,
        from_session_id    TEXT NOT NULL UNIQUE REFERENCES sessions(session_id),
        created_at         TEXT NOT NULL,
        semantic_context   TEXT NOT NULL,
        objective_evidence TEXT NOT NULL
    )
    """,
    # Finalization is authorized by current ownership, so raw SQL cannot write
    # an authoritative handoff on behalf of a session that does not hold the
    # active lease. A stale-but-active owner is still the owner and passes.
    """
    CREATE TRIGGER IF NOT EXISTS handoffs_require_active_owner_insert
    BEFORE INSERT ON handoffs
    BEGIN
        SELECT RAISE(ABORT, 'handoff finalization requires active ownership by the originating session')
        WHERE (SELECT ended_at FROM sessions WHERE session_id = NEW.from_session_id) IS NOT NULL
           OR NOT EXISTS (
                SELECT 1 FROM ownership_leases
                WHERE status = 'active' AND session_id = NEW.from_session_id
              );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS handoffs_are_immutable_update
    BEFORE UPDATE ON handoffs
    BEGIN
        SELECT RAISE(ABORT, 'finalized handoffs are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS handoffs_are_immutable_delete
    BEFORE DELETE ON handoffs
    BEGIN
        SELECT RAISE(ABORT, 'finalized handoffs are immutable');
    END
    """,
)

_SESSION_COLUMNS = (
    "session_id",
    "harness_type",
    "harness_instance_id",
    "started_at",
    "ended_at",
)

_LEASE_COLUMNS = (
    "lease_id",
    "session_id",
    "acquired_at",
    "last_heartbeat_at",
    "expires_at",
    "status",
)

_SELECT_SESSION = (
    "SELECT session_id, harness_type, harness_instance_id, started_at, ended_at "
    "FROM sessions WHERE session_id = ?"
)

_SELECT_LEASE = (
    "SELECT lease_id, session_id, acquired_at, last_heartbeat_at, expires_at, status "
    "FROM ownership_leases WHERE lease_id = ?"
)

_SELECT_ACTIVE_LEASE = (
    "SELECT lease_id, session_id, acquired_at, last_heartbeat_at, expires_at, status "
    "FROM ownership_leases WHERE status = 'active'"
)

_HANDOFF_COLUMNS = (
    "handoff_id",
    "from_session_id",
    "created_at",
    "semantic_context",
    "objective_evidence",
)

_SELECT_HANDOFF = (
    "SELECT handoff_id, from_session_id, created_at, semantic_context, objective_evidence "
    "FROM handoffs WHERE handoff_id = ?"
)

_SELECT_HANDOFF_BY_SESSION = (
    "SELECT handoff_id, from_session_id, created_at, semantic_context, objective_evidence "
    "FROM handoffs WHERE from_session_id = ?"
)

# handoff_id is a stable tie-breaker, so "latest" is deterministic even when two
# handoffs share a timestamp.
_SELECT_LATEST_HANDOFF = (
    "SELECT handoff_id, from_session_id, created_at, semantic_context, objective_evidence "
    "FROM handoffs ORDER BY created_at DESC, handoff_id DESC LIMIT 1"
)

# Outcome vocabulary shared by the guarded ownership operations.
OUTCOME_ACQUIRED = "acquired"
OUTCOME_ACTIVE_CONFLICT = "active_conflict"
OUTCOME_TAKEOVER_REQUIRED = "takeover_required"
OUTCOME_REFRESHED = "refreshed"
OUTCOME_ALREADY_RELEASED = "already_released"
OUTCOME_RELEASED = "released"
OUTCOME_TAKEN_OVER = "taken_over"
OUTCOME_NO_ACTIVE_LEASE = "no_active_lease"
OUTCOME_LEASE_CHANGED = "lease_changed"
OUTCOME_LEASE_FRESH = "lease_fresh"
OUTCOME_SAME_SESSION_TAKEOVER = "same_session_takeover"
OUTCOME_ENDED = "ended"
OUTCOME_ALREADY_ENDED = "already_ended"
OUTCOME_OWNS_ACTIVE_LEASE = "owns_active_lease"
OUTCOME_SESSION_MISSING = "session_missing"
OUTCOME_SESSION_ALREADY_ENDED = "session_already_ended"
OUTCOME_LEASE_MISSING = "lease_missing"
OUTCOME_SESSION_MISMATCH = "session_mismatch"
OUTCOME_LEASE_NOT_ACTIVE = "lease_not_active"
OUTCOME_LEASE_STALE = "lease_stale"
OUTCOME_HANDOFF_FINALIZED = "handoff_finalized"
OUTCOME_HANDOFF_ALREADY_EXISTS = "handoff_already_exists"
OUTCOME_NO_ACTIVE_OWNER = "no_active_owner"
OUTCOME_NOT_ACTIVE_OWNER = "not_active_owner"

# PRAGMA statements cannot be parameterized; both values are fixed literals that
# never include user-controlled input.
_FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys = ON"
_JOURNAL_MODE_PRAGMA = "PRAGMA journal_mode = WAL"

# Concurrent writers wait for the in-flight transaction instead of failing.
_CONNECT_TIMEOUT_SECONDS = 5.0


class StorageError(Exception):
    """Raised when project-local storage cannot be used safely."""


class StorageEmptyError(StorageError):
    """Raised when a database holds no usable metadata."""


class UnsupportedDatabaseVersionError(StorageError):
    """Raised when the database schema version is missing, malformed, or unsupported.

    This value gates how the rest of the database may be interpreted, so it is
    never guessed, migrated, repaired, or rebuilt automatically.
    """


def connect(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` with the PRAGMAs required for workspace storage."""
    connection = sqlite3.connect(str(db_path), timeout=_CONNECT_TIMEOUT_SECONDS)
    connection.execute(_FOREIGN_KEYS_PRAGMA)
    connection.execute(_JOURNAL_MODE_PRAGMA)
    return connection


@contextlib.contextmanager
def write_transaction(db_path: Path):
    """Run a block as one ``BEGIN IMMEDIATE`` transaction.

    The write lock is taken before the block runs, so a guarded read inside it
    cannot be invalidated by a concurrent writer and cannot be raced between the
    read and the write. The block either commits as a whole or rolls back as a
    whole; the original exception always propagates unchanged.
    """
    _require_database(db_path)

    connection = connect(db_path)
    connection.isolation_level = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()


def initialize_database(
    db_path: Path,
    workspace_id: str,
    schema_version: int = DATABASE_SCHEMA_VERSION,
) -> None:
    """Create the schema and record identity in a single transaction."""
    connection = connect(db_path)
    try:
        try:
            with connection:
                for statement in _SCHEMA_DDL:
                    connection.execute(statement)
                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    (
                        ("schema_version", str(schema_version)),
                        ("workspace_id", workspace_id),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"{db_path} already holds metadata") from exc
    finally:
        connection.close()


def read_metadata(db_path: Path) -> dict[str, str]:
    """Return every metadata row.

    Reading never creates a database; a missing file is an error rather than an
    empty result.
    """
    _require_database(db_path)

    connection = connect(db_path)
    try:
        try:
            rows = connection.execute("SELECT key, value FROM metadata").fetchall()
        except sqlite3.OperationalError as exc:
            raise StorageEmptyError(f"{db_path} has no metadata table") from exc
    finally:
        connection.close()

    if not rows:
        raise StorageEmptyError(f"{db_path} has an empty metadata table")
    return dict(rows)


def read_workspace_id(db_path: Path) -> str:
    """Return the workspace identity mirrored in the database metadata."""
    try:
        return read_metadata(db_path)["workspace_id"]
    except KeyError as exc:
        raise StorageEmptyError(f"{db_path} metadata has no workspace_id") from exc


def read_database_schema_version(db_path: Path) -> int:
    """Return the validated database schema version.

    A missing key, a non-integer value, and an unsupported version are all
    rejected: the caller must never proceed on a guessed schema.
    """
    try:
        raw_version = read_metadata(db_path)["schema_version"]
    except KeyError as exc:
        raise UnsupportedDatabaseVersionError(
            f"{db_path} metadata has no schema_version"
        ) from exc

    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise UnsupportedDatabaseVersionError(
            f"{db_path} metadata has a non-integer schema_version: {raw_version!r}"
        ) from exc

    if version != DATABASE_SCHEMA_VERSION:
        raise UnsupportedDatabaseVersionError(
            f"{db_path} declares schema_version {version}, "
            f"but this build only supports {DATABASE_SCHEMA_VERSION}"
        )
    return version


def insert_session(
    db_path: Path,
    *,
    session_id: str,
    harness_type: str,
    harness_instance_id: str,
    started_at: str,
) -> None:
    """Insert one session row.

    ``started_at`` is written once here and no statement ever updates it;
    ``ended_at`` starts as NULL and is only ever set by ``end_session_if_unowned``.
    """
    _require_database(db_path)

    connection = connect(db_path)
    try:
        try:
            with connection:
                connection.execute(
                    "INSERT INTO sessions "
                    "(session_id, harness_type, harness_instance_id, started_at, ended_at) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (session_id, harness_type, harness_instance_id, started_at),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"session {session_id!r} already exists in {db_path}") from exc
    finally:
        connection.close()


def fetch_session(db_path: Path, session_id: str) -> dict | None:
    """Return one stored session row as a mapping, or ``None`` if it is absent."""
    _require_database(db_path)

    connection = connect(db_path)
    try:
        return _fetch_session_row(connection, session_id)
    finally:
        connection.close()


def end_session_if_unowned(db_path: Path, session_id: str, ended_at: str) -> tuple[str, dict | None]:
    """End a session unless it still owns an active lease.

    Checking ownership and recording ``ended_at`` happen in the same
    transaction that acquisition uses, so a session cannot be committed as ended
    while the same commit also leaves it holding an active lease.
    """
    with write_transaction(db_path) as connection:
        session = _fetch_session_row(connection, session_id)
        if session is None:
            return OUTCOME_SESSION_MISSING, None

        active = _fetch_active_lease_row(connection)
        if active is not None and active["session_id"] == session_id:
            return OUTCOME_OWNS_ACTIVE_LEASE, active

        if session["ended_at"] is not None:
            return OUTCOME_ALREADY_ENDED, session

        connection.execute(
            "UPDATE sessions SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
            (ended_at, session_id),
        )
        return OUTCOME_ENDED, _fetch_session_row(connection, session_id)


def fetch_lease(db_path: Path, lease_id: str) -> dict | None:
    """Return one stored lease row as a mapping, or ``None`` if it is absent."""
    _require_database(db_path)

    connection = connect(db_path)
    try:
        return _fetch_lease_row(connection, lease_id)
    finally:
        connection.close()


def fetch_active_lease(db_path: Path) -> dict | None:
    """Return the single active lease, or ``None`` when ownership is free."""
    _require_database(db_path)

    connection = connect(db_path)
    try:
        return _fetch_active_lease_row(connection)
    finally:
        connection.close()


def try_acquire_lease(
    db_path: Path,
    *,
    lease_id: str,
    session_id: str,
    now: str,
    expires_at: str,
) -> tuple[str, dict | None]:
    """Acquire ownership for one session, or report why it cannot.

    Session existence and state, current ownership, and the new row are all
    resolved inside one transaction, so two callers can never both observe "no
    active lease" and both insert one. An expired active lease is reported as
    requiring explicit takeover: expiration never authorizes acquisition.
    """
    with write_transaction(db_path) as connection:
        session = _fetch_session_row(connection, session_id)
        if session is None:
            return OUTCOME_SESSION_MISSING, None
        if session["ended_at"] is not None:
            return OUTCOME_SESSION_ALREADY_ENDED, None

        active = _fetch_active_lease_row(connection)
        if active is not None:
            if _is_stale(active["expires_at"], now):
                return OUTCOME_TAKEOVER_REQUIRED, active
            return OUTCOME_ACTIVE_CONFLICT, active

        _insert_lease_row(
            connection,
            lease_id=lease_id,
            session_id=session_id,
            acquired_at=now,
            last_heartbeat_at=now,
            expires_at=expires_at,
        )
        return OUTCOME_ACQUIRED, _fetch_lease_row(connection, lease_id)


def try_refresh_lease(
    db_path: Path,
    *,
    lease_id: str,
    session_id: str,
    now: str,
    expires_at: str,
) -> tuple[str, dict | None]:
    """Refresh a lease, but only for its owning session and only while fresh.

    A stale or non-active lease is reported without touching any timestamp, so a
    heartbeat can never revive ownership that has already lapsed.
    """
    with write_transaction(db_path) as connection:
        lease = _fetch_lease_row(connection, lease_id)
        if lease is None:
            return OUTCOME_LEASE_MISSING, None
        if lease["session_id"] != session_id:
            return OUTCOME_SESSION_MISMATCH, lease
        if lease["status"] != "active":
            return OUTCOME_LEASE_NOT_ACTIVE, lease
        if _is_stale(lease["expires_at"], now):
            return OUTCOME_LEASE_STALE, lease

        connection.execute(
            "UPDATE ownership_leases SET last_heartbeat_at = ?, expires_at = ? "
            "WHERE lease_id = ? AND session_id = ? AND status = 'active'",
            (now, expires_at, lease_id, session_id),
        )
        return OUTCOME_REFRESHED, _fetch_lease_row(connection, lease_id)


def try_release_lease(
    db_path: Path,
    *,
    lease_id: str,
    session_id: str,
) -> tuple[str, dict | None]:
    """Release a lease, but only for its owning session.

    Release is the current owner voluntarily giving up ownership rather than
    another session taking it, so a lapsed lease may still be released by its
    own owner. That lets a stalled session recover — or unblock the workspace
    for an ordinary acquisition — without waiting for a takeover. Releasing an
    already released lease is reported separately so the caller can treat it as
    a no-op.
    """
    with write_transaction(db_path) as connection:
        lease = _fetch_lease_row(connection, lease_id)
        if lease is None:
            return OUTCOME_LEASE_MISSING, None
        if lease["session_id"] != session_id:
            return OUTCOME_SESSION_MISMATCH, lease
        if lease["status"] == "released":
            return OUTCOME_ALREADY_RELEASED, lease
        if lease["status"] != "active":
            return OUTCOME_LEASE_NOT_ACTIVE, lease

        connection.execute(
            "UPDATE ownership_leases SET status = 'released' "
            "WHERE lease_id = ? AND session_id = ? AND status = 'active'",
            (lease_id, session_id),
        )
        return OUTCOME_RELEASED, _fetch_lease_row(connection, lease_id)


def try_takeover_lease(
    db_path: Path,
    *,
    expected_previous_lease_id: str,
    lease_id: str,
    session_id: str,
    now: str,
    expires_at: str,
) -> tuple[str, dict | None]:
    """Replace a stale active lease with a new one, atomically.

    The caller must name the lease it is taking over from, so a confirmation
    that went stale cannot retire a different, newer owner. Retiring the old
    lease and inserting the new one commit together or not at all: there is no
    committed state with two active leases, nor with none.
    """
    with write_transaction(db_path) as connection:
        session = _fetch_session_row(connection, session_id)
        if session is None:
            return OUTCOME_SESSION_MISSING, None
        if session["ended_at"] is not None:
            return OUTCOME_SESSION_ALREADY_ENDED, None

        active = _fetch_active_lease_row(connection)
        if active is None:
            return OUTCOME_NO_ACTIVE_LEASE, None
        if active["session_id"] == session_id:
            # Takeover means a successor session replacing a previous one. A
            # session recovering its own lapsed lease releases and reacquires
            # instead, so this can never work as a revival channel.
            return OUTCOME_SAME_SESSION_TAKEOVER, active
        if active["lease_id"] != expected_previous_lease_id:
            return OUTCOME_LEASE_CHANGED, active
        if not _is_stale(active["expires_at"], now):
            return OUTCOME_LEASE_FRESH, active

        connection.execute(
            "UPDATE ownership_leases SET status = 'expired' "
            "WHERE lease_id = ? AND status = 'active'",
            (active["lease_id"],),
        )
        _insert_lease_row(
            connection,
            lease_id=lease_id,
            session_id=session_id,
            acquired_at=now,
            last_heartbeat_at=now,
            expires_at=expires_at,
        )
        return OUTCOME_TAKEN_OVER, _fetch_lease_row(connection, lease_id)


def fetch_handoff(db_path: Path, handoff_id: str) -> dict | None:
    """Return one finalized handoff as a mapping, or ``None`` if it is absent.

    Reading is a plain read: it never acquires ownership and never touches a
    lease.
    """
    _require_database(db_path)

    connection = connect(db_path)
    try:
        return _fetch_handoff_row(connection, handoff_id)
    finally:
        connection.close()


def fetch_handoff_by_session(db_path: Path, from_session_id: str) -> dict | None:
    """Return the handoff produced by one session, or ``None`` if it has none.

    An exact lookup on ``from_session_id`` rather than "whatever is newest", so
    a caller can tell that a historical session already finalized even after a
    later session finalized its own handoff. Read-only.
    """
    _require_database(db_path)

    connection = connect(db_path)
    try:
        return _fetch_handoff_row_by_session(connection, from_session_id)
    finally:
        connection.close()


def fetch_latest_handoff(db_path: Path) -> dict | None:
    """Return the most recent finalized handoff, or ``None`` when there is none."""
    _require_database(db_path)

    connection = connect(db_path)
    try:
        row = connection.execute(_SELECT_LATEST_HANDOFF).fetchone()
    finally:
        connection.close()

    return None if row is None else dict(zip(_HANDOFF_COLUMNS, row))


def try_finalize_handoff(
    db_path: Path,
    *,
    handoff_id: str,
    from_session_id: str,
    created_at: str,
    semantic_context: str,
    objective_evidence: str,
) -> tuple[str, dict | None]:
    """Finalize the authoritative handoff of the session that owns the workspace.

    Authorization and the insert happen in one transaction, so ownership cannot
    change between the check and the write. A stale-but-active owner still holds
    the ownership slot and is therefore still allowed to finalize; this function
    neither releases nor expires anything, and finalization never implies
    ownership transfer.
    """
    with write_transaction(db_path) as connection:
        session = _fetch_session_row(connection, from_session_id)
        if session is None:
            return OUTCOME_SESSION_MISSING, None
        if session["ended_at"] is not None:
            return OUTCOME_SESSION_ALREADY_ENDED, None

        active = _fetch_active_lease_row(connection)
        if active is None:
            return OUTCOME_NO_ACTIVE_OWNER, None
        if active["session_id"] != from_session_id:
            return OUTCOME_NOT_ACTIVE_OWNER, active

        existing = _fetch_handoff_row_by_session(connection, from_session_id)
        if existing is not None:
            return OUTCOME_HANDOFF_ALREADY_EXISTS, existing

        _insert_handoff_row(
            connection,
            handoff_id=handoff_id,
            from_session_id=from_session_id,
            created_at=created_at,
            semantic_context=semantic_context,
            objective_evidence=objective_evidence,
        )
        return OUTCOME_HANDOFF_FINALIZED, _fetch_handoff_row(connection, handoff_id)


def _fetch_session_row(connection: sqlite3.Connection, session_id: str) -> dict | None:
    row = connection.execute(_SELECT_SESSION, (session_id,)).fetchone()
    return None if row is None else dict(zip(_SESSION_COLUMNS, row))


def _fetch_lease_row(connection: sqlite3.Connection, lease_id: str) -> dict | None:
    row = connection.execute(_SELECT_LEASE, (lease_id,)).fetchone()
    return None if row is None else dict(zip(_LEASE_COLUMNS, row))


def _fetch_active_lease_row(connection: sqlite3.Connection) -> dict | None:
    row = connection.execute(_SELECT_ACTIVE_LEASE).fetchone()
    return None if row is None else dict(zip(_LEASE_COLUMNS, row))


def _fetch_handoff_row(connection: sqlite3.Connection, handoff_id: str) -> dict | None:
    row = connection.execute(_SELECT_HANDOFF, (handoff_id,)).fetchone()
    return None if row is None else dict(zip(_HANDOFF_COLUMNS, row))


def _fetch_handoff_row_by_session(
    connection: sqlite3.Connection, from_session_id: str
) -> dict | None:
    row = connection.execute(_SELECT_HANDOFF_BY_SESSION, (from_session_id,)).fetchone()
    return None if row is None else dict(zip(_HANDOFF_COLUMNS, row))


def _insert_handoff_row(
    connection: sqlite3.Connection,
    *,
    handoff_id: str,
    from_session_id: str,
    created_at: str,
    semantic_context: str,
    objective_evidence: str,
) -> None:
    """Insert a finalized handoff.

    A failure here rolls the whole finalization back, which is what keeps a
    handoff persistence failure from releasing ownership as a side effect.
    """
    try:
        connection.execute(
            "INSERT INTO handoffs "
            "(handoff_id, from_session_id, created_at, semantic_context, objective_evidence) "
            "VALUES (?, ?, ?, ?, ?)",
            (handoff_id, from_session_id, created_at, semantic_context, objective_evidence),
        )
    except sqlite3.IntegrityError as exc:
        raise StorageError(
            f"cannot finalize handoff {handoff_id!r} for session {from_session_id!r}: {exc}"
        ) from exc


def _insert_lease_row(
    connection: sqlite3.Connection,
    *,
    lease_id: str,
    session_id: str,
    acquired_at: str,
    last_heartbeat_at: str,
    expires_at: str,
) -> None:
    """Insert a new active lease.

    A failure here is what makes a takeover roll back: the partial unique index
    and the session foreign key are the database's own backstops, and an
    injected failure leaves the previous lease untouched.
    """
    try:
        connection.execute(
            "INSERT INTO ownership_leases "
            "(lease_id, session_id, acquired_at, last_heartbeat_at, expires_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'active')",
            (lease_id, session_id, acquired_at, last_heartbeat_at, expires_at),
        )
    except sqlite3.IntegrityError as exc:
        raise StorageError(
            f"cannot record active lease {lease_id!r} for session {session_id!r}: {exc}"
        ) from exc


def _is_stale(expires_at: str, now: str) -> bool:
    """Report whether a lease is no longer fresh.

    Expiration is a statement about freshness only. It never changes ownership
    and never authorizes another session to acquire.
    """
    return _parse_timestamp(expires_at) <= _parse_timestamp(now)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StorageError(f"malformed timestamp in workspace database: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _require_database(db_path: Path) -> None:
    """Refuse to touch a database that does not exist, rather than creating one."""
    if not Path(db_path).is_file():
        raise StorageError(f"no workspace database at {db_path}")
