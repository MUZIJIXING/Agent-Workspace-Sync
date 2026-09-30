"""Harness-neutral ownership decisions for participating-harness guards.

This module knows only the identity requesting access and a normalized snapshot
of workspace ownership.  Platform adapters remain responsible for workspace
discovery, path resolution, tool parsing, recovery wording, and hook output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GuardFreshness(str, Enum):
    """Freshness values understood by the shared guard policy."""

    FRESH = "fresh"
    STALE = "stale"


class GuardDecision(str, Enum):
    """The semantic result of one ownership access check."""

    ALLOW = "allow"
    NO_OWNER = "no_owner"
    FOREIGN_FRESH_OWNER = "foreign_fresh_owner"
    FOREIGN_STALE_OWNER = "foreign_stale_owner"
    CURRENT_OWNER_STALE = "current_owner_stale"
    INVALID_STATE = "invalid_state"

    @property
    def allowed(self) -> bool:
        """Whether the participating harness may perform the guarded action."""
        return self is GuardDecision.ALLOW


@dataclass(frozen=True)
class GuardIdentity:
    """The real harness instance requesting access."""

    harness_type: str
    harness_instance_id: str


@dataclass(frozen=True)
class WorkspaceGuardState:
    """The minimum normalized ownership state needed for a guard decision."""

    managed: bool
    owner_session_exists: bool = False
    active_lease_exists: bool = False
    owner_harness_type: str | None = None
    owner_harness_instance_id: str | None = None
    owner_freshness: GuardFreshness | None = None


def decide_workspace_access(
    identity: GuardIdentity,
    state: WorkspaceGuardState,
) -> GuardDecision:
    """Decide whether ``identity`` may access the normalized workspace state.

    The function is pure: it performs no discovery, persistence, ownership
    transition, heartbeat, release, or takeover.
    """
    if not state.managed:
        return GuardDecision.ALLOW if _has_no_owner_data(state) else GuardDecision.INVALID_STATE

    if not state.owner_session_exists and not state.active_lease_exists:
        return GuardDecision.NO_OWNER if _has_no_owner_data(state) else GuardDecision.INVALID_STATE

    if not state.owner_session_exists or not state.active_lease_exists:
        return GuardDecision.INVALID_STATE

    if (
        not state.owner_harness_type
        or not state.owner_harness_instance_id
        or state.owner_freshness not in (GuardFreshness.FRESH, GuardFreshness.STALE)
    ):
        return GuardDecision.INVALID_STATE

    current_owner = (
        state.owner_harness_type == identity.harness_type
        and state.owner_harness_instance_id == identity.harness_instance_id
    )

    if current_owner:
        if state.owner_freshness is GuardFreshness.FRESH:
            return GuardDecision.ALLOW
        return GuardDecision.CURRENT_OWNER_STALE

    if state.owner_freshness is GuardFreshness.FRESH:
        return GuardDecision.FOREIGN_FRESH_OWNER
    return GuardDecision.FOREIGN_STALE_OWNER


def _has_no_owner_data(state: WorkspaceGuardState) -> bool:
    return (
        not state.owner_session_exists
        and not state.active_lease_exists
        and state.owner_harness_type is None
        and state.owner_harness_instance_id is None
        and state.owner_freshness is None
    )
