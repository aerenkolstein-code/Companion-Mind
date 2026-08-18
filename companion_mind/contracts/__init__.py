"""Versioned engineering contracts shared by Companion-Mind adapters."""

from .canonical_event_v1 import (
    CanonicalEvent,
    ContractValidationError,
    authority_snapshot_after_event,
    canonical_order_key,
    duplicate_event_ids,
    knowledge_state,
    stable_identity,
    validate_canonical_event,
)

__all__ = [
    "CanonicalEvent",
    "ContractValidationError",
    "authority_snapshot_after_event",
    "canonical_order_key",
    "duplicate_event_ids",
    "knowledge_state",
    "stable_identity",
    "validate_canonical_event",
]
