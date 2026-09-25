# Connector S3 local design — first bounded-policy slice

This is local, synthetic-only engineering material for WO-S3-01. It does not
authorize OAuth, token reads, Picker interaction, or Google API dispatch.

## Confirmed selection constraint

The user-operated desktop/mobile Google Picker supports `allow_multiple=true`
and returns `picked_file_ids`; its folder-selection option can include the
selected folder. S3 must record exactly `{folder, A, B}` from that interaction:
the folder is only the checked parent and creation target, not a document or a
grant for every child. A and B are the only existing files admitted. C can enter
the ledger only after one exact create request and post-create parent/MIME/ID
verification. There is no files.list, search, recursive traversal, shortcut
following, automated browser action, or implicit sibling admission.

`drive.file` is retained. It permits files the app created or the user selected
or shared with the app; a selected folder alone does not prove access to its
existing descendants. [Google scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
and the [desktop/mobile Picker guide](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)
are the technical basis.

## Implemented local policy

`companion_mind.connector_s3.policy` is independent of Connector Alpha. It
stores only a synthetic/non-secret state model and has no HTTP or OAuth code.
Before any future adapter can dispatch, it durably records both a per-operation
state and cumulative charge. Its frozen defaults mirror WO-S3-02: total 240,
ordinary 168 split 30/50/40/28/20, safety 72 split 40/12/4/16, OAuth 4, refresh
6, A/B/C writes 8, rollbacks 3/3/2, and C creation 1.

Unknown dispatches remain charged and cannot replay. A `PREPARED` or
`DISPATCHED` operation after restart requires recovery rather than reuse. Local
revocation increments a persistent epoch and blocks business reservations;
only the explicitly tagged revoke cleanup request remains admissible.

## Native persistence slice

The S3 namespace now uses Windows Credential Manager with machine-persistent,
current-user records. No S2 target is admitted. The non-secret disk ledger is
paired with a native sequence/hash witness bound to its stage and absolute path.
A native PREPARED witness precedes file replacement; COMMITTED is saved only
after file readback. Interrupted, missing, mismatched, or stale disk state fails
closed. A named Windows mutex encloses Controller read/modify/write operations.
The witness does not protect against an administrator or a same-user adversary
restoring both native credentials and disk together.

Reservation and every counter charge are now one commit. Dispatch also requires
a process-local ticket, so restarting cannot reuse a persisted PREPARED request.
The Windows synthetic canary creates fresh UUID slots, exercises two competing
processes against one 30-request bucket, and verifies deletion of both native
slots. It never uses a real token or calls a provider. Physical lock, reboot,
refresh, and the 24-hour observation are still untested.

## Local edit and recovery planning

`docs_plan` parses only one explicit tab of uniform plaintext from a full
`SUGGESTIONS_INLINE` response. Unsupported structures, suggestions, revision
absence, mismatched indices, or oversized text are rejected. Edit requests use
UTF-16 indices, retain the mandatory final newline, and bind
`requiredRevisionId`. Rollback planning first verifies the expected post-edit
text and styles, then uses the latest readback revision. These are local
request plans; no provider operation is performed.

`recovery` uses current-user Windows DPAPI with binding/operation entropy and
immutable ciphertext files. Load requires both ciphertext and plaintext hashes
from a receipt that the future broker must pin in the witnessed ledger. No
plaintext fallback exists. The caller still must enforce operation authorization,
recovery-package registration, and session state before use.

Synthetic tests exercise Unicode edits, paragraph deletion, restoration,
revision conflicts, external edit refusal, and native encrypted-package reopen,
tamper refusal, and wrong-binding refusal. Provider behavior remains unqualified.

Technical sources: [Docs structure and UTF-16](https://developers.google.com/workspace/docs/api/concepts/structure),
[Docs request constraints](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request),
[Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).

## Remaining integration gaps

- Purpose-bound secret broker, generation/reconnect lifecycle, retest budgets,
  and full account/subject/scope/expiry binding. The native layer is a primitive,
  not a qualified provider broker or complete S3 runtime.
- Manual CLI/PCKE Picker flow, including the exact `{folder,A,B}` callback.
- Fixed Drive/Docs transport, broker-enforced real `requiredRevisionId` and
  durable recovery registration, provider readback and `UNKNOWN` reconciliation.
- Real test qualification, restart/lock tests, and all WO-S3-02 actions.

Writes must remain blocked until a future adapter obtains a current Docs
`revisionId` and supplies `requiredRevisionId`; the Docs API rejects a stale
required revision rather than applying the update. [Docs batchUpdate](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
