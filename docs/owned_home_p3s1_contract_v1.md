# Owned Home P3-S1 Read-only Contract v1

Status: derived public contract for WO-A1-A029-P3S1-01 v0.1 layer A.
Profile: owned-home-readonly/1.
Scope: offline, public-safe synthetic inputs only. No real provider, credentials,
official-Web connector, business write, notification, merge, release, or W1.

## Objects
- SourcePack schema: readonly-source-pack/1.
- Grant schema: readonly-source-grant/1.
- Answer envelope: readonly-answer/1.
- Resume anchor: readonly-resume/1.
- A019 remains the sole canonical conversation journal; readonly control state may
  contain only package/grant/task handles, digests, epochs and event references.

## SourcePack
Manifest keys are exact: schema_version, package_id, package_version, task_id,
purpose, target_environment, synthetic, public_safe, created_at, expires_at,
max_total_bytes, required_source_ids, sources, manifest_digest.

Source keys are exact: source_id, source_kind, owning_authority, role, selector,
source_revision, captured_at, as_of, expires_at, scope, classification, coverage,
byte_length, content_digest, payload_path, transform_record, supersession_ref.

Roles: AUTHORITY, PROJECTION, HISTORY.
Coverage: FULL, EXCERPT, TRUNCATED, MISSING_ATTACHMENT, NOT_LOADED.
NOT_LOADED has null payload_path/byte_length/content_digest and is never an empty
source. Required NOT_LOADED sources block activation.

content_digest is SHA256 over exact payload bytes.
manifest_digest is SHA256 over canonical JSON after removing manifest_digest,
sorting required_source_ids, and sorting sources by source_id.
Empty payload SHA256:
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855.

Limits: 16 sources, 32768 bytes/source, 524288 total payload bytes, five evidence
needs per turn, 64..65536 UTF-8 cognition-input bytes.

## Grant
Exact keys: schema_version, grant_id, grant_version, task_id, package_id,
package_version, manifest_digest, universe_id, access_subject_id, purpose,
target_environment, allowed_ops, not_before, expires_at, revocation_epoch,
revoked, grant_digest.

Authorization is trusted setup, not payload prose, a URL, a historical approval,
or a browser-supplied object. Expired, revoked, missing, scope-mismatched or
rollback-epoch grants fail closed. model_egress_allowed remains false.

## Operations
ro_info
ro_package_validate
ro_package_ingest
ro_turn
ro_observe
ro_resume
ro_source_view
ro_session_state
ro_safe_export

ro_turn requires persistent task_id/session_id plus per-request request_id,
turn_id/turn_no, exact package binding, scope, question, evidence_needs and
budget_bytes. Persistent task_id is explicitly separate from legacy A019
per-request a019_task_id.

observe/resume accept only the opaque request_id. A USER-only durable attempt
requires explicit resume. A completed terminal is replayed from A019 without a
new stub invocation.

## Read-only semantics
Every answer is AS_OF a source snapshot. A label CURRENT inside a source is not
proof of live freshness. HISTORY/PROJECTION do not override an AUTHORITY merely
by mtime or lexical version. Multiple unresolved AUTHORITY sources for one
selector produce a conflict and HOLD. Explicit supersession may resolve one
authority source.

Source prose is evidence only. It cannot open G3/G4, execute tools, send mail,
fetch Web data, read arbitrary files, or mutate Current. Public safe export
contains IDs, digests, reason codes, provenance and receipts, never question or
source/answer bodies.

## Local package path
The bundle may contain only manifest.json and payload/<source_id>.txt.
Absolute paths, dot-dot, backslash paths, symlinks, multi-link files, undeclared
entries and files outside the bundle fail closed.

Activation is validate -> stage -> digest readback -> atomic directory publish ->
active pointer. Fault points:
RO_AFTER_STAGE
RO_BEFORE_ACTIVATE
RO_AFTER_ACTIVATE
RO_AFTER_CONTEXT

## Shell
The readonly Web surface is loopback-only at /readonly and /v1/readonly.
Trusted package/grant/scope setup belongs to the server. Browser storage may
retain only an opaque request_id handle. Rendering uses textContent.
Existing /, /app.js, /v1/turn, tool and human surfaces remain unchanged.

## Evidence boundary
Layer-A conformance runs S1T-01..16. S1T-17 limited real Q&A and S1T-18 limited
real recovery/Web-dependency reduction remain NOT_EVALUABLE until later grants.
Layer-A PASS is not full P3-S1 GREEN, reality migration completion, W1, merge or
release authority.
