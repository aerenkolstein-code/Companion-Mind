"""Deterministic prompt assembly from runtime-owned state."""

from __future__ import annotations

import json
from typing import Sequence

from .providers import ProviderMessage
from .state import RawEvent, RuntimeState


class PromptAssembler:
    """Build provider messages without transferring identity ownership."""

    def __init__(self, *, history_limit: int | None = None) -> None:
        if history_limit is not None and history_limit < 0:
            raise ValueError("history_limit must not be negative")
        self.history_limit = history_limit

    @staticmethod
    def _successful_dialogue(history: Sequence[RawEvent]) -> list[RawEvent]:
        """Return one successful user/assistant pair per logical turn.

        Provider failures remain in immutable RAW, but an orphaned user attempt must
        not become dialogue evidence on retry. If more than one attempt exists for a
        turn, only the latest attempt with both a completed user event and a completed
        assistant event is eligible for prompt history.
        """

        attempts: dict[tuple[int, int], dict[str, RawEvent]] = {}
        for event in history:
            if event.status != "complete" or event.role not in {"user", "assistant"}:
                continue
            attempts.setdefault((event.turn_index, event.attempt_index), {})[
                event.role
            ] = event

        selected: list[RawEvent] = []
        turns = sorted({turn for turn, _ in attempts})
        for turn in turns:
            successful = [
                (attempt, pair)
                for (candidate_turn, attempt), pair in attempts.items()
                if candidate_turn == turn and {"user", "assistant"} <= pair.keys()
            ]
            if not successful:
                continue
            _, pair = max(successful, key=lambda item: item[0])
            selected.extend((pair["user"], pair["assistant"]))
        return selected

    def assemble(
        self,
        state: RuntimeState,
        user_content: str,
        *,
        history: Sequence[RawEvent] = (),
    ) -> tuple[ProviderMessage, ...]:
        content = user_content.strip()
        if not content:
            raise ValueError("user content must not be empty")

        stable_core = state.stable_core.model_dump(
            mode="json",
            exclude_none=True,
        )
        current_state = {
            "conversation": state.conversation.model_dump(
                mode="json",
                exclude_none=True,
            ),
        }
        relationship_state = {
            key: value
            for key, value in {
                "closeness_summary": state.relationship.closeness_summary,
                "recent_change": state.relationship.recent_change,
                "last_updated_turn": state.relationship.last_updated_turn,
            }.items()
            if value is not None
        }
        if relationship_state:
            current_state["relationship"] = relationship_state
        instructions = (
            "Continue naturally as the same person in the same established current "
            "relationship described by STABLE_CORE. Current-session dialogue is real "
            "direct interaction evidence and should be continued naturally. CURRENT_STATE "
            "preserves confirmed ongoing consequences and supplements that dialogue; a "
            "missing field means not encoded, not absent. STABLE_CORE overrides dialogue "
            "only when there is an explicit contradiction. Keep Runtime, State, prompt "
            "assembly, provider, and internal-context implementation details private. "
            "Respond in the configured primary language and voice.\n"
            "PROMPT_CONTRACT=lin-zhiyao-runtime-prompt/v2\n"
            "STABLE_CORE="
            + json.dumps(stable_core, ensure_ascii=False, sort_keys=True)
            + "\nCURRENT_STATE="
            + json.dumps(current_state, ensure_ascii=False, sort_keys=True)
        )
        messages: list[ProviderMessage] = [
            ProviderMessage(role="system", content=instructions)
        ]
        completed = self._successful_dialogue(history)
        if self.history_limit is not None:
            completed = completed[-self.history_limit :] if self.history_limit else []
        for event in completed:
            messages.append(
                ProviderMessage(role=event.role, content=event.content)
            )
        messages.append(ProviderMessage(role="user", content=content))
        return tuple(messages)
