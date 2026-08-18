# Current roadmap — personal-first owned runtime

**Status:** public-safe engineering narrative  
**Scope:** roadmap orientation only; not an implementation authority or completion claim

Companion-Mind is currently following a **personal-first owned-runtime path**. The project is deliberately separating three questions that are easy to conflate:

1. can the runtime preserve what happened and recover correctly;
2. can an owned client/runtime assemble the right context and authorities for real longitudinal work;
3. only after sustained use, is there enough evidence to extract a commercial product.

The third question is intentionally downstream of the first two.

## Sequence

```text
Phase 0 — Canonical Event / Runtime Boundary Contract      GREEN on main
        ↓
Phase 1 — Durable Journal / local canonical persistence   next gated increment
        ↓
Owned-client foundation                                  after an explicit E1 gate
        ↓
Longitudinal personal dogfooding                         real work, real failures, real recovery
        ↓
Productization readiness                                 evidence-based decision point
        ↓
Optional commercial product discovery                    only if evidence justifies extraction
```

## Phase 0 — published contract

The published Canonical Event / RAW v1 contract defines a shared event shape and authority boundary. It does not implement the Durable Journal itself.

The core invariant remains:

```text
Journal != Current != Memory != Persona / Relationship Authority
```

Adapters may feed one canonical event contract, but no adapter or model receives silent ownership of higher-level state.

## Phase 1 — Durable Journal

The next product-engineering increment is a local canonical journal with explicit durability, ordering, deduplication, correction lineage, crash/restart recovery, terminal assistant outcomes, secret exclusion, and asynchronous replica semantics.

This roadmap does **not** claim that transactional Durable Journal behavior is implemented on `main` today.

## Owned-client foundation

Only after an explicit durability gate should the product path move above the Journal into an owned client/runtime. Candidate responsibilities include:

- a thin local client shell;
- context assembly with model-specific input budgets;
- retrieval and authority routing;
- a model gateway with capability profiles;
- source-linked context, retrieval, and tool traces;
- dynamic recent-context compression without destructive history loss;
- topic-aware working context so semantically related segments can be reactivated across interruptions.

The Journal remains the durable evidence/chronology layer. Context is reconstructed per turn and may be compressed or selectively retrieved; it must not silently overwrite history.

## Vendor-web boundary

Browser capture and historical recovery remain useful compatibility, migration, and fault-recovery adapters. They are not the target primary interaction surface.

The owned-client path aims to make normal work independent of a specific vendor chat UI while still allowing external model providers and tools through explicit adapters.

## Model boundary

A foundation model is a replaceable cognition provider. Switching providers or context-window sizes must not silently switch identity, history, authority, or durable state ownership.

If required context does not fit a target model, the runtime should make the omission/compaction decision explicit and traceable rather than silently truncating mandatory context.

## Productization boundary

Personal longitudinal use is not treated as a hidden commercial beta. Its purpose is to expose real failure modes, permission boundaries, context mistakes, recovery needs, costs, and recurring value.

Commercialization is intentionally deferred until there is evidence that some of those needs are stable and generalizable. A future commercial product may reuse low-level contracts and interfaces, but its user model, privacy boundary, permissions, retention, billing, and product scope would require separate discovery and architecture.

## Evidence boundary

This document describes the current direction. It does **not** claim that the following are implemented:

- Durable Journal / transactional persistence;
- owned-client context engine;
- model capability routing;
- production tool orchestration;
- zero vendor-web dependency;
- a commercial Alpha, token system, billing, or payment path;
- a specialized relationship model.

Implemented and measured claims remain limited to the repository evidence documented in the main README, contracts, tests, and case studies.
