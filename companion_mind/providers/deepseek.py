"""DeepSeek V4 adapter using the provider-neutral runtime contract."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import (
    JsonTransport,
    ProviderError,
    ProviderMessage,
    ProviderResponse,
)


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ProviderError("DEEPSEEK_API_KEY is required")
        if not self.model.strip():
            raise ProviderError("DEEPSEEK_MODEL must not be empty")
        if not self.base_url.startswith("https://"):
            raise ProviderError("DeepSeek base_url must use https")
        if self.timeout_seconds <= 0:
            raise ProviderError("DeepSeek timeout must be positive")

    @classmethod
    def from_env(cls) -> "DeepSeekConfig":
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )


class UrllibJsonTransport:
    """Small standard-library JSON transport with sanitized errors."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise ProviderError(f"DeepSeek HTTP error: {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError("DeepSeek request failed") from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("DeepSeek returned invalid JSON") from exc
        if not isinstance(document, Mapping):
            raise ProviderError("DeepSeek response must be an object")
        return document


class DeepSeekProvider:
    """OpenAI-compatible adapter for DeepSeek V4 Flash."""

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibJsonTransport()

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def model(self) -> str:
        return self.config.model

    def generate(
        self,
        messages: Sequence[ProviderMessage],
        *,
        thinking: bool = False,
    ) -> ProviderResponse:
        if not messages:
            raise ProviderError("DeepSeek request requires messages")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.as_mapping() for message in messages],
            "stream": False,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        document = self.transport.post_json(
            url=f"{self.config.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout_seconds=self.config.timeout_seconds,
        )
        return self._parse_response(document)

    def _parse_response(self, document: Mapping[str, Any]) -> ProviderResponse:
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("DeepSeek response has no choices")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderError("DeepSeek choice must be an object")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError("DeepSeek choice has no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("DeepSeek response content is empty")
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ProviderError("DeepSeek reasoning_content must be text")
        response_id = document.get("id")
        if response_id is not None and not isinstance(response_id, str):
            response_id = None
        usage = document.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            content=content.strip(),
            response_id=response_id,
            reasoning_content=reasoning,
            usage=dict(usage),
        )
