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

Phase 0 defines and tests the interface only. It contains no durable journal,
fsync/recovery engine, provider call, billing path, or Alpha implementation.
