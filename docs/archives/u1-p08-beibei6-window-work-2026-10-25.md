# U1-P08 / Beibei-6 — Window Work Archive (2026-10-25)

> **Public-safe, evidence-scoped engineering archive.** This document is a retrospective of an assistant window, **not** a transcript, raw-message export, Persona/Relationship authority, Journal, Current, L0, independent acceptance, or production deployment notice. Private message bodies, personal identifiers, chat URLs, credentials, and local RAW files are intentionally excluded.

**Date:** 2026-10-25 (Europe/Madrid)  
**Scope:** U1-P08 continuation, isolated RAW-HANDOFF v1 prototype, project-instruction update, preceding-window known-incomplete closure, and current single-writer observation.  
**Authority:** User authorized this archive/push. User separately authorized *known-incomplete administrative sealing* of the preceding window. Neither authorizes retroactive claims of message recovery or technical acceptance.

## 1. Window identity and continuity

- The successive numbered chat windows refer to **one stable U1-P08 companion/persona**, not new characters. The personnel document remains the sole identity authority.
- The newest active window retains its own independently collected RAW. The previous window's official RAW remains separate.
- Journal, Current, Memory, and Persona/Relationship authority remain distinct. Tool receipts, development tests, and user decisions must not be conflated with independent validation.
- Source-specific current-work claims require fresh authority checks; status `UNKNOWN` must never be rewritten as `DONE`.

## 2. RAW-HANDOFF v1: isolated implementation only

A local **isolated staging candidate** was implemented, without installing it into the production RAW collector. Main components:

- Strict project URL / conversation-ID validation and trigger recognition for an exact, standalone new-window continuation request.
- Local revisioned handoff registry and compare-and-swap atomic registry writes.
- Single-writer registry-driven target hot reload, designed to replace fixed target lists after an *explicit future* deployment.
- Inspect → prepare → pending bootstrap → two stable full-history scans → scoped atomic JSON/Markdown write → readback → ACTIVE transition.
- Guards against unstable DOM, missing history, mismatched IDs/text/order, incomplete generation, accidental old-window removal, second writers, and replayed triggers.
- Status and PowerShell wrappers designed for the project's designated local Windows and Chrome DevTools tooling.
- A deployment/runbook document and project-instruction text.

**Development evidence:** 52/52 synthetic and unit/integration tests passed (24 core validation, 15 bootstrap/target loader, 13 synthetic end-to-end). Staging-only observations also exercised Chrome page detection, dynamic registry reload, and isolated RAW writes without changing the production writer or official files.

**Critical qualification:** Staging success is **not** successful real cross-generation handoff. As of this archive, RAW-HANDOFF v1 was **not deployed to production**, and the planned real new-window trial is explicitly postponed until the active window naturally reaches its successor. No claims of an independently accepted or production-ready rollout are made.

Local developer evidence remains on the user's authorized Windows workspace, including a 2026-10-25 staged-readiness JSON receipt and test logs. Private/local-only evidence is not pushed into this public repository.

## 3. Project instruction update

On 2026-10-25, the U1-P08 ChatGPT project instructions were modified through the designated Chrome UI, explicitly **saved**, then reopened and read back. The added section specifies:

- An exact standalone continuation message in a *new window of the same project* triggers **an attempt**, not automatic success.
- Verify same-project URL, new ID, registered old active ID, installed production handoff code, and the presence of exactly one `--handoff-registry` writer.
- If deployment is absent or a fixed `--targets` collector is still running, report `HANDOFF_NOT_READY`.
- Only execute `prepare` after an `inspect` result of `HANDOFF_PREPARED`.
- Retain old-window protection while the new window is `BOOTSTRAP_PENDING`; require two consistent complete historical passes and formal JSON/MD readback before promoting new ACTIVE.
- Preserve unresolved legacy gaps and refuse automatic seal/deletion or competing writers.

**Important:** This instruction change does **not** deploy code, alter persona authority, or launch any background procedure by itself.

## 4. Previous-window RAW closeout: known-incomplete administrative seal

The preceding numbered window was examined in the *actual* designated Chrome tab and against its official local RAW:

