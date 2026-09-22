# Connector S3 local qualification delivery

Status: draft implementation slices, not qualified for real OAuth or provider
writes. This branch builds on the accepted Connector Alpha code baseline without
changing its single-use read contract. No merge or deployment is included.

## Sanitized work order

Implement an independent controlled-work profile for one selected synthetic
folder, two explicitly selected existing Docs, and one app-created Doc. Persist
bounded stage budgets across processes and connection generations. Enforce
current-user native token storage, purpose separation, conditional content
edits, protected recovery packages, and fail-closed revocation/recovery.

The stage contract caps OAuth starts at 4, reconnects at 3, refreshes at 6, total
requests at 240 (168 normal / 72 safety), writes at 8 per file, rollbacks at
3/3/2, non-rollback writes at 5/5/6, and creation at 1. Two repair packages each
consume the same global pools and cap requests at 20, OAuth at 1, non-rollback
writes at 2, and rollback at 1. Tests use only generated fixtures and UUID test
credential slots. Private bindings, provider content, and raw evidence are kept
outside the repository.

## Delivered local components

- Windows S3 credential namespace, persistent native sequence/hash witness,
  serialized ledger commits, atomic budget precharge, and stale-ticket rejection.
- Stage/generation budget model and a fixed **synthetic-only** purpose broker.
  It cannot dispatch HTTP and is not the final provider broker.
- Strict single-tab plaintext parsing, UTF-16 edit plans, required-revision
  requests, external-change-safe rollback plans, and readback predicates.
- Current-user DPAPI recovery files with binding/operation entropy, immutable
  creation, and receipt hashes.
- Fixed Google HTTPS request grammar, with locally derived routes and bodies,
  strict response/transaction bounds, no redirects or automatic retries, and
  fixed error codes. Qualification uses patched HTTPS only.
- A/B write-and-restore coordinator: actual recovery save/readback precedes the
  receipt/request/revision pin, write and readback precharges are atomic, and
  five restoration requests plus one rollback write remain held in the same
  stage pools. UNKNOWN locks the file; an explicit counted exact-resource probe
  never retries the write. External text/style changes prevent rollback.
- Manual PKCE/offline-consent request construction with exact folder/A/B Picker
  callback validation. No real authorization has been launched for S3.

## Qualification and limits

Local Windows tests exercise native UUID credential cleanup, competing processes,
ledger corruption and interrupted commits, scope and quota refusal, generation
closure, Unicode edit/rollback algorithms, synthetic revision conflicts, and
DPAPI reopen/tamper refusal. These test classes remain separate from provider
qualification. Runtime commands are the corresponding
`tests.test_connector_s3_*` unittest modules. Full remote CI is reported in the
pull request and the project delivery receipt once it actually runs.

The first native cut passed 20 tests and the edit/recovery cut passed 13 tests.
Subsequent lifecycle/broker/OAuth tests are tracked separately; no count here
implies that an unrun real scenario passed. Linux cannot exercise Windows-native
checks; Windows cannot import the historical POSIX-only `fcntl` test suites.

The current local cut passed all 85 S3 tests on Windows, including a fresh UUID
native witness plus real DPAPI write/reopen/restore test with HTTPS mocked.
Portable executor tests explicitly substitute recovery I/O and are not DPAPI
evidence. A native-stage schema version change fails closed on older development
ledgers; no automatic reset or budget migration is implemented. API counters
conservatively include precharged requests even if a later step aborts before
dispatch. Reserved restoration capacity is tracked separately from consumed
counts and is unavailable to other requests.

Remaining: final native purpose broker/session and credential factory, persistent
C admission/creation recovery, live CLI integration, identity/resource capability
proof, token rotation/expiry handling, and independent qualification of that
complete runtime. The coordinator alone is not a live entry point. Actual consent,
conditional writes and content restoration, physical lock/reboot, real expiry
refresh, local/external revocation, and the minimum 24-hour observation are all
NOT RUN for S3. Real S3 requests remain zero.

The witness detects disk/native mismatch; it does not prove power-loss atomicity
or protect against privileged restoration of both native credentials and disk.
