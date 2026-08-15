"""Provider-neutral chat contracts for replaceable model engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence


MessageRole = Literal["system", "user", "assistant"]


class ProviderError(RuntimeError):
    """Raised when a provider request or response cannot be trusted."""


@dataclass(frozen=True)
class ProviderMessage:
    role: MessageRole
    content: str

    def as_mapping(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ProviderResponse:
    provider: str
    model: str
    content: str
    response_id: str | None = None
    reasoning_content: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)


class ChatProvider(Protocol):
    """Runtime-facing provider boundary."""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def generate(
        self,
        messages: Sequence[ProviderMessage],
        *,
        thinking: bool = False,
    ) -> ProviderResponse: ...


class JsonTransport(Protocol):
    """Minimal injectable HTTP boundary used by provider adapters."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...
