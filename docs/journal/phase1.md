# A019 Phase 1: local canonical evidence Journal

Implementation work order: [ENG-A019-P1-01 / Issue 26](https://github.com/aerenkolstein-code/Companion-Mind/issues/26).
Scope: P1-S0 through P1-S7, offline implementation candidate. A1 conformance is
not independent A2 evaluation or Board acceptance. Return the Draft PR and stop.

## Published contract and storage freeze (P1-S0)

The event authority remains `canonical_event/v1` from Phase-0 merge
`63ac8d7de8eb35915cc291b0f7ea67c33b366922`:

- [human contract](../contracts/canonical_event_v1.md)
- [machine schema](../../schemas/canonical_event_v1.schema.json)
- `companion_mind.contracts.canonical_event_v1`

Those artifacts are unchanged. Startup verifies the schema and validator bytes
against their pinned SHA-256 values. The schema reader interprets the exact
published properties/ref/enum/type constraints, including `additionalProperties`;
semantic validation still calls the published validator. A changed pin fails with
`CONTRACT_CHANGE_REQUIRED`. No automatic schema migration or private event shape.
The canonical schema is installed as package data from the existing source file.

The implementation store version is `a019-store/1`, migration version `0`.
Unknown store versions/contract pins/replica targets fail closed. The five tables
in `companion_mind/journal/store.py` are the frozen operational storage layout:

| Table | Role |
| --- | --- |
| `canonical_event_log` | Immutable complete serialized v1 events, SHA-256, physical offset, original commit generation, derived unique indexes |
| `turn_attempt_control` | Reserved terminal slot, sanitized template, sanitized-input signature, phase, external outcome; no Current facts |
| `assistant_spool` | Sanitized durable visible frames, indexed by attempt/frame |
| `replica_outbox` | Atomic enqueue, fixed target, preallocated remote ID, retry/readback control |
| `store_meta` | Contract/store/migration pins, store UUID, commit/recovery generations, clean-shutdown marker |

Event JSON is authoritative; index columns cannot independently replace it.
Startup checks database integrity, foreign keys, canonical fingerprints/index
agreement, and outbox cardinality before recovering attempts. SQL triggers reject
UPDATE/DELETE of canonical rows. Direct privileged SQL/schema tampering is not
a supported API or a security isolation boundary.

## Durability and append (P1-S1)

Supported substrate: Python 3.11+, POSIX local filesystem, process-scoped single
writer via `flock`, SQLite WAL with `synchronous=FULL`. No network filesystem,
multi-host writer, Windows port, malicious filesystem, or storage that lies about
flush completion is claimed. Creation syncs the directory and its parent; every
transaction syncs the store directory after SQLite commit. IO/commit/sync errors
invalidate the open handle (`REOPEN_REQUIRED`) and never release a new receipt.

SQLite documents that FULL in WAL mode synchronizes the WAL for every commit;
the durability claim assumes the OS/storage honors that sync. The fault harness
proves process-crash recovery; it does not emulate a physical power-cut device.
See [SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous)
and [WAL persistence](https://www.sqlite.org/wal.html).

Append runs structural validation, pre-persistence scrub, published semantic
validation, then one transaction containing identity/sequence/correction checks,
event append and outbox enqueue. The transaction finishes before its receipt is
returned. `DurableAppendReceipt` contains event ID, offset, fingerprint, contract
version/commit, store generation and the event's original commit generation.

Same event ID and sanitized canonical fingerprint returns `ALREADY_COMMITTED`
with the original offset/generation. Different fingerprint is `IDENTITY_CONFLICT`.
Per-session sequence collisions fail with `SEQUENCE_CONFLICT`. No sequence numbers
are silently reassigned. An in-flight attempt reserves its assistant event ID and
sequence slot so a competing ingest cannot steal them.

`export(order="journal")` returns physical append order. The default
`export(order="canonical")` uses the Phase-0 canonical order key. A late A020 import
therefore keeps its historical sequence while receiving a later physical offset.

## USER and ASSISTANT lifecycle (P1-S2, S3, S4)

`append_user_then_invoke_stub` atomically appends USER, outbox and attempt control,
then receives the durable commit. It durably records provider intent, and only
then reaches the deterministic offline stub boundary. The trace exposes that
order. A successful return acknowledges ASSISTANT only after terminal durability.
This seam accepts an empty assistant template with stable identity/sequence/time
fields from the caller; it validates identity, provenance and reserved slots
before committing USER. It does not route models or construct prompts.

Each script is sanitized as a whole **before** durable frame staging, including
secret patterns split across frames. If redaction changes a script, its sanitized
output is staged as one frame. This is an offline deterministic test provider,
not a general live token-stream redactor. A live adapter needs separate work.

`complete` requires a durable observed-success marker; interruption with visible
spool becomes `partial`; no visible output becomes `failed`. An observed explicit
failure retains safe classification and any visible text as `failed`. Error
messages/transport responses are never copied into evidence. Terminal append,
outbox and attempt closure are atomic. Duplicate attempt invocation does not
reinvoke the stub. Retry uses a new attempt and terminal event/sequence, retaining
prior evidence. The fixed per-attempt sanitized signature rejects conflicting
callbacks/scripts under an existing attempt ID.

| Crash | Durable evidence | Startup result |
| --- | --- | --- |
| F1 | USER + `USER_DURABLE` | USER retained, `NOT_SENT`, await explicit same-attempt resume; no invented ASSISTANT |
| F2 | Provider intent | Local `failed`, external `UNKNOWN`; no automatic replay |
| F3 | Visible sanitized spool | `partial`, external `UNKNOWN`; first visible fragment preserved |
| F4 | Observed terminal success marker | `complete` appended once |
| F5 | Terminal event + closed attempt | Original event/receipt, no duplicate |
| F6 | Local event + outbox | Local retained, replica pending |
| F7 | Remote create issued | Read/reconcile same persisted remote ID |
| F8 | Remote readback completed | Repeat readback, then checkpoint; no second file |

An interrupted local attempt's `failed` status does **not** assert external
provider failure: `metadata.extensions.a019_attempt.external_outcome=UNKNOWN`
and the control outcome retain that distinction. There is no provider
exactly-once claim. Repeated recovery has zero provider invocations. F1 can be
explicitly resumed because durable control proves the boundary was not entered.

Recovery finishes locally in the constructor before any append is accepted.
Pending remote work is exposed in `RecoveryReceipt`, then resumed by explicit
`drain_replica`; network access never occurs during local constructor recovery.
Pending async replication does not block new local durability.

Corrections require new event/correction IDs and an existing `correction_of`
target. Reverting a correction appends another corrected-provenance v1 event
pointing at that correction; `metadata.extensions.correction_action` explains
the action. All originals remain exported. No destructive rollback/reducer.

## Replica completion envelope (P1-S5)

`drain_replica(DriveTransport)` captures a physical high-water offset. Each event
gets at most one bounded attempt per explicit drain. A remote file ID is obtained
and committed locally **before** create. Every retry uses the same ID and checks
existing bytes; it never creates a fresh file to resolve an ambiguous write.
Only fresh byte-identical readback permits `READBACK_VERIFIED`. Even previously
verified rows are rechecked on the next explicit drain. A mismatch is blocked,
timeouts remain `REMOTE_IO_UNKNOWN`, and local canonical events never roll back.

The completion receipt binds high-water, expected/verified counts, per-event
fingerprints and IDs, and ordered local fingerprint. `INCOMPLETE` has no aggregate
agreement fingerprint. It makes no claim about appends beyond the high-water or
future remote mutations. Operators must reuse the same remote target/store; a
fake service moved to an empty directory is not evidence of the same service.

`DriveTransport` defines reserve/create/read; authentication lives outside the
Journal. `GoogleDrive` maps reserve to Drive `files.generateIds`, create
to binary JSON `files.create(id=reserved_id)`, and read to exact-ID media bytes.
409 must be reconciled by exact readback; only 404 may map to absence. These
idempotent retry semantics follow [Google's pre-generated ID guidance](https://developers.google.com/workspace/drive/api/guides/manage-uploads#use_a_pre-generated_id_to_upload_files).

The REST adapter accepts an injected authenticated sender; no default network
sender or credentials are installed. Its request shapes, 409 reconciliation and
403/404 distinction are tested through a recording fake sender. The end-to-end
fault matrix runs `OfflineDrive`, a separate persistent fake remote service,
and labels its receipts `OFFLINE_DRIVE_STUB`. No production credentials,
provisioning or real replication run is included. Live integration remains
unverified and requires a separately authorized integration action.

## Ingest, privacy and negative authority (P1-S6)

`ingest("A018", event)` requires browser-sidecar/observed provenance;
`ingest("A020", event)` requires historical-backfill/imported provenance;
`ingest("A019", event)` requires owned-client/observed provenance. The published
metadata adapter label must agree. All call the same append transaction. Derived,
inferred and projected evidence retain the published source matrix and cannot be
relabelled observed. No sidecar capture or historical miner implementation added.

Redaction runs before any durable surface, including control/template signatures
and spool. Structured secret field names reuse the published contract's list.
Additional conservative patterns cover labeled credentials, bearer/API tokens,
private-key blocks, full card-shaped digit strings and credential-bearing URLs.
Envelope secrets are rejected so stable identity cannot be silently rewritten.
Synthetic known-pattern tests scan database, WAL, control, spool, outbox, replica
and receipt surfaces. Arbitrary unlabeled secrets cannot be identified perfectly;
the detector is not a universal data-loss prevention system. Use only public-safe
synthetic inputs for this phase. Rejection/errors never echo input values.

The contract spells N/A as `N_A`; the seam preserves that exact enum alongside
`UNKNOWN`, `KNOWN_EMPTY`, `NOT_LOOKED_UP` and `KNOWN_VALUE`. Structured payloads
retain null, false and zero. Model/provider changes do not alter stable identities.

Journal has no Current/Memory/Persona/Relationship tables or write API. Lifecycle,
replay, correction and replication are evidence operations only; no reducer is
invoked. Negative tests reject authority mutation metadata/operations and verify
external synthetic authority files remain unchanged.

## Return and acceptance ceiling (P1-S7)

Run `python tools/a019_conformance.py --output /tmp/a019-conformance.json` on a
clean committed checkout. It records candidate SHA, contract pin, environment,
tests, measured 120-turn/240-event results, F1-F8 matrix, all required conformance
categories and a normalized result fingerprint. The 120-turn manifest runs twice
on fresh stores, with 100 complete / 10 partial / 10 failed attempts per run.
CI uploads the receipt as an artifact named with its checked-out commit.
PR CI can use GitHub's synthetic merge commit; compare it separately with the
actual PR head. Do not mislabel the two identities.

`tools/a019_durability_probe.py STORE` can emit synthetic boundary markers under
`strace -yy -e trace=fsync,fdatasync,write` on a host permitting ptrace. The local
Work container denies ptrace, so this optional syscall trace was unavailable;
the durability basis is the verified SQLite FULL/WAL configuration, documented
sync semantics, IO-error dispatch gate and process-crash evidence.

See [A2 handoff](a2-handoff.md) for the implementation-opaque CLI. A1 may return
`READY_FOR_E1_REVIEW` only after the work-order package and CI are present.
`E1=GREEN` additionally requires an independent A2 receipt and explicit Board
acceptance. No merge, Issue close, production release, Phase 2, A029, A012,
Persona/Relationship change, provider/live/paid or Wallet/Billing/Payment work.
