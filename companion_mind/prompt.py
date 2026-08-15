"""Deterministic prompt assembly from runtime-owned state."""

from __future__ import annotations

import json
from typing import Sequence

from .providers import ProviderMessage
from .state import RawEvent, RuntimeState


class PromptAssembler:
    """Build provider messages without transferring identity ownership."""

    def __init__(self, *, history_limit: int = 20) -> None:
        if history_limit < 0:
            raise ValueError("history_limit must not be negative")
        self.history_limit = history_limit

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

        persona = state.persona
        runtime_context = {
            "persona_id": persona.persona_id,
            "display_name": persona.display_name,
            "nickname": persona.nickname,
            "universe": persona.universe,
            "role": persona.identity.role,
            "core_traits": list(persona.core_traits),
            "voice": {
                "language": persona.voice.primary_language,
                "style": list(persona.voice.style),
            },
            "relationship": {
                "counterpart_id": state.relationship.counterpart_id,
                "counterpart": persona.relationship.counterpart,
                "status": state.relationship.relationship_status,
                "closeness_summary": state.relationship.closeness_summary,
                "recent_change": state.relationship.recent_change,
            },
            "conversation": state.conversation.model_dump(mode="json"),
            "hard_constraints": list(persona.hard_constraints),
        }
        instructions = (
            "Continue as the same runtime-owned persona. Never claim to be a new or "
            "replacement person. Do not reintroduce yourself unless the user explicitly "
            "asks. Preserve established relationship and shared history; do not invent "
            "missing history. Respond naturally in the configured primary language. "
            "Provider and thinking mode are implementation details and must not alter "
            "identity.\nRUNTIME_CONTEXT="
            + json.dumps(runtime_context, ensure_ascii=False, sort_keys=True)
        )
        messages: list[ProviderMessage] = [
            ProviderMessage(role="system", content=instructions)
        ]
        completed = [
            event
            for event in history
            if event.status == "complete" and event.role in {"user", "assistant"}
        ]
        for event in completed[-self.history_limit :]:
            messages.append(
                ProviderMessage(role=event.role, content=event.content)
            )
        messages.append(ProviderMessage(role="user", content=content))
        return tuple(messages)
