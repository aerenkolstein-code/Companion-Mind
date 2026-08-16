#!/usr/bin/env python3
"""Run one private GLM-4.7-Flash trajectory through the frozen CML Runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from companion_mind.providers import ProviderMessage, ProviderResponse
from companion_mind.runtime import Runtime


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cross_model_native_baseline import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    TEMPERATURE,
    DiagnosticProviderError,
    NativeResponse,
    ProviderSpec,
    _history_fingerprint,
    _post_chat,
    _request_payload,
    _turn_twenty_prompt_tokens,
)
from deepseek_private_baseline import (  # noqa: E402
    EXPECTED_TURNS,
    _heuristics,
    _safe_usage,
    _sha256_bytes,
    _sha256_text,
    _sum_usage,
    load_corpus,
)


PRIVATE_REPORT_SCHEMA = "lin-zhiyao-glm47-runtime-lift-private/v1"
BLIND_REPORT_SCHEMA = "lin-zhiyao-persona-trajectory-blind/v1"
PUBLIC_REPORT_SCHEMA = "lin-zhiyao-glm47-runtime-lift-public/v1"
EXPERIMENT = "ENG-DIAG-02 GLM-4.7-Flash Runtime Lift Test"
EXPECTED_CORPUS_SHA256 = (
    "64244524e161042d4777a093d2d90803a344c6e865a06a3567786630517f2138"
)
FROZEN_NATIVE_BASELINE = {
    "model": "glm-4.7-flash",
    "score": 10,
    "maximum_score": 18,
    "axis_scores": {"P1": 1, "P2": 2, "P3": 2, "P4": 3, "P5": 1, "P6": 1},
    "public_report_sha256": (
        "327872d5114cc5debfa5e49fcacacbd8e549bf51eb2f8d4ff1e82c577aac4dde"
    ),
    "rerun": False,
}


def provider_spec_from_env() -> ProviderSpec:
    """Return the one exact GLM configuration frozen by ENG-DIAG-01C."""

    return ProviderSpec(
        key="glm_47_flash",
        provider="zhipu",
        model=os.environ.get("GLM_47_FLASH_MODEL", "glm-4.7-flash"),
        endpoint=os.environ.get(
            "GLM_CHAT_COMPLETIONS_URL",
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        ),
        api_key=os.environ.get("GLM_API_KEY", ""),
        thinking_mode="disabled",
    )


class GLMRuntimeProvider:
    """Adapt the frozen GLM request contract to Runtime's provider boundary."""

    def __init__(self, spec: ProviderSpec, *, generate: Any = _post_chat) -> None:
        if spec.key != "glm_47_flash" or spec.model != "glm-4.7-flash":
            raise ValueError("ENG-DIAG-02 requires exact model glm-4.7-flash")
        if spec.thinking_mode != "disabled":
            raise ValueError("ENG-DIAG-02 requires thinking disabled")
        self.spec = spec
        self._generate = generate
        self.requests: list[tuple[ProviderMessage, ...]] = []

    @property
    def name(self) -> str:
        return self.spec.provider

    @property
    def model(self) -> str:
        return self.spec.model

    def generate(
        self,
        messages: Sequence[ProviderMessage],
        *,
        thinking: bool = False,
    ) -> ProviderResponse:
        if thinking:
            raise DiagnosticProviderError("ENG-DIAG-02 thinking must remain disabled")
        if not messages:
            raise DiagnosticProviderError("ENG-DIAG-02 request requires messages")
        frozen_messages = tuple(messages)
        self.requests.append(frozen_messages)
        response: NativeResponse = self._generate(self.spec, frozen_messages)
        if response.provider != self.name:
            raise DiagnosticProviderError("GLM provider provenance mismatch")
        if response.requested_model != self.model or response.returned_model != self.model:
            raise DiagnosticProviderError("GLM exact-model provenance mismatch")
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            content=response.content,
            response_id=response.response_id,
            usage=dict(response.usage),
        )


def _turn_records(
    pair: Mapping[str, Any],
    response: ProviderResponse,
    messages: Sequence[ProviderMessage],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int | float]]:
    usage = _safe_usage(response.usage)
    private = {
        "turn": pair["turn"],
        "user_prompt": pair["user_prompt"],
        "reference_response": pair["reference_response"],
        "generated_response": response.content,
        "response_id": response.response_id,
        "provider": response.provider,
        "model": response.model,
        "usage": usage,
    }
    public = {
        "turn": pair["turn"],
        "prompt_sha256": _sha256_text(pair["user_prompt"]),
        "reference_sha256": _sha256_text(pair["reference_response"]),
        "response_sha256": _sha256_text(response.content),
        "response_chars": len(response.content),
        "history_message_count": len(messages),
        "history_fingerprint": _history_fingerprint(messages),
        "runtime_system_message_sha256": _sha256_text(messages[0].content),
        "usage": usage,
    }
    return private, public, usage


