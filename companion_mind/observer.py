"""Replaceable turn-level State Observer boundary for Runtime Contract v2."""

from __future__ import annotations

from typing import Protocol

from .state.transitions import ObserverInput, StateDeltaCandidate


class StateObserver(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def observe(self, observer_input: ObserverInput) -> StateDeltaCandidate: ...


class NullStateObserver:
    """Deterministic no-change observer used until a live lane is authorized."""

    name = "runtime"
    model = "deterministic-empty-observer/v2"

    def observe(self, observer_input: ObserverInput) -> StateDeltaCandidate:
        del observer_input
        return StateDeltaCandidate(changes=())
