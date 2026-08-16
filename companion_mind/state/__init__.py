"""Runtime state contracts and persistence."""

from .models import (
    ConversationState,
    RawEvent,
    RelationshipCore,
    RelationshipState,
    RuntimeState,
    SessionState,
    StableCore,
)
from .delta_store import DeltaStoreError, JsonlDeltaStore
from .store import JsonStateStore, StateStoreError
from .transitions import (
    DeterministicStateReducer,
    ObserverInput,
    StateChangeCandidate,
    StateDeltaCandidate,
    StateDeltaRecord,
    StateTransitionError,
    StateTransitionResult,
    replay_runtime_state,
)

__all__ = [
    "ConversationState",
    "DeltaStoreError",
    "DeterministicStateReducer",
    "JsonStateStore",
    "JsonlDeltaStore",
    "ObserverInput",
    "RawEvent",
    "RelationshipCore",
    "RelationshipState",
    "RuntimeState",
    "SessionState",
    "StableCore",
    "StateChangeCandidate",
    "StateDeltaCandidate",
    "StateDeltaRecord",
    "StateStoreError",
    "StateTransitionError",
    "StateTransitionResult",
    "replay_runtime_state",
]