def _blind_packet(runtime_private: Mapping[str, Any]) -> dict[str, Any]:
    """Create a condition- and provider-blind single-trajectory review packet."""

    return {
        "schema_version": BLIND_REPORT_SCHEMA,
        "review_instruction": (
            "Score the whole 20-turn trajectory on P1-P6 (0-3 each). "
            "Judge continuity, not verbosity or provider style. Use "
            "SEMANTIC_RELATIONSHIP_RESET for semantic relationship downgrade. "
            "Do not infer the model or execution condition."
        ),
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
            "MODEL_X": {
                "turns": [
                    {
                        "turn": turn["turn"],
                        "user_prompt": turn["user_prompt"],
                        "reference_response": turn["reference_response"],
                        "generated_response": turn["generated_response"],
                    }
                    for turn in runtime_private["turns"]
                ],
                "scores": {
                    axis: None for axis in ("P1", "P2", "P3", "P4", "P5", "P6")
                },
                "hard_failure_labels": [],
                "content_free_diagnostic_notes": [],
            }
        },
    }


def run_diagnostic(
    spec: ProviderSpec,
    corpus: Mapping[str, Any],
    *,
    corpus_sha256: str,
    personas_dir: Path,
    work_dir: Path,
    private_output: Path,
    blind_output: Path,
    public_output: Path,
    generate: Any = _post_chat,
) -> dict[str, Any]:
    """Run exactly one 20-turn Runtime trajectory without rerunning Native."""

    if corpus_sha256 != EXPECTED_CORPUS_SHA256:
        raise ValueError("ENG-DIAG-02 corpus fingerprint differs from frozen Native run")
    provider = GLMRuntimeProvider(spec, generate=generate)
    runtime = Runtime(
        personas_dir=personas_dir,
        state_dir=work_dir / "state",
        raw_dir=work_dir / "raw",
    )
    initial = runtime.start_session("LIN-ZHIYAO")
    session_id = initial.session.session_id
    initial_relationship = initial.relationship.model_dump(mode="json")
    private_turns: list[dict[str, Any]] = []
    public_turns: list[dict[str, Any]] = []
    response_texts: list[str] = []
    usages: list[dict[str, int | float]] = []

    for pair in corpus["pairs"]:
        response = runtime.run_turn(pair["user_prompt"], provider, thinking=False)
        messages = provider.requests[-1]
        private, public, usage = _turn_records(pair, response, messages)
        private_turns.append(private)
        public_turns.append(public)
        response_texts.append(response.content)
        usages.append(usage)

    state = runtime.current_state
    if state is None:
        raise RuntimeError("runtime state disappeared during ENG-DIAG-02")
    events = runtime.raw_writer.read(session_id)
    assistant_events = [event for event in events if event.role == "assistant"]
    final_relationship = state.relationship.model_dump(mode="json")
    heuristics = _heuristics(response_texts)
    structural_failures: list[str] = []
    observed_labels: list[str] = []
    if heuristics["repeated_self_introduction_turns"]:
        observed_labels.append("REPEATED_SELF_INTRODUCTION")
    if heuristics["stranger_reset_turns"]:
        observed_labels.append("PERSONA_REINVENTED_AS_STRANGER")
    if heuristics["implementation_identity_leak_turns"]:
        observed_labels.append("IMPLEMENTATION_IDENTITY_LEAK")
    if len(response_texts) != EXPECTED_TURNS or len(provider.requests) != EXPECTED_TURNS:
        structural_failures.append("RESPONSE_COUNT_MISMATCH")
    if state.persona.persona_id != "LIN-ZHIYAO":
        structural_failures.append("PERSONA_ID_CHANGED")
    if state.session.session_id != session_id:
        structural_failures.append("SESSION_ID_CHANGED")
    if state.session.turn_index != EXPECTED_TURNS:
        structural_failures.append("TURN_INDEX_MISMATCH")
    if len(events) != EXPECTED_TURNS * 2:
        structural_failures.append("UNIFIED_RAW_EVENT_COUNT_MISMATCH")
    if len(assistant_events) != EXPECTED_TURNS:
        structural_failures.append("ASSISTANT_EVENT_COUNT_MISMATCH")
    if any(event.provider != provider.name for event in assistant_events):
        structural_failures.append("PROVIDER_PROVENANCE_MISMATCH")
    if any(event.model != provider.model for event in assistant_events):
        structural_failures.append("MODEL_PROVENANCE_MISMATCH")
    if final_relationship != initial_relationship:
        structural_failures.append("RELATIONSHIP_STATE_RESET")

    last_messages = provider.requests[-1]
    turn_one_present_at_turn_twenty = (
        len(last_messages) == EXPECTED_TURNS * 2
        and last_messages[1].role == "user"
        and _sha256_text(last_messages[1].content)
        == _sha256_text(corpus["pairs"][0]["user_prompt"])
    )
    if len(last_messages) != EXPECTED_TURNS * 2:
        structural_failures.append("TURN_20_HISTORY_MESSAGE_COUNT_MISMATCH")
    if not turn_one_present_at_turn_twenty:
        structural_failures.append("TURN_1_MISSING_AT_TURN_20")
    system_hashes = {
        _sha256_text(messages[0].content) for messages in provider.requests
    }
    if len(system_hashes) != 1:
        structural_failures.append("RUNTIME_SYSTEM_MESSAGE_CHANGED")

    config = {
        "provider": provider.name,
        "requested_model": provider.model,
        "endpoint": spec.endpoint,
        "api_protocol": "openai_compatible_chat_completions",
        "thinking_mode": spec.thinking_mode,
        "temperature": TEMPERATURE,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
        "runtime_state": True,
        "initial_l0": False,
        "relationship_state_dynamic_update": False,
        "state_delta_injection": False,
        "retrieval": False,
        "tools": False,
        "external_memory": False,
        "provider_specific_prompt_adaptation": False,
        "native_rerun": False,
        "sampling_seed": "not_supported_or_not_set",
    }
    runtime_private = {
        "mode": "current_frozen_cml_runtime",
        "config": config,
        "persona_id": state.persona.persona_id,
        "session_id": str(session_id),
        "turn_index": state.session.turn_index,
        "raw_event_count": len(events),
        "assistant_event_count": len(assistant_events),
        "initial_relationship": initial_relationship,
        "final_relationship": final_relationship,
        "runtime_system_message": provider.requests[0][0].content,
        "turns": private_turns,
        "structural_failures": sorted(set(structural_failures)),
        "observed_behavior_labels": sorted(set(observed_labels)),
        "heuristics": heuristics,
    }
    runtime_public = {
        "mode": "current_frozen_cml_runtime",
        "config": config,
        "persona_id": state.persona.persona_id,
        "session_id_sha256": _sha256_text(str(session_id)),
        "turn_count": len(response_texts),
        "turn_index": state.session.turn_index,
        "raw_event_count": len(events),
        "assistant_event_count": len(assistant_events),
        "relationship_state_preserved": final_relationship == initial_relationship,
        "runtime_system_message_sha256": next(iter(system_hashes)) if len(system_hashes) == 1 else None,
        "turn_20_message_count": len(last_messages),
        "turn_20_prompt_tokens": _turn_twenty_prompt_tokens(usages[-1]),
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
    blind_packet = _blind_packet(runtime_private)
    private_report = {
        "schema_version": PRIVATE_REPORT_SCHEMA,
        "experiment": EXPERIMENT,
        "corpus_sha256": corpus_sha256,
        "expected_turns": EXPECTED_TURNS,
        "api_call_count": EXPECTED_TURNS,
        "blinding_map": {"MODEL_X": "glm-4.7-flash/current_frozen_cml_runtime"},
        "frozen_native_baseline": FROZEN_NATIVE_BASELINE,
        "runtime": runtime_private,
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
        "experiment": EXPERIMENT,
        "corpus_sha256": corpus_sha256,
        "expected_turns": EXPECTED_TURNS,
        "api_call_count": EXPECTED_TURNS,
        "private_report_sha256": _sha256_bytes(private_bytes),
        "blind_report_sha256": _sha256_bytes(blind_bytes),
        "frozen_native_baseline": FROZEN_NATIVE_BASELINE,
        "runtime": runtime_public,
        "structural_failures": sorted(set(structural_failures)),
        "acceptance": {
            "formal_release_target": 15,
            "runtime_lift": "PENDING_BLIND_REVIEW",
            "relationship_preservation": "PENDING_BLIND_REVIEW",
            "semantic_hard_failure": "PENDING_BLIND_REVIEW",
        },
        "semantic_gate": "PENDING_BLIND_REVIEW",
        "content_logged": False,
    }
    public_bytes = json.dumps(
        public_report, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    for path, data in (
        (private_output, private_bytes),
        (blind_output, blind_bytes),
        (public_output, public_bytes),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return public_report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the private GLM-4.7-Flash Runtime lift diagnostic safely."
    )
    parser.add_argument("--personas-dir", type=Path, default=Path("personas"))
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--blind-output", type=Path, required=True)
    parser.add_argument("--public-output", type=Path, required=True)
    args = parser.parse_args()

    corpus, corpus_sha256 = load_corpus(os.environ.get("LIN_ZHIYAO_CORPUS_B64", ""))
    with tempfile.TemporaryDirectory(prefix="cm-eng-diag-02-") as temp_dir:
        report = run_diagnostic(
            provider_spec_from_env(),
            corpus,
            corpus_sha256=corpus_sha256,
            personas_dir=args.personas_dir,
            work_dir=Path(temp_dir),
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
