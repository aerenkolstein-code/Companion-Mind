# Canonical Event / RAW Contract v1

**Engineering phase:** ENG-A021-P0-01 / Phase 0  
**Contract version:** `canonical_event/v1`  
**Scope:** interface + schema + public-safe fixtures + offline conformance tests  
**Non-goal:** no A019 Durable Journal, A012 Alpha, C1 implementation, provider/live calls, billing, or Phase 1+ runtime.

This file materializes the board-ratified Phase-0 contract from Companion-Mind
Issue #11. After review/merge, the versioned repository artifacts are the
engineering authority for Canonical Event / RAW shape; the Drive contract
continues to explain the product/governance reason for the boundary.

## 1. Hard boundary

> **Journal != Current != Memory != Persona / Relationship Authority.**

| Layer | Owns | Does not own |
|---|---|---|
| Journal | event evidence, chronology, provenance, corrections | present-state truth, biography, relationship truth |
| Current | present-state projection through its proper authority / StateDelta | immutable event evidence |
| Memory | retrieval/reactivation selection | Biography or Relationship write authority |
| Persona Authority | stable identity, Biography, formal persona facts | raw event chronology |
| Relationship Authority | formal relationship facts, milestones, Current | raw event chronology |

A Journal event can be evidence *for* an authority reducer. It is never itself an
authority mutation. No adapter, writer, retriever, or model response may directly
upgrade Persona or Relationship Authority merely because one event was observed
or generated.

## 2. Stable identity

Stable IDs own identity; names are aliases.

- `persona_id` and `relationship_id` are independent of `provider` and `model`.
- provider/model switching must not create a new persona or relationship.
- `session_id`, `turn_id`, `message_id`, `event_id`, and `correction_id` are stable identifiers at their respective layers.
- corrections append a new event referencing `correction_of`; original evidence is not silently overwritten.

Provider and model are **turn metadata only**.

## 3. Canonical event fields

The machine schema requires an explicit serialized slot for every minimum field:

```yaml
event_id:
session_id:
turn_id:
sequence_no:
actor_role: user | assistant | tool | system-derived-visible-event
message_id:
persona_id:
relationship_id:
provider:
model:
observed_at:
created_at:
content_type:
content_payload:
status: complete | partial | failed
source_ref:
attachment_ref:
correction_id:
correction_of:
redaction_state:
metadata:
```

Nullable identity/metadata fields remain present as `null` rather than being
silently dropped. `content_payload` accepts any JSON value, so structured,
non-string and multimodal descriptions are not coerced into plain text.

### Status

- `complete`: the visible event completed normally.
- `partial`: a visible assistant/tool event was only partially captured or produced.
- `failed`: an attempted visible event failed; failure is distinct from absence.

These states are evidence semantics. Phase 0 does not implement the future
USER-before-request durable append or assistant crash/recovery engine, but the
shape supports those records without redesign.

## 4. Deterministic order and dedupe

- `sequence_no` is a non-negative per-session ordering value.
- deterministic replay order is `(session_id, sequence_no, turn_id, actor_role, event_id)`.
- `event_id` is the stable duplicate-detection identity.
- provider/model are deliberately excluded from identity and ordering semantics.
- a correction is a new event with a new `event_id` and `correction_id`, plus `correction_of=<prior event_id>`.

## 5. Provenance

`source_ref` is structured and distinguishes source from interpretation.

Supported Phase-0 source kinds:

| source_kind | required observation_type | meaning |
|---|---|---|
| `owned_client` | `observed` | native Owned Client input/output evidence |
| `browser_sidecar` | `observed` | A018 browser capture evidence |
| `historical_backfill` | `imported` | A020 source-grounded old history |
| `correction` | `corrected` | append-only correction event |
| `replay` | `replayed` | replay provenance |
| `derived` | `inferred` or `projected` | explicitly non-original derived state |

An imported, inferred or projected record therefore cannot serialize as an
original observed event without failing conformance validation.

Attachments use stable reference objects (`attachment_id`, `media_type`,
`source_ref`, optional `sha256`). The Journal preserves the reference and
provenance; it does not claim that referenced media is itself Persona or
Relationship truth.

## 6. UNKNOWN semantics

The contract preserves these states as different serialized values:

```text
UNKNOWN != KNOWN_EMPTY != N/A != NOT_LOOKED_UP
```

`metadata.knowledge.<key>.state` may be:

- `KNOWN_VALUE` (and then must carry `value`)
- `KNOWN_EMPTY`
- `UNKNOWN`
- `N_A`
- `NOT_LOOKED_UP`

Non-`KNOWN_VALUE` states must not smuggle a value. Absence from the Journal does
not prove a negative Current state.

## 7. Secret boundary

Canonical Event v1 is not a credential store. API keys/tokens, passwords/OTP,
cookies/Authorization/CSRF material, and full payment credentials are rejected
by the semantic validator when present in secret-like structured fields.

When the visible user content itself contains a secret-like field, secondary
persistence must use the literal marker:

```text
[SECRET_REDACTED]
```

and set `redaction_state: redacted`. Repository fixtures contain only synthetic,
public-safe content and no real conversation bodies or secrets.

## 8. Adapter conformance

All adapters share one target:

- **A019 Owned Client / Durable Journal:** eventual local canonical writer; Phase 0 only defines its event interface.
- **A018 Browser Sidecar:** migration/capture adapter; no second session/turn schema, Persona Current, Relationship Current, or product Memory.
- **A020 Historical Backfill:** imports only source-grounded old history and marks it `imported`; missing history remains missing.
- **A012 Persistent AI Companion:** consumes the journal contract; it does not implement another RAW writer, Drive sync path, or state DB.

Adapter-specific capture data may exist before normalization, but the durable
handoff boundary is Canonical Event v1.

## 9. Public-safe fixture coverage

The fixture set covers:

- user and assistant text;
- multimodal + attachment reference;
- partial assistant;
- failed assistant;
- correction chain;
- structured/non-string payload;
- redacted secret-like payload;
- explicit UNKNOWN semantics;
- A018/A019/A020 source variants.

No fixture is copied from private project conversations.

## 10. Gate P0 offline checks

`tests/test_canonical_event_contract.py` maps directly to Issue #11 gates:

1. stable identity survives provider/model changes;
2. deterministic sequence ordering;
3. duplicate event ID detection;
4. append-only correction reference;
5. complete/partial/failed remain distinct;
6. multimodal and structured payloads round-trip losslessly;
7. source and attachment provenance round-trip;
8. secret-like fields are redacted or rejected;
9. UNKNOWN does not collapse into KNOWN_EMPTY;
10. A018/A019/A020 outputs validate against the same contract;
11. Journal evidence cannot mutate frozen Persona/Relationship authority snapshots;
12. provider/model cannot replace persona/relationship IDs.

**STOP after Phase-0 Draft PR evidence.** Gate P0 approval does not itself authorize
A019 Phase 1 or any other Phase 1+ work.
