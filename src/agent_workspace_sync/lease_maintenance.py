"""Activity-triggered maintenance for participating client adapters only."""

from datetime import datetime

from .ownership import OwnershipError
from .workflow import FRESHNESS_FRESH, InspectWorkspaceResult, heartbeat_workspace


def refresh_for_activity(view: InspectWorkspaceResult, harness: str, instance: str) -> None:
    """Keep this fresh owner's chosen TTL, using the core's transactional check.

    There is no acquisition, background timer or stale-lease recovery here.
    A failure must prevent the adapter from allowing the protected operation.
    """
    owner, lease = view.owner_session, view.lease
    if (
        owner is None or lease is None
        or not instance or owner.harness_type != harness
        or owner.harness_instance_id != instance
        or owner.session_id != lease.session_id
        or owner.ended_at is not None
        or view.lease_freshness != FRESHNESS_FRESH
    ):
        raise OwnershipError("Cannot maintain ownership for this client instance.")
    try:
        heartbeat = datetime.fromisoformat(lease.last_heartbeat_at)
        expires = datetime.fromisoformat(lease.expires_at)
        ttl = (expires - heartbeat).total_seconds()
        if heartbeat.tzinfo is None or expires.tzinfo is None or ttl <= 0 or not ttl.is_integer():
            raise ValueError("invalid recorded TTL")
    except (TypeError, ValueError, OverflowError) as error:
        raise OwnershipError("Cannot verify the current ownership duration.") from error
    heartbeat_workspace(view.workspace_root, owner.session_id, lease.lease_id, int(ttl))
