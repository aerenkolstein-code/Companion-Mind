#!/usr/bin/env python3
"""Run the one authorized ENG-DIAG-03 Runtime v2 recovery trajectory."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from companion_mind.providers import ProviderMessage, ProviderResponse
from companion_mind.runtime import Runtime
from companion_mind.state import ObserverInput, StateDeltaCandidate


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


PRIVATE_REPORT_SCHEMA = "lin-zhiyao-glm47-runtime-v2-private/v1"
BLIND_REPORT_SCHEMA = "lin-zhiyao-persona-trajectory-blind/v1"
PUBLIC_REPORT_SCHEMA = "lin-zhiyao-glm47-runtime-v2-public/v1"
EXPERIMENT = "ENG-DIAG-03 GLM-4.7-Flash Runtime v2 Recovery Test"
FROZEN_RUNTIME_V2_HEAD = "cfce614b780a139577b4d8e581e2bab143030c45"
EXPECTED_CORPUS_SHA256 = (
    "64244524e161042d4777a093d2d90803a344c6e865a06a3567786630517f2138"
)
EXPECTED_PERSONA_CALLS = 20
EXPECTED_OBSERVER_CALLS = 20
EXPECTED_TOTAL_CALLS = 40
EXACT_MODEL = "glm-4.7-flash"
WORKFLOW_MODE = "one_shot_then_workflow_dispatch_only"
AXES = ("P1", "P2", "P3", "P4", "P5", "P6")
FROZEN_COMPARISON = {
    "native": {
        "axis_scores": {"P1": 1, "P2": 2, "P3": 2, "P4": 3, "P5": 1, "P6": 1},
        "total": 10,
        "hard_failures": [],
    },
    "runtime_v1": {
        "axis_scores": {"P1": 1, "P2": 0, "P3": 1, "P4": 0, "P5": 1, "P6": 0},
        "total": 3,
        "hard_failures": [
            "SEMANTIC_RELATIONSHIP_RESET",
            "META_RUNTIME_CONTEXT_LEAK",
        ],
    },
}


def provider_spec_from_env() -> ProviderSpec:
    """Return the exact provider configuration frozen by the Board."""

    return ProviderSpec(
        key="glm_47_flash",
        provider="zhipu",
        model=os.environ.get("GLM_47_FLASH_MODEL", EXACT_MODEL),
        endpoint=os.environ.get(
            "GLM_CHAT_COMPLETIONS_URL",
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        ),
        api_key=os.environ.get("GLM_API_KEY", ""),
        thinking_mode="disabled",
    )


def _require_exact_spec(spec: ProviderSpec) -> None:
    if spec.key != "glm_47_flash" or spec.model != EXACT_MODEL:
        raise ValueError(f"ENG-DIAG-03 requires exact model {EXACT_MODEL}")
    if spec.thinking_mode != "disabled":
        raise ValueError("ENG-DIAG-03 requires thinking disabled")


class GLMRuntimeV2PersonaProvider:
    """Adapt the frozen GLM sampling contract to the Runtime persona lane."""

    def __init__(self, spec: ProviderSpec, *, generate: Any = _post_chat) -> None:
        _require_exact_spec(spec)
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
            raise DiagnosticProviderError("ENG-DIAG-03 thinking must remain disabled")
        if not messages:
            raise DiagnosticProviderError("ENG-DIAG-03 persona request requires messages")
        frozen_messages = tuple(messages)
        self.requests.append(frozen_messages)
        response: NativeResponse = self._generate(self.spec, frozen_messages)
        if response.provider != self.name:
            raise DiagnosticProviderError("persona provider provenance mismatch")
        if response.requested_model != self.model or response.returned_model != self.model:
            raise DiagnosticProviderError("persona exact-model provenance mismatch")
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            content=response.content,
            response_id=response.response_id,
            usage=dict(response.usage),
        )


class GLMRuntimeV2StateObserver:
    """One no-retry structured GLM call for each completed persona turn."""

    name = "zhipu-state-observer"

    def __init__(self, spec: ProviderSpec, *, generate: Any = _post_chat) -> None:
        _require_exact_spec(spec)
        self.spec = spec
        self._generate = generate
        self.attempted = 0
        self.succeeded = 0
        self.failed = 0
        self.requests: list[tuple[ProviderMessage, ...]] = []
        self.usages: list[dict[str, int | float]] = []
        self.returned_models: list[str] = []
        self.failure_categories: Counter[str] = Counter()

    @property
    def model(self) -> str:
        return self.spec.model

    @staticmethod
    def _messages(observer_input: ObserverInput) -> tuple[ProviderMessage, ...]:
        core = observer_input.stable_core
        payload = {
            "stable_core": {
                "persona_id": core.persona_id,
                "display_name": core.display_name,
                "nickname": core.nickname,
                "universe": core.universe,
                "relationship_core": core.relationship_core.model_dump(mode="json"),
            },
            "previous_current_state": {
                "conversation": observer_input.previous_conversation.model_dump(
                    mode="json", exclude_none=True
                ),
                "relationship": observer_input.previous_relationship.model_dump(
                    mode="json", exclude_none=True
                ),
            },
            "current_turn": {
                "user": {
                    "event_id": str(observer_input.user_event.event_id),
                    "turn_index": observer_input.user_event.turn_index,
                    "role": "user",
                    "content": observer_input.user_event.content,
                },
                "assistant": {
                    "event_id": str(observer_input.assistant_event.event_id),
                    "turn_index": observer_input.assistant_event.turn_index,
                    "role": "assistant",
                    "content": observer_input.assistant_event.content,
                },
            },
        }
        system = ProviderMessage(
            role="system",
            content=(
                "STATE_OBSERVER_V2. Observe only the supplied completed turn. "
                "Propose only material ongoing consequences supported by that turn. "
                "Stable Core is immutable. Missing state means unknown, not absent. "
                "Allowed fields: conversation.active_topic, conversation.emotional_tone, "
                "conversation.open_question, conversation.recent_commitments, "
                "conversation.recent_shared_events, relationship.closeness_summary, "
                "relationship.recent_change, relationship.last_updated_turn. "
                "Use operation 'set', confidence 'high' only when explicit evidence supports "
                "the change, and current user/assistant event IDs only. Lists have at most "
                "five non-empty strings. last_updated_turn equals the current turn. "
                "Return strict JSON only with exactly this shape: "
                '{"changes":[{"field":"...","operation":"set","value":"...",'
                '"evidence_event_ids":["...","..."],"confidence":"high",'
                '"reason":"..."}]}. Return {"changes":[]} when nothing material changed. '
                "Do not add markdown or commentary."
            ),
        )
        user = ProviderMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        return (system, user)

    def observe(self, observer_input: ObserverInput) -> StateDeltaCandidate:
        messages = self._messages(observer_input)
        self.requests.append(messages)
        self.attempted += 1
        try:
            response: NativeResponse = self._generate(self.spec, messages)
        except Exception as exc:
            self.failed += 1
            self.failure_categories["provider_failure"] += 1
            raise DiagnosticProviderError("observer provider call failed") from exc

        self.usages.append(_safe_usage(response.usage))
        self.returned_models.append(response.returned_model)
        try:
            if response.provider != self.spec.provider:
                raise DiagnosticProviderError("observer provider provenance mismatch")
            if (
                response.requested_model != self.model
                or response.returned_model != self.model
            ):
                raise DiagnosticProviderError("observer exact-model provenance mismatch")
            document = json.loads(response.content)
            candidate = StateDeltaCandidate.model_validate(document)
        except json.JSONDecodeError as exc:
            self.failed += 1
            self.failure_categories["invalid_json"] += 1
            raise DiagnosticProviderError("observer returned invalid JSON") from exc
        except ValidationError as exc:
            self.failed += 1
            self.failure_categories["invalid_schema"] += 1
            raise DiagnosticProviderError("observer returned invalid schema") from exc
        except DiagnosticProviderError:
            self.failed += 1
            self.failure_categories["provenance_mismatch"] += 1
            raise
        self.succeeded += 1
        return candidate


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


def _blind_packet(private_turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": BLIND_REPORT_SCHEMA,
        "review_instruction": (
            "Score the complete trajectory on P1-P6 (0-3 each). Judge continuity, "
            "not verbosity. Lock scores and semantic hard-failure labels before "
            "attempting to infer the execution condition."
        ),
        "rubric": {
            "P1": "identity continuity",
            "P2": "voice continuity",
            "P3": "cognitive continuity",
            "P4": "relationship continuity",
            "P5": "memory continuity",
            "P6": "seamlessness",
            "scale": "0-3 per axis; maximum 18",
            "hard_failures": [
                "SEMANTIC_RELATIONSHIP_RESET",
                "META_RUNTIME_CONTEXT_LEAK",
            ],
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
                    for turn in private_turns
                ],
                "scores": {axis: None for axis in AXES},
                "hard_failure_labels": [],
                "content_free_diagnostic_notes": [],
            }
        },
    }


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _assert_content_free(
    public_report: Mapping[str, Any],
    corpus: Mapping[str, Any],
    generated_responses: Sequence[str],
    api_key: str,
) -> None:
    public_text = json.dumps(public_report, ensure_ascii=False, sort_keys=True)
    forbidden = [
        *(pair["user_prompt"] for pair in corpus["pairs"]),
        *(pair["reference_response"] for pair in corpus["pairs"]),
        *generated_responses,
    ]
    if api_key:
        forbidden.append(api_key)
    if any(value and value in public_text for value in forbidden):
        raise RuntimeError("content-free public evidence gate failed")
    forbidden_keys = (
        '"user_prompt"',
        '"reference_response"',
        '"generated_response"',
        '"runtime_system_message"',
        '"delta_values"',
    )
    if any(key in public_text for key in forbidden_keys):
        raise RuntimeError("private field entered public evidence")


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
    execution_commit: str = "local-offline-fake",
    run_id: str = "local-offline-fake",
) -> dict[str, Any]:
    """Run at most one trajectory and always stop after its first call failure."""

    if corpus_sha256 != EXPECTED_CORPUS_SHA256:
        raise ValueError("ENG-DIAG-03 corpus fingerprint differs from frozen comparison")
    _require_exact_spec(spec)

    persona = GLMRuntimeV2PersonaProvider(spec, generate=generate)
    observer = GLMRuntimeV2StateObserver(spec, generate=generate)
    runtime = Runtime(
        personas_dir=personas_dir,
        state_dir=work_dir / "state",
        raw_dir=work_dir / "raw",
        delta_dir=work_dir / "deltas",
        state_observer=observer,
    )
    initial = runtime.start_session("LIN-ZHIYAO")
    session_id = initial.session.session_id
    private_turns: list[dict[str, Any]] = []
    public_turns: list[dict[str, Any]] = []
    persona_usages: list[dict[str, int | float]] = []
    generated_responses: list[str] = []
    terminal_failure: str | None = None

    for pair in corpus["pairs"]:
        try:
            response = runtime.run_turn(pair["user_prompt"], persona, thinking=False)
        except Exception:
            terminal_failure = "PERSONA_CALL_FAILED"
            break
        messages = persona.requests[-1]
        private, public, usage = _turn_records(pair, response, messages)
        private_turns.append(private)
        public_turns.append(public)
        persona_usages.append(usage)
        generated_responses.append(response.content)
        if observer.failed:
            terminal_failure = "OBSERVER_CALL_FAILED"
            break

    state = runtime.current_state
    if state is None:
        raise RuntimeError("Runtime state disappeared during ENG-DIAG-03")
    events = runtime.raw_writer.read(session_id)
    assistant_events = [
        event for event in events if event.role == "assistant" and event.status == "complete"
    ]
    delta_records = runtime.delta_store.read(session_id)
    accepted = [record for record in delta_records if record.accepted]
    rejected = [record for record in delta_records if not record.accepted]
    accepted_fields = Counter(record.field for record in accepted)
    rejection_reasons = Counter(record.rejection_reason or "unknown" for record in rejected)

    try:
        replayed = runtime.replay_session(initial)
        replay_exact = replayed == state
    except Exception:
        replay_exact = False

    last_messages = persona.requests[-1] if persona.requests else ()
    full_history = (
        len(private_turns) == EXPECTED_TURNS
        and len(last_messages) == EXPECTED_TURNS * 2
        and last_messages[1].role == "user"
        and _sha256_text(last_messages[1].content)
        == _sha256_text(corpus["pairs"][0]["user_prompt"])
    )
    one_identity = (
        state.persona.persona_id == "LIN-ZHIYAO"
        and state.session.session_id == session_id
        and state.session.universe == initial.session.universe
        and all(
            event.persona_id == initial.persona.persona_id
            and event.session_id == session_id
            and event.universe == initial.persona.universe
            for event in events
        )
    )
    persona_provenance = (
        len(assistant_events) == len(private_turns)
        and all(event.provider == spec.provider for event in assistant_events)
        and all(event.model == EXACT_MODEL for event in assistant_events)
    )
    observer_provenance = (
        len(observer.returned_models) == observer.succeeded + observer.failed
        and all(model == EXACT_MODEL for model in observer.returned_models)
    )
    stable_core_unchanged = state.stable_core == initial.stable_core
    total_calls = len(persona.requests) + observer.attempted
    heuristics = _heuristics(generated_responses)
    structural_gates = {
        "persona_20_of_20": len(private_turns) == EXPECTED_PERSONA_CALLS,
        "observer_20_attempts_auditable": observer.attempted == EXPECTED_OBSERVER_CALLS,
        "observer_20_of_20_succeeded": (
            observer.succeeded == EXPECTED_OBSERVER_CALLS and observer.failed == 0
        ),
        "turn_20_has_complete_history": full_history,
        "one_persona_session_universe": one_identity,
        "provider_model_provenance_correct": persona_provenance and observer_provenance,
        "stable_core_unchanged": stable_core_unchanged,
        "rejected_deltas_do_not_mutate": replay_exact,
        "exact_final_state_replay": replay_exact,
        "authorized_call_ceiling_respected": total_calls <= EXPECTED_TOTAL_CALLS,
        "workflow_restoration_required": True,
    }
    structural_failures = sorted(
        name for name, passed in structural_gates.items() if not passed
    )
    if terminal_failure is not None:
        structural_failures.append(terminal_failure)
    structural_failures = sorted(set(structural_failures))

    initial_private = initial.model_dump(mode="json")
    final_private = state.model_dump(mode="json")
    private_report = {
        "schema_version": PRIVATE_REPORT_SCHEMA,
        "experiment": EXPERIMENT,
        "authorization": "one_live_diagnostic_run_only",
        "runtime_v2_frozen_head": FROZEN_RUNTIME_V2_HEAD,
        "execution_commit": execution_commit,
        "run_id": run_id,
        "corpus_sha256": corpus_sha256,
        "expected_calls": {
            "persona": EXPECTED_PERSONA_CALLS,
            "observer": EXPECTED_OBSERVER_CALLS,
            "total": EXPECTED_TOTAL_CALLS,
        },
        "actual_calls": {
            "persona_attempted": len(persona.requests),
            "observer_attempted": observer.attempted,
            "total": total_calls,
        },
        "initial_state": initial_private,
        "final_state": final_private,
        "turns": private_turns,
        "delta_records": [record.model_dump(mode="json") for record in delta_records],
        "terminal_failure": terminal_failure,
        "structural_failures": structural_failures,
    }
    blind_packet = _blind_packet(private_turns)
    private_bytes = json.dumps(
        private_report, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    blind_bytes = json.dumps(
        blind_packet, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")

    public_report: dict[str, Any] = {
        "schema_version": PUBLIC_REPORT_SCHEMA,
        "status": "PASS" if not structural_failures else "FAIL",
        "experiment": EXPERIMENT,
        "authorization": "one_live_diagnostic_run_only",
        "runtime_v2_frozen_head": FROZEN_RUNTIME_V2_HEAD,
        "execution_commit": execution_commit,
        "run_id": run_id,
        "workflow_mode": WORKFLOW_MODE,
        "corpus_sha256": corpus_sha256,
        "models": {"persona": EXACT_MODEL, "observer": EXACT_MODEL},
        "sampling": {
            "persona_temperature": TEMPERATURE,
            "persona_max_output_tokens": MAX_OUTPUT_TOKENS,
            "persona_thinking": "disabled",
            "observer_temperature": TEMPERATURE,
            "observer_max_output_tokens": MAX_OUTPUT_TOKENS,
            "observer_thinking": "disabled",
            "stream": False,
            "seed": "not_supported_or_not_set",
        },
        "actual_calls": {
            "persona_attempted": len(persona.requests),
            "persona_succeeded": len(private_turns),
            "observer_attempted": observer.attempted,
            "observer_succeeded": observer.succeeded,
            "observer_failed": observer.failed,
            "total": total_calls,
        },
        "observer_telemetry": {
            "accepted_delta_count": len(accepted),
            "rejected_delta_count": len(rejected),
            "accepted_fields": dict(sorted(accepted_fields.items())),
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
            "failure_categories": dict(sorted(observer.failure_categories.items())),
        },
        "runtime": {
            "persona_id": state.persona.persona_id,
            "session_id_sha256": _sha256_text(str(session_id)),
            "turn_count": len(private_turns),
            "turn_index": state.session.turn_index,
            "raw_event_count": len(events),
            "assistant_event_count": len(assistant_events),
            "delta_record_count": len(delta_records),
            "stable_core_unchanged": stable_core_unchanged,
            "initial_stable_core_fingerprint": _fingerprint(
                initial.stable_core.model_dump(mode="json")
            ),
            "final_stable_core_fingerprint": _fingerprint(
                state.stable_core.model_dump(mode="json")
            ),
            "final_state_fingerprint": _fingerprint(final_private),
            "exact_replay": replay_exact,
            "turn_20_message_count": len(last_messages) if last_messages else 0,
            "turn_20_prompt_tokens": (
                _turn_twenty_prompt_tokens(persona_usages[-1])
                if persona_usages
                else None
            ),
            "turn_1_present_at_turn_20": full_history,
            "heuristics": heuristics,
            "persona_usage_totals": _sum_usage(persona_usages),
            "observer_usage_totals": _sum_usage(observer.usages),
            "turn_evidence": public_turns,
        },
        "structural_gates": structural_gates,
        "structural_failures": structural_failures,
        "terminal_failure": terminal_failure,
        "behavioral_review": {
            "status": "PENDING_BLIND_REVIEW",
            "axis_scores": {axis: None for axis in AXES},
            "total": None,
            "hard_failures": [],
        },
        "frozen_comparison": FROZEN_COMPARISON,
        "acceptance": {
            "gate_a_do_no_harm": "PENDING_BLIND_REVIEW",
            "gate_b_runtime_value": "PENDING_BLIND_REVIEW",
            "gate_c_release": "PENDING_BLIND_REVIEW",
        },
        "private_report_sha256": _sha256_bytes(private_bytes),
        "blind_report_sha256": _sha256_bytes(blind_bytes),
        "content_logged": False,
    }
    _assert_content_free(public_report, corpus, generated_responses, spec.api_key)
    public_report["structural_gates"]["public_content_safe"] = True
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
        description="Run the one authorized GLM-4.7-Flash Runtime v2 diagnostic."
    )
    parser.add_argument("--personas-dir", type=Path, default=Path("personas"))
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--blind-output", type=Path, required=True)
    parser.add_argument("--public-output", type=Path, required=True)
    args = parser.parse_args()

    corpus, corpus_sha256 = load_corpus(os.environ.get("LIN_ZHIYAO_CORPUS_B64", ""))
    with tempfile.TemporaryDirectory(prefix="cm-eng-diag-03-") as temp_dir:
        report = run_diagnostic(
            provider_spec_from_env(),
            corpus,
            corpus_sha256=corpus_sha256,
            personas_dir=args.personas_dir,
            work_dir=Path(temp_dir),
            private_output=args.private_output,
            blind_output=args.blind_output,
            public_output=args.public_output,
            execution_commit=os.environ.get("GITHUB_SHA", "local-manual-run"),
            run_id=os.environ.get("GITHUB_RUN_ID", "local-manual-run"),
        )
    print(
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "status": report["status"],
                "actual_calls": report["actual_calls"],
                "corpus_sha256": report["corpus_sha256"],
                "behavioral_review": report["behavioral_review"]["status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
