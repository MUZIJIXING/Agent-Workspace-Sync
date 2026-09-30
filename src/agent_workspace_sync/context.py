"""Bounded views of immutable handoffs; never rewrite history or grant ownership."""

from __future__ import annotations

import json

from .handoff import Handoff, HandoffError

DEFAULT_CONTEXT_MAX_CHARS = 8000


class ContextError(HandoffError):
    """A requested context view cannot be safely constructed."""


def validate_context_options(mode: str, max_chars: int) -> None:
    """Validate before any workflow acquires or releases ownership."""
    if mode not in ("full", "compact"):
        raise ContextError("context_mode must be full or compact")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1024 <= max_chars <= 64000:
        raise ContextError("context_max_chars must be an integer between 1024 and 64000")


def compact_handoff(handoff: Handoff, max_chars: int = DEFAULT_CONTEXT_MAX_CHARS) -> dict:
    """Return a deterministic character-bounded historical view, not new evidence.

    Budget counts compact JSON characters, not tokens or the protocol envelope.
    All continuation constraints (including unknown semantic fields) are kept
    together or omitted with a mandatory full-read warning. The view selects
    verification, a progress summary, changed files and unrun verification;
    repeated completion reports and other evidence stay in the full record.
    Selected values are admitted whole; no string or JSON value is cut in half.
    """
    validate_context_options("compact", max_chars)
    fields = [("semantic_context", key, value) for key, value in sorted(handoff.semantic_context.items())]
    fields += [("objective_evidence", key, value) for key, value in sorted(handoff.objective_evidence.items())]
    optional_semantic = {"progress_summary", "completed_work"}
    critical = [(section, key, value) for section, key, value in fields
                if section == "semantic_context" and key not in optional_semantic]
    payload = {
        "handoff_id": handoff.handoff_id,
        "from_session_id": handoff.from_session_id,
        "created_at": handoff.created_at,
        "semantic_context": {},
        "objective_evidence": {},
        "context_info": {},
    }

    def update_info(requires_full: bool) -> None:
        omitted = [f"{section}.{key}" for section, key, _ in fields if key not in payload[section]]
        pending = payload["semantic_context"].get("pending_work")
        payload["context_info"] = {
            "source": "historical_handoff",
            "verification": "recorded_not_reexecuted",
            "requires_full_context": requires_full,
            "pending_work_status": "empty" if pending == [] else "recorded" if pending is not None else "unknown",
            "omitted_fields": omitted[:16],
            "omitted_count": len(omitted),
            "retrieval_hint": "Use context_mode=full before development if requires_full_context; retrieve omitted evidence before relying on it.",
        }

    def fits() -> bool:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= max_chars

    for section, key, value in critical:
        payload[section][key] = value
    update_info(False)
    if not fits():
        payload["semantic_context"].clear()
        update_info(True)
        # Field names themselves may be huge. Counts and the full-read warning
        # remain even if the bounded list of omitted names cannot fit.
        if not fits():
            payload["context_info"]["omitted_fields"] = []
        if not fits():
            raise ContextError("handoff identity metadata exceeds the context budget")
        return payload

    order = [("objective_evidence", "verification"),
             ("semantic_context", "progress_summary"),
             ("objective_evidence", "changed_files"),
             ("objective_evidence", "unrun_verification")]
    optional = sorted((entry for entry in fields if entry[:2] in order),
                      key=lambda entry: order.index(entry[:2]))
    for section, key, value in optional:
        payload[section][key] = value
        update_info(False)
        if not fits():
            del payload[section][key]
            update_info(False)
    if not fits():
        payload["context_info"]["omitted_fields"] = []
    if not fits():
        raise ContextError("handoff identity metadata exceeds the context budget")
    return payload
