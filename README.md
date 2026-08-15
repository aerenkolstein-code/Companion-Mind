# Companion-Mind

**A minimal continuous cognitive runtime with observable state, provenance and executable safeguards.**

Portfolio Status: **CURRENT ARTIFACT** · Evidence Level: **E3 — executable, locally tested prototype**

Long-context retrieval answers: *Can the system find the past?* Companion-Mind asks the operational question: *Can the right constraint become active before the next state write?*

## First closed-loop artifact

`CM-GUARD-001` implements a **Closure Guard** for `EVAL-CASE-001`:

```text
Event → StateDelta → Agenda → BeliefCandidate
→ Evaluation → DecisionTrace → guarded state write
```

The failure: one child task is complete, another required child remains open, but a parent goal is declared done. The guard reads structured child state and rejects parent closure while any required child is `OPEN`, `UNKNOWN`, waiting, blocked or pending.

The paired LLM Evaluation Lab run measured the naive baseline against this guard on five public-safe variants:

| Policy | Accuracy | Premature closure rate |
|---|---:|---:|
| Naive baseline | 20% | 100% |
| `CM-GUARD-001` | 100% | 0% |

These are deterministic fixture results, not production or model-quality claims.

## Run

```bash
python -m unittest discover -s tests -v
python -m companion_mind.runtime
```

No API key or third-party dependency is required.

## What is real today

- six small public data contracts;
- persistent state and active agenda separated from prose;
- candidate beliefs evaluated before state mutation;
- an executable Closure Guard;
- sourced `DecisionTrace` records;
- duplicate-event suppression and a per-event task budget;
- a shared public `EvaluationCase` schema with LLM Evaluation Lab.

## Limits and next step

This is a deterministic research prototype, not a production agent framework. It does not call a foundation model, persist to a database, prove autonomous cognition or establish performance beyond the published fixture. The next step is to add a second public-safe failure class without weakening traceability or the public/private boundary.

## Repository map

- `companion_mind/models.py` — minimal observable contracts
- `companion_mind/runtime.py` — runtime loop and `CM-GUARD-001`
- `tests/test_runtime.py` — safeguard and regression assertions
- `schemas/evaluation_case.schema.json` — shared case contract
- `docs/history/README.md` — audited Gen1 migration ledger

## Five-repository role

**Companion-Mind builds safeguards.** LLM Evaluation Lab owns cases, metrics, mitigations and regressions. Human-AI Education Lab stages cognitive pressure; Qigouguan and Guo remain deferred public-safe case mines.

## Privacy

Only synthetic/public-safe events and traces are used. There are no links or paths to private Raw, L0, relationship, financial, medical, account or unpublished-manuscript material.

