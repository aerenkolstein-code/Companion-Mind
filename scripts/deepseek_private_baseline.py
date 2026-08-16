#!/usr/bin/env python3
"""Run paired Native/Runtime DeepSeek baselines with content-safe output."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from companion_mind.persona import PersonaLoader
from companion_mind.providers import DeepSeekConfig, DeepSeekProvider, ProviderMessage
from companion_mind.runtime import Runtime


CORPUS_SCHEMA = "lin-zhiyao-private-corpus/v1"
PRIVATE_REPORT_SCHEMA = "lin-zhiyao-no-regression-baseline/v1"
PUBLIC_REPORT_SCHEMA = "lin-zhiyao-no-regression-baseline-public/v1"
EXPECTED_TURNS = 20

REPEATED_INTRO_PATTERNS = (
    re.compile(r"(?:我叫|我是)\s*林知遥"),
    re.compile(r"重新认识(?:一下)?"),
    re.compile(r"初次见面"),
    re.compile(r"很高兴认识你"),
)
STRANGER_RESET_PATTERNS = (
    re.compile(r"我不是林知遥"),
    re.compile(r"我不认识(?:你|馆长)"),
    re.compile(r"你说的林知遥是谁"),
    re.compile(r"请(?:先)?介绍(?:一下)?林知遥"),
    re.compile(r"我们(?:才|刚)认识(?!(?:那会儿|的时候|时))"),
)
IMPLEMENTATION_LEAK_PATTERNS = (
    re.compile(r"作为(?:一个|一名)?(?:AI|人工智能|语言模型)", re.IGNORECASE),
    re.compile(r"我是(?:一个|一名)?(?:AI|人工智能|语言模型)", re.IGNORECASE),
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _safe_usage(usage: Mapping[str, Any]) -> dict[str, int | float]:
    return {
        key: value
        for key, value in usage.items()
        if isinstance(key, str)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }


def load_corpus(encoded: str) -> tuple[dict[str, Any], str]:
    """Decode and strictly validate one private corpus secret."""

    value = encoded.strip()
    if not value:
        raise ValueError("LIN_ZHIYAO_CORPUS_B64 is required")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("private corpus secret is not valid base64") from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("private corpus secret is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != CORPUS_SCHEMA:
        raise ValueError("private corpus schema is unsupported")
    pairs = document.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != EXPECTED_TURNS:
        raise ValueError(f"private corpus must contain exactly {EXPECTED_TURNS} pairs")
    normalized: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs, start=1):
        if not isinstance(pair, Mapping) or pair.get("turn") != index:
            raise ValueError("private corpus turns must be sequential")
        prompt = pair.get("user_prompt")
        reference = pair.get("reference_response")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"turn {index} user_prompt must be non-empty text")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"turn {index} reference_response must be non-empty text")
        normalized.append(
            {
                "turn": index,
                "user_prompt": prompt.strip(),
                "reference_response": reference.strip(),
            }
        )
    return {"schema_version": CORPUS_SCHEMA, "pairs": normalized}, _sha256_bytes(raw)


def _matching_turns(
    responses: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
    *,
    start_turn: int = 1,
) -> list[int]:
    return [
        turn
        for turn, response in enumerate(responses, start=1)
        if turn >= start_turn and any(pattern.search(response) for pattern in patterns)
    ]


def _sum_usage(items: Sequence[Mapping[str, int | float]]) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}
    for item in items:
        for key, value in item.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _native_system_message(personas_dir: Path) -> ProviderMessage:
    persona = PersonaLoader(personas_dir).load("LIN-ZHIYAO")
    content = (
        f"你是{persona.display_name}（{persona.nickname}），{persona.identity.role}。"
        f"你正在与{persona.relationship.counterpart}继续一段已经存在且连续的中文对话。"
        "请自然地延续此前对话。"
    )
    return ProviderMessage(role="system", content=content)


def _heuristics(responses: Sequence[str]) -> dict[str, list[int]]:
    return {
        "repeated_self_introduction_turns": _matching_turns(
            responses,
            REPEATED_INTRO_PATTERNS,
            start_turn=3,
        ),
        "stranger_reset_turns": _matching_turns(
            responses,
            STRANGER_RESET_PATTERNS,
        ),
        "implementation_identity_leak_turns": _matching_turns(
            responses,
            IMPLEMENTATION_LEAK_PATTERNS,
        ),
    }


def _turn_records(
    pair: Mapping[str, Any],
    response: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int | float]]:
    usage = _safe_usage(response.usage)
    private = {
        "turn": pair["turn"],
        "user_prompt": pair["user_prompt"],
        "reference_response": pair["reference_response"],
        "generated_response": response.content,
        "reasoning_content": response.reasoning_content,
        "provider": response.provider,
        "model": response.model,
        "response_id": response.response_id,
        "usage": usage,
    }
    public = {
        "turn": pair["turn"],
        "prompt_sha256": _sha256_text(pair["user_prompt"]),
        "reference_sha256": _sha256_text(pair["reference_response"]),
        "response_sha256": _sha256_text(response.content),
        "response_chars": len(response.content),
        "reasoning_present": bool(response.reasoning_content),
        "usage": usage,
    }
    return private, public, usage


def _run_native(
    provider: DeepSeekProvider,
    pairs: Sequence[Mapping[str, Any]],
    *,
    personas_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system = _native_system_message(personas_dir)
    history: list[ProviderMessage] = []
    private_turns: list[dict[str, Any]] = []
    public_turns: list[dict[str, Any]] = []
    responses: list[str] = []
    usages: list[dict[str, int | float]] = []
    hard_failures: list[str] = []

    for pair in pairs:
        messages = [
            system,
            *history,
            ProviderMessage(role="user", content=pair["user_prompt"]),
        ]
        response = provider.generate(messages, thinking=False)
        if response.provider != provider.name or response.model != provider.model:
            hard_failures.append("PROVIDER_PROVENANCE_MISMATCH")
        private, public, usage = _turn_records(pair, response)
        private_turns.append(private)
        public_turns.append(public)
        usages.append(usage)
        responses.append(response.content)
        history.extend(
            (
                ProviderMessage(role="user", content=pair["user_prompt"]),
                ProviderMessage(role="assistant", content=response.content),
            )
        )

    heuristics = _heuristics(responses)
    if len(responses) != EXPECTED_TURNS:
        hard_failures.append("RESPONSE_COUNT_MISMATCH")
    if heuristics["repeated_self_introduction_turns"]:
        hard_failures.append("REPEATED_SELF_INTRODUCTION")
    if heuristics["stranger_reset_turns"]:
        hard_failures.append("PERSONA_REINVENTED_AS_STRANGER")

    private = {
        "mode": "native",
        "full_history": True,
        "minimal_system_message": system.content,
        "hard_failures": sorted(set(hard_failures)),
        "heuristics": heuristics,
        "turns": private_turns,
    }
    public = {
        "mode": "native",
        "full_history": True,
        "minimal_system_message_sha256": _sha256_text(system.content),
        "turn_count": len(responses),
        "hard_failures": sorted(set(hard_failures)),
        "heuristics": heuristics,
        "usage_totals": _sum_usage(usages),
        "turn_evidence": public_turns,
        "semantic_score": "PENDING_PRIVATE_REVIEW",
    }
    return private, public


def _run_runtime(
    provider: DeepSeekProvider,
    pairs: Sequence[Mapping[str, Any]],
    *,
    personas_dir: Path,
    work_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
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
    responses: list[str] = []
    usages: list[dict[str, int | float]] = []

    for pair in pairs:
        response = runtime.run_turn(pair["user_prompt"], provider, thinking=False)
        private, public, usage = _turn_records(pair, response)
        private_turns.append(private)
        public_turns.append(public)
        usages.append(usage)
        responses.append(response.content)

    state = runtime.current_state
    if state is None:
        raise RuntimeError("runtime state disappeared during private baseline")
    events = runtime.raw_writer.read(session_id)
    assistant_events = [event for event in events if event.role == "assistant"]
    heuristics = _heuristics(responses)
    hard_failures: list[str] = []
    if state.persona.persona_id != "LIN-ZHIYAO":
        hard_failures.append("PERSONA_ID_CHANGED")
    if state.session.session_id != session_id:
        hard_failures.append("SESSION_ID_CHANGED")
    if state.session.turn_index != EXPECTED_TURNS:
        hard_failures.append("TURN_INDEX_MISMATCH")
    if len(events) != EXPECTED_TURNS * 2:
        hard_failures.append("UNIFIED_RAW_EVENT_COUNT_MISMATCH")
    if len(assistant_events) != EXPECTED_TURNS:
        hard_failures.append("ASSISTANT_EVENT_COUNT_MISMATCH")
    if any(event.provider != provider.name for event in assistant_events):
        hard_failures.append("PROVIDER_PROVENANCE_MISMATCH")
    if any(event.model != provider.model for event in assistant_events):
        hard_failures.append("MODEL_PROVENANCE_MISMATCH")
    final_relationship = state.relationship.model_dump(mode="json")
    if final_relationship != initial_relationship:
        hard_failures.append("RELATIONSHIP_STATE_RESET")
    if heuristics["repeated_self_introduction_turns"]:
        hard_failures.append("REPEATED_SELF_INTRODUCTION")
    if heuristics["stranger_reset_turns"]:
        hard_failures.append("PERSONA_REINVENTED_AS_STRANGER")

    private = {
        "mode": "runtime_passthrough",
        "full_history": True,
        "persona_id": state.persona.persona_id,
        "session_id": str(session_id),
        "turn_index": state.session.turn_index,
        "raw_event_count": len(events),
        "assistant_event_count": len(assistant_events),
        "initial_relationship": initial_relationship,
        "final_relationship": final_relationship,
        "hard_failures": hard_failures,
        "heuristics": heuristics,
        "turns": private_turns,
    }
    public = {
        "mode": "runtime_passthrough",
        "full_history": True,
        "persona_id": state.persona.persona_id,
        "session_id_sha256": _sha256_text(str(session_id)),
        "turn_index": state.session.turn_index,
        "raw_event_count": len(events),
        "assistant_event_count": len(assistant_events),
        "relationship_state_preserved": final_relationship == initial_relationship,
        "hard_failures": hard_failures,
        "heuristics": heuristics,
        "usage_totals": _sum_usage(usages),
        "turn_evidence": public_turns,
        "semantic_score": "PENDING_PRIVATE_REVIEW",
    }
    return private, public


def run_baseline(
    provider: DeepSeekProvider,
    corpus: Mapping[str, Any],
    *,
    corpus_sha256: str,
    personas_dir: Path,
    work_dir: Path,
    private_output: Path,
) -> dict[str, Any]:
    """Run paired 20-turn baselines and keep all content in one private report."""

    pairs = corpus["pairs"]
    native_private, native_public = _run_native(
        provider,
        pairs,
        personas_dir=personas_dir,
    )
    runtime_private, runtime_public = _run_runtime(
        provider,
        pairs,
        personas_dir=personas_dir,
        work_dir=work_dir,
    )
    hard_failures = [
        *(f"NATIVE_{item}" for item in native_public["hard_failures"]),
        *(f"RUNTIME_{item}" for item in runtime_public["hard_failures"]),
    ]
    private_report = {
        "schema_version": PRIVATE_REPORT_SCHEMA,
        "corpus_sha256": corpus_sha256,
        "provider": provider.name,
        "model": provider.model,
        "expected_turns_per_mode": EXPECTED_TURNS,
        "native": native_private,
        "runtime": runtime_private,
        "acceptance": {
            "maximum_runtime_regression_points": 1,
            "runtime_intermediate_target": 12,
            "formal_release_target": 15,
        },
    }
    private_bytes = json.dumps(
        private_report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    private_output.parent.mkdir(parents=True, exist_ok=True)
    private_output.write_bytes(private_bytes)

    return {
        "schema_version": PUBLIC_REPORT_SCHEMA,
        "status": "PASS" if not hard_failures else "FAIL",
        "provider": provider.name,
        "model": provider.model,
        "corpus_sha256": corpus_sha256,
        "private_report_sha256": _sha256_bytes(private_bytes),
        "expected_turns_per_mode": EXPECTED_TURNS,
        "paid_call_count": EXPECTED_TURNS * 2,
        "native": native_public,
        "runtime": runtime_public,
        "hard_failures": hard_failures,
        "acceptance": {
            "maximum_runtime_regression_points": 1,
            "runtime_intermediate_target": 12,
            "formal_release_target": 15,
            "semantic_gate": "PENDING_PRIVATE_REVIEW",
        },
        "content_logged": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run paired private DeepSeek no-regression baselines safely."
    )
    parser.add_argument("--personas-dir", type=Path, default=Path("personas"))
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()

    corpus, corpus_sha256 = load_corpus(os.environ.get("LIN_ZHIYAO_CORPUS_B64", ""))
    provider = DeepSeekProvider(DeepSeekConfig.from_env())
    with tempfile.TemporaryDirectory(prefix="cm-deepseek-no-regression-") as temp_dir:
        public_report = run_baseline(
            provider,
            corpus,
            corpus_sha256=corpus_sha256,
            personas_dir=args.personas_dir,
            work_dir=Path(temp_dir),
            private_output=args.private_output,
        )
    print(json.dumps(public_report, ensure_ascii=False, sort_keys=True))
    return 0 if public_report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
