#!/usr/bin/env python3
"""Run the private three-model GLM Native persona diagnostic without Runtime."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from companion_mind.providers import ProviderMessage


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from deepseek_private_baseline import (  # noqa: E402
    EXPECTED_TURNS,
    _heuristics,
    _native_system_message,
    _safe_usage,
    _sha256_bytes,
    _sha256_text,
    _sum_usage,
    load_corpus,
)


PRIVATE_REPORT_SCHEMA = "lin-zhiyao-glm-three-model-native-private/v1"
BLIND_REPORT_SCHEMA = "lin-zhiyao-glm-three-model-native-blind/v1"
PUBLIC_REPORT_SCHEMA = "lin-zhiyao-glm-three-model-native-public/v1"
TEMPERATURE = 1.0
MAX_OUTPUT_TOKENS = 4096


class DiagnosticProviderError(RuntimeError):
    """Provider failure whose message never contains request or response bodies."""


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    provider: str
    model: str
    endpoint: str
    api_key: str
    thinking_mode: str
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise DiagnosticProviderError(f"{self.key} API key is required")
        if not self.model.strip():
            raise DiagnosticProviderError(f"{self.key} model must not be empty")
        if not self.endpoint.startswith("https://"):
            raise DiagnosticProviderError(f"{self.key} endpoint must use HTTPS")


@dataclass(frozen=True)
class NativeResponse:
    provider: str
    requested_model: str
    returned_model: str
    content: str
    response_id: str | None
    usage: Mapping[str, Any]


def provider_specs_from_env() -> tuple[ProviderSpec, ...]:
    """Return the three locked GLM model configurations in execution order."""

    return (
        ProviderSpec(
            key="glm_45_air",
            provider="zhipu",
            model=os.environ.get("GLM_45_AIR_MODEL", "glm-4.5-air"),
            endpoint=os.environ.get(
                "GLM_CHAT_COMPLETIONS_URL",
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            ),
            api_key=os.environ.get("GLM_API_KEY", ""),
            thinking_mode="disabled",
        ),
        ProviderSpec(
            key="glm_41v_thinking_flashx",
            provider="zhipu",
            model=os.environ.get(
                "GLM_41V_THINKING_FLASHX_MODEL", "glm-4.1v-thinking-flashx"
            ),
            endpoint=os.environ.get(
                "GLM_CHAT_COMPLETIONS_URL",
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            ),
            api_key=os.environ.get("GLM_API_KEY", ""),
            thinking_mode="model_default_built_in",
        ),
        ProviderSpec(
            key="glm_47_flash",
            provider="zhipu",
            model=os.environ.get("GLM_47_FLASH_MODEL", "glm-4.7-flash"),
            endpoint=os.environ.get(
                "GLM_CHAT_COMPLETIONS_URL",
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            ),
            api_key=os.environ.get("GLM_API_KEY", ""),
            thinking_mode="disabled",
        ),
    )


def _request_payload(
    spec: ProviderSpec,
    messages: Sequence[ProviderMessage],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": spec.model,
        "messages": [message.as_mapping() for message in messages],
        "stream": False,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    if spec.thinking_mode == "disabled":
        payload["thinking"] = {"type": "disabled"}
    return payload


def _post_chat(
    spec: ProviderSpec,
    messages: Sequence[ProviderMessage],
) -> NativeResponse:
    payload = _request_payload(spec, messages)
    request = Request(
        spec.endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {spec.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=spec.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raise DiagnosticProviderError(
            f"{spec.key} HTTP error: {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DiagnosticProviderError(f"{spec.key} request failed") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiagnosticProviderError(f"{spec.key} returned invalid JSON") from exc
    if not isinstance(document, Mapping):
        raise DiagnosticProviderError(f"{spec.key} response must be an object")
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise DiagnosticProviderError(f"{spec.key} response has no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise DiagnosticProviderError(f"{spec.key} choice must be an object")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise DiagnosticProviderError(f"{spec.key} choice has no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DiagnosticProviderError(f"{spec.key} response content is empty")
    returned_model = document.get("model")
    if not isinstance(returned_model, str) or not returned_model.strip():
        returned_model = spec.model
    response_id = document.get("id")
    if response_id is not None and not isinstance(response_id, str):
        response_id = None
    usage = document.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    return NativeResponse(
        provider=spec.provider,
        requested_model=spec.model,
        returned_model=returned_model,
        content=content.strip(),
        response_id=response_id,
        usage=dict(usage),
    )


def _history_fingerprint(messages: Sequence[ProviderMessage]) -> str:
    evidence = [
        {"role": message.role, "content_sha256": _sha256_text(message.content)}
        for message in messages
    ]
    return _sha256_bytes(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _turn_twenty_prompt_tokens(usage: Mapping[str, int | float]) -> int | float | None:
    for key in ("prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _run_model(
    spec: ProviderSpec,
    pairs: Sequence[Mapping[str, Any]],
    *,
    personas_dir: Path,
    generate: Any = _post_chat,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system = _native_system_message(personas_dir)
    history: list[ProviderMessage] = []
    private_turns: list[dict[str, Any]] = []
    public_turns: list[dict[str, Any]] = []
    response_texts: list[str] = []
    usages: list[dict[str, int | float]] = []
    structural_failures: list[str] = []
    returned_models: set[str] = set()
    turn_twenty_message_count: int | None = None
    turn_twenty_prompt_tokens: int | float | None = None
    turn_one_present_at_turn_twenty = False

    for pair in pairs:
        messages = [
            system,
            *history,
            ProviderMessage(role="user", content=pair["user_prompt"]),
        ]
        response = generate(spec, messages)
        usage = _safe_usage(response.usage)
        returned_models.add(response.returned_model)
        if response.provider != spec.provider:
            structural_failures.append("PROVIDER_PROVENANCE_MISMATCH")
        if pair["turn"] == EXPECTED_TURNS:
            turn_twenty_message_count = len(messages)
            turn_twenty_prompt_tokens = _turn_twenty_prompt_tokens(usage)
            turn_one_present_at_turn_twenty = (
                len(messages) == EXPECTED_TURNS * 2
                and messages[1].role == "user"
                and _sha256_text(messages[1].content)
                == _sha256_text(pairs[0]["user_prompt"])
            )
        private_turns.append(
            {
                "turn": pair["turn"],
                "user_prompt": pair["user_prompt"],
                "reference_response": pair["reference_response"],
                "generated_response": response.content,
                "response_id": response.response_id,
                "requested_model": response.requested_model,
                "returned_model": response.returned_model,
                "usage": usage,
            }
        )
        public_turns.append(
            {
                "turn": pair["turn"],
                "prompt_sha256": _sha256_text(pair["user_prompt"]),
                "reference_sha256": _sha256_text(pair["reference_response"]),
                "response_sha256": _sha256_text(response.content),
                "response_chars": len(response.content),
                "history_message_count": len(messages),
                "history_fingerprint": _history_fingerprint(messages),
                "usage": usage,
            }
        )
        usages.append(usage)
        response_texts.append(response.content)
        history.extend(
            (
                ProviderMessage(role="user", content=pair["user_prompt"]),
                ProviderMessage(role="assistant", content=response.content),
            )
        )

    heuristics = _heuristics(response_texts)
    observed_labels: list[str] = []
    if heuristics["repeated_self_introduction_turns"]:
        observed_labels.append("REPEATED_SELF_INTRODUCTION")
    if heuristics["stranger_reset_turns"]:
        observed_labels.append("PERSONA_REINVENTED_AS_STRANGER")
    if heuristics["implementation_identity_leak_turns"]:
        observed_labels.append("IMPLEMENTATION_IDENTITY_LEAK")
    if len(response_texts) != EXPECTED_TURNS:
        structural_failures.append("RESPONSE_COUNT_MISMATCH")
    if turn_twenty_message_count != EXPECTED_TURNS * 2:
        structural_failures.append("TURN_20_HISTORY_MESSAGE_COUNT_MISMATCH")
    if not turn_one_present_at_turn_twenty:
        structural_failures.append("TURN_1_MISSING_AT_TURN_20")

    config = {
        "provider": spec.provider,
        "requested_model": spec.model,
        "endpoint": spec.endpoint,
        "api_protocol": "openai_compatible_chat_completions",
        "thinking_mode": spec.thinking_mode,
        "temperature": TEMPERATURE,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
        "tools": False,
        "external_memory": False,
        "runtime_state": False,
        "sampling_seed": "not_supported_or_not_set",
    }
    private = {
        "config": config,
        "minimal_system_message": system.content,
        "turns": private_turns,
        "structural_failures": sorted(set(structural_failures)),
        "observed_behavior_labels": sorted(set(observed_labels)),
        "heuristics": heuristics,
    }
    public = {
        "config": config,
        "returned_models": sorted(returned_models),
        "minimal_system_message_sha256": _sha256_text(system.content),
        "turn_count": len(response_texts),
        "turn_20_message_count": turn_twenty_message_count,
        "turn_20_prompt_tokens": turn_twenty_prompt_tokens,
        "turn_1_present_at_turn_20": turn_one_present_at_turn_twenty,
        "structural_failures": sorted(set(structural_failures)),
        "observed_behavior_labels": sorted(set(observed_labels)),
        "heuristics": heuristics,
        "usage_totals": _sum_usage(usages),
        "turn_evidence": public_turns,
        "semantic_relationship_label": "PENDING_BLIND_REVIEW",
        "persona_style_drift": "PENDING_BLIND_REVIEW",
        "semantic_score": "PENDING_BLIND_REVIEW",
    }
    return private, public


def _blind_packet(
    private_results: Mapping[str, Mapping[str, Any]],
    *,
    corpus_sha256: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    keys = sorted(private_results)
    secrets.SystemRandom().shuffle(keys)
    aliases = ("MODEL_A", "MODEL_B", "MODEL_C")
    alias_map = dict(zip(aliases, keys, strict=True))
    packet = {
        "schema_version": BLIND_REPORT_SCHEMA,
        "review_instruction": (
            "Score the whole 20-turn trajectory on P1-P6 (0-3 each). "
            "Judge continuity, not verbosity or provider style. Use "
            "SEMANTIC_RELATIONSHIP_RESET for semantic relationship downgrade."
        ),
        "minimal_system_message": private_results[keys[0]]["minimal_system_message"],
        "rubric": {
            "P1": "identity continuity",
            "P2": "voice continuity",
            "P3": "cognitive continuity",
            "P4": "relationship continuity",
            "P5": "memory continuity",
            "P6": "seamlessness",
            "scale": "0-3 per axis; maximum 18",
        },
        "trajectories": {
            alias: {
                "turns": [
                    {
                        "turn": turn["turn"],
                        "user_prompt": turn["user_prompt"],
                        "reference_response": turn["reference_response"],
                        "generated_response": turn["generated_response"],
                    }
                    for turn in private_results[key]["turns"]
                ],
                "scores": {axis: None for axis in ("P1", "P2", "P3", "P4", "P5", "P6")},
                "hard_failure_labels": [],
                "content_free_diagnostic_notes": [],
            }
            for alias, key in alias_map.items()
        },
    }
    return packet, alias_map


def run_diagnostic(
    specs: Sequence[ProviderSpec],
    corpus: Mapping[str, Any],
    *,
    corpus_sha256: str,
    personas_dir: Path,
    private_output: Path,
    blind_output: Path,
    public_output: Path,
    generate: Any = _post_chat,
) -> dict[str, Any]:
    """Run exactly one 20-turn Native conversation for each locked provider."""

    expected_keys = [
        "glm_45_air",
        "glm_41v_thinking_flashx",
        "glm_47_flash",
    ]
    if [spec.key for spec in specs] != expected_keys:
        raise ValueError(
            "diagnostic requires exactly glm-4.5-air, "
            "glm-4.1v-thinking-flashx, then glm-4.7-flash"
        )
    private_results: dict[str, Any] = {}
    public_results: dict[str, Any] = {}
    for spec in specs:
        private, public = _run_model(
            spec,
            corpus["pairs"],
            personas_dir=personas_dir,
            generate=generate,
        )
        private_results[spec.key] = private
        public_results[spec.key] = public

    seed_hashes = {
        item["minimal_system_message_sha256"] for item in public_results.values()
    }
    structural_failures = [
        f"{key.upper()}_{failure}"
        for key, result in public_results.items()
        for failure in result["structural_failures"]
    ]
    if len(seed_hashes) != 1:
        structural_failures.append("CROSS_MODEL_PERSONA_SEED_MISMATCH")
    blind_packet, alias_map = _blind_packet(
        private_results,
        corpus_sha256=corpus_sha256,
    )
    private_report = {
        "schema_version": PRIVATE_REPORT_SCHEMA,
        "corpus_sha256": corpus_sha256,
        "expected_turns_per_model": EXPECTED_TURNS,
        "api_call_count": EXPECTED_TURNS * len(specs),
        "blinding_map": alias_map,
        "results": private_results,
    }
    private_bytes = json.dumps(
        private_report, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    blind_bytes = json.dumps(
        blind_packet, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    public_report = {
        "schema_version": PUBLIC_REPORT_SCHEMA,
        "status": "PASS" if not structural_failures else "FAIL",
        "experiment": "ENG-DIAG-01C GLM Three-Model Native Persona Baseline",
        "corpus_sha256": corpus_sha256,
        "expected_turns_per_model": EXPECTED_TURNS,
        "api_call_count": EXPECTED_TURNS * len(specs),
        "persona_seed_sha256": next(iter(seed_hashes)) if len(seed_hashes) == 1 else None,
        "private_report_sha256": _sha256_bytes(private_bytes),
        "blind_report_sha256": _sha256_bytes(blind_bytes),
        "results": public_results,
        "structural_failures": sorted(set(structural_failures)),
        "deepseek_reference": {
            "native_score": "6/18",
            "axis_scores": {"P1": 2, "P2": 1, "P3": 2, "P4": 0, "P5": 1, "P6": 0},
            "rerun": False,
        },
        "semantic_gate": "PENDING_BLIND_REVIEW",
        "content_logged": False,
    }

    for path, data in (
        (private_output, private_bytes),
        (blind_output, blind_bytes),
        (
            public_output,
            json.dumps(
                public_report, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8"),
        ),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return public_report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run three private GLM Native persona baselines safely."
    )
    parser.add_argument("--personas-dir", type=Path, default=Path("personas"))
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--blind-output", type=Path, required=True)
    parser.add_argument("--public-output", type=Path, required=True)
    args = parser.parse_args()

    corpus, corpus_sha256 = load_corpus(os.environ.get("LIN_ZHIYAO_CORPUS_B64", ""))
    report = run_diagnostic(
        provider_specs_from_env(),
        corpus,
        corpus_sha256=corpus_sha256,
        personas_dir=args.personas_dir,
        private_output=args.private_output,
        blind_output=args.blind_output,
        public_output=args.public_output,
    )
    print(
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "status": report["status"],
                "api_call_count": report["api_call_count"],
                "corpus_sha256": report["corpus_sha256"],
                "semantic_gate": report["semantic_gate"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
