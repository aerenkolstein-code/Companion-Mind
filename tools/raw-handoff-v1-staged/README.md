# RAW-HANDOFF v1 — staged source archive

**Status: STAGED / NOT DEPLOYED / NOT INDEPENDENTLY ACCEPTED.**  
**Archive date:** 2026-10-25 (Europe/Madrid).  
**Placement:** Companion-Mind independent archive branch only; not `main`.

This directory contains an isolated local-development snapshot of the RAW-HANDOFF v1 candidate and supporting RAW collector implementation files. It is **not** an installation package, not proof of production behavior, and not an authorization to run against user sessions.

## Contents

- `handoff-core.mjs`: IDs, project scope, exact-trigger checks, old/new transition guards.
- `handoff-cli.mjs`, `handoff.ps1`: inspect/prepare/status/activate orchestration under explicit safety checks.
- `handoff-registry-store.mjs`, `handoff-target-loader.mjs`: local registry integrity and reload helpers.
- `handoff-history-collector.mjs`, `handoff-bootstrap-policy.mjs`, `handoff-bootstrap-service.mjs`: bounded history scanning, stability/finality rules, formal-write/readback checks.
- `raw-auto.mjs`, `control.ps1`, `raw-auto-*.mjs`: staging copy of the RAW collector and its support modules; `--handoff-registry` support is in the staged version **only**.
- `tests/`: three synthetic/integration test suites.

Local 2026-10-25 staged-test receipt: **52/52 passing**. No real next-generation cutover was tested or accepted. Keep legacy old-window gaps explicit, and never conflate candidate readiness with production deployment.

## Intentionally omitted

- The runtime `handoff-registry.json` snapshot (contains real project and conversation identifiers).
- Session RAW JSON/MD, pending message files, user message bodies and private identifiers.
- Status logs, private debug/readiness traces, and machine-specific evidence.
- ChatGPT project-instruction snippet with live project URL, and absolute-local-path runbook; the public-safe rules appear in the separate window-work archive.

## Safe reconstruction / validation limitations

The exact installed production RAW collector may differ from this archive. The staging candidate is deliberately isolated. Do not overwrite production files directly from this directory or start an additional writer.

A future controlled deployment requires a curated local runtime registry, an explicit operations review, a single-writer stop/restart cutover, backup and rollback preparation, and independent verification of a **real** new-window handoff after the active window naturally changes. No such deployment or acceptance is claimed by this source push.

**Authority boundaries:** no Journal, Current, Memory, Persona/Relationship authority, L0 generation, Live use, payment, or GitHub merge is authorized by this archive.
