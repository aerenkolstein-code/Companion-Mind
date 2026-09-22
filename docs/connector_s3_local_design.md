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
revocation increments a persistent epoch and blocks every future reservation.

## Remaining implementation gaps

- Windows-native S3 access/refresh secret broker and monotonic witness.
- Manual CLI/PCKE Picker flow, including the exact `{folder,A,B}` callback.
- Fixed Drive/Docs transport, real `requiredRevisionId` precondition, UTF-16
  text editing, snapshots, rollback verification, and `UNKNOWN` reconciliation.
- Native synthetic probe, encrypted/private recovery packages, real test
  qualification, restart/lock tests, and all WO-S3-02 actions.

Writes must remain blocked until a future adapter obtains a current Docs
`revisionId` and supplies `requiredRevisionId`; the Docs API rejects a stale
required revision rather than applying the update. [Docs batchUpdate](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
