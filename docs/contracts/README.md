# Companion-Mind engineering contracts

This directory contains versioned shared interfaces. A contract is an engineering
conformance target, not a second state authority.

## Canonical Event / RAW v1

- Human contract: [`canonical_event_v1.md`](canonical_event_v1.md)
- Machine schema: [`../../schemas/canonical_event_v1.schema.json`](../../schemas/canonical_event_v1.schema.json)
- Typed/semantic validator: `companion_mind.contracts.canonical_event_v1`
- Public-safe fixtures: `tests/fixtures/canonical_event_v1/`
- Offline gates: `tests/test_canonical_event_contract.py`

A018 Browser Sidecar, A019 Owned Client / Durable Journal, and A020 Historical
Backfill must emit this one event shape. A012 Persistent AI Companion consumes
that same journal contract and must not create a second RAW writer or state DB.

**Migration rule:** adapters may keep internal capture fields, but their durable
handoff boundary must normalize into Canonical Event v1. Provider/model are turn
metadata; they never own `persona_id` or `relationship_id`.

## A018 Retirement / Maintenance Gate

A018 Browser Sidecar is a vendor-web capture and migration bridge, not a second
Companion Client. Its frozen exit condition is:

```text
stable USER/ASSISTANT capture
+ dedupe across virtualization/reload/reopen
+ attachment/provenance traceability
+ Canonical Contract conformance
+ representative persistence/recovery regression
= MAINLINE COMPLETE
→ maintenance / migration adapter
```

After this gate is satisfied, A018 must remain a maintenance/migration adapter;
it must not grow its own competing session/turn contract, Persona Current,
Relationship Current, product Memory, or Companion Client runtime.

## A012 Thin Relationship Scope Guard

For Phase 3, A012 Persistent AI Companion is limited to the following minimum
relationship surface:

```text
stable persona_id
+ stable relationship_id
+ source-grounded minimal Relationship Current projection
```

A012 Phase 3 must **not** implement:

- a full R-MASTER client clone;
- a complex emotion state machine;
- numeric intimacy/affection optimization; or
- automatic permanent relationship upgrades.

The thin projection consumes source-grounded authority through the shared
contract boundary; it does not become Relationship Authority merely because the
client displays or uses that projection.

Phase 0 defines and tests the interface only. It contains no durable journal,
fsync/recovery engine, provider call, billing path, or Alpha implementation.

## Current roadmap placement — non-normative

The A012 constraints above remain valid interface and scope guards for any future
commercial companion path, but they do **not** activate A012 or place it directly
after Durable Journal in the current roadmap.

The current public roadmap is personal-first: after an explicit Durable Journal /
E1 gate, the product-engineering path moves to owned-client foundations and
longitudinal dogfooding. A012 is dormant unless a later productization decision
selects a Persistent AI Companion as a commercial direction.

This placement note changes **no** Canonical Event v1 schema, validator, fixture,
offline gate, identity rule, authority boundary, or A018/A019/A020 conformance
requirement. See [`../current-roadmap.md`](../current-roadmap.md) for the public-safe
roadmap narrative.