| Subject | Evidence and final scope |
|---|---|
| Official local RAW | **310 entries**, 310 unique keys, SHA-256 verified and snapshotted with its Markdown representation |
| Historical pending evidence | **47 pending items**, all 47 key/role/body values matched entries already in the 310-entry official RAW; no duplicate append |
| Old-window tail | **2 later user messages** appeared in two stable DOM scans **57 seconds apart**, with matching IDs/bodies; both were marked incomplete and were absent from formal RAW |
| Generation/server signals | Global stream endpoint reported COMPLETE, while corresponding DOM turns were `in_progress`; later reload showed partial in-progress assistant output. These signals did not justify full-message acceptance |
| Overall history completeness | **UNKNOWN**, including branch/server/cross-device completeness |
| L0 checkpoint | Historical checkpoint retained at **nextIndex=200**; no new L0/Journal generated |
| Closure authorization | User explicitly accepted the two unresolved tail messages and authorized **known-incomplete administrative sealing** |
| Final seal classification | `KNOWN_INCOMPLETE_ADMIN_SEALED`, **not** `FULL_HISTORY_VERIFIED` |

The two tail message texts and identifiers remain preserved in **private local evidence**, not published here. Neither missing assistant completions nor unwitnessed history were synthesized. The official 310-entry RAW and old pending file were **not** modified or deleted.

A local admin-seal Markdown report and JSON readback receipt were written and checked. This is a documented authorized administrative closure, **not independent recovery acceptance**.

## 5. Production writer observation during closure

At the final local verification, exactly one production Node RAW writer was observed, tracking **only the active successor window**, no longer the preceding one. Old-window formal RAW hash/count remained unchanged; successor RAW continued to be held independently.

During attempted guarded cutover, the preflight detected that the process had **already changed** to the successor-only writer. The assistant **did not retry stop/start**. Subsequent live PID/command/log inspection confirmed the new-only configuration. **The identity of the actor that performed this concurrent process change is not established in this archive**, and the aborted command is not recorded as the successful cutover.

Production continued in its **existing fixed `--target` mode**, not the undeployed handoff-registry mode.

## 6. Frozen status and deferred work

| Workstream | At archive time |
|---|---|
| RAW-HANDOFF v1 isolated candidate | **STAGED_AND_TESTED**, deployment / real-new-window acceptance **NOT_DONE** |
| U1-P08 project instructions | **SAVED_AND_UI_READBACK_VERIFIED** |
| Prior window official RAW snapshot | **310_RECORDS_SHA_VERIFIED**, not all-history complete |
| Prior window administrative closure | **KNOWN_INCOMPLETE_ADMIN_SEALED** by explicit user decision |
| Two unrecovered tail messages | **PRESERVED_AS_EVIDENCE**, not formalized as complete RAW |
| Active window automatic RAW | **ONE_WRITER_SUCCESSOR_ONLY_OBSERVED** |
| Persona/Relationship, L0, Journal, Current | **NOT_MODIFIED** by these closeout operations |
| GitHub merge | **NOT_AUTHORIZED / NOT_PERFORMED** |

**Deferred trigger:** Carry out a real RAW-HANDOFF v1 rehearsal **only when the active numbered window naturally needs a successor**, after checking deployed code and single-writer operation. Until then, do not claim it is deployed or usable.

## 7. Evidence rules and archive limitations

1. This GitHub record is a **public-safe index of conclusions and verification boundaries**, not a second authoritative Current or a copy of the private conversation.
2. Real local SHA-256 receipts exist for the 310-entry official RAW, Markdown snapshot, two tail scans, closeout HOLD record, and final known-incomplete administrative-seal receipt. Exact local artifacts should be checked in their original scoped environment for future audits.
3. Nothing in this retrospective confers permission to merge branches, copy private RAW to GitHub, rewrite past messages, publish unverified Persona content, or trigger Live/paid APIs.
4. The project's curator retains final judgment; an administrative seal explicitly accepting a known gap must remain distinguishable from historical completeness.

**Archive provenance:** written on the user's explicit request to archive and push the present window's work. Branch is an isolated archive branch and has not been merged into `main`.
