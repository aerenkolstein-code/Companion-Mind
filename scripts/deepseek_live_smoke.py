#!/usr/bin/env python3
"""Run a paid DeepSeek connectivity smoke test without logging model text."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from companion_mind.providers import DeepSeekConfig, DeepSeekProvider
from companion_mind.runtime import Runtime


SMOKE_TURNS: tuple[tuple[bool, str], ...] = (
    (False, "请用一句简短中文确认连接正常，不要自我介绍。"),
    (True, "请再次用一句简短中文确认连接正常，不要自我介绍。"),
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_usage(usage: Mapping[str, Any]) -> dict[str, int | float]:
    """Keep numeric billing counters only; never echo provider-owned text."""

    safe: dict[str, int | float] = {}
    for key, value in usage.items():
        if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(
            value, bool
        ):
            safe[key] = value
    return safe


def run_smoke(
    provider: DeepSeekProvider,
    *,
    personas_dir: Path,
    work_dir: Path,
    turns: Sequence[tuple[bool, str]] = SMOKE_TURNS,
) -> dict[str, Any]:
    """Exercise the real runtime while returning only non-content evidence."""

    runtime = Runtime(
        personas_dir=personas_dir,
        state_dir=work_dir / "state",
        raw_dir=work_dir / "raw",
    )
    initial = runtime.start_session("LIN-ZHIYAO")
    mode_reports: list[dict[str, Any]] = []
    for thinking, prompt in turns:
        response = runtime.run_turn(prompt, provider, thinking=thinking)
        mode_reports.append(
            {
                "thinking": thinking,
                "response_chars": len(response.content),
                "response_sha256": _sha256_text(response.content),
                "reasoning_present": bool(response.reasoning_content),
                "usage": _safe_usage(response.usage),
            }
        )

    state = runtime.current_state
    if state is None:
        raise RuntimeError("runtime state disappeared during smoke test")
    events = runtime.raw_writer.read(initial.session.session_id)
    assistant_events = [event for event in events if event.role == "assistant"]
    if state.persona.persona_id != "LIN-ZHIYAO":
        raise RuntimeError("persona identity changed during smoke test")
    if state.session.turn_index != len(turns):
        raise RuntimeError("session turn index did not advance exactly")
    if len(events) != len(turns) * 2 or len(assistant_events) != len(turns):
        raise RuntimeError("unified RAW event count is incomplete")
    if any(event.provider != provider.name for event in assistant_events):
        raise RuntimeError("assistant RAW provider identity mismatch")
    if any(event.model != provider.model for event in assistant_events):
        raise RuntimeError("assistant RAW model identity mismatch")

    return {
        "schema_version": "deepseek-live-smoke/v1",
        "status": "PASS",
        "provider": provider.name,
        "model": provider.model,
        "persona_id": state.persona.persona_id,
        "session_id_sha256": _sha256_text(str(state.session.session_id)),
        "turn_index": state.session.turn_index,
        "raw_event_count": len(events),
        "assistant_event_count": len(assistant_events),
        "modes": mode_reports,
        "content_logged": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a secret-safe paid DeepSeek runtime smoke test."
    )
    parser.add_argument("--personas-dir", type=Path, default=Path("personas"))
    args = parser.parse_args()

    config = DeepSeekConfig.from_env()
    provider = DeepSeekProvider(config)
    with tempfile.TemporaryDirectory(prefix="cm-deepseek-smoke-") as temp_dir:
        report = run_smoke(
            provider,
            personas_dir=args.personas_dir,
            work_dir=Path(temp_dir),
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
